"""Overnight restart: a cycle entered by one process is adopted by a second
one, against the *same* database file, with no re-entry and no duplicate
row anywhere — the whole point of the durable ``cycle_id`` +
``cycle_position_bindings`` design (spec section 9.2/9.5, D69/limitation
30). Reuses the exact same production ``build_engine`` wiring as
``test_weekly_delta_neutral_entry.py``.

No live order API is constructed or called anywhere in this module.
"""

from __future__ import annotations

import contextlib
import csv
import io
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from common.config.models import ExecutionMode
from common.engine.config import SessionConfig
from common.engine.feed import SimulatedFeed
from common.engine.positional.positional_models import CycleState, LegRole, LegState
from common.execution import ExecutionRepository
from common.margin import LegMarginRequest, MarginEstimator
from common.market_data.scrip_master import ScripMaster
from common.models import Tick
from common.persistence import Database, MigrationRunner
from runtimes.positional_options.config_adapter import WorkerConfig
from runtimes.positional_options.worker import build_engine


def _fake_margin_fetcher(_leg: LegMarginRequest) -> float:
    return 20_000.0

IST = ZoneInfo("Asia/Kolkata")
NIFTY_SECURITY_ID = "13"
ENTRY_DATE = "2026-08-19"  # a Wednesday
RESTART_DATE = "2026-08-20"  # the next trading day
EXPIRY_DATE = "2026-08-26"

HEDGE_PUT_STRIKE = 23150.0
SHORT_PUT_STRIKE = 23500.0
SHORT_CALL_STRIKE = 24500.0
HEDGE_CALL_STRIKE = 24850.0

_SECURITY_IDS = {
    (HEDGE_PUT_STRIKE, "PE"): "90001",
    (SHORT_PUT_STRIKE, "PE"): "90002",
    (SHORT_CALL_STRIKE, "CE"): "90003",
    (HEDGE_CALL_STRIKE, "CE"): "90004",
}


def _scrip_master_csv() -> str:
    rows = [
        [
            "SEM_SMST_SECURITY_ID", "SEM_INSTRUMENT_NAME", "SEM_TRADING_SYMBOL",
            "SEM_CUSTOM_SYMBOL", "SEM_EXPIRY_DATE", "SEM_STRIKE_PRICE",
            "SEM_OPTION_TYPE", "SEM_LOT_UNITS", "SEM_EXM_EXCH_ID", "SEM_SEGMENT",
        ]
    ]
    for (strike, option_type), security_id in _SECURITY_IDS.items():
        rows.append(
            [
                security_id, "OPTIDX", f"NIFTY-26AUG2026-{strike:.0f}-{option_type}",
                f"NIFTY {strike:.0f} {option_type}", f"{EXPIRY_DATE} 00:00:00",
                f"{strike:.0f}", option_type, "75", "NSE", "D",
            ]
        )
    buffer = io.StringIO()
    csv.writer(buffer).writerows(rows)
    return buffer.getvalue()


def _build_scrip_master() -> ScripMaster:
    return ScripMaster("NIFTY", exchange="NSE").load_from_text(_scrip_master_csv())


def _leg(delta: float, bid: float, ask: float) -> dict[str, Any]:
    return {
        "greeks": {"delta": delta, "gamma": 0.001, "theta": -5.0, "vega": 10.0},
        "implied_volatility": 14.0,
        "last_price": (bid + ask) / 2.0,
        "oi": 500_000,
        "top_bid_price": bid,
        "top_ask_price": ask,
        "volume": 25_000,
    }


def _chain_fetcher(_security_id: int, _segment: str, _expiry: str) -> dict[str, Any]:
    return {
        "status": "success",
        "data": {
            "last_price": 24000.0,
            "oc": {
                f"{HEDGE_PUT_STRIKE:.6f}": {"pe": _leg(-0.06, 18.0, 20.0)},
                f"{SHORT_PUT_STRIKE:.6f}": {"pe": _leg(-0.20, 80.0, 82.0)},
                f"{SHORT_CALL_STRIKE:.6f}": {"ce": _leg(0.20, 78.0, 80.0)},
                f"{HEDGE_CALL_STRIKE:.6f}": {"ce": _leg(0.06, 17.0, 19.0)},
            },
        },
    }


def _entry_ts(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 8, 19, hour, minute, second, tzinfo=IST)


def _restart_ts(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 8, 20, hour, minute, second, tzinfo=IST)


def _underlying_tick(ts: datetime, price: float = 24000.0) -> Tick:
    return Tick(
        security_id=NIFTY_SECURITY_ID, instrument="NIFTY", last_price=price,
        exchange_time=ts, received_at=ts,
    )


def _leg_tick(security_id: str, bid: float, ask: float, ts: datetime) -> Tick:
    return Tick(
        security_id=security_id, instrument=security_id, last_price=(bid + ask) / 2.0,
        exchange_time=ts, received_at=ts, bid_price=bid, ask_price=ask,
    )


def _build_worker_config(trading_date: str) -> WorkerConfig:
    return WorkerConfig(
        runtime_id="positional_options",
        strategy_id="weekly_delta_neutral",
        strategy_ref=(
            "strategies.positional_options.weekly_delta_neutral.strategy:"
            "WeeklyDeltaNeutralStrategy"
        ),
        trading_date=trading_date,
        lots=1,
        timezone="Asia/Kolkata",
        underlying_security_id=NIFTY_SECURITY_ID,
        underlying_instrument="NIFTY",
        underlying_segment="IDX_I",
        option_segment="NSE_FNO",
        session=SessionConfig(
            timezone="Asia/Kolkata", start_time="09:25", end_time="09:40",
            square_off_time="15:15", holidays=(),
        ),
        risk_free_rate=0.065,
        dividend_yield=0.0,
        quote_max_age_seconds=30.0,
        evaluation_interval_seconds=0.0,
        max_adjustments_per_day=1,
        max_adjustments_per_cycle=3,
        min_minutes_between_adjustments=90,
        parameters={
            "underlying": "NIFTY", "index_security_id": NIFTY_SECURITY_ID,
            "index_segment": "IDX_I", "fno_segment": "NSE_FNO",
        },
    )


def test_restart_adopts_the_same_cycle_with_no_re_entry_and_no_duplicate_rows(
    tmp_path,
) -> None:
    db_path = tmp_path / "positional_options_restart.db"
    cycle_id = f"weekly_delta_neutral:{EXPIRY_DATE}"

    # -------------------------------------------------------------- day 1
    database = Database(db_path)
    MigrationRunner(database).run_pending()
    repository = ExecutionRepository(database)
    entry_ts = _entry_ts(9, 26, 0)
    session1 = repository.open_session(
        runtime_id="positional_options", strategy_id="weekly_delta_neutral",
        execution_mode=ExecutionMode.PAPER, process_role="worker", pid=1111,
    )
    ticks_day1 = [
        _leg_tick(_SECURITY_IDS[(HEDGE_PUT_STRIKE, "PE")], 18.0, 20.0, entry_ts),
        _leg_tick(_SECURITY_IDS[(HEDGE_CALL_STRIKE, "CE")], 17.0, 19.0, entry_ts),
        _leg_tick(_SECURITY_IDS[(SHORT_PUT_STRIKE, "PE")], 80.0, 82.0, entry_ts),
        _leg_tick(_SECURITY_IDS[(SHORT_CALL_STRIKE, "CE")], 78.0, 80.0, entry_ts),
        _underlying_tick(entry_ts),
    ]
    built1 = build_engine(
        _build_worker_config(ENTRY_DATE), repository=repository, session_id=session1.id,
        feed=SimulatedFeed(ticks_day1), chain_fetcher=_chain_fetcher,
        scrip_master=_build_scrip_master(),
        margin_estimator=MarginEstimator(margin_fetcher=_fake_margin_fetcher),
        clock=lambda: entry_ts,
    )
    built1.engine.run()
    repository.close_session(session1.id, reason="clean_shutdown")
    database.close()

    cycle_after_day1 = repository.load_cycle(cycle_id=cycle_id)
    assert cycle_after_day1 is not None
    assert cycle_after_day1["state"] == CycleState.ACTIVE.value
    legs_after_day1 = repository.load_cycle_legs(cycle_id=cycle_id)
    assert all(leg["state"] == "OPEN" for leg in legs_after_day1)
    positions_after_day1 = {
        p.security_id: p for p in repository.open_positions_for_cycle(cycle_id=cycle_id)
    }
    assert len(positions_after_day1) == 4

    # -------------------------------------------------- day 2 — a restart
    database2 = Database(db_path)
    MigrationRunner(database2).run_pending()  # replay-safe no-op
    repository2 = ExecutionRepository(database2)
    restart_ts = _restart_ts(10, 0, 0)
    session2 = repository2.open_session(
        runtime_id="positional_options", strategy_id="weekly_delta_neutral",
        execution_mode=ExecutionMode.PAPER, process_role="worker", pid=2222,
    )
    ticks_day2 = [_underlying_tick(restart_ts)]
    built2 = build_engine(
        _build_worker_config(RESTART_DATE), repository=repository2, session_id=session2.id,
        feed=SimulatedFeed(ticks_day2), chain_fetcher=_chain_fetcher,
        scrip_master=_build_scrip_master(),
        margin_estimator=MarginEstimator(margin_fetcher=_fake_margin_fetcher),
        clock=lambda: restart_ts,
    )
    built2.engine.run()
    repository2.close_session(session2.id, reason="clean_shutdown")

    # ------------------------------------------------------------ asserts
    # Exactly one cycle row for this (runtime, strategy, mode, expiry) —
    # idx_one_open_cycle/UNIQUE(resolved_expiry_date) would raise at the
    # database level if recovery had instead tried to create a second one.
    cycle_after_restart = repository2.load_cycle(cycle_id=cycle_id)
    assert cycle_after_restart is not None
    assert cycle_after_restart["cycle_id"] == cycle_after_day1["cycle_id"]
    assert cycle_after_restart["state"] == CycleState.ACTIVE.value
    # opened_trading_date is the cycle's own opening date — never rewritten
    # by the restart (spec review correction 4).
    assert cycle_after_restart["opened_trading_date"] == ENTRY_DATE

    legs_after_restart = repository2.load_cycle_legs(cycle_id=cycle_id)
    assert len(legs_after_restart) == 4  # no duplicate/extra leg rows
    assert {leg["leg_id"] for leg in legs_after_restart} == {
        leg["leg_id"] for leg in legs_after_day1
    }
    assert all(leg["state"] == "OPEN" for leg in legs_after_restart)

    positions_after_restart = {
        p.security_id: p for p in repository2.open_positions_for_cycle(cycle_id=cycle_id)
    }
    assert len(positions_after_restart) == 4
    # The *same* position rows (by id) — cycle_position_bindings resolved
    # them, restart did not open new ones.
    for security_id, before in positions_after_day1.items():
        after = positions_after_restart[security_id]
        assert after.quantity == before.quantity
        # The *same* underlying row, not a freshly opened one: a new
        # position would carry a new, freshly minted entry_correlation_id.
        assert after.entry_correlation_id == before.entry_correlation_id
        # trading_date on the position row is still the opening date —
        # never rewritten across the restart, even though the restart
        # itself happened on a different trading_date.
        assert after.trading_date == ENTRY_DATE

    database2.close()


# ======================================================================
# Phase 5A: partial-entry restart boundaries (0/4, 1/4, 2/4, 3/4, 4/4) —
# the cross-evaluation staged-entry resume mechanism
# (PositionalMultiLegEngine._resume_pending_entry/_drive_entry) proven
# safe at every restart point a real crash could land on, mirroring
# test_weekly_delta_neutral_adjustment.py's own nine restart-boundary
# suite's crash-injection style (_SimulatedCrash, a BaseException that
# passes straight through every internal "except Exception" resilience
# layer, paired with a swallowing "supervisor" wrapper).
#
# Ordering across a restart is never proven with order_intents.
# sequence_number here (a per-*session* counter — comparing it across two
# different sessions is the exact false-positive risk already found and
# fixed in Phase 3). Each leg's own durable, timezone-aware entry_time is
# used instead — an absolute timestamp set from the tick's own
# exchange_time regardless of which session performed the fill.
# ======================================================================

CYCLE_ID = f"weekly_delta_neutral:{EXPIRY_DATE}"
_HEDGE_PUT_LEG_ID = f"{CYCLE_ID}:HEDGE_PUT:1"
_HEDGE_CALL_LEG_ID = f"{CYCLE_ID}:HEDGE_CALL:1"
_SHORT_PUT_LEG_ID = f"{CYCLE_ID}:SHORT_PUT:1"
_SHORT_CALL_LEG_ID = f"{CYCLE_ID}:SHORT_CALL:1"


class _SimulatedCrash(BaseException):
    """Stand-in for a hard process kill, injected from inside a persistence
    callback — deliberately *not* an ``Exception`` subclass, so it passes
    straight through every internal ``except Exception`` resilience layer
    the engine already has. Duplicated from
    ``test_weekly_delta_neutral_adjustment.py``'s own helper of the same
    name/reasoning rather than imported — these are sibling acceptance
    modules, not a shared library."""


def _crash_before_cycle_write_when(engine: Any, predicate: Any) -> None:
    """The engine's ``_persist_cycle_cb`` raises :class:`_SimulatedCrash`
    the first time ``predicate(cycle)`` is true, *without* performing the
    real write — simulating a crash before that value ever lands durably."""
    real_cb = engine._persist_cycle_cb
    state = {"raised": False}

    def wrapper(cycle: Any) -> None:
        if not state["raised"] and predicate(cycle):
            state["raised"] = True
            raise _SimulatedCrash()
        real_cb(cycle)

    engine._persist_cycle_cb = wrapper


def _crash_after_cycle_write_when(engine: Any, predicate: Any) -> None:
    """Like :func:`_crash_before_cycle_write_when`, but performs the real,
    durable write first, then raises — simulating a crash immediately after
    that value *did* land durably."""
    real_cb = engine._persist_cycle_cb
    state = {"raised": False}

    def wrapper(cycle: Any) -> None:
        real_cb(cycle)
        if not state["raised"] and predicate(cycle):
            state["raised"] = True
            raise _SimulatedCrash()

    engine._persist_cycle_cb = wrapper


def _crash_after_leg_open(engine: Any, leg_id: str) -> None:
    """Performs the real, durable write for ``leg_id``'s own ``OPEN`` row,
    then raises :class:`_SimulatedCrash` — simulating a crash immediately
    after exactly that one leg's fill became durable, before
    ``_drive_entry``'s loop can proceed to the next role."""
    real_cb = engine._persist_cycle_leg_cb
    state = {"raised": False}

    def wrapper(leg: Any) -> None:
        real_cb(leg)
        if not state["raised"] and leg.leg_id == leg_id and leg.state is LegState.OPEN:
            state["raised"] = True
            raise _SimulatedCrash()

    engine._persist_cycle_leg_cb = wrapper


def _fail_leg_persist_once(engine: Any, leg_id: str, state_value: LegState) -> None:
    """The engine's ``_persist_cycle_leg_cb`` raises once — the first time
    ``leg_id`` is persisted in ``state_value`` — simulating that one leg's
    own best-effort projection write failing right after the real broker-
    side fill it describes already succeeded (spec section 9.4). An
    ordinary ``Exception``, caught by the engine's own best-effort
    ``_persist_leg`` (unlike ``_SimulatedCrash`` above) — the session
    continues to completion."""
    real_cb = engine._persist_cycle_leg_cb
    state = {"raised": False}

    def wrapper(leg: Any) -> None:
        if not state["raised"] and leg.leg_id == leg_id and leg.state is state_value:
            state["raised"] = True
            raise RuntimeError(f"injected leg-persistence failure for {leg_id}")
        real_cb(leg)

    engine._persist_cycle_leg_cb = wrapper


def _run_entry_session(
    tmp_path: Any,
    *,
    db_name: str,
    trading_date: str,
    ticks: list[Tick],
    clock_ts: datetime,
    session_pid: int = 1234,
    before_run: Any = None,
    tolerate_crash: bool = False,
) -> tuple[Database, ExecutionRepository]:
    """One engine session against ``tmp_path/db_name``, mirroring
    ``test_weekly_delta_neutral_adjustment.py``'s own ``_run_session``
    helper (not imported: a different worker-config/chain-fetcher shape,
    already duplicated by this file's own single-session test above)."""
    database = Database(tmp_path / db_name)
    MigrationRunner(database).run_pending()
    repository = ExecutionRepository(database)
    session = repository.open_session(
        runtime_id="positional_options", strategy_id="weekly_delta_neutral",
        execution_mode=ExecutionMode.PAPER, process_role="worker", pid=session_pid,
    )
    built = build_engine(
        _build_worker_config(trading_date), repository=repository, session_id=session.id,
        feed=SimulatedFeed(ticks), chain_fetcher=_chain_fetcher,
        scrip_master=_build_scrip_master(),
        margin_estimator=MarginEstimator(margin_fetcher=_fake_margin_fetcher),
        clock=lambda: clock_ts,
    )
    if before_run is not None:
        before_run(built.engine)
    if tolerate_crash:
        with contextlib.suppress(BaseException):
            built.engine.run()
    else:
        built.engine.run()
    return database, repository


def _entry_ticks(ts: datetime) -> list[Tick]:
    return [
        _leg_tick(_SECURITY_IDS[(HEDGE_PUT_STRIKE, "PE")], 18.0, 20.0, ts),
        _leg_tick(_SECURITY_IDS[(HEDGE_CALL_STRIKE, "CE")], 17.0, 19.0, ts),
        _leg_tick(_SECURITY_IDS[(SHORT_PUT_STRIKE, "PE")], 80.0, 82.0, ts),
        _leg_tick(_SECURITY_IDS[(SHORT_CALL_STRIKE, "CE")], 78.0, 80.0, ts),
        _underlying_tick(ts),
    ]


def _assert_no_naked_short(legs: list[Any]) -> None:
    """Structural consequence of ``ENTRY_ROLE_ORDER`` (hedges strictly
    before either short) made an explicit regression test: whenever a
    short is durably ``OPEN``, its own protective hedge must be too."""
    states = {leg["leg_role"]: leg["state"] for leg in legs}
    if states.get("SHORT_PUT") == "OPEN":
        assert states.get("HEDGE_PUT") == "OPEN", "short put open with no hedge put"
    if states.get("SHORT_CALL") == "OPEN":
        assert states.get("HEDGE_CALL") == "OPEN", "short call open with no hedge call"


def _assert_active_four_leg_cycle(repository: ExecutionRepository) -> dict[str, Any]:
    cycle = repository.load_cycle(cycle_id=CYCLE_ID)
    assert cycle is not None
    assert cycle["state"] == CycleState.ACTIVE.value
    legs = {leg["leg_role"]: leg for leg in repository.load_cycle_legs(cycle_id=CYCLE_ID)}
    assert len(legs) == 4
    assert all(leg["state"] == "OPEN" for leg in legs.values())
    return legs


# --------------------------------------------------------------------- 0/4
def test_restart_resumes_0_of_4_partial_entry_safely(tmp_path: Any) -> None:
    """Cycle identity and every original leg intent are durably persisted
    (spec 5.1's pre-effect checkpoint), but the very first hedge's own
    staged-entry deadline was never armed — a crash landing in the gap
    between ``_enter_cycle``'s own pre-effect persist and
    ``_advance_entry_stage``'s first call."""
    entry_ts = _entry_ts(9, 26, 0)
    db_name = "restart_0_of_4.db"

    def _before_run(engine: Any) -> None:
        _crash_before_cycle_write_when(
            engine, lambda cycle: cycle.entry_stage_role is LegRole.HEDGE_PUT
        )

    database, repository = _run_entry_session(
        tmp_path, db_name=db_name, trading_date=ENTRY_DATE, ticks=[_underlying_tick(entry_ts)],
        clock_ts=entry_ts, before_run=_before_run, tolerate_crash=True,
    )
    cycle_after_crash = repository.load_cycle(cycle_id=CYCLE_ID)
    assert cycle_after_crash is not None
    assert cycle_after_crash["state"] == CycleState.ENTERING.value
    legs_after_crash = repository.load_cycle_legs(cycle_id=CYCLE_ID)
    assert len(legs_after_crash) == 4
    assert all(leg["state"] == "PENDING_ORDER" for leg in legs_after_crash)
    stage_after_crash = repository.load_cycle_entry_stage(cycle_id=CYCLE_ID)
    if stage_after_crash is not None:
        assert stage_after_crash["entry_stage_role"] is None
    _assert_no_naked_short(legs_after_crash)
    database.close()

    restart_ts = _restart_ts(10, 0, 0)
    database2, repository2 = _run_entry_session(
        tmp_path, db_name=db_name, trading_date=RESTART_DATE, ticks=_entry_ticks(restart_ts),
        clock_ts=restart_ts, session_pid=2222,
    )
    try:
        legs2 = _assert_active_four_leg_cycle(repository2)
        _assert_no_naked_short(list(legs2.values()))
        # No leg was ever re-submitted twice: exactly one entry-side
        # order_intents row per leg.
        history2 = repository2.cycle_order_history(cycle_id=CYCLE_ID)
        for leg in legs2.values():
            entry_rows = [
                r for r in history2 if r["leg_id"] == leg["leg_id"] and r["side"] == leg["side"]
            ]
            assert len(entry_rows) == 1
    finally:
        database2.close()


# ------------------------------------------------------------ 1/4, 2/4, 3/4
def _crash_after_n_legs(
    tmp_path: Any, *, db_name: str, crash_leg_id: str
) -> dict[str, Any]:
    """First session: crash right after ``crash_leg_id``'s own ``OPEN`` row
    lands durably, cutting ``_drive_entry``'s loop off before the next role
    is even attempted. Returns the crash session's own durable leg rows
    (keyed by role) — the database is closed before returning, exactly like
    this file's own single-session test above, so a second session can
    safely reopen the same file."""
    entry_ts = _entry_ts(9, 26, 0)

    def _before_run(engine: Any) -> None:
        _crash_after_leg_open(engine, crash_leg_id)

    database, repository = _run_entry_session(
        tmp_path, db_name=db_name, trading_date=ENTRY_DATE, ticks=_entry_ticks(entry_ts),
        clock_ts=entry_ts, before_run=_before_run, tolerate_crash=True,
    )
    legs_after_crash = {
        leg["leg_role"]: leg for leg in repository.load_cycle_legs(cycle_id=CYCLE_ID)
    }
    database.close()
    return legs_after_crash


def _restart(tmp_path: Any, db_name: str) -> tuple[Database, ExecutionRepository]:
    restart_ts = _restart_ts(10, 0, 0)
    return _run_entry_session(
        tmp_path, db_name=db_name, trading_date=RESTART_DATE, ticks=_entry_ticks(restart_ts),
        clock_ts=restart_ts, session_pid=2222,
    )


def test_restart_resumes_1_of_4_partial_entry_safely(tmp_path: Any) -> None:
    db_name = "restart_1_of_4.db"
    legs_after_crash = _crash_after_n_legs(
        tmp_path, db_name=db_name, crash_leg_id=_HEDGE_PUT_LEG_ID
    )
    assert legs_after_crash["HEDGE_PUT"]["state"] == "OPEN"
    for role in ("HEDGE_CALL", "SHORT_PUT", "SHORT_CALL"):
        assert legs_after_crash[role]["state"] == "PENDING_ORDER"
    _assert_no_naked_short(list(legs_after_crash.values()))
    hedge_put_before = legs_after_crash["HEDGE_PUT"]

    database2, repository2 = _restart(tmp_path, db_name)
    try:
        legs2 = _assert_active_four_leg_cycle(repository2)
        assert legs2["HEDGE_PUT"]["entry_price"] == hedge_put_before["entry_price"]
        assert (
            legs2["HEDGE_PUT"]["entry_correlation_id"]
            == hedge_put_before["entry_correlation_id"]
        )
        _assert_no_naked_short(list(legs2.values()))
    finally:
        database2.close()


def test_restart_resumes_2_of_4_partial_entry_safely(tmp_path: Any) -> None:
    db_name = "restart_2_of_4.db"
    legs_after_crash = _crash_after_n_legs(
        tmp_path, db_name=db_name, crash_leg_id=_HEDGE_CALL_LEG_ID
    )
    assert legs_after_crash["HEDGE_PUT"]["state"] == "OPEN"
    assert legs_after_crash["HEDGE_CALL"]["state"] == "OPEN"
    assert legs_after_crash["SHORT_PUT"]["state"] == "PENDING_ORDER"
    assert legs_after_crash["SHORT_CALL"]["state"] == "PENDING_ORDER"
    _assert_no_naked_short(list(legs_after_crash.values()))

    database2, repository2 = _restart(tmp_path, db_name)
    try:
        legs2 = _assert_active_four_leg_cycle(repository2)
        for role in ("HEDGE_PUT", "HEDGE_CALL"):
            assert (
                legs2[role]["entry_correlation_id"]
                == legs_after_crash[role]["entry_correlation_id"]
            )
        _assert_no_naked_short(list(legs2.values()))
    finally:
        database2.close()


def test_restart_resumes_3_of_4_partial_entry_safely(tmp_path: Any) -> None:
    db_name = "restart_3_of_4.db"
    legs_after_crash = _crash_after_n_legs(
        tmp_path, db_name=db_name, crash_leg_id=_SHORT_PUT_LEG_ID
    )
    for role in ("HEDGE_PUT", "HEDGE_CALL", "SHORT_PUT"):
        assert legs_after_crash[role]["state"] == "OPEN"
    assert legs_after_crash["SHORT_CALL"]["state"] == "PENDING_ORDER"
    # The naked-short-never-exposed proof matters most exactly here: a
    # short (SHORT_PUT) is genuinely open while the entry is still
    # incomplete.
    _assert_no_naked_short(list(legs_after_crash.values()))

    database2, repository2 = _restart(tmp_path, db_name)
    try:
        legs2 = _assert_active_four_leg_cycle(repository2)
        for role in ("HEDGE_PUT", "HEDGE_CALL", "SHORT_PUT"):
            assert (
                legs2[role]["entry_correlation_id"]
                == legs_after_crash[role]["entry_correlation_id"]
            )
        _assert_no_naked_short(list(legs2.values()))
    finally:
        database2.close()


# ------------------------------------------------------------------- 4/4
def test_restart_resumes_4_of_4_partial_entry_without_duplicate_orders(tmp_path: Any) -> None:
    """All four legs genuinely filled (broker-authoritative, durable), but
    the cycle's own ``ACTIVE`` projection was never persisted — a crash
    landing inside ``_finalize_entry``'s own write. Restart must
    reconstruct/finalize ``ACTIVE`` from the already-open legs alone,
    without placing a single new order."""
    entry_ts = _entry_ts(9, 26, 0)
    db_name = "restart_4_of_4.db"

    def _before_run(engine: Any) -> None:
        _crash_before_cycle_write_when(engine, lambda cycle: cycle.state is CycleState.ACTIVE)

    database, repository = _run_entry_session(
        tmp_path, db_name=db_name, trading_date=ENTRY_DATE, ticks=_entry_ticks(entry_ts),
        clock_ts=entry_ts, before_run=_before_run, tolerate_crash=True,
    )
    cycle_after_crash = repository.load_cycle(cycle_id=CYCLE_ID)
    assert cycle_after_crash is not None
    assert cycle_after_crash["state"] == CycleState.ENTERING.value, (
        "every leg is genuinely OPEN, but the ACTIVE projection never landed"
    )
    legs_after_crash = {
        leg["leg_role"]: leg for leg in repository.load_cycle_legs(cycle_id=CYCLE_ID)
    }
    assert all(leg["state"] == "OPEN" for leg in legs_after_crash.values())
    history_after_crash = repository.cycle_order_history(cycle_id=CYCLE_ID)
    database.close()

    restart_ts = _restart_ts(10, 0, 0)
    # No leg ticks needed at all — every leg is already OPEN; only an
    # underlying tick is required to drive one more evaluation.
    database2, repository2 = _run_entry_session(
        tmp_path, db_name=db_name, trading_date=RESTART_DATE, ticks=[_underlying_tick(restart_ts)],
        clock_ts=restart_ts, session_pid=2222,
    )
    try:
        legs2 = _assert_active_four_leg_cycle(repository2)
        history2 = repository2.cycle_order_history(cycle_id=CYCLE_ID)
        # No leg gained a second entry-side order_intents row across the
        # restart — the finalize-and-persist that ACTIVE needed was a pure
        # reconstruction, never a new order.
        for leg in legs2.values():
            entry_rows_before = [
                r for r in history_after_crash
                if r["leg_id"] == leg["leg_id"] and r["side"] == leg["side"]
            ]
            entry_rows_after = [
                r for r in history2 if r["leg_id"] == leg["leg_id"] and r["side"] == leg["side"]
            ]
            assert len(entry_rows_before) == 1
            assert len(entry_rows_after) == 1
    finally:
        database2.close()


# ------------------------------------------------ unknown submission (entry)
def test_restart_reconciles_an_unknown_entry_submission_before_retrying(tmp_path: Any) -> None:
    """Mirrors ``test_weekly_delta_neutral_adjustment.py``'s own
    ``test_restart_boundary_8_replacement_filled_leg_projection_missing_
    reconciles``, for an *entry* leg instead of a replacement: the real
    broker-side open fully succeeds, but HEDGE_PUT's own best-effort
    projection persist fails (swallowed) — leaving its durable row stale
    ``PENDING_ORDER`` even though it is truly open. Restart reconciliation's
    existing ``PENDING_ORDER -> OPEN`` promotion from the authoritative
    fill must resolve it — no incident, no operator, and never a second
    order — *before* ``_drive_entry`` ever gets a chance to re-attempt it
    (by the time reconciliation runs, the cycle is already ``ACTIVE``, so
    ``_resume_pending_entry`` never even fires for it)."""
    entry_ts = _entry_ts(9, 26, 0)
    db_name = "restart_unknown_entry_submission.db"

    def _before_run(engine: Any) -> None:
        _fail_leg_persist_once(engine, _HEDGE_PUT_LEG_ID, LegState.OPEN)

    database, repository = _run_entry_session(
        tmp_path, db_name=db_name, trading_date=ENTRY_DATE, ticks=_entry_ticks(entry_ts),
        clock_ts=entry_ts, before_run=_before_run,
    )
    cycle_after_day1 = repository.load_cycle(cycle_id=CYCLE_ID)
    assert cycle_after_day1 is not None
    assert cycle_after_day1["state"] == CycleState.ACTIVE.value, (
        "the in-memory entry still completed — only HEDGE_PUT's own durable "
        "projection is stale"
    )
    legs_after_day1 = {
        leg["leg_role"]: leg for leg in repository.load_cycle_legs(cycle_id=CYCLE_ID)
    }
    assert legs_after_day1["HEDGE_PUT"]["state"] == "PENDING_ORDER", (
        "stale — the real fill never landed here"
    )
    history_after_day1 = repository.cycle_order_history(cycle_id=CYCLE_ID)
    hedge_put_entry_rows = [
        r for r in history_after_day1
        if r["leg_id"] == _HEDGE_PUT_LEG_ID and r["side"] == legs_after_day1["HEDGE_PUT"]["side"]
    ]
    assert len(hedge_put_entry_rows) == 1, "the real order was placed exactly once"
    database.close()

    restart_ts = _restart_ts(10, 0, 0)
    database2, repository2 = _run_entry_session(
        tmp_path, db_name=db_name, trading_date=RESTART_DATE, ticks=[_underlying_tick(restart_ts)],
        clock_ts=restart_ts, session_pid=2222,
    )
    try:
        legs2 = _assert_active_four_leg_cycle(repository2)
        assert legs2["HEDGE_PUT"]["state"] == "OPEN", "reconstructed from the authoritative fill"
        history2 = repository2.cycle_order_history(cycle_id=CYCLE_ID)
        hedge_put_entry_rows2 = [
            r for r in history2
            if r["leg_id"] == _HEDGE_PUT_LEG_ID and r["side"] == legs2["HEDGE_PUT"]["side"]
        ]
        assert len(hedge_put_entry_rows2) == 1, "never a second, duplicate submission"
    finally:
        database2.close()
