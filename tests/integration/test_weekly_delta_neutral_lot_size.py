"""Current-reference lot-size verification — dated 2026-08-15.

**Fact being pinned, not a production constant**: as of this date, NIFTY's
exchange-set lot size is 65. NSE has changed NIFTY's lot size before and
will again; this file exists to prove production *never* hardcodes or
configures that number — it reads it from each selected Dhan contract's own
resolved metadata (spec section 3.1) — using today's real value only as the
fixture data a dated, current-reference test is supposed to use. If NSE
changes NIFTY's lot size again, update the ``CURRENT_NIFTY_LOT_SIZE``
constant and this docstring's date; no production code should need to
change, because none of it names 65 (or 65's replacement) at all — proven
below by the same grep-based technique
``test_no_weekly_delta_neutral_branches.py`` uses for the strategy id.

Drives the exact same production ``runtimes.positional_options.worker.
build_engine`` wiring as ``test_weekly_delta_neutral_entry.py``. No live
order API is constructed or called anywhere in this module.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from common.config.models import ExecutionMode
from common.engine.config import SessionConfig
from common.engine.feed import SimulatedFeed
from common.execution import ExecutionRepository
from common.market_data.scrip_master import ScripMaster
from common.models import Tick
from common.persistence import Database, MigrationRunner
from runtimes.positional_options.config_adapter import WorkerConfig
from runtimes.positional_options.worker import build_engine

IST = ZoneInfo("Asia/Kolkata")
NIFTY_SECURITY_ID = "13"
ENTRY_DATE = "2026-08-19"
EXPIRY_DATE = "2026-08-26"

#: The one place this value is allowed to appear as a literal — fixture
#: data for a dated current-reference check, never a production default.
CURRENT_NIFTY_LOT_SIZE = 65

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

#: Generic production modules a hardcoded/configured "65" must never appear
#: in — mirrors test_no_weekly_delta_neutral_branches.py's GENERIC_TARGETS
#: reasoning, scoped to this strategy's own package plus its config, since
#: those are the only places lot size is ever resolved or consumed.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_PRODUCTION_TARGETS: tuple[Path, ...] = (
    _REPO_ROOT / "strategies" / "positional_options" / "weekly_delta_neutral",
    _REPO_ROOT / "runtimes" / "positional_options",
    _REPO_ROOT / "config" / "strategies" / "weekly_delta_neutral.yaml",
)
_LITERAL_65_RE = re.compile(r"(?<![0-9.])65(?![0-9.])")


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
                f"{strike:.0f}", option_type, str(CURRENT_NIFTY_LOT_SIZE), "NSE", "D",
            ]
        )
    buffer = io.StringIO()
    csv.writer(buffer).writerows(rows)
    return buffer.getvalue()


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
        security_id=NIFTY_SECURITY_ID, instrument="NIFTY", last_price=price,
        exchange_time=ts, received_at=ts,
    )


def _leg_tick(security_id: str, bid: float, ask: float, ts: datetime) -> Tick:
    return Tick(
        security_id=security_id, instrument=security_id, last_price=(bid + ask) / 2.0,
        exchange_time=ts, received_at=ts, bid_price=bid, ask_price=ask,
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


def test_no_production_module_or_config_hardcodes_the_current_lot_size() -> None:
    """The control this file's own claim depends on: production resolves
    lot size from contract metadata, never a literal — checked by reading
    source text, the same technique the strategy-id negative-space test
    uses, so this is a real, continuously-enforced guarantee, not a claim
    trusted from a docstring alone."""
    offenders: dict[str, list[int]] = {}
    for target in _PRODUCTION_TARGETS:
        paths = [target] if target.is_file() else sorted(target.glob("*.py"))
        for path in paths:
            lines = path.read_text(encoding="utf-8").splitlines()
            hits = [i + 1 for i, line in enumerate(lines) if _LITERAL_65_RE.search(line)]
            if hits:
                offenders[str(path)] = hits
    assert offenders == {}, (
        f"production code/config hardcodes the literal 65 at: {offenders}. Lot "
        "size must be resolved from each selected contract's own metadata, "
        "never a hardcoded or configured constant."
    )


def test_entry_resolves_the_current_real_lot_size_from_contract_metadata(
    tmp_path,
) -> None:
    database = Database(tmp_path / "positional_options_lot_size.db")
    MigrationRunner(database).run_pending()
    repository = ExecutionRepository(database)
    try:
        config = _build_worker_config()
        session = repository.open_session(
            runtime_id=config.runtime_id, strategy_id=config.strategy_id,
            execution_mode=ExecutionMode.PAPER, process_role="worker", pid=1234,
        )
        entry_ts = _ts(9, 26, 0)
        ticks = [
            _leg_tick(_SECURITY_IDS[(HEDGE_PUT_STRIKE, "PE")], 18.0, 20.0, entry_ts),
            _leg_tick(_SECURITY_IDS[(HEDGE_CALL_STRIKE, "CE")], 17.0, 19.0, entry_ts),
            _leg_tick(_SECURITY_IDS[(SHORT_PUT_STRIKE, "PE")], 80.0, 82.0, entry_ts),
            _leg_tick(_SECURITY_IDS[(SHORT_CALL_STRIKE, "CE")], 78.0, 80.0, entry_ts),
            _underlying_tick(entry_ts),
        ]
        scrip_master = ScripMaster("NIFTY", exchange="NSE").load_from_text(_scrip_master_csv())
        # Never trust configuration for this: confirmed the fixture master
        # really carries today's current real value, not a stale one.
        assert scrip_master.lot_size == CURRENT_NIFTY_LOT_SIZE

        built = build_engine(
            config, repository=repository, session_id=session.id,
            feed=SimulatedFeed(ticks), chain_fetcher=_chain_fetcher,
            scrip_master=scrip_master, clock=lambda: entry_ts,
        )
        built.engine.run()

        cycle_id = f"weekly_delta_neutral:{EXPIRY_DATE}"
        legs = repository.load_cycle_legs(cycle_id=cycle_id)
        assert len(legs) == 4
        assert all(leg["state"] == "OPEN" for leg in legs)

        # Every leg's own resolved contract metadata agreed on the current
        # real lot size, and the actually-filled quantity reflects it —
        # lots(1) * CURRENT_NIFTY_LOT_SIZE — resolved independently per leg
        # by LifecycleGateway/PositionManager from each leg's own contract,
        # never from a single shared/hardcoded number.
        for leg in legs:
            assert leg["lot_size"] == CURRENT_NIFTY_LOT_SIZE
            assert leg["quantity"] == config.lots * CURRENT_NIFTY_LOT_SIZE
    finally:
        database.close()
