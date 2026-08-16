"""Expiry-day acceptance rows (spec section 8, spec section 13.4/13.5) —
driven through the real
``runtimes.positional_options.worker.build_engine`` wiring against a real
temp SQLite database. See
``tests/integration/_weekly_delta_neutral_fixtures.py`` for the shared
fixture helpers.

**Known gap, disclosed rather than silently fixed (out of this task's
"add tests" scope):** spec section 8's "from 12:00 IST: tighten monitoring
and do not make aggressive inward rolls" has no strategy-side consumer.
``common.engine.positional.lifecycle.PositionalLifecyclePolicy.
aggressive_inward_rolls_permitted``/``expiry_day_phase``'s ``TIGHTEN`` value
exist and are correctly computed, but
``WeeklyDeltaNeutralStrategy`` never reads either — only the 14:30 no-
adjustment cutoff (already covered,
``test_weekly_delta_neutral_adjustment.py::
test_no_normal_adjustment_after_the_expiry_day_cutoff``) is actually wired.
Defining "aggressive/inward" precisely enough to implement and test is a
real design decision, not a test gap — recorded here and in
``docs/IMPLEMENTATION_STATUS_AND_RUNBOOK.md`` rather than guessed at.

No live order API is constructed or called anywhere in this module.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from _weekly_delta_neutral_fixtures import (
    EXPIRY_DATE,
    HEDGE_CALL_STRIKE,
    HEDGE_PUT_STRIKE,
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
    underlying_tick,
)

from common.config.models import ExecutionMode
from common.engine.positional.positional_models import CycleState
from common.execution import ExecutionRepository
from common.margin import MarginEstimator
from common.market_data.scrip_master import ScripMaster
from common.models import Tick
from common.persistence import Database
from runtimes.positional_options.worker import build_engine

IST = ZoneInfo("Asia/Kolkata")
ENTRY_DATE = "2026-08-19"  # a Wednesday
CYCLE_ID = cycle_id_for()

_SCRIP_MASTER = build_scrip_master()


def _ts(hour: int, minute: int, second: int = 0, day: int = 19, month: int = 8) -> datetime:
    return datetime(2026, month, day, hour, minute, second, tzinfo=IST)


def _run_session(
    tmp_path: Any,
    *,
    db_name: str,
    trading_date: str,
    steps: list[Any],
    payload: dict[str, Any],
    clock_ts: datetime,
    scrip_master: ScripMaster = _SCRIP_MASTER,
    session_pid: int = 1234,
    before_run: Any = None,
) -> tuple[Database, ExecutionRepository]:
    database, repository = open_repository(tmp_path, db_name)
    session = repository.open_session(
        runtime_id="positional_options", strategy_id="weekly_delta_neutral",
        execution_mode=ExecutionMode.PAPER, process_role="worker", pid=session_pid,
    )
    config = build_worker_config(trading_date, cost_rates=ZERO_COST_RATES)
    feed = ScriptedFeed(steps=list(steps), initial_now=clock_ts)
    built = build_engine(
        config, repository=repository, session_id=session.id,
        feed=feed,
        chain_fetcher=chain_fetcher_for(payload),
        scrip_master=scrip_master,
        margin_estimator=MarginEstimator(margin_fetcher=fake_margin_fetcher),
        clock=feed.clock,
    )
    if before_run is not None:
        before_run(built.engine)
    built.engine.run()
    return database, repository


def _enter(tmp_path: Any, db_name: str) -> tuple[Database, ExecutionRepository]:
    entry_ts = _ts(9, 26)
    database, repository = _run_session(
        tmp_path, db_name=db_name, trading_date=ENTRY_DATE,
        steps=entry_ticks(entry_ts), payload=initial_chain_payload(), clock_ts=entry_ts,
    )
    cycle = repository.load_cycle(cycle_id=CYCLE_ID)
    assert cycle is not None and cycle["state"] == CycleState.ACTIVE.value
    return database, repository


def _flatten_leg_ticks(ts: datetime) -> list[Any]:
    """Fresh, complete quotes for every original leg — what a real exit
    close needs (spec section 8: bounded aggressive limit repricing from
    the freshest permitted quote, never a synthesized market fill)."""
    return [
        leg_tick(HEDGE_PUT_STRIKE, "PE", 18.0, 20.0, ts),
        leg_tick(HEDGE_CALL_STRIKE, "CE", 17.0, 19.0, ts),
        leg_tick(SHORT_PUT_STRIKE, "PE", 80.0, 82.0, ts),
        leg_tick(SHORT_CALL_STRIKE, "CE", 78.0, 80.0, ts),
    ]


# ============================================== 3.4/13.1/13.5 — actual expiry
_MONDAY_SECURITY_IDS = {
    (HEDGE_PUT_STRIKE, "PE"): "95001",
    (SHORT_PUT_STRIKE, "PE"): "95002",
    (SHORT_CALL_STRIKE, "CE"): "95003",
    (HEDGE_CALL_STRIKE, "CE"): "95004",
}


def _monday_shifted_scrip_master() -> ScripMaster:
    """A holiday-shifted expiry: the exchange's own nearest listed weekly
    expiry after entry Wednesday 2026-08-19 is Monday 2026-08-24, not the
    nominal following Wednesday — spec section 3.4: "never derive the
    active expiry from weekday arithmetic alone."""
    monday_expiry = "2026-08-24"
    rows = [
        [
            "SEM_SMST_SECURITY_ID", "SEM_INSTRUMENT_NAME", "SEM_TRADING_SYMBOL",
            "SEM_CUSTOM_SYMBOL", "SEM_EXPIRY_DATE", "SEM_STRIKE_PRICE",
            "SEM_OPTION_TYPE", "SEM_LOT_UNITS", "SEM_EXM_EXCH_ID", "SEM_SEGMENT",
        ]
    ]
    for (strike, option_type), security_id in _MONDAY_SECURITY_IDS.items():
        rows.append(
            [
                security_id, "OPTIDX", f"NIFTY-24AUG2026-{strike:.0f}-{option_type}",
                f"NIFTY {strike:.0f} {option_type}", f"{monday_expiry} 00:00:00",
                f"{strike:.0f}", option_type, "75", "NSE", "D",
            ]
        )
    buffer = io.StringIO()
    csv.writer(buffer).writerows(rows)
    return ScripMaster("NIFTY", exchange="NSE").load_from_text(buffer.getvalue())


def _monday_leg_tick(strike: float, option_type: str, bid: float, ask: float, ts: datetime) -> Any:
    security_id = _MONDAY_SECURITY_IDS[(strike, option_type)]
    return Tick(
        security_id=security_id, instrument=security_id, last_price=(bid + ask) / 2.0,
        exchange_time=ts, received_at=ts, bid_price=bid, ask_price=ask,
    )


def _monday_entry_ticks(entry_ts: datetime) -> list[Any]:
    return [
        _monday_leg_tick(HEDGE_PUT_STRIKE, "PE", 18.0, 20.0, entry_ts),
        _monday_leg_tick(HEDGE_CALL_STRIKE, "CE", 17.0, 19.0, entry_ts),
        _monday_leg_tick(SHORT_PUT_STRIKE, "PE", 80.0, 82.0, entry_ts),
        _monday_leg_tick(SHORT_CALL_STRIKE, "CE", 78.0, 80.0, entry_ts),
        underlying_tick(entry_ts),
    ]


def test_actual_persisted_expiry_is_the_real_monday_shifted_date(tmp_path: Any) -> None:
    scrip_master = _monday_shifted_scrip_master()
    entry_ts = _ts(9, 26)
    database, repository = _run_session(
        tmp_path, db_name="monday_shift.db", trading_date=ENTRY_DATE,
        steps=_monday_entry_ticks(entry_ts), payload=initial_chain_payload(),
        clock_ts=entry_ts, scrip_master=scrip_master,
    )
    monday_cycle_id = cycle_id_for("2026-08-24")
    try:
        cycle = repository.load_cycle(cycle_id=monday_cycle_id)
        assert cycle is not None
        assert cycle["resolved_expiry_date"] == "2026-08-24", (
            "the exchange's own nearest listed expiry — never weekday arithmetic"
        )
        assert cycle["state"] == CycleState.ACTIVE.value

        # Expiry-day rules now apply on the *persisted* Monday — not the
        # nominal following Wednesday. A tick on the real Monday expiry
        # past the 15:05 planned-exit boundary begins the exit. (No fresh
        # leg quotes are needed for the close itself — LegInstance.
        # last_price from entry's own fill is still valid input to
        # _close_leg_safely — so the underlying tick alone is enough here.)
        monday_after_planned_exit = _ts(15, 6, day=24)
        database.close()
        database, repository = _run_session(
            tmp_path, db_name="monday_shift.db", trading_date="2026-08-24",
            steps=[underlying_tick(monday_after_planned_exit)],
            payload=initial_chain_payload(), clock_ts=monday_after_planned_exit,
            scrip_master=scrip_master, session_pid=2,
        )
        cycle_after = repository.load_cycle(cycle_id=monday_cycle_id)
        assert cycle_after is not None
        assert cycle_after["state"] == CycleState.COMPLETED.value, (
            "the planned exit fired on the real persisted Monday expiry"
        )
        # The nominal following Wednesday (2026-08-26) never received any
        # expiry-day control — sanity-checked by the fact this cycle is
        # already flat before that date could ever matter.
    finally:
        database.close()


# ============================================================== 8/13.4 — 15:05/15:15
def test_planned_exit_begins_at_1505_on_the_actual_expiry_day(tmp_path: Any) -> None:
    database, repository = _enter(tmp_path, "planned_exit.db")
    try:
        before = _ts(14, 59, day=26)
        after = _ts(15, 6, day=26)
        database.close()

        database, repository = _run_session(
            tmp_path, db_name="planned_exit.db", trading_date=EXPIRY_DATE,
            steps=[underlying_tick(before)], payload=initial_chain_payload(),
            clock_ts=before, session_pid=2,
        )
        cycle_before = repository.load_cycle(cycle_id=CYCLE_ID)
        assert cycle_before is not None
        assert cycle_before["state"] == CycleState.ACTIVE.value, "still before 15:05"
        database.close()

        database, repository = _run_session(
            tmp_path, db_name="planned_exit.db", trading_date=EXPIRY_DATE,
            steps=[*_flatten_leg_ticks(after), underlying_tick(after)],
            payload=initial_chain_payload(), clock_ts=after, session_pid=3,
        )
        cycle_after = repository.load_cycle(cycle_id=CYCLE_ID)
        assert cycle_after is not None
        assert cycle_after["state"] == CycleState.COMPLETED.value
    finally:
        database.close()


def test_hard_exit_forces_closure_at_1515_even_if_1505_was_never_evaluated(
    tmp_path: Any,
) -> None:
    """The engine's own hard-expiry-deadline net (positional_engine.py's
    own module docstring) fires regardless of what the strategy itself
    would have signalled — proven by skipping straight past 15:05 to
    15:16 in one evaluation, with no market_fallback_enabled path
    involved (this strategy's config always ships that false; every fill
    in this suite is a real bid/ask-crossing LIMIT-equivalent paper fill,
    never a synthesized market order)."""
    database, repository = _enter(tmp_path, "hard_exit.db")
    try:
        past_hard_deadline = _ts(15, 16, day=26)
        database.close()
        database, repository = _run_session(
            tmp_path, db_name="hard_exit.db", trading_date=EXPIRY_DATE,
            steps=[*_flatten_leg_ticks(past_hard_deadline), underlying_tick(past_hard_deadline)],
            payload=initial_chain_payload(), clock_ts=past_hard_deadline, session_pid=2,
        )
        cycle = repository.load_cycle(cycle_id=CYCLE_ID)
        assert cycle is not None
        assert cycle["state"] == CycleState.COMPLETED.value
        legs = repository.load_cycle_legs(cycle_id=CYCLE_ID)
        assert all(leg["state"] == "CLOSED" for leg in legs)

        # Shorts closed before hedges (spec section 8's exit order),
        # proven the same way as the P&L suite's own hard-stop test — using
        # only each leg's own *closing*-side order, since sequence_number
        # resets per session and this exit ran in its own later session.
        history = repository.cycle_order_history(cycle_id=CYCLE_ID)
        closing_seq = closing_sequence_numbers(history, legs)
        short_ids = [
            leg["leg_id"] for leg in legs if leg["leg_role"] in ("SHORT_PUT", "SHORT_CALL")
        ]
        hedge_ids = [
            leg["leg_id"] for leg in legs if leg["leg_role"] in ("HEDGE_PUT", "HEDGE_CALL")
        ]
        assert max(closing_seq[lid] for lid in short_ids) < min(
            closing_seq[lid] for lid in hedge_ids
        )
    finally:
        database.close()


def test_incomplete_flattening_stays_critical_unresolved_never_completed(
    tmp_path: Any,
) -> None:
    """Spec section 8: "if flattening cannot be proven, keep the cycle
    critical/unresolved ... never mark it complete merely because retry
    limits were exhausted." Forces one hedge's own close to raise (an
    unknown outcome) during the hard-exit sweep."""
    database, repository = _enter(tmp_path, "incomplete_flatten.db")
    try:
        past_hard_deadline = _ts(15, 16, day=26)

        def _break_hedge_close(engine: Any) -> None:
            real_close = engine.positions.close

            def _raising_close(security_id: str, *args: Any, **kwargs: Any) -> Any:
                if security_id == "90001":  # the hedge put
                    raise RuntimeError("simulated paper-broker close failure")
                return real_close(security_id, *args, **kwargs)

            engine.positions.close = _raising_close

        database.close()
        database, repository = _run_session(
            tmp_path, db_name="incomplete_flatten.db", trading_date=EXPIRY_DATE,
            steps=[*_flatten_leg_ticks(past_hard_deadline), underlying_tick(past_hard_deadline)],
            payload=initial_chain_payload(), clock_ts=past_hard_deadline, session_pid=2,
            before_run=_break_hedge_close,
        )
        cycle = repository.load_cycle(cycle_id=CYCLE_ID)
        assert cycle is not None
        assert cycle["state"] == CycleState.CRITICAL_UNRESOLVED.value, (
            "never COMPLETED while any leg's close outcome is unknown"
        )
        legs = repository.load_cycle_legs(cycle_id=CYCLE_ID)
        hedge_put = next(leg for leg in legs if leg["leg_role"] == "HEDGE_PUT")
        assert hedge_put["state"] == "CLOSE_SUBMISSION_UNKNOWN"
        # The shorts, which close first, are unaffected and genuinely closed.
        for role in ("SHORT_PUT", "SHORT_CALL"):
            matching = next(leg for leg in legs if leg["leg_role"] == role)
            assert matching["state"] == "CLOSED"
    finally:
        database.close()
