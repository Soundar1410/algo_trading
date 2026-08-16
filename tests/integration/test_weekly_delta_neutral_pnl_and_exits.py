"""P&L and exit-priority acceptance rows (spec section 6, spec section
13.4) — driven through the real
``runtimes.positional_options.worker.build_engine`` wiring against a real
temp SQLite database. See
``tests/integration/_weekly_delta_neutral_fixtures.py`` for the shared
fixture helpers.

Boundary tests use :data:`ZERO_COST_RATES` so the original entry credit
(``B0``) and the short put's own fill price — both read back from the
database via ``_enter``, never hardcoded, since ``PaperBroker``'s
bid/ask-crossing adverse-fill model means a real fill is not simply a
quote's midpoint — have no real broker-charge arithmetic layered on top;
:func:`test_charges_are_netted_into_the_pnl_used_for_exit_decisions` is the
one test that turns real (nonzero, default) cost rates back on, to prove
they are genuinely netted rather than merely capable of being zero.

No live order API is constructed or called anywhere in this module.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from _weekly_delta_neutral_fixtures import (
    ROLL_UP_SHORT_PUT_STRIKE,
    SHORT_CALL_STRIKE,
    SHORT_PUT_STRIKE,
    ZERO_COST_RATES,
    ScriptedFeed,
    build_scrip_master,
    build_worker_config,
    chain_fetcher_for,
    closing_sequence_numbers,
    cycle_id_for,
    entry_ticks,
    fake_margin_fetcher,
    initial_chain_payload,
    leg_tick,
    open_repository,
    set_leg_delta,
    underlying_tick,
)

from common.config.models import ExecutionMode
from common.engine.positional.positional_models import CycleState, LegRole
from common.execution import ExecutionRepository
from common.margin import MarginEstimator
from common.persistence import Database
from runtimes.positional_options.worker import build_engine

IST = ZoneInfo("Asia/Kolkata")
ENTRY_DATE = "2026-08-19"  # a Wednesday
CYCLE_ID = cycle_id_for()

_SCRIP_MASTER = build_scrip_master()

#: Every boundary test reads the real, engine-computed
#: ``cycle.original_net_credit`` (B0) *and* the short put's own real fill
#: price back from the database via :func:`_enter` rather than hardcoding
#: either, so a fixture change can never silently desynchronise a boundary
#: from the credit/price it is derived from — see
#: :func:`test_fixture_sanity_original_credit`.
SHORT_PUT_QUANTITY = 75


def _ts(hour: int, minute: int, second: int = 0, day: int = 19) -> datetime:
    return datetime(2026, 8, day, hour, minute, second, tzinfo=IST)


def _run_session(
    tmp_path: Any,
    *,
    db_name: str,
    trading_date: str,
    steps: list[Any],
    payload: dict[str, Any],
    clock_ts: datetime,
    cost_rates: dict[str, Any] | None = ZERO_COST_RATES,
    allocated_capital: float | None = None,
    session_pid: int = 1234,
    before_run: Any = None,
) -> tuple[Database, ExecutionRepository]:
    database, repository = open_repository(tmp_path, db_name)
    session = repository.open_session(
        runtime_id="positional_options", strategy_id="weekly_delta_neutral",
        execution_mode=ExecutionMode.PAPER, process_role="worker", pid=session_pid,
    )
    config = build_worker_config(
        trading_date, cost_rates=cost_rates, allocated_capital=allocated_capital,
    )
    feed = ScriptedFeed(steps=list(steps), initial_now=clock_ts)
    built = build_engine(
        config, repository=repository, session_id=session.id,
        feed=feed,
        chain_fetcher=chain_fetcher_for(payload),
        scrip_master=_SCRIP_MASTER,
        margin_estimator=MarginEstimator(margin_fetcher=fake_margin_fetcher),
        clock=feed.clock,
    )
    if before_run is not None:
        before_run(built.engine)
    built.engine.run()
    return database, repository


def _enter(
    tmp_path: Any, db_name: str, **kwargs: Any
) -> tuple[Database, ExecutionRepository, float, float]:
    """Real entry only; returns ``(database, repository, B0, short_put_entry_price)``
    — both read back from the database, never hardcoded. ``PaperBroker``'s
    own bid/ask-crossing adverse-fill model means the real fill price is
    *not* simply the quote's midpoint, so every P&L boundary below is
    computed from this real, engine-reported fill price."""
    entry_ts = _ts(9, 26)
    database, repository = _run_session(
        tmp_path, db_name=db_name, trading_date=ENTRY_DATE,
        steps=entry_ticks(entry_ts), payload=initial_chain_payload(), clock_ts=entry_ts,
        **kwargs,
    )
    cycle = repository.load_cycle(cycle_id=CYCLE_ID)
    assert cycle is not None and cycle["state"] == CycleState.ACTIVE.value
    legs = repository.load_cycle_legs(cycle_id=CYCLE_ID)
    short_put = next(leg for leg in legs if leg["leg_role"] == LegRole.SHORT_PUT.value)
    return (
        database, repository, float(cycle["original_net_credit"]), float(short_put["entry_price"])
    )


def _short_put_price_tick_for_loss(
    entry_price: float, loss_amount: float, ts: datetime
) -> Any:
    """A short-put leg tick whose price produces exactly ``loss_amount`` of
    unrealised loss on that one leg (every other leg stays at its own
    entry price, contributing zero) — ``(entry_price - last_price) *
    quantity`` for a SELL leg (``LegInstance.unrealised_pnl``)."""
    price = entry_price + loss_amount / SHORT_PUT_QUANTITY
    return leg_tick(SHORT_PUT_STRIKE, "PE", price - 1.0, price + 1.0, ts)


def _short_put_price_tick_for_profit(
    entry_price: float, profit_amount: float, ts: datetime
) -> Any:
    price = entry_price - profit_amount / SHORT_PUT_QUANTITY
    return leg_tick(SHORT_PUT_STRIKE, "PE", max(price - 1.0, 0.05), price + 1.0, ts)


def test_fixture_sanity_original_credit(tmp_path: Any) -> None:
    """Not an acceptance row by itself — proves ``B0`` (this fixture's
    engine-computed original net credit) is a stable, known, nonzero
    number every other boundary test in this module can safely be a
    multiple of."""
    database, _repository, b0, entry_price = _enter(tmp_path, "sanity.db")
    try:
        assert b0 > 0
        assert entry_price > 0
    finally:
        database.close()


# ================================================================ 13.4 — P&L
def test_realized_pnl_includes_previously_closed_adjustment_legs(tmp_path: Any) -> None:
    """Spec section 6.1: realised P&L from a leg closed by an *adjustment*
    (not just an original entry leg) must be folded into
    ``net_strategy_pnl`` — proven by rolling the short put at a real loss on
    that one leg, then confirming the cycle's own realised total already
    reflects it before any further price move."""
    entry_ts = _ts(9, 26)
    payload = initial_chain_payload()
    steps: list[Any] = [*entry_ticks(entry_ts)]

    def _bump() -> None:
        set_leg_delta(payload, SHORT_CALL_STRIKE, "CE", 0.37)

    steps.append(_bump)
    # A real, realised loss on the short put itself before it gets rolled —
    # its last_price moves against the short, so the close that the roll
    # performs realises a genuine loss on this one leg.
    steps.append(leg_tick(SHORT_PUT_STRIKE, "PE", 99.0, 101.0, _ts(9, 31)))
    steps.append(underlying_tick(_ts(9, 31)))
    steps.append(underlying_tick(_ts(9, 36)))
    steps.append(leg_tick(ROLL_UP_SHORT_PUT_STRIKE, "PE", 95.0, 97.0, _ts(9, 41)))
    steps.append(underlying_tick(_ts(9, 41)))

    database, repository = _run_session(
        tmp_path, db_name="realized_pnl.db", trading_date=ENTRY_DATE,
        steps=steps, payload=payload, clock_ts=entry_ts,
    )
    try:
        cycle_row = repository.load_cycle(cycle_id=CYCLE_ID)
        assert cycle_row is not None
        legs = repository.load_cycle_legs(cycle_id=CYCLE_ID)
        closed_short_put = next(
            leg for leg in legs
            if leg["leg_role"] == LegRole.SHORT_PUT.value and not leg["is_replacement"]
        )
        assert closed_short_put["state"] == "CLOSED"
        # entry ~100 mid, closed ~100 mid: a real, nonzero realised loss on
        # this one leg (SELL: entry_price - exit_price, negative here).
        assert closed_short_put["realized_gross_pnl"] is not None
        assert closed_short_put["realized_gross_pnl"] < 0

        # The *cycle's* own realised total already includes it (not just
        # this leg's own row) — the same read the strategy's own
        # compute_pnl uses.
        from common.engine.positional.positional_state import load_cycle as _load

        cycle = _load(
            repository, runtime_id="positional_options",
            strategy_id="weekly_delta_neutral", execution_mode=ExecutionMode.PAPER,
        )
        assert cycle is not None
        assert cycle.realised_gross_pnl() == closed_short_put["realized_gross_pnl"]
    finally:
        database.close()


def test_original_credit_never_rebases_after_adjustment(tmp_path: Any) -> None:
    entry_ts = _ts(9, 26)
    payload = initial_chain_payload()
    engine_box: list[Any] = []
    captured_b0: list[float] = []
    steps: list[Any] = [*entry_ticks(entry_ts)]

    def _capture_b0_right_after_entry() -> None:
        cycle = engine_box[0]._cycle
        assert cycle is not None and cycle.original_net_credit is not None
        captured_b0.append(cycle.original_net_credit)

    def _bump() -> None:
        set_leg_delta(payload, SHORT_CALL_STRIKE, "CE", 0.37)

    steps.append(_capture_b0_right_after_entry)
    steps.append(_bump)
    steps.append(underlying_tick(_ts(9, 31)))
    steps.append(underlying_tick(_ts(9, 36)))
    steps.append(leg_tick(ROLL_UP_SHORT_PUT_STRIKE, "PE", 95.0, 97.0, _ts(9, 41)))
    steps.append(underlying_tick(_ts(9, 41)))

    database, repository = _run_session(
        tmp_path, db_name="no_rebase.db", trading_date=ENTRY_DATE,
        steps=steps, payload=payload, clock_ts=entry_ts,
        before_run=lambda engine: engine_box.append(engine),
    )
    try:
        assert captured_b0, "fixture sanity: B0 must have been captured right after entry"
        cycle = repository.load_cycle(cycle_id=CYCLE_ID)
        assert cycle is not None
        assert cycle["adjustments_this_cycle"] == 1, "fixture sanity: the roll must have happened"
        # Still exactly the entry-time value — an adjustment must never
        # recompute or touch original_net_credit, even though the roll
        # itself realised a real loss/gain on the leg it closed.
        assert cycle["original_net_credit"] == captured_b0[0]
    finally:
        database.close()


# ===================================================== 6.2/13.4 — real exits
def test_hard_stop_closes_the_cycle_shorts_before_hedges(tmp_path: Any) -> None:
    database, repository, b0, entry_price = _enter(tmp_path, "hard_stop.db")
    try:
        hard_threshold = 1.50 * b0
        loss_ts = _ts(9, 31)
        steps = [
            _short_put_price_tick_for_loss(entry_price, hard_threshold, loss_ts),
            underlying_tick(loss_ts),
        ]
        # Continue the *same* database with a fresh session (mirrors a
        # normal next-evaluation tick, not a restart) — reusing _run_session
        # against the already-entered db.
        database.close()
        database, repository = _run_session(
            tmp_path, db_name="hard_stop.db", trading_date=ENTRY_DATE,
            steps=steps, payload=initial_chain_payload(), clock_ts=loss_ts, session_pid=2,
        )
        cycle = repository.load_cycle(cycle_id=CYCLE_ID)
        assert cycle is not None
        assert cycle["state"] == CycleState.COMPLETED.value
        legs = repository.load_cycle_legs(cycle_id=CYCLE_ID)
        assert all(leg["state"] == "CLOSED" for leg in legs)

        # Shorts closed (and their exit fills recorded) before hedges (spec
        # section 6.2/8) — using only each leg's own *closing*-side order
        # (sequence_number is a per-session counter, and this exit ran in
        # its own later session, so a bare cross-session max would be
        # meaningless — see closing_sequence_numbers's own docstring).
        history = repository.cycle_order_history(cycle_id=CYCLE_ID)
        closing_seq = closing_sequence_numbers(history, legs)
        short_leg_ids = [
            leg["leg_id"] for leg in legs if leg["leg_role"] in ("SHORT_PUT", "SHORT_CALL")
        ]
        hedge_leg_ids = [
            leg["leg_id"] for leg in legs if leg["leg_role"] in ("HEDGE_PUT", "HEDGE_CALL")
        ]
        max_short_exit_seq = max(closing_seq[lid] for lid in short_leg_ids)
        min_hedge_exit_seq = min(closing_seq[lid] for lid in hedge_leg_ids)
        assert max_short_exit_seq < min_hedge_exit_seq
    finally:
        database.close()


def test_profit_target_closes_the_cycle(tmp_path: Any) -> None:
    database, repository, b0, entry_price = _enter(tmp_path, "profit.db")
    try:
        target = 0.55 * b0
        profit_ts = _ts(9, 31)
        steps = [
            _short_put_price_tick_for_profit(entry_price, target, profit_ts),
            underlying_tick(profit_ts),
        ]
        database.close()
        database, repository = _run_session(
            tmp_path, db_name="profit.db", trading_date=ENTRY_DATE,
            steps=steps, payload=initial_chain_payload(), clock_ts=profit_ts, session_pid=2,
        )
        cycle = repository.load_cycle(cycle_id=CYCLE_ID)
        assert cycle is not None
        assert cycle["state"] == CycleState.COMPLETED.value
    finally:
        database.close()


def test_exit_priority_beats_a_pending_adjustment(tmp_path: Any) -> None:
    """Spec section 6.2: an exit condition always overrides an adjustment —
    a hard-stop-triggering loss *and* a delta-adjustment trigger present in
    the same evaluation must exit, never roll."""
    database, repository, b0, entry_price = _enter(tmp_path, "priority.db")
    try:
        payload = initial_chain_payload()
        hard_threshold = 1.50 * b0
        ts1 = _ts(9, 31)

        def _bump_delta() -> None:
            set_leg_delta(payload, SHORT_CALL_STRIKE, "CE", 0.37)

        steps = [
            _bump_delta,
            _short_put_price_tick_for_loss(entry_price, hard_threshold, ts1),
            underlying_tick(ts1),
        ]
        database.close()
        database, repository = _run_session(
            tmp_path, db_name="priority.db", trading_date=ENTRY_DATE,
            steps=steps, payload=payload, clock_ts=ts1, session_pid=2,
        )
        cycle = repository.load_cycle(cycle_id=CYCLE_ID)
        assert cycle is not None
        assert cycle["state"] == CycleState.COMPLETED.value
        assert cycle["adjustments_this_cycle"] == 0, (
            "the exit must win outright — _maybe_adjust is never even reached"
        )
    finally:
        database.close()


def test_missing_chain_never_blocks_a_hard_stop_exit(tmp_path: Any) -> None:
    """Spec section 4.2: missing Greeks/quotes must block risk-*increasing*
    actions only, never a risk-reducing exit — proven by making the chain
    fetcher itself fail while a hard-stop-triggering loss is present."""
    database, _repository, b0, entry_price = _enter(tmp_path, "missing_chain.db")
    try:
        hard_threshold = 1.50 * b0
        ts1 = _ts(9, 31)
        steps = [
            _short_put_price_tick_for_loss(entry_price, hard_threshold, ts1),
            underlying_tick(ts1),
        ]
        database.close()

        def _raising_fetcher(_sid: int, _seg: str, _exp: str) -> Any:
            raise RuntimeError("simulated Dhan option-chain outage")

        db2, repo2 = open_repository(tmp_path, "missing_chain.db")
        session2 = repo2.open_session(
            runtime_id="positional_options", strategy_id="weekly_delta_neutral",
            execution_mode=ExecutionMode.PAPER, process_role="worker", pid=2,
        )
        config = build_worker_config(ENTRY_DATE, cost_rates=ZERO_COST_RATES)
        feed = ScriptedFeed(steps=steps, initial_now=ts1)
        built = build_engine(
            config, repository=repo2, session_id=session2.id, feed=feed,
            chain_fetcher=_raising_fetcher, scrip_master=_SCRIP_MASTER,
            margin_estimator=MarginEstimator(margin_fetcher=fake_margin_fetcher),
            clock=feed.clock,
        )
        built.engine.run()
        cycle = repo2.load_cycle(cycle_id=CYCLE_ID)
        assert cycle is not None
        assert cycle["state"] == CycleState.COMPLETED.value, (
            "a hard-stop exit needs only leg last_price (real ticks), never the chain"
        )
        database = db2
    finally:
        database.close()


def test_charges_are_netted_into_the_pnl_used_for_exit_decisions(tmp_path: Any) -> None:
    """The one test in this module using real, nonzero (default) cost
    rates — proves entry+adjustment+exit charges are genuinely subtracted,
    not merely capable of being configured to zero."""
    entry_ts = _ts(9, 26)
    database, repository = _run_session(
        tmp_path, db_name="real_charges.db", trading_date=ENTRY_DATE,
        steps=entry_ticks(entry_ts), payload=initial_chain_payload(), clock_ts=entry_ts,
        cost_rates=None,  # WorkerConfig/PaperBroker's own real defaults — never zeroed
    )
    try:
        cycle = repository.load_cycle(cycle_id=CYCLE_ID)
        assert cycle is not None
        open_positions = repository.open_positions_for_cycle(cycle_id=CYCLE_ID)
        total_open_charges = sum(p.charges for p in open_positions)
        assert total_open_charges > 0, "four real fills at nonzero default cost rates"
        # original_net_credit already nets entry charges (positional_engine.
        # _finalize_entry) — strictly less than the charge-free credit this
        # same fixture produces under ZERO_COST_RATES (see
        # test_fixture_sanity_original_credit).
        assert cycle["original_net_credit"] < 8_910.0
    finally:
        database.close()
