"""Phase 5A production-path proof: the *real* ``weekly_delta_neutral``
strategy completes a genuine four-leg staged entry over the real shared
multi-process feed hub (:class:`~common.feed.hub.SharedFeedHub` under
:class:`~runtimes.positional_options.supervisor.PositionalOptionsSupervisor`)
— not only the inert ``_fixture_second_strategy`` Phase 5 used, whose own
docstring documented this exact gap as out of its scope. Real
``multiprocessing.Process`` workers, a real temp SQLite database, a real
dynamic-subscription round trip through a real control queue — the only
fake is the market-data adapter (no live order API, no network).

Proves the cross-evaluation staged-entry resume this phase adds
(``PositionalMultiLegEngine._resume_pending_entry``/``_advance_entry_stage``):
each of the four original legs' own fresh quote fills it on a *later* hub
iteration than the one that requested its dynamic subscription — never
requiring the quote within the same evaluation — and a second, independent
positional worker (``_fixture_second_strategy``) is never disrupted by it.

See ``_positional_multi_strategy_fixtures.py`` for the picklable chain/
margin factory functions this needs, and ``_weekly_delta_neutral_fixtures.py``
for the shared strikes/security-ids/scrip-master this reuses rather than
duplicates.
"""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import _weekly_delta_neutral_fixtures as weekly_fixtures

from common.engine.config import SessionConfig
from common.engine.positional.positional_models import ENTRY_ROLE_ORDER
from common.models import Tick
from common.persistence import Database, MigrationRunner
from common.utils.timeutils import now_ist
from runtimes.positional_options.config_adapter import WorkerConfig
from runtimes.positional_options.supervisor import (
    PositionalOptionsSupervisor,
    PositionalSupervisorConfig,
)

IST = ZoneInfo("Asia/Kolkata")
NIFTY_SECURITY_ID = weekly_fixtures.NIFTY_SECURITY_ID
CYCLE_ID = weekly_fixtures.cycle_id_for()
ENTRY_TS = datetime(2026, 8, 19, 9, 26, 0, tzinfo=IST)  # a Wednesday, entry window 09:25-09:40

_LEG_QUOTE = {
    "HEDGE_PUT": (weekly_fixtures.HEDGE_PUT_STRIKE, "PE", 18.0, 20.0),
    "HEDGE_CALL": (weekly_fixtures.HEDGE_CALL_STRIKE, "CE", 17.0, 19.0),
    "SHORT_PUT": (weekly_fixtures.SHORT_PUT_STRIKE, "PE", 80.0, 82.0),
    "SHORT_CALL": (weekly_fixtures.SHORT_CALL_STRIKE, "CE", 78.0, 80.0),
}
_ROLE_SECURITY_ID = {
    role: weekly_fixtures.SECURITY_IDS[(strike, option_type)]
    for role, (strike, option_type, _bid, _ask) in _LEG_QUOTE.items()
}
#: Every original role's own leg id — deterministic (Cycle.next_leg_id is
#: f"{cycle_id}:{role}:{sequence}", the original leg is always sequence 1).
_ROLE_LEG_ID = {role.value: f"{CYCLE_ID}:{role.value}:1" for role in ENTRY_ROLE_ORDER}


def _stage_tick(role: str, second: int) -> Tick:
    strike, option_type, bid, ask = _LEG_QUOTE[role]
    ts = ENTRY_TS + timedelta(seconds=second)
    return weekly_fixtures.leg_tick(strike, option_type, bid, ask, ts)


def _underlying_tick(second: int, price: float = 24000.0) -> Tick:
    return weekly_fixtures.underlying_tick(ENTRY_TS + timedelta(seconds=second), price=price)


def _leg_row(database_path: Path, *, role: str) -> Any:
    db = Database(database_path)
    try:
        return (
            db.connect()
            .execute(
                "SELECT * FROM strategy_cycle_legs WHERE cycle_id = ? AND leg_role = ?",
                (CYCLE_ID, role),
            )
            .fetchone()
        )
    finally:
        db.close()


def _cycle_row(database_path: Path) -> Any:
    db = Database(database_path)
    try:
        return (
            db.connect()
            .execute("SELECT * FROM strategy_cycles WHERE cycle_id = ?", (CYCLE_ID,))
            .fetchone()
        )
    finally:
        db.close()


def _cycle_order_history(database_path: Path) -> list[Any]:
    db = Database(database_path)
    try:
        return list(
            db.connect()
            .execute(
                "SELECT leg_id, side FROM order_intents WHERE basket_id = ?", (CYCLE_ID,)
            )
            .fetchall()
        )
    finally:
        db.close()


class _StagedAdapter:
    """Replays a real ``weekly_delta_neutral`` entry's tick script across
    real hub iterations, gated on genuine, observable events — never a
    fixed sleep. Satisfies :class:`~common.market_data.adapter.
    MarketFeedAdapter` structurally.

    Two distinct waits per stage, both necessary: :meth:`_wait_until_wanted`
    (the dynamic subscription has round-tripped child -> control queue ->
    hub, so a tick for that security is no longer silently dropped by
    ``SharedFeedHub._fan_out_tick``/``_fan_out`` — confirmed by reading
    ``common/feed/hub.py``) before sending that leg's own tick, then
    :meth:`_wait_until_open` (the child genuinely processed the fill and
    persisted it) before moving to the next stage — which is also what
    makes the *next* role's own dynamic subscription request exist at all,
    since the engine only requests it once the previous role resolves.
    """

    def __init__(
        self,
        *,
        database_path: Path,
        max_wait_seconds: float = 30.0,
        poll_seconds: float = 0.05,
    ) -> None:
        self._database_path = database_path
        #: Set via :meth:`set_channel` once known — ``PositionalOptions
        #: Supervisor``'s own constructor needs an adapter *before*
        #: ``add_worker`` exists to return one, so this cannot be a
        #: constructor argument here (a genuine ordering constraint, not a
        #: convenience).
        self._channel: Any = None
        self._max_wait = max_wait_seconds
        self._poll = poll_seconds
        self._running = False
        self.subscribe_calls: list[tuple[tuple[str, ...], int | None, int | None]] = []
        #: role -> {every role's own state}, snapshotted the instant that
        #: role itself first reached OPEN — the "fills only that leg per
        #: hub iteration" proof.
        self.snapshots_at_fill: dict[str, dict[str, str]] = {}

    def set_channel(self, channel: Any) -> None:
        self._channel = channel

    def subscribe(
        self, security_ids: Any, *, segment: int | None = None, mode: int | None = None
    ) -> None:
        self.subscribe_calls.append((tuple(security_ids), segment, mode))

    def request_stop(self) -> None:
        self._running = False

    def stop(self) -> None:
        self._running = False

    def start(self, on_tick: Any, *, on_idle: Any = None) -> None:
        assert self._channel is not None, "set_channel must be called before start()"
        self._running = True
        on_tick(_underlying_tick(0))  # the initial evaluation -> ENTER_CYCLE

        for offset, role_enum in enumerate(ENTRY_ROLE_ORDER):
            role = role_enum.value
            second = 1 + offset * 2
            self._wait_until_wanted(_ROLE_SECURITY_ID[role], on_idle)
            on_tick(_stage_tick(role, second))
            on_tick(_underlying_tick(second + 1))
            self._wait_until_open(role, on_idle)
            self.snapshots_at_fill[role] = self._all_leg_states()

        # Unrelated ticks after the cycle is fully ACTIVE: a fresh
        # underlying price, and a *repeat* of HEDGE_PUT's own tick (already
        # OPEN, so _on_leg_tick only updates last_price) — the "no
        # duplicate orders on unrelated ticks" proof, asserted from the
        # durable rows by the test itself after this run finishes.
        on_tick(_underlying_tick(20, price=24010.0))
        on_tick(_stage_tick("HEDGE_PUT", 21))
        on_tick(_underlying_tick(22, price=24005.0))

        self._running = False

    def _wait_until_wanted(self, security_id: str, on_idle: Any) -> None:
        deadline = time.monotonic() + self._max_wait
        while self._running and time.monotonic() < deadline:
            if on_idle is not None:
                on_idle()
            if self._channel.wants(security_id):
                return
            time.sleep(self._poll)
        raise AssertionError(
            f"{security_id} was never dynamically subscribed within {self._max_wait}s"
        )

    def _wait_until_open(self, role: str, on_idle: Any) -> None:
        deadline = time.monotonic() + self._max_wait
        while self._running and time.monotonic() < deadline:
            if on_idle is not None:
                on_idle()
            row = _leg_row(self._database_path, role=role)
            if row is not None and row["state"] == "OPEN":
                return
            time.sleep(self._poll)
        raise AssertionError(f"{role} never reached OPEN within {self._max_wait}s")

    def _all_leg_states(self) -> dict[str, str]:
        states = {}
        for role_enum in ENTRY_ROLE_ORDER:
            row = _leg_row(self._database_path, role=role_enum.value)
            states[role_enum.value] = row["state"] if row is not None else "MISSING"
        return states


def _alpha_worker_config(tmp_path: Path, *, database_path: Path) -> WorkerConfig:
    runtime_root = tmp_path / "runtime"
    # Defaults (3600s each — see build_worker_config) are fine here: the
    # spawned worker's own clock is now the test's OffsetClock (see the
    # test function itself), tracking the same simulated timeline as every
    # scripted tick, so there is no real-wall-clock gap for these timeouts
    # to have to outlast. The timeouts' own boundary behaviour has
    # dedicated, clock-controlled coverage in
    # test_weekly_delta_neutral_entry_stage_timeout.py.
    base = weekly_fixtures.build_worker_config("2026-08-19")
    return replace(
        base,
        database_path=database_path,
        lock_dir=runtime_root / "locks",
        pid_dir=runtime_root / "pid",
        log_dir=tmp_path / "logs",
        runtime_root=runtime_root,
        cache_dir=tmp_path / "cache",
        chain_fetcher_factory="_positional_multi_strategy_fixtures:build_weekly_chain_fetcher",
        scrip_master_factory="_weekly_delta_neutral_fixtures:build_scrip_master",
        margin_fetcher_factory="_positional_multi_strategy_fixtures:build_weekly_margin_fetcher",
    )


def _beta_worker_config(tmp_path: Path, *, database_path: Path) -> WorkerConfig:
    """The existing, inert Phase 5 fixture strategy — never enters a cycle
    (see its own docstring) — proving this weekly worker's staged entry
    never disrupts a second, independent positional worker sharing the
    same hub/underlying subscription."""
    runtime_root = tmp_path / "runtime"
    return WorkerConfig(
        runtime_id="positional_options",
        strategy_id="_fixture_beta",
        strategy_ref=(
            "strategies.positional_options._fixture_second_strategy.strategy:"
            "FixtureSecondStrategy"
        ),
        trading_date="2026-08-19",
        lots=1,
        timezone="Asia/Kolkata",
        underlying_security_id=NIFTY_SECURITY_ID,
        underlying_instrument="NIFTY",
        underlying_segment="IDX_I",
        option_segment="NSE_FNO",
        session=SessionConfig(
            timezone="Asia/Kolkata", start_time="09:15", end_time="15:15",
            square_off_time="15:20", holidays=(),
        ),
        risk_free_rate=0.065,
        dividend_yield=0.0,
        quote_max_age_seconds=3600.0,
        evaluation_interval_seconds=0.0,
        max_adjustments_per_day=1,
        max_adjustments_per_cycle=3,
        min_minutes_between_adjustments=90,
        parameters={},
        database_path=database_path,
        lock_dir=runtime_root / "locks",
        pid_dir=runtime_root / "pid",
        log_dir=tmp_path / "logs",
        runtime_root=runtime_root,
        cache_dir=tmp_path / "cache",
        chain_fetcher_factory="_positional_multi_strategy_fixtures:build_fixture_chain_fetcher",
        scrip_master_factory="_positional_multi_strategy_fixtures:fixture_scrip_master",
        margin_fetcher_factory="_positional_multi_strategy_fixtures:build_fixture_margin_fetcher",
    )


def _build_supervisor(
    tmp_path: Path, *, database_path: Path, adapter: Any, clock: Any = None
) -> PositionalOptionsSupervisor:
    runtime_root = tmp_path / "runtime"
    config = PositionalSupervisorConfig(
        runtime_id="positional_options",
        database_path=database_path,
        lock_dir=runtime_root / "locks",
        pid_dir=runtime_root / "pid",
        log_dir=tmp_path / "logs",
        runtime_root=runtime_root,
        cache_dir=tmp_path / "cache",
    )
    if clock is None:
        return PositionalOptionsSupervisor(config, adapter)
    return PositionalOptionsSupervisor(config, adapter, clock=clock)


def _incident_messages(database_path: Path, *, strategy_id: str) -> list[str]:
    db = Database(database_path)
    try:
        rows = db.connect().execute(
            "SELECT message FROM errors WHERE strategy_id = ? "
            "AND component = 'positional_multi_leg_engine.incident' ORDER BY id",
            (strategy_id,),
        ).fetchall()
        return [row["message"] for row in rows]
    finally:
        db.close()


def test_real_weekly_strategy_completes_a_staged_entry_over_the_shared_hub(
    tmp_path: Any,
) -> None:
    database_path = tmp_path / "positional_options.db"
    MigrationRunner(Database(database_path)).run_pending()

    adapter = _StagedAdapter(database_path=database_path)
    # Phase 6A: HubTickFeed's own on_poll now drives engine.poll() on a
    # timer, independent of whichever tick last arrived — see
    # positional_engine._maybe_evaluate. A worker's clock therefore has to
    # track the *same* timeline this test's scripted ticks use (ENTRY_TS),
    # never the real wall clock a poll would otherwise default to: a poll
    # evaluating "now" as the real current instant, while the last tick it
    # saw carries this test's simulated 09:26 IST timestamp, would make
    # _maybe_evaluate's own elapsed-time gate go negative and silently drop
    # every tick-driven evaluation forever. OffsetClock is picklable, so it
    # survives the real multiprocessing.Process spawn boundary — a bare
    # lambda would not.
    clock = weekly_fixtures.OffsetClock(offset=ENTRY_TS - now_ist())
    supervisor = _build_supervisor(
        tmp_path, database_path=database_path, adapter=adapter, clock=clock
    )
    alpha = _alpha_worker_config(tmp_path, database_path=database_path)
    beta = _beta_worker_config(tmp_path, database_path=database_path)
    alpha_channel = supervisor.add_worker(alpha)
    supervisor.add_worker(beta)
    adapter.set_channel(alpha_channel)

    result = supervisor.run()

    assert result.workers_started == 2
    assert result.worker_exit_codes["weekly_delta_neutral"] == 0
    assert result.worker_exit_codes["_fixture_beta"] == 0

    # ----------------------------------------------------- staged fills
    # Every role filled on its own hub iteration, and *only* that role —
    # every later-in-order role was still not OPEN at the instant this one
    # first became OPEN (hedge-first order preserved, no bulk/parallel
    # fill).
    order = [role.value for role in ENTRY_ROLE_ORDER]
    for index, role in enumerate(order):
        snapshot = adapter.snapshots_at_fill[role]
        assert snapshot[role] == "OPEN"
        for later_role in order[index + 1 :]:
            assert snapshot[later_role] != "OPEN", (
                f"{later_role} was already OPEN when {role} first filled"
            )

    # ------------------------------------------------------- final state
    cycle = _cycle_row(database_path)
    assert cycle is not None
    assert cycle["state"] == "ACTIVE"

    legs = {role: _leg_row(database_path, role=role) for role in order}
    assert all(leg is not None and leg["state"] == "OPEN" for leg in legs.values())

    # Exact hedge-then-short fill order, via each leg's own durable,
    # timezone-aware entry_time — never order_intents.sequence_number
    # (this whole run is one session, so that risk does not literally
    # apply here, but the fixture-family convention is to prove ordering
    # this way everywhere, never re-introducing the Phase 3 false-positive
    # pattern by habit).
    entry_times = {
        role: datetime.fromisoformat(legs[role]["entry_time"]) for role in order
    }
    assert entry_times["HEDGE_PUT"] < entry_times["HEDGE_CALL"] < entry_times["SHORT_PUT"] < (
        entry_times["SHORT_CALL"]
    )

    # ------------------------------------------------ dynamic subscription
    for role in order:
        security_id = _ROLE_SECURITY_ID[role]
        assert security_id not in alpha_channel.security_ids, "never part of the static union"
        assert security_id in alpha_channel.dynamic_ids, "genuinely requested at runtime"

    # ------------------------------------------------- no duplicate orders
    history = _cycle_order_history(database_path)
    for role in order:
        leg_id = _ROLE_LEG_ID[role]
        side = legs[role]["side"]
        entry_rows = [r for r in history if r["leg_id"] == leg_id and r["side"] == side]
        assert len(entry_rows) == 1, f"{role} was submitted more than once"
    strategy_cycle_legs_count = len(
        [r for r in history if r["leg_id"] in _ROLE_LEG_ID.values()]
    )
    assert strategy_cycle_legs_count == 4

    # ------------------------------------- beta's own non-disruption proof
    beta_incidents = _incident_messages(database_path, strategy_id="_fixture_beta")
    assert len(beta_incidents) == 1
    assert f"underlying_security_id={NIFTY_SECURITY_ID}" in beta_incidents[0]
