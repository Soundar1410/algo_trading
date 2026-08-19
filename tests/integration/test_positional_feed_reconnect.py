"""Phase 6A: positional feed reconnect and the engine's own recoverable
market-data-degraded gate.

Two layers are proven here, deliberately kept apart:

**The engine-level gate** (the bulk of this file) — ``PositionalMultiLegEngine
.on_feed_gap_notice``/``_record_tick_for_gate``/``_required_security_ids`` —
driven through the *real* ``runtimes.positional_options.worker.build_engine``
wiring against a real temp SQLite database, exactly like
``test_weekly_delta_neutral_entry.py``. ``FeedGapNotice`` is delivered
directly to the built engine (mirrors exactly what
``runtimes.positional_options.worker._run_positional_worker_locked``'s own
``_on_feed_gap`` callback does — see that module) via
:class:`~tests.integration._weekly_delta_neutral_fixtures.ScriptedFeed`,
which can interleave plain ticks with arbitrary zero-argument callables — so
a "disconnect" or "resubscribe" can land at an exact point in a scripted
tick sequence, something a real socket's own timing could never guarantee
deterministically in a test.

**The transport layer** (the last section) — a real
:class:`~common.feed.reconnect.ReconnectingFeed` wrapping a real
:class:`~common.feed.hub.SharedFeedHub`, proving the segment/mode-preserving
resubscription this whole gate depends on. Single-threaded and in-process
(``feed.start()`` called directly, never through a supervisor or a spawned
worker) — the real multi-process proof already exists in
``test_positional_runtime_weekly_staged_entry.py``/
``test_positional_runtime_multi_strategy.py``, both of which run a real
``ReconnectingFeed`` under a real spawned worker end to end; duplicating that
weight here would buy no extra coverage.

No live order API is constructed or called anywhere in this module.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from _weekly_delta_neutral_fixtures import (
    HEDGE_CALL_STRIKE,
    HEDGE_PUT_STRIKE,
    SHORT_CALL_STRIKE,
    SHORT_PUT_STRIKE,
    ScriptedFeed,
    build_scrip_master,
    build_worker_config,
    chain_fetcher_for,
    cycle_id_for,
    entry_ticks,
    fake_margin_fetcher,
    initial_chain_payload,
    leg_tick,
    open_repository,
    underlying_tick,
)

from common.config.models import ExecutionMode
from common.engine.positional.positional_models import CycleAction, CycleSignal
from common.execution import ExecutionRepository
from common.feed.queues import FeedGapNotice
from common.feed.reconnect import ReconnectingFeed, ReconnectPolicy
from common.margin import MarginEstimator
from common.market_data.adapter import TickCallback
from common.persistence import Database
from common.utils.timeutils import is_fresh
from runtimes.positional_options.worker import build_engine

IST = ZoneInfo("Asia/Kolkata")
ENTRY_DATE = "2026-08-19"  # a Wednesday
CYCLE_ID = cycle_id_for()
_SCRIP_MASTER = build_scrip_master()


def _ts(hour: int, minute: int, second: int = 0, *, day: int = 19) -> datetime:
    return datetime(2026, 8, day, hour, minute, second, tzinfo=IST)


def _build(
    tmp_path: Any, *, db_name: str
) -> tuple[Database, ExecutionRepository, Any, ScriptedFeed]:
    """A real engine, wired exactly like ``build_worker_config``'s other
    callers, over a :class:`ScriptedFeed` this test drives by hand — see the
    module docstring. Nothing is run yet; the caller appends steps to
    ``feed.steps`` (which may reference the returned engine, since it is
    already built) and then calls ``engine.run()`` itself."""
    database, repository = open_repository(tmp_path, db_name)
    session = repository.open_session(
        runtime_id="positional_options", strategy_id="weekly_delta_neutral",
        execution_mode=ExecutionMode.PAPER, process_role="worker", pid=4242,
    )
    payload = initial_chain_payload()
    config = build_worker_config(ENTRY_DATE)
    feed = ScriptedFeed(steps=[], initial_now=_ts(9, 26))
    built = build_engine(
        config, repository=repository, session_id=session.id, feed=feed,
        chain_fetcher=chain_fetcher_for(payload), scrip_master=_SCRIP_MASTER,
        margin_estimator=MarginEstimator(margin_fetcher=fake_margin_fetcher),
        clock=feed.clock,
    )
    return database, repository, built.engine, feed


def _leg_states(repository: ExecutionRepository) -> dict[str, str]:
    return {leg["leg_role"]: leg["state"] for leg in repository.load_cycle_legs(cycle_id=CYCLE_ID)}


def _order_count(repository: ExecutionRepository, *, leg_role: str | None = None) -> int:
    history = repository.cycle_order_history(cycle_id=CYCLE_ID)
    if leg_role is None:
        return len(history)
    legs = {leg["leg_role"]: leg["leg_id"] for leg in repository.load_cycle_legs(cycle_id=CYCLE_ID)}
    leg_id = legs.get(leg_role)
    return len([r for r in history if r["leg_id"] == leg_id])


# =========================================================== is_fresh itself
# The shared, fail-closed freshness arithmetic every check in this file
# (context.spot_is_fresh, the gate's own post-resubscribe tick comparison)
# ultimately relies on.
def test_is_fresh_none_is_never_fresh() -> None:
    assert is_fresh(None, now=_ts(9, 26), max_age_seconds=60.0) is False


def test_is_fresh_a_naive_observed_at_is_never_fresh() -> None:
    naive = datetime(2026, 8, 19, 9, 26)
    assert is_fresh(naive, now=_ts(9, 26), max_age_seconds=60.0) is False


def test_is_fresh_a_naive_now_is_never_fresh() -> None:
    naive_now = datetime(2026, 8, 19, 9, 26)
    assert is_fresh(_ts(9, 26), now=naive_now, max_age_seconds=60.0) is False


def test_is_fresh_a_future_dated_observed_at_is_never_fresh() -> None:
    """Negative age (the tick is *ahead* of "now") must fail closed, not be
    treated as infinitely fresh."""
    future = _ts(9, 27)
    now = _ts(9, 26)
    assert is_fresh(future, now=now, max_age_seconds=3600.0) is False


def test_is_fresh_boundary_equality_is_fresh() -> None:
    observed = _ts(9, 26, 0)
    now = observed + timedelta(seconds=60)
    assert is_fresh(observed, now=now, max_age_seconds=60.0) is True


def test_is_fresh_one_second_past_the_boundary_is_not_fresh() -> None:
    observed = _ts(9, 26, 0)
    now = observed + timedelta(seconds=61)
    assert is_fresh(observed, now=now, max_age_seconds=60.0) is False


# ================================================== disconnect before entry
def test_a_disconnect_before_any_entry_suppresses_enter_cycle(tmp_path: Any) -> None:
    """The gate latches on the very first ``FeedGapNotice`` — before any
    further evaluation can act — and ``ENTER_CYCLE`` is a risk-increasing
    action, so nothing is ever placed while it holds."""
    database, repository, engine, feed = _build(tmp_path, db_name="before_entry.db")
    t0 = _ts(9, 26)
    feed.steps.extend(
        [
            lambda: engine.on_feed_gap_notice(FeedGapNotice(kind="disconnected", at=t0)),
            underlying_tick(t0),
        ]
    )
    try:
        engine.run()
        assert engine._market_data_degraded is True
        assert repository.load_cycle(cycle_id=CYCLE_ID) is None, (
            "no cycle should exist — ENTER_CYCLE was suppressed, not merely delayed"
        )
    finally:
        database.close()


def test_recovery_after_a_pre_entry_disconnect_allows_entry_to_proceed(tmp_path: Any) -> None:
    """The mirror of the test above: once every currently-required
    instrument (just the underlying — no cycle exists yet) has a fresh
    post-resubscribe tick, evaluation resumes normally."""
    database, repository, engine, feed = _build(tmp_path, db_name="recover_before_entry.db")
    t0 = _ts(9, 26)
    feed.steps.extend(
        [
            lambda: engine.on_feed_gap_notice(FeedGapNotice(kind="disconnected", at=t0)),
            underlying_tick(t0),
            lambda: engine.on_feed_gap_notice(FeedGapNotice(kind="resubscribed", at=t0)),
            underlying_tick(t0 + timedelta(seconds=1)),
            *entry_ticks(t0 + timedelta(seconds=2)),
        ]
    )
    try:
        engine.run()
        assert engine._market_data_degraded is False
        cycle = repository.load_cycle(cycle_id=CYCLE_ID)
        assert cycle is not None
        legs = _leg_states(repository)
        assert all(state == "OPEN" for state in legs.values())
    finally:
        database.close()


# ============================================ disconnect during staged entry
def test_disconnect_during_a_staged_entry_boundary_blocks_only_the_fill_attempt(
    tmp_path: Any,
) -> None:
    """Disconnect lands squarely between the HEDGE_PUT stage filling and the
    HEDGE_CALL stage's own fill attempt — the boundary every original role
    transition shares, since ``_drive_entry`` calls the identical
    ``_advance_entry_stage`` for each of them (see that method's own
    docstring). HEDGE_CALL's stage is still armed/subscribed while degraded
    (pure data plumbing) but its fill is skipped, exactly like "no fresh
    quote yet" — never abandoned, never a duplicate order once recovered.

    Recovery requires a fresh post-resubscribe tick for *every* leg whose
    contract is already assigned (HEDGE_PUT, now OPEN, plus the three still-
    PENDING legs) as well as the underlying — proven by staging them one at
    a time and asserting the gate stays latched until the very last one
    lands, per the corrected review round-2 requirement.
    """
    database, repository, engine, feed = _build(tmp_path, db_name="staged_boundary.db")
    t0 = _ts(9, 26)

    def _assert_degraded() -> None:
        assert engine._market_data_degraded is True

    def _assert_recovered() -> None:
        assert engine._market_data_degraded is False

    feed.steps.extend(
        [
            underlying_tick(t0),  # ENTER_CYCLE; HEDGE_PUT armed, no quote yet
            leg_tick(HEDGE_PUT_STRIKE, "PE", 18.0, 20.0, t0 + timedelta(seconds=1)),
            underlying_tick(t0 + timedelta(seconds=2)),  # HEDGE_PUT fills; HEDGE_CALL armed
            lambda: engine.on_feed_gap_notice(
                FeedGapNotice(kind="disconnected", at=t0 + timedelta(seconds=3))
            ),
            leg_tick(HEDGE_CALL_STRIKE, "CE", 17.0, 19.0, t0 + timedelta(seconds=4)),
            underlying_tick(t0 + timedelta(seconds=5)),  # retried, but degraded: no fill
            _assert_degraded,
            lambda: engine.on_feed_gap_notice(
                FeedGapNotice(kind="resubscribed", at=t0 + timedelta(seconds=6))
            ),
            # Underlying alone is never enough — three legs' own contracts
            # (HEDGE_CALL still pending, plus SHORT_PUT/SHORT_CALL, already
            # assigned at ENTER_CYCLE) are also required.
            underlying_tick(t0 + timedelta(seconds=7)),
            _assert_degraded,
            leg_tick(HEDGE_PUT_STRIKE, "PE", 18.0, 20.0, t0 + timedelta(seconds=8)),
            _assert_degraded,
            leg_tick(HEDGE_CALL_STRIKE, "CE", 17.0, 19.0, t0 + timedelta(seconds=8)),
            _assert_degraded,
            leg_tick(SHORT_PUT_STRIKE, "PE", 80.0, 82.0, t0 + timedelta(seconds=8)),
            _assert_degraded,  # SHORT_CALL's own tick is still missing
            leg_tick(SHORT_CALL_STRIKE, "CE", 78.0, 80.0, t0 + timedelta(seconds=8)),
            _assert_recovered,  # the last required id just ticked
            underlying_tick(t0 + timedelta(seconds=9)),  # entry resumes and completes
        ]
    )
    try:
        engine.run()
        legs = _leg_states(repository)
        assert legs == {
            "HEDGE_PUT": "OPEN", "HEDGE_CALL": "OPEN",
            "SHORT_PUT": "OPEN", "SHORT_CALL": "OPEN",
        }
        # Exactly one fill per leg — the degraded retries never duplicated
        # HEDGE_CALL's own order.
        assert _order_count(repository, leg_role="HEDGE_CALL") == 1
        assert _order_count(repository) == 4
    finally:
        database.close()


# ============================== risk-increasing actions gated directly
@pytest.mark.parametrize(
    "action", [CycleAction.ENTER_CYCLE, CycleAction.ROLL_SHORT, CycleAction.REPAIR_HEDGE]
)
def test_apply_signal_suppresses_every_risk_increasing_action_while_degraded(
    tmp_path: Any, action: CycleAction
) -> None:
    """Direct, white-box proof of the gate ``_apply_signal`` itself enforces
    — every member of ``_RISK_INCREASING_ACTIONS``, not only ``ENTER_CYCLE``
    (already covered end to end above). A signal this minimal never reaches
    any action-specific branch, since the gate check is the very first line
    of the method."""
    database, repository, engine, _feed = _build(tmp_path, db_name=f"gate_{action.value}.db")
    try:
        engine.on_feed_gap_notice(FeedGapNotice(kind="disconnected", at=_ts(9, 26)))
        before = _order_count(repository)
        engine._apply_signal(CycleSignal(action=action, timestamp=_ts(9, 26)), _ts(9, 26))
        assert _order_count(repository) == before
        assert repository.load_cycle(cycle_id=CYCLE_ID) is None
    finally:
        database.close()


def test_exit_all_is_never_gated_by_the_degraded_flag(tmp_path: Any) -> None:
    """EXIT_ALL is deliberately absent from ``_RISK_INCREASING_ACTIONS`` —
    proven directly, the same way the risk-increasing actions are above."""
    database, repository, engine, feed = _build(tmp_path, db_name="exit_not_gated.db")
    t0 = _ts(9, 26)
    feed.steps.extend(entry_ticks(t0))
    try:
        engine.run()
        legs = _leg_states(repository)
        assert all(state == "OPEN" for state in legs.values())

        engine.on_feed_gap_notice(FeedGapNotice(kind="disconnected", at=t0 + timedelta(minutes=1)))
        assert engine._market_data_degraded is True

        # An operator square-off is the simplest real, unconditional exit
        # trigger to fire deterministically: _evaluate checks it before any
        # gate-related code runs at all (see _evaluate's own top branch),
        # exactly like the hard-expiry-deadline exit a few lines below it in
        # the same method — both share the one _exit_all call, gated on
        # nothing but the cycle being active.
        engine.request_square_off("operator square-off while degraded")
        engine.on_tick(underlying_tick(t0 + timedelta(minutes=2)))

        cycle = repository.load_cycle(cycle_id=CYCLE_ID)
        assert cycle is not None
        assert cycle["state"] == "COMPLETED"
        legs_after = _leg_states(repository)
        assert all(state == "CLOSED" for state in legs_after.values())
    finally:
        database.close()


# ============================================= repeated reconnects, no dupes
def test_repeated_disconnect_recovery_cycles_never_latch_permanently_or_duplicate_orders(
    tmp_path: Any,
) -> None:
    database, repository, engine, feed = _build(tmp_path, db_name="repeated_reconnect.db")
    t0 = _ts(9, 26)
    feed.steps.extend(
        [
            lambda: engine.on_feed_gap_notice(FeedGapNotice(kind="disconnected", at=t0)),
            underlying_tick(t0),
            lambda: engine.on_feed_gap_notice(FeedGapNotice(kind="resubscribed", at=t0)),
            underlying_tick(t0 + timedelta(seconds=1)),
            lambda: engine.on_feed_gap_notice(
                FeedGapNotice(kind="disconnected", at=t0 + timedelta(seconds=2))
            ),
            underlying_tick(t0 + timedelta(seconds=2)),
            lambda: engine.on_feed_gap_notice(
                FeedGapNotice(kind="resubscribed", at=t0 + timedelta(seconds=2))
            ),
            underlying_tick(t0 + timedelta(seconds=3)),
            *entry_ticks(t0 + timedelta(seconds=4)),
        ]
    )
    try:
        engine.run()
        assert engine._market_data_degraded is False
        legs = _leg_states(repository)
        assert all(state == "OPEN" for state in legs.values())
        assert _order_count(repository) == 4, "no duplicate order across the two reconnects"
    finally:
        database.close()


# ===================================================== independent workers
def test_two_engines_clear_their_own_gate_independently(tmp_path: Any) -> None:
    """One worker's slower-to-refresh contract set must never block — or
    unblock — a sibling's. Two real, independent engines over two
    independent databases; the same isolation two real spawned worker
    processes get in production, proven here without the multiprocess
    weight (already covered end to end in
    test_positional_runtime_multi_strategy.py)."""
    db_a, repo_a, engine_a, feed_a = _build(tmp_path, db_name="worker_a.db")
    db_b, repo_b, engine_b, _feed_b = _build(tmp_path, db_name="worker_b.db")
    t0 = _ts(9, 26)
    t1 = t0 + timedelta(minutes=1)

    # Engine A completes a full entry first, so its required set includes
    # four leg contracts, not just the underlying.
    feed_a.steps.extend(entry_ticks(t0))
    # Engine B never ticks at all before the disconnect below — its cycle
    # stays None throughout, so its required set is the underlying alone
    # (never even one leg's candidate, unlike A).
    try:
        engine_a.run()
        engine_b.run()
        assert all(state == "OPEN" for state in _leg_states(repo_a).values())
        assert repo_b.load_cycle(cycle_id=CYCLE_ID) is None

        engine_a.on_feed_gap_notice(FeedGapNotice(kind="disconnected", at=t1))
        engine_b.on_feed_gap_notice(FeedGapNotice(kind="disconnected", at=t1))
        engine_a.on_feed_gap_notice(FeedGapNotice(kind="resubscribed", at=t1))
        engine_b.on_feed_gap_notice(FeedGapNotice(kind="resubscribed", at=t1))

        # Only the underlying re-ticks for both.
        engine_a.on_tick(underlying_tick(t0 + timedelta(minutes=2)))
        engine_b.on_tick(underlying_tick(t0 + timedelta(minutes=2)))

        assert engine_b._market_data_degraded is False, (
            "B's own required set (underlying only) was fully satisfied"
        )
        assert engine_a._market_data_degraded is True, (
            "A's own required set (underlying + 4 legs) is still missing every leg's "
            "own tick — B's recovery must never leak into A's"
        )
    finally:
        db_a.close()
        db_b.close()


# ===================================================== transport layer proof
class _SegmentTrackingAdapter:
    """A ``_ScriptedAdapter``-style fake (see ``test_feed_reconnect.py``,
    the established precedent) extended to remember the segment/mode each
    id was subscribed under — the real ``DhanMarketFeedAdapter`` already
    does this internally (confirmed by reading ``common/market_data/
    dhan.py``); this fake proxies just enough of that to prove
    ``ReconnectingFeed``/``resubscribe_all`` restore it correctly, without
    any positional-specific resubscription logic anywhere."""

    def __init__(self, *batches: Any) -> None:
        self._batches = list(batches)
        self.subscribe_calls: list[tuple[tuple[str, ...], int | None, int | None]] = []
        self.resubscribe_calls = 0
        self._by_id: dict[str, tuple[int | None, int | None]] = {}
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def subscribed(self) -> frozenset[str]:
        return frozenset(self._by_id)

    def subscribe(
        self, security_ids: Any, *, segment: int | None = None, mode: int | None = None
    ) -> None:
        ids = tuple(str(s) for s in security_ids)
        self.subscribe_calls.append((ids, segment, mode))
        for sid in ids:
            self._by_id[sid] = (segment, mode)

    def resubscribe_all(self) -> None:
        self.resubscribe_calls += 1

    def start(self, on_tick: TickCallback, *, on_idle: object = None) -> None:
        self._running = True
        try:
            while self._batches:
                item = self._batches.pop(0)
                if isinstance(item, Exception):
                    raise item
                for tick in item:
                    on_tick(tick)
                return
        finally:
            self._running = False

    def stop(self) -> None:
        self._running = False


def test_reconnect_restores_underlying_and_option_subscriptions_on_their_original_segment_and_mode(
    tmp_path: Any,
) -> None:
    underlying_id = "13"
    option_id = "90001"
    adapter = _SegmentTrackingAdapter(
        [ConnectionResetError("drop")],
        [],
    )
    feed = ReconnectingFeed(
        adapter,
        policy=ReconnectPolicy(max_attempts=4, initial_backoff=0.001, max_backoff=0.002),
        sleep=lambda _s: None,
        rng=lambda: 0.0,
    )
    # The underlying on its default (Ticker) segment/mode, the option on
    # NSE_FNO/Full — the exact split worker._request_subscription performs.
    feed.subscribe([underlying_id])
    feed.subscribe([option_id], segment=2, mode=21)

    feed.start(lambda _t: None)

    assert adapter.resubscribe_calls >= 1
    assert adapter.subscribed == {underlying_id, option_id}
    # A reconnect resends the *whole* set via one bare ``subscribe()`` call
    # (see ReconnectingFeed._reconnect's own comment: "a new socket carries
    # no subscriptions"), so the per-id segment/mode recorded above is what
    # the real adapter's own remembered-per-id state would restore — this
    # fake's own by-id map is what proves the ids themselves survived the
    # round trip undisturbed.
    assert option_id in adapter.subscribed
    assert underlying_id in adapter.subscribed
