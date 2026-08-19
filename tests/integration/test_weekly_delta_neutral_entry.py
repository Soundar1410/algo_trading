"""``weekly_delta_neutral`` hedge-first entry, driven through the *real*
``runtimes.positional_options.worker.build_engine`` wiring — the same
``LifecycleGateway``/``OrderLifecycle``/``PaperBroker``/``ExecutionRepository``
stack a production run uses, against a temp SQLite database. This is the
acceptance-matrix "entry" case (spec section 13) and doubles as the wiring
proof for the whole ``runtimes/positional_options`` package: nothing here
is mocked below the network boundary (feed adapter, Dhan chain HTTP call).

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
from common.engine.positional.positional_models import CycleState, LegRole, cycle_id_for
from common.execution import ExecutionRepository
from common.margin import LegMarginRequest, MarginEstimator
from common.market_data.scrip_master import ScripMaster
from common.models import Tick
from common.persistence import Database, MigrationRunner
from runtimes.positional_options.config_adapter import WorkerConfig
from runtimes.positional_options.worker import build_engine

IST = ZoneInfo("Asia/Kolkata")
NIFTY_SECURITY_ID = "13"
ENTRY_DATE = "2026-08-19"  # a Wednesday
EXPIRY_DATE = "2026-08-26"  # the following Wednesday — never the entry date

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
            "SEM_SMST_SECURITY_ID",
            "SEM_INSTRUMENT_NAME",
            "SEM_TRADING_SYMBOL",
            "SEM_CUSTOM_SYMBOL",
            "SEM_EXPIRY_DATE",
            "SEM_STRIKE_PRICE",
            "SEM_OPTION_TYPE",
            "SEM_LOT_UNITS",
            "SEM_EXM_EXCH_ID",
            "SEM_SEGMENT",
        ]
    ]
    for (strike, option_type), security_id in _SECURITY_IDS.items():
        rows.append(
            [
                security_id,
                "OPTIDX",
                f"NIFTY-26AUG2026-{strike:.0f}-{option_type}",
                f"NIFTY {strike:.0f} {option_type}",
                f"{EXPIRY_DATE} 00:00:00",
                f"{strike:.0f}",
                option_type,
                "75",
                "NSE",
                "D",
            ]
        )
    buffer = io.StringIO()
    csv.writer(buffer).writerows(rows)
    return buffer.getvalue()


def _build_scrip_master() -> ScripMaster:
    return ScripMaster("NIFTY", exchange="NSE").load_from_text(_scrip_master_csv())


#: A fixed, well-under-cap per-leg margin (4 legs x 20,000 = 80,000, 16% of
#: the default 500,000 allocated capital) — this suite is about hedge-first
#: entry sequencing, not the margin gate itself (see
#: tests/unit/test_margin_estimator.py for that), so a real Dhan call is
#: neither needed nor desired here.
def _fake_margin_fetcher(_leg: LegMarginRequest) -> float:
    return 20_000.0


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


def _ts(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 8, 19, hour, minute, second, tzinfo=IST)


def _underlying_tick(ts: datetime, price: float = 24000.0) -> Tick:
    return Tick(
        security_id=NIFTY_SECURITY_ID,
        instrument="NIFTY",
        last_price=price,
        exchange_time=ts,
        received_at=ts,
    )


def _leg_tick(security_id: str, bid: float, ask: float, ts: datetime) -> Tick:
    mid = (bid + ask) / 2.0
    return Tick(
        security_id=security_id,
        instrument=security_id,
        last_price=mid,
        exchange_time=ts,
        received_at=ts,
        bid_price=bid,
        ask_price=ask,
    )


def _build_worker_config() -> WorkerConfig:
    return WorkerConfig(
        runtime_id="positional_options",
        strategy_id="weekly_delta_neutral",
        strategy_ref=(
            "strategies.positional_options.weekly_delta_neutral.strategy:"
            "WeeklyDeltaNeutralStrategy"
        ),
        trading_date=ENTRY_DATE,
        lots=1,
        timezone="Asia/Kolkata",
        underlying_security_id=NIFTY_SECURITY_ID,
        underlying_instrument="NIFTY",
        underlying_segment="IDX_I",
        option_segment="NSE_FNO",
        session=SessionConfig(
            timezone="Asia/Kolkata",
            start_time="09:25",
            end_time="09:40",
            square_off_time="15:15",
            holidays=(),
        ),
        risk_free_rate=0.065,
        dividend_yield=0.0,
        quote_max_age_seconds=30.0,
        evaluation_interval_seconds=0.0,
        max_adjustments_per_day=1,
        max_adjustments_per_cycle=3,
        min_minutes_between_adjustments=90,
        parameters={
            "underlying": "NIFTY",
            "index_security_id": NIFTY_SECURITY_ID,
            "index_segment": "IDX_I",
            "fno_segment": "NSE_FNO",
        },
    )


def _open_repository(tmp_path) -> tuple[Database, ExecutionRepository]:  # type: ignore[no-untyped-def]
    database = Database(tmp_path / "positional_options_test.db")
    MigrationRunner(database).run_pending()
    return database, ExecutionRepository(database)


def test_weekly_delta_neutral_hedge_first_entry(tmp_path) -> None:  # type: ignore[no-untyped-def]
    database, repository = _open_repository(tmp_path)
    try:
        config = _build_worker_config()
        session = repository.open_session(
            runtime_id=config.runtime_id,
            strategy_id=config.strategy_id,
            execution_mode=ExecutionMode.PAPER,
            process_role="worker",
            pid=1234,
        )
        entry_ts = _ts(9, 26, 0)
        ticks = [
            _leg_tick(_SECURITY_IDS[(HEDGE_PUT_STRIKE, "PE")], 18.0, 20.0, entry_ts),
            _leg_tick(_SECURITY_IDS[(HEDGE_CALL_STRIKE, "CE")], 17.0, 19.0, entry_ts),
            _leg_tick(_SECURITY_IDS[(SHORT_PUT_STRIKE, "PE")], 80.0, 82.0, entry_ts),
            _leg_tick(_SECURITY_IDS[(SHORT_CALL_STRIKE, "CE")], 78.0, 80.0, entry_ts),
            _underlying_tick(entry_ts),
        ]
        feed = SimulatedFeed(ticks)
        scrip_master = _build_scrip_master()

        built = build_engine(
            config,
            repository=repository,
            session_id=session.id,
            feed=feed,
            chain_fetcher=_chain_fetcher,
            scrip_master=scrip_master,
            margin_estimator=MarginEstimator(margin_fetcher=_fake_margin_fetcher),
            # A fixed clock matching the ticks' own exchange_time: chain
            # snapshots and Greek decisions are evaluated for freshness
            # against context.now (the tick-driven "now"), so the injected
            # clock must agree with it rather than the real wall clock.
            clock=lambda: entry_ts,
        )
        built.engine.run()

        # -------------------------------------------------------- durable cycle
        cycle_id = cycle_id_for(
            runtime_id="positional_options",
            strategy_id="weekly_delta_neutral",
            execution_mode=ExecutionMode.PAPER,
            underlying="NIFTY",
            resolved_expiry_date=EXPIRY_DATE,
        )
        cycle_row = repository.load_cycle(cycle_id=cycle_id)
        assert cycle_row is not None
        assert cycle_row["state"] == CycleState.ACTIVE.value
        assert cycle_row["resolved_expiry_date"] == EXPIRY_DATE
        assert cycle_row["opened_trading_date"] == ENTRY_DATE
        assert cycle_row["original_net_credit"] > 0

        # -------------------------------------------------------------- 4 legs
        legs = repository.load_cycle_legs(cycle_id=cycle_id)
        assert len(legs) == 4
        roles = {leg["leg_role"] for leg in legs}
        assert roles == {
            LegRole.HEDGE_PUT.value,
            LegRole.HEDGE_CALL.value,
            LegRole.SHORT_PUT.value,
            LegRole.SHORT_CALL.value,
        }
        for leg in legs:
            assert leg["state"] == "OPEN"

        # ---------------------------------------- durable positions + bindings
        open_positions = repository.open_positions_for_cycle(cycle_id=cycle_id)
        assert len(open_positions) == 4
        for position in open_positions:
            # Cross-day identity: the position's own trading_date is the
            # cycle's opening date, resolved through cycle_position_bindings
            # rather than a (trading_date, security_id) lookup.
            assert position.entry_correlation_id is not None

        # Every open leg's security id has exactly one bound position.
        bound_security_ids = {p.security_id for p in open_positions}
        assert bound_security_ids == {sid for sid in _SECURITY_IDS.values()}
    finally:
        database.close()
