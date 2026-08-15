"""Overnight restart: a cycle entered by one process is adopted by a second
one, against the *same* database file, with no re-entry and no duplicate
row anywhere — the whole point of the durable ``cycle_id`` +
``cycle_position_bindings`` design (spec section 9.2/9.5, D69/limitation
30). Reuses the exact same production ``build_engine`` wiring as
``test_weekly_delta_neutral_entry.py``.

No live order API is constructed or called anywhere in this module.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from common.config.models import ExecutionMode
from common.engine.config import SessionConfig
from common.engine.feed import SimulatedFeed
from common.engine.positional.positional_models import CycleState
from common.execution import ExecutionRepository
from common.market_data.scrip_master import ScripMaster
from common.models import Tick
from common.persistence import Database, MigrationRunner
from runtimes.positional_options.config_adapter import WorkerConfig
from runtimes.positional_options.worker import build_engine

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
        scrip_master=_build_scrip_master(), clock=lambda: entry_ts,
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
        scrip_master=_build_scrip_master(), clock=lambda: restart_ts,
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
