"""Shared Phase 3 harness for ``supertrend_buy_1_1p2``: a real engine over a real
repository, on a temporary database.

Nothing here fabricates a persisted row. Positions, order intents, the contract
record a restart needs, realised P&L and the exit-state snapshot are all written by
production code — ``LifecycleGateway``/``OrderLifecycle``/``ExecutionRepository`` and
``common.engine.state_payload.merge_payload`` — and read back by the production
recovery readers in ``runtimes.intraday_options.engine_worker``
(``recover_position``, ``recover_exit_state``, ``recover_daily_risk``). A "restart"
here is two sequential engines over one database, exactly as
``tests/integration/test_engine_worker_restart.py`` models it.

Why the engine is assembled here rather than by calling ``run_worker``: this strategy
is ``continuity_required``, so a worker-level run would demand
``warmup_source: dhan`` and construct a real ``DhanHistoricalDataClient``. These
tests must make no network call of any kind, so the warm-up manager is injected with
a local fetch function instead. Everything downstream of that — broker, lifecycle,
gateway, position manager, persistence, recovery — is the production wiring copied
from ``runtimes.intraday_options.engine_worker._build``.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from common.broker.factory import build_broker
from common.broker.paper import QuoteBook
from common.config.models import ExecutionMode
from common.engine.config import EngineConfig, SessionConfig
from common.engine.engine import TradingEngine
from common.engine.feed import SimulatedFeed
from common.engine.gateway import LifecycleGateway
from common.engine.models import AdoptedPosition
from common.engine.positions import PositionManager
from common.engine.selection import OptionSelector, SimulatedOptionChainResolver
from common.engine.session import MarketSession
from common.engine.state_payload import EXIT_STATE_KEY, merge_payload, read_payload
from common.execution import ExecutionRepository, OrderLifecycle
from common.models import Candle, Tick
from common.persistence import Database
from common.persistence.migrations import MigrationRunner
from common.risk import SquareOffPolicy
from common.warmup.manager import WarmupManager
from common.warmup.session_buckets import session_bucket_starts
from common.warmup.source import WarmupSource
from runtimes.intraday_options.engine_worker import (
    recover_daily_risk,
    recover_exit_state,
    recover_position,
)
from runtimes.intraday_options.worker import (
    EngineWorkerConfig,
    WorkerConfig,
    resolved_config_from_worker,
)
from strategies.intraday_options.supertrend_buy_1_1p2.strategy import SupertrendBuy1x1p2Strategy

IST = ZoneInfo("Asia/Kolkata")
RUNTIME_ID = "intraday_options"
STRATEGY_ID = "supertrend_buy_1_1p2"
UNDERLYING = "INDEX"
LOT_SIZE = 75
LOTS = 10
TIMEFRAME_MINUTES = 5

#: Thursday 2026-08-20 — an ordinary trading day. Its previous two sessions are
#: Wednesday 08-19 and Tuesday 08-18.
TRADING_DAY = date(2026, 8, 20)
TRADING_DATE = TRADING_DAY.isoformat()
NEXT_TRADING_DAY = date(2026, 8, 21)  # Friday

WARMUP_SOURCE = WarmupSource(
    security_id="13", exchange_segment="IDX_I", instrument_type="INDEX"
)

#: A fill model with no slippage and no LTP-fallback padding, so an entry fills at
#: exactly the tick price. Used **only** where an exact rupee boundary is the thing
#: under test (the -Rs 30,000 daily cap): with the normal one-tick slippage the fill
#: is 100.10 and no achievable exit price makes the P&L land on exactly -30,000 in
#: IEEE-754. The committed configuration keeps the real model, and every other test
#: here uses it.
EXACT_FILL_PAPER_EXECUTION: dict[str, Any] = {
    "slippage": {"options": {"mode": "ticks", "market_order_ticks": 0}},
    "submission_latency_ms": 0,
    "tick_size": 0.05,
    "allow_ltp_fallback": True,
    "ltp_fallback_extra_ticks": 0,
}

#: The flat warm-up level. With ``period=1`` the ATR is the bar's own range, so a
#: flat series pins both bands at this value and produces no flips at all — every
#: flip a test observes therefore belongs to its live tape.
WARMUP_LEVEL = 20000.0


def dt(h: int, m: int, s: int = 0, *, day: date = TRADING_DAY) -> datetime:
    return datetime(day.year, day.month, day.day, h, m, s, tzinfo=IST)


def tick(security_id: str, price: float, ts: datetime) -> Tick:
    return Tick(
        security_id=security_id,
        instrument=security_id,
        last_price=price,
        exchange_time=ts,
        received_at=ts,
    )


def underlying_ticks(closes: Sequence[float], *, start: datetime) -> list[Tick]:
    """One tick per 5-minute bucket, plus a trailing tick to close the last one.

    Bar ``i``'s signal is produced when tick ``i + 1`` is delivered — which is also
    the tick whose price becomes ``self._spot``, and therefore the strike.
    """
    ticks = [
        tick(UNDERLYING, price, start + timedelta(minutes=5 * i + 1))
        for i, price in enumerate(closes)
    ]
    ticks.append(tick(UNDERLYING, closes[-1], start + timedelta(minutes=5 * len(closes) + 1)))
    return ticks


def contract_id(spot: float, option_type: str) -> str:
    """The contract the engine resolves for a signal taken at ``spot``.

    Mirrors the production rule rather than restating a constant: strike =
    ``round(spot / 50) * 50`` (``nearest_atm_strike``, banker's rounding included),
    and the simulated resolver names contracts ``SIM:NIFTY:WEEKLY:<strike>:<CE|PE>``.
    """
    return f"SIM:NIFTY:WEEKLY:{int(round(spot / 50) * 50)}:{option_type}"


def session_config(*, start="09:15", end="15:15", square_off="15:20") -> SessionConfig:
    return SessionConfig(
        timezone="Asia/Kolkata",
        start_time=start,
        end_time=end,
        square_off_time=square_off,
    )


def warmup_candles(
    session: MarketSession,
    *,
    level: float = WARMUP_LEVEL,
    days: tuple[date, date] = (date(2026, 8, 18), date(2026, 8, 19)),
) -> list[Candle]:
    """The 75 completed buckets ending the second day's 15:15, all flat at ``level``.

    75 is the strategy's trust floor and a session contributes 73 completed buckets,
    so this necessarily spans both days — the previous session's 73 plus the last two
    of the one before it.
    """
    starts: list[datetime] = []
    for day in days:
        starts.extend(session_bucket_starts(session, day, TIMEFRAME_MINUTES))
    starts = starts[-75:]
    assert len(starts) == 75
    return [
        Candle(
            security_id="13",
            instrument="NIFTY",
            open=level,
            high=level,
            low=level,
            close=level,
            volume=0,
            start_at=s,
            end_at=s + timedelta(minutes=TIMEFRAME_MINUTES),
        )
        for s in starts
    ]


def warmup_series(
    session: MarketSession,
    *,
    today_closes: Sequence[float],
    now: datetime,
    prior_level: float = WARMUP_LEVEL,
    count: int = 75,
) -> list[Candle]:
    """A mid-session warm-up set: the ``count`` most recent completed buckets ending
    at the latest completed bucket before ``now``.

    Today's included buckets carry ``today_closes`` (oldest first); everything earlier
    is flat at ``prior_level``. This is what a genuine mid-session restart replays —
    the previous session(s) *plus* the part of today the dead process already saw —
    and it is why a restart's warm-up cannot be satisfied by prior sessions alone.
    """
    from common.candles.aggregator import floor_to_interval

    current_bucket = floor_to_interval(now, TIMEFRAME_MINUTES * 60)
    starts: list[datetime] = []
    for day in (date(2026, 8, 18), date(2026, 8, 19), TRADING_DAY):
        starts.extend(
            b
            for b in session_bucket_starts(session, day, TIMEFRAME_MINUTES)
            if b < current_bucket
        )
    starts = starts[-count:]
    assert len(starts) == count
    today_start = dt(0, 0)
    today_included = [b for b in starts if b >= today_start]
    assert len(today_included) == len(today_closes), (
        f"{len(today_included)} of today's buckets are included but "
        f"{len(today_closes)} closes were supplied"
    )
    closes = [prior_level] * (count - len(today_closes)) + list(today_closes)
    return [
        Candle(
            security_id="13",
            instrument="NIFTY",
            open=c,
            high=c,
            low=c,
            close=c,
            volume=0,
            start_at=s,
            end_at=s + timedelta(minutes=TIMEFRAME_MINUTES),
        )
        for s, c in zip(starts, closes, strict=True)
    ]


def worker_config(
    tmp_path: Path,
    *,
    trading_date: str = TRADING_DATE,
    lots: int = LOTS,
    max_daily_loss_percent: float | None = 3.0,
    capital_base: float = 1_000_000.0,
    entry_cutoff: str = "15:15",
    square_off_at: str = "15:20",
    paper_execution: dict[str, Any] | None = None,
) -> WorkerConfig:
    """A ``WorkerConfig`` shaped like the committed strategy config.

    ``paper_execution`` deliberately omits ``max_quote_age_ms``: that check compares a
    quote's exchange timestamp against the *wall clock*, which is meaningful on a live
    feed and meaningless on a replayed historical tape, where every order would be
    refused (the field's own docstring says exactly this, and records it as deviation
    D53). The committed config sets it for the live feed; these tests are the replayed
    case it explicitly does not apply to.
    """
    return WorkerConfig(
        runtime_id=RUNTIME_ID,
        strategy_id=STRATEGY_ID,
        security_id=UNDERLYING,
        instrument="NIFTY",
        database_path=tmp_path / "operational.db",
        lock_dir=tmp_path / "locks",
        pid_dir=tmp_path / "pid",
        log_dir=tmp_path / "logs",
        trading_date=trading_date,
        execution_mode=ExecutionMode.PAPER,
        square_off_policy=SquareOffPolicy(
            entry_cutoff=datetime.strptime(entry_cutoff, "%H:%M").time(),
            square_off_at=datetime.strptime(square_off_at, "%H:%M").time(),
            timezone="Asia/Kolkata",
        ),
        paper_execution=paper_execution
        if paper_execution is not None
        else {
            "slippage": {"options": {"mode": "ticks", "market_order_ticks": 1}},
            "submission_latency_ms": 0,
            "tick_size": 0.05,
            "allow_ltp_fallback": True,
            "ltp_fallback_extra_ticks": 1,
        },
        engine=EngineWorkerConfig(
            strategy_ref=(
                "strategies.intraday_options.supertrend_buy_1_1p2.strategy:"
                "SupertrendBuy1x1p2Strategy"
            ),
            strategy_kwargs={"lots_per_trade": lots},
            timeframe="5m",
            lots=lots,
            strike_step=50,
            lot_size=LOT_SIZE,
            contract_resolver="simulated",
            starting_capital=capital_base,
            max_daily_loss_percent=max_daily_loss_percent,
            warmup_from_history=True,
            warmup_source="dhan",
            warmup_max_lookback_sessions=3,
            session_start_time="09:15",
        ),
    )


@dataclass
class Stack:
    """One assembled engine plus the real persistence behind it."""

    engine: TradingEngine
    strategy: SupertrendBuy1x1p2Strategy
    positions: PositionManager
    repository: ExecutionRepository
    config: WorkerConfig
    database: Database

    def payload(self) -> dict[str, Any]:
        return read_payload(
            self.repository,
            strategy_id=self.config.strategy_id,
            execution_mode=self.config.execution_mode,
            trading_date=self.config.trading_date,
        )

    def exit_state(self) -> dict[str, Any] | None:
        return recover_exit_state(self.config, self.repository)

    def adopted(self) -> AdoptedPosition | None:
        return recover_position(self.config, self.repository)

    def open_position_rows(self) -> list[Any]:
        return self.repository.open_positions(
            strategy_id=self.config.strategy_id,
            execution_mode=self.config.execution_mode,
            trading_date=self.config.trading_date,
        )

    def order_intent_count(self, side: str) -> int:
        return int(
            self.database.connect()
            .execute("SELECT COUNT(*) FROM order_intents WHERE side = ?", (side,))
            .fetchone()[0]
        )


def build_stack(
    config: WorkerConfig,
    ticks: Sequence[Tick],
    *,
    warmup: Sequence[Candle] | None = None,
    clock_at: datetime | None = None,
    strategy: SupertrendBuy1x1p2Strategy | None = None,
    lot_size: int = LOT_SIZE,
    warmup_fetch: Callable[..., list[Candle]] | None = None,
    recover: bool = True,
) -> Stack:
    """Assemble the production stack for one engine run against ``config``'s database.

    Migrations run on first use and are a no-op afterwards, so several ``build_stack``
    calls against the same ``WorkerConfig`` behave exactly like successive worker
    processes over one operational database.
    """
    database = Database(config.database_path)
    MigrationRunner(database).run_pending()
    repository = ExecutionRepository(database)
    session = repository.open_session(
        runtime_id=config.runtime_id,
        strategy_id=config.strategy_id,
        execution_mode=config.execution_mode,
        process_role="worker",
        pid=os.getpid(),
    )

    engine_config = config.engine
    assert engine_config is not None
    market_session = MarketSession(
        SessionConfig.from_square_off_policy(
            config.square_off_policy,
            start_time=engine_config.session_start_time,
            holidays=tuple(engine_config.holidays),
        )
    )

    quotes = QuoteBook()
    broker = build_broker(
        resolved_config_from_worker(config),
        preflight_passed=False,
        paper_execution=config.paper_execution,
        cost_rates=config.cost_rates,
        quotes=quotes,
    )
    lifecycle = OrderLifecycle(
        repository=repository,
        broker=broker,
        runtime_id=config.runtime_id,
        strategy_id=config.strategy_id,
        execution_mode=config.execution_mode,
        session_id=session.id,
        quotes=quotes,
    )
    gateway = LifecycleGateway(
        lifecycle,
        strategy_id=config.strategy_id,
        execution_mode=config.execution_mode,
        trading_date=config.trading_date,
        repository=repository,
        runtime_id=config.runtime_id,
    )
    positions = PositionManager(gateway, lots=engine_config.lots)
    strategy = strategy or SupertrendBuy1x1p2Strategy(**engine_config.strategy_kwargs)

    candles = list(warmup) if warmup is not None else warmup_candles(market_session)

    def _fetch(source, *, session, timeframe_minutes, lookback_sessions, now=None):
        assert source is WARMUP_SOURCE
        return candles

    def _persist_exit_state(data: dict[str, Any] | None, last_candle_end_at: str | None) -> None:
        merge_payload(
            repository,
            {EXIT_STATE_KEY: data},
            runtime_id=config.runtime_id,
            strategy_id=config.strategy_id,
            execution_mode=config.execution_mode,
            trading_date=config.trading_date,
            last_candle_end_at=last_candle_end_at,
        )

    engine = TradingEngine(
        EngineConfig(
            timeframe=engine_config.timeframe,
            session=SessionConfig.from_square_off_policy(
                config.square_off_policy,
                start_time=engine_config.session_start_time,
                holidays=tuple(engine_config.holidays),
            ),
            execution_mode=config.execution_mode,
            max_daily_loss_percent=engine_config.max_daily_loss_percent,
            starting_capital=engine_config.starting_capital,
            warmup_from_history=True,
        ),
        feed=SimulatedFeed(list(ticks)),
        option_selector=OptionSelector(
            SimulatedOptionChainResolver("NIFTY", lot_size=lot_size), strike_step=50
        ),
        strategy=strategy,
        position_manager=positions,
        underlying_security_id=UNDERLYING,
        runtime_id=config.runtime_id,
        warmup_manager=WarmupManager(warmup_fetch or _fetch, max_lookback_sessions=3),
        warmup_source=WARMUP_SOURCE,
        clock=(lambda: clock_at) if clock_at is not None else (lambda: dt(9, 15)),
        recover_position=(lambda: recover_position(config, repository)) if recover else None,
        recover_exit_state=(lambda: recover_exit_state(config, repository)) if recover else None,
        recover_daily_risk=(lambda: recover_daily_risk(config, repository)) if recover else None,
        persist_exit_state=_persist_exit_state,
    )
    return Stack(
        engine=engine,
        strategy=strategy,
        positions=positions,
        repository=repository,
        config=config,
        database=database,
    )
