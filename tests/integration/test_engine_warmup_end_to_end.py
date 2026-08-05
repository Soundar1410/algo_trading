"""Phase 4 Part 4 — the warm-up manager/source seam, proven end to end through
a real :class:`~common.engine.engine.TradingEngine`, not just at
:class:`~common.warmup.manager.WarmupManager`'s own unit level.

Mirrors the ``_build_engine`` pattern in
``tests/integration/test_engine_premium_candle_exit.py``: a fully real engine
over a simulated tape, no monkeypatching of engine internals. A hand-built
``WarmupManager``/``WarmupSource`` is injected directly — this file does not
need a real Dhan fetch (that is :mod:`common.warmup.historical`'s job,
covered elsewhere) to prove the seam itself works.

This is what closes the loop on runbook limitation 16: the unit-level tests
prove ``WarmupManager.warm()`` computes the right status; this file proves
that status actually changes what the *engine* does — a continuity-required
strategy that cold-starts must not trade, and one that warms successfully
must trade normally.

The last test in this file is a same-part amendment: writing the others
found that a continuity-required strategy with no manager at all did not
merely cold-start (the case the other tests cover) but sailed past
construction entirely and traded with only a log warning.
``validate_warmup_config`` now refuses that outright — see
``common/warmup/requirements.py`` and deviation D47.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from common.engine.config import EngineConfig, SessionConfig
from common.engine.engine import TradingEngine
from common.engine.feed import SimulatedFeed
from common.engine.positions import InMemoryGateway, PositionManager
from common.engine.selection import OptionSelector, SimulatedOptionChainResolver
from common.models import Candle, Tick
from common.warmup.manager import WarmupManager
from common.warmup.requirements import InvalidWarmupConfig
from common.warmup.source import WarmupSource
from strategies.intraday_options.engine_fixture_strategy import EngineFixtureStrategy

IST = ZoneInfo("Asia/Kolkata")
UNDERLYING = "INDEX"
LOT_SIZE = 65

_SOURCE = WarmupSource(security_id="13", exchange_segment="IDX_I", instrument_type="INDEX")


def _ts(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 7, 16, hour, minute, second, tzinfo=IST)


def _tick(security_id: str, price: float, ts: datetime) -> Tick:
    return Tick(
        security_id=security_id,
        instrument=security_id,
        last_price=price,
        exchange_time=ts,
        received_at=ts,
    )


def _warmup_candle(start: datetime, price: float) -> Candle:
    return Candle(
        security_id="13",
        instrument="NIFTY",
        open=price,
        high=price,
        low=price,
        close=price,
        volume=0,
        start_at=start,
        end_at=start + timedelta(minutes=5),
    )


def _entry_tape() -> list[Tick]:
    """Two underlying ticks: the second closes the one live candle."""
    return [
        _tick(UNDERLYING, 24000.0, _ts(9, 16)),
        _tick(UNDERLYING, 24010.0, _ts(9, 21)),
    ]


def _build_engine(
    ticks: list[Tick],
    strategy: EngineFixtureStrategy,
    *,
    warmup_manager: WarmupManager | None = None,
    warmup_source: WarmupSource | None = None,
) -> tuple[TradingEngine, PositionManager]:
    positions = PositionManager(InMemoryGateway(slippage_points=0.0), lots=1)
    engine = TradingEngine(
        EngineConfig(
            timeframe="5m",
            session=SessionConfig(
                timezone="Asia/Kolkata",
                start_time="09:15",
                end_time="15:15",
                square_off_time="15:20",
            ),
        ),
        feed=SimulatedFeed(ticks),
        option_selector=OptionSelector(
            SimulatedOptionChainResolver("NIFTY", lot_size=LOT_SIZE), strike_step=50
        ),
        strategy=strategy,
        position_manager=positions,
        underlying_security_id=UNDERLYING,
        warmup_manager=warmup_manager,
        warmup_source=warmup_source,
    )
    return engine, positions


def test_warmup_replay_seeds_the_strategy_before_the_first_live_candle() -> None:
    """Two warm-up candles are replayed; the live tape supplies a third. The
    strategy is configured to enter only on the *third* on_candle call
    overall -- if warm-up genuinely flowed through on_candle before the live
    tick did, that third call is the live one and a real trade results. If
    warm-up did nothing, the live tape's own first candle would be call #1,
    entry_candles={3} would never match, and nothing would trade.
    """
    warmup_candles = [
        _warmup_candle(_ts(9, 0), 23950.0),
        _warmup_candle(_ts(9, 5), 23980.0),
    ]

    def _fetch(source: object, **kwargs: object) -> list[Candle]:
        assert source is _SOURCE
        return warmup_candles

    manager = WarmupManager(_fetch)
    strategy = EngineFixtureStrategy(enter_on_candle=3, continuity_required=True)
    engine, positions = _build_engine(
        _entry_tape(), strategy, warmup_manager=manager, warmup_source=_SOURCE
    )
    engine.run()

    assert strategy.candles_seen == 3  # 2 replayed + 1 live
    # A replay signal is discarded by construction (the sink never reaches the
    # engine's order path) -- proven here by there being no trade/position
    # opened at a warm-up candle's timestamp, only (if any) at the live one.
    assert engine._entry_blocked is None
    # The live candle (call #3) is a real ENTER that reached the order path.
    assert engine._pending is not None or positions.has_position()


def test_a_fetch_failure_degrades_to_cold_start_and_blocks_the_first_live_entry() -> None:
    """The closed-loop proof for limitation 16: a warm-up failure must not
    merely compute COLD_START in isolation -- it must actually stop the
    engine from taking an entry that would otherwise fire.
    """

    def _failing_fetch(source: object, **kwargs: object) -> list[Candle]:
        raise RuntimeError("simulated network failure")

    manager = WarmupManager(_failing_fetch)
    strategy = EngineFixtureStrategy(enter_on_candle=1, continuity_required=True)
    engine, positions = _build_engine(
        _entry_tape(), strategy, warmup_manager=manager, warmup_source=_SOURCE
    )
    engine.run()

    assert engine._entry_blocked is not None
    assert "COLD_START" in engine._entry_blocked
    # The candle was still evaluated -- only the entry was refused.
    assert strategy.candles_seen == 1
    assert engine._pending is None
    assert not positions.has_position()


def test_warmed_status_permits_entries() -> None:
    """A successful WARMED replay does not block the live entry that follows."""
    warmup_candles = [_warmup_candle(_ts(9, 0), 23950.0)]

    def _fetch(source: object, **kwargs: object) -> list[Candle]:
        return warmup_candles

    manager = WarmupManager(_fetch)
    strategy = EngineFixtureStrategy(enter_on_candle=2, continuity_required=True)
    engine, positions = _build_engine(
        _entry_tape(), strategy, warmup_manager=manager, warmup_source=_SOURCE
    )
    engine.run()

    assert engine._entry_blocked is None
    assert strategy.candles_seen == 2  # 1 replayed + 1 live
    assert engine._pending is not None or positions.has_position()


def test_no_manager_or_source_now_refuses_construction() -> None:
    """Omitting both (the default -- every existing config's behaviour, since
    ``engine_worker`` only injects a manager when ``warmup_source="dhan"`` is
    set) used to reach a fallback that only logged a WARNING and let the
    strategy trade on the cold-started indicator -- found while first writing
    this file, and recorded then as an open, pre-existing (Phase 3
    Part 2b-ii-B-2) gap.

    **Closed as a same-part amendment.**
    ``common.warmup.requirements.validate_warmup_config`` now also takes the
    ``warmup_manager`` that will actually be injected, and refuses
    construction outright when a continuity-required strategy has
    ``warmup_from_history: true`` (the default) but no manager -- not just
    when the flag is explicitly false. The WARNING-only fallback is
    unreachable for a continuity-required strategy now: either a real
    warm-up is wired, or construction fails loudly, never silently.
    """
    strategy = EngineFixtureStrategy(enter_on_candle=1, continuity_required=True)
    with pytest.raises(InvalidWarmupConfig, match="no warmup_manager was supplied"):
        _build_engine(_entry_tape(), strategy)
