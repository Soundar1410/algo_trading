"""The traded option's own premium candle stream, end to end through the engine.

**A rebuild, not a port** (Phase 3 Part 2b-i, deviation D22). The reference
repository's only end-to-end ``TradingEngine`` suite is
``strategies/ema_cross/tests/test_premium_candle_exit.py`` (9 tests), and it
constructs the real EMA-cross strategy — indicator, trade manager, tuned config.
``CLAUDE.md`` defers real strategies to Phase 9, so the nine *properties* that
suite establishes are rebuilt here against
:class:`~strategies.intraday_options.engine_fixture_strategy.EngineFixtureStrategy`.

What is deliberately **not** faked: the engine is real and fully constructed (no
``__new__``, no monkeypatched internals), the position is a real
:class:`~common.engine.models.OpenPosition`, and the exit decision is made by the
**real** Part 2a ``MOMENTUM_CLOSE`` policy in premium mode. The double supplies
only the entry timing and the wiring — which is exactly what ``EMA1Strategy``
supplied in the reference.

The diff-fidelity loss is real and is recorded rather than papered over: these
assertions are written here, not inherited, so they prove the *engine's* behaviour
rather than proving that a port of a strategy still matches itself.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from common.engine.config import EngineConfig, SessionConfig
from common.engine.engine import TradingEngine
from common.engine.feed import SimulatedFeed
from common.engine.models import ExitReason, OptionType, OrderSide
from common.engine.positions import InMemoryGateway, PositionManager
from common.engine.selection import OptionSelector, SimulatedOptionChainResolver
from common.models import Tick
from common.warmup.requirements import InvalidWarmupConfig
from strategies.intraday_options.engine_fixture_strategy import EngineFixtureStrategy

IST = ZoneInfo("Asia/Kolkata")
UNDERLYING = "INDEX"
LOT_SIZE = 65
#: Spot sits here when the first candle closes, so the ATM strike is 24000.
CE_CONTRACT = "SIM:NIFTY:WEEKLY:24000:CE"
PE_CONTRACT = "SIM:NIFTY:WEEKLY:24000:PE"
#: The strike a re-entry lands on once spot has moved — used to prove that a
#: strike change carries no premium state across with it.
CE_CONTRACT_2 = "SIM:NIFTY:WEEKLY:24100:CE"


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


def _build_engine(
    ticks: Sequence[Tick],
    *,
    strategy: EngineFixtureStrategy | None = None,
    warmup_from_history: bool = True,
) -> tuple[TradingEngine, EngineFixtureStrategy, PositionManager]:
    """A fully real engine over a simulated tape. No monkeypatching anywhere."""
    strategy = strategy or EngineFixtureStrategy(enter_on_candle=1, premium_exit=True)
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
            warmup_from_history=warmup_from_history,
        ),
        feed=SimulatedFeed(ticks),
        option_selector=OptionSelector(
            SimulatedOptionChainResolver("NIFTY", lot_size=LOT_SIZE), strike_step=50
        ),
        strategy=strategy,
        position_manager=positions,
        underlying_security_id=UNDERLYING,
    )
    return engine, strategy, positions


def _entry_tape() -> list[Tick]:
    """Two underlying ticks: the second closes candle #1 and triggers the ENTER."""
    return [
        _tick(UNDERLYING, 24000.0, _ts(9, 16)),
        _tick(UNDERLYING, 24010.0, _ts(9, 21)),
    ]


def _premium_tape(contract: str, prices_at: Sequence[tuple[datetime, float]]) -> list[Tick]:
    return [_tick(contract, price, ts) for ts, price in prices_at]


#: Fills the entry at 100, then walks the premium series up (105, 108) and down
#: (85) so exactly one adverse *closed* bar exists.
_PREMIUM_WALK = [
    (_ts(9, 21, 30), 100.0),  # fills the pending entry
    (_ts(9, 23), 105.0),  # opens premium bar 09:20
    (_ts(9, 26), 110.0),  # closes 09:20 bar @105 — first bar, no predecessor
    (_ts(9, 28), 108.0),
    (_ts(9, 31), 90.0),  # closes 09:25 bar @108 — higher, no exit for a BUY
    (_ts(9, 33), 85.0),
    (_ts(9, 36), 80.0),  # closes 09:30 bar @85 — lower, EXIT
]


# ------------------------------------------------------------------ 1. routing
def test_entry_is_gated_on_the_underlying_stream_not_the_premium_stream() -> None:
    """Premium ticks must never produce an entry — only underlying candles do."""
    strategy = EngineFixtureStrategy(enter_on_candle=1, premium_exit=True)
    # Option ticks arrive *before* any underlying candle closes.
    ticks = [
        _tick(CE_CONTRACT, 100.0, _ts(9, 16)),
        _tick(CE_CONTRACT, 120.0, _ts(9, 22)),
        *_entry_tape(),
    ]
    engine, strategy, positions = _build_engine(ticks, strategy=strategy)
    engine.run()

    # The two early premium ticks matched no position and no pending entry, so
    # they were routed and dropped — they did not open anything.
    assert strategy.candles_seen == 1
    assert positions.trades == []
    # The entry is pending, awaiting its first fresh option tick.
    assert engine._pending is not None
    assert engine._pending.security_id == CE_CONTRACT
    assert not positions.has_position()


# --------------------------------------------------------------- 2. CE premium
def test_a_ce_position_exits_on_a_lower_premium_close() -> None:
    ticks = [*_entry_tape(), *_premium_tape(CE_CONTRACT, _PREMIUM_WALK)]
    engine, strategy, positions = _build_engine(ticks)
    engine.run()

    assert len(positions.trades) == 1
    trade = positions.trades[0]
    assert trade.contract.security_id == CE_CONTRACT
    assert trade.entry_price == 100.0
    assert trade.exit_price == 80.0
    assert trade.exit_reason is ExitReason.MOMENTUM_LOW
    # The real policy saw a real OpenPosition, not a stand-in.
    assert strategy.last_position is not None
    assert strategy.last_position.contract.option_type is OptionType.CE
    assert strategy.last_position.side is OrderSide.BUY


# --------------------------------------------------------------- 3. PE premium
def test_a_pe_position_exits_on_its_own_premium_stream() -> None:
    """The PE leg is driven by the PE contract's bars, not the CE's or the index's."""
    strategy = EngineFixtureStrategy(
        enter_on_candle=1, premium_exit=True, option_type=OptionType.PE
    )
    ticks = [
        *_entry_tape(),
        *_premium_tape(PE_CONTRACT, _PREMIUM_WALK),
        # A CE stream moving the other way must not influence the PE position.
        *_premium_tape(
            CE_CONTRACT,
            [(_ts(9, 24), 500.0), (_ts(9, 29), 600.0), (_ts(9, 34), 700.0)],
        ),
    ]
    engine, strategy, positions = _build_engine(ticks, strategy=strategy)
    engine.run()

    assert len(positions.trades) == 1
    trade = positions.trades[0]
    assert trade.contract.security_id == PE_CONTRACT
    assert trade.contract.option_type is OptionType.PE
    assert trade.exit_reason is ExitReason.MOMENTUM_LOW
    # Only the PE contract's closes ever reached the strategy.
    assert strategy.premium_closes == [105.0, 108.0, 85.0]


# ------------------------------------------------------------------- 4. gap
def test_a_premium_tick_gap_is_logged_and_square_off_still_closes_the_position(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A starved premium stream disables only that exit — square-off still fires."""
    ticks = [
        *_entry_tape(),
        _tick(CE_CONTRACT, 100.0, _ts(9, 21, 30)),  # fills the entry
        # No option ticks at all after this. The underlying keeps closing candles,
        # which is where the gap is detected.
        _tick(UNDERLYING, 24020.0, _ts(9, 26)),
        _tick(UNDERLYING, 24030.0, _ts(9, 31)),
        # Past square-off: the position must still be force-closed.
        _tick(UNDERLYING, 24040.0, _ts(15, 21)),
    ]
    engine, strategy, positions = _build_engine(ticks)
    with caplog.at_level(logging.ERROR):
        engine.run()

    assert "[PREMIUM_CANDLE_GAP]" in caplog.text
    assert len(positions.trades) == 1
    assert positions.trades[0].exit_reason is ExitReason.SQUARE_OFF
    # The premium exit never fired — it had no bars to fire from.
    assert strategy.option_candles_seen == 0


# --------------------------------------------------------------- 5. no leakage
def test_premium_candle_state_does_not_leak_across_a_re_entry() -> None:
    """A second entry starts its premium series from scratch — proven by re-entering.

    The tape deliberately re-enters on a **different strike** and makes the new
    contract's first completed bar close *below the previous contract's last close*
    (75 vs 85). If premium state leaked across the re-entry, that first bar would
    read as an adverse streak continuation and exit immediately, booking three
    trades instead of two. Correct behaviour is that the new series has no
    predecessor, so it holds.
    """
    strategy = EngineFixtureStrategy(enter_on_candle=(1, 3), premium_exit=True)
    ticks = [
        *_entry_tape(),
        *_premium_tape(CE_CONTRACT, _PREMIUM_WALK),  # trade 1: 100 in, 80 out
        # Two more underlying candles; the second is entry #3, and spot has moved
        # far enough that the ATM strike is 24100, not 24000.
        _tick(UNDERLYING, 24095.0, _ts(9, 41)),  # closes candle #2
        _tick(UNDERLYING, 24100.0, _ts(9, 46)),  # closes candle #3 -> ENTER again
        *_premium_tape(
            CE_CONTRACT_2,
            [
                (_ts(9, 46, 30), 80.0),  # fills entry #2
                (_ts(9, 48), 78.0),  # opens the new series' first bar
                (_ts(9, 51), 75.0),  # closes it @78 — LOWER than trade 1's last
                #                       close of 85. Must NOT exit: no predecessor.
                (_ts(9, 53), 74.0),
                (_ts(9, 56), 70.0),  # closes @74 — 74 < 78, a real streak: EXIT
            ],
        ),
    ]
    engine, strategy, positions = _build_engine(ticks, strategy=strategy)
    engine.run()

    assert len(positions.trades) == 2, "the re-entry did not happen, or state leaked"
    first, second = positions.trades
    # Two different contracts — a strike change, not a re-entry into the same leg.
    assert first.contract.security_id == CE_CONTRACT
    assert second.contract.security_id == CE_CONTRACT_2
    assert first.entry_price == 100.0 and first.exit_price == 80.0
    assert second.entry_price == 80.0 and second.exit_price == 70.0
    assert second.exit_reason is ExitReason.MOMENTUM_LOW
    # The bar that would have fired on leaked state (78) is present and did not.
    assert strategy.premium_closes == [105.0, 108.0, 85.0, 78.0, 74.0]

    # And after the final close the engine holds no premium state at all.
    assert engine._option_candle_contract_id is None
    assert engine._last_option_tick_ts is None
    assert engine._option_candles is not None
    assert engine._option_candles.current is None


# ------------------------------------------------------------- 6. exit priority
def test_the_risk_manager_stop_takes_priority_over_the_premium_candle_exit() -> None:
    """A hard stop must win over a candle-structure exit on the same tick."""
    strategy = EngineFixtureStrategy(
        enter_on_candle=1,
        premium_exit=True,
        # Entry at 100 with lot size 65: a drop to 85 is -15 * 65 = -975.
        stop_loss_rupees=500.0,
    )
    ticks = [*_entry_tape(), *_premium_tape(CE_CONTRACT, _PREMIUM_WALK)]
    engine, strategy, positions = _build_engine(ticks, strategy=strategy)
    engine.run()

    assert len(positions.trades) == 1
    # STOP_LOSS, not MOMENTUM_LOW: the risk manager is consulted first, and the
    # premium exit is only reached when it returns None.
    assert positions.trades[0].exit_reason is ExitReason.STOP_LOSS


# ------------------------------------------------------------- 7. no double exit
def test_a_stray_tick_for_a_closed_position_does_not_exit_twice() -> None:
    ticks = [
        *_entry_tape(),
        *_premium_tape(CE_CONTRACT, _PREMIUM_WALK),
        # Ticks for a contract that is no longer held.
        _tick(CE_CONTRACT, 70.0, _ts(9, 41)),
        _tick(CE_CONTRACT, 60.0, _ts(9, 46)),
    ]
    engine, _strategy, positions = _build_engine(ticks)
    engine.run()

    assert len(positions.trades) == 1
    assert not positions.has_position()


# ------------------------------------------------------- 8. fail-closed warm-up
def test_a_continuity_required_strategy_with_warm_up_disabled_is_rejected() -> None:
    """``warmup_from_history: false`` on a path-dependent indicator is a config error."""
    strategy = EngineFixtureStrategy(enter_on_candle=1, continuity_required=True)
    with pytest.raises(InvalidWarmupConfig, match="continuity-required"):
        _build_engine(_entry_tape(), strategy=strategy, warmup_from_history=False)


def test_a_strategy_whose_warmup_spec_raises_is_blocked_from_entering() -> None:
    """Unable to establish that a cold start is safe => no entries, but exits live."""
    strategy = EngineFixtureStrategy(enter_on_candle=1, warmup_spec_raises=True)
    engine, strategy, positions = _build_engine(_entry_tape(), strategy=strategy)
    engine.run()

    assert engine._entry_blocked is not None
    assert "warmup_spec() raised" in engine._entry_blocked
    # The signal fired and was evaluated; only the entry was refused.
    assert strategy.candles_seen == 1
    assert engine._pending is None
    assert not positions.has_position()


# ------------------------------------------------------------- 9. gap re-priming
def test_a_gap_resets_the_premium_candle_and_re_primes_the_streak() -> None:
    """After a gap the next close has no predecessor, so no exit is manufactured."""
    strategy = EngineFixtureStrategy(enter_on_candle=1, premium_exit=True)
    walk = [
        (_ts(9, 21, 30), 100.0),  # fills the entry
        (_ts(9, 23), 105.0),
        (_ts(9, 26), 110.0),  # closes 09:20 bar @105
        (_ts(9, 28), 108.0),
        (_ts(9, 31), 120.0),  # closes 09:25 bar @108
        # A 20-minute hole: longer than the 5-minute interval, so the incomplete
        # bar is discarded and the strategy is told to forget its predecessor.
        (_ts(9, 51), 60.0),
        (_ts(9, 56), 55.0),  # closes 09:50 bar @60 — lower than 108, but the
        # streak was re-primed, so this must NOT exit.
    ]
    ticks = [*_entry_tape(), *_premium_tape(CE_CONTRACT, walk)]
    engine, strategy, positions = _build_engine(ticks, strategy=strategy)
    engine.run()

    assert strategy.gaps_notified == 1
    # Without the gap reset, 60 < 108 would have closed the position.
    assert positions.trades == []
    assert positions.has_position()
    # The structural half, as the reference asserted it directly: the stale
    # incomplete bar was discarded and rebuilt from the resuming tick's bucket.
    assert engine._option_candles is not None
    assert engine._option_candles.current is not None
    assert engine._option_candles.current.start_at == _ts(9, 55)
    # The post-gap bar *did* close, at 60 — far below the pre-gap 108. It reached
    # the strategy and was evaluated; what it could not do is compare across the
    # hole, because the gap hook cleared its predecessor. That is the difference
    # between "the bar was suppressed" and "the streak re-primed".
    assert strategy.premium_closes == [105.0, 108.0, 60.0]


# ---------------------------------------------- runbook item 5: the streak gap
def test_momentum_close_walks_a_real_consecutive_streak_on_the_premium_stream() -> None:
    """Consecutive-streak behaviour on the premium stream, with a real caller.

    Part 2a recorded that ``test_momentum_close_option_premium_is_side_aware_and_
    consecutive`` does not test the ``_and_consecutive`` half of its name: it makes
    five isolated ``should_exit_closes`` calls and never builds a streak. It
    deferred closing that gap to "where the engine gives it a caller that actually
    walks a streak". This is that caller.

    The tape walks 105 -> 108 -> 85 -> (would-be 70): a rising pair then a fall.
    The engine must exit on the *first* adverse close and never evaluate the bar
    after it, because the position is gone.
    """
    walk = [
        (_ts(9, 21, 30), 100.0),
        (_ts(9, 23), 105.0),
        (_ts(9, 26), 110.0),  # closes @105 — no predecessor
        (_ts(9, 28), 108.0),
        (_ts(9, 31), 90.0),  # closes @108 — up, hold
        (_ts(9, 33), 85.0),
        (_ts(9, 36), 80.0),  # closes @85 — first down, EXIT here
        (_ts(9, 38), 75.0),
        (_ts(9, 41), 70.0),  # would close @75 — must never be evaluated
    ]
    ticks = [*_entry_tape(), *_premium_tape(CE_CONTRACT, walk)]
    engine, strategy, positions = _build_engine(ticks)
    engine.run()

    assert len(positions.trades) == 1
    assert positions.trades[0].exit_reason is ExitReason.MOMENTUM_LOW
    # Three closed premium bars reached the strategy, in order, and the streak
    # stopped at the first adverse one.
    assert strategy.premium_closes == [105.0, 108.0, 85.0]
    assert strategy.option_candles_seen == 3


def test_the_premium_builder_reuses_the_interval_of_the_underlying() -> None:
    """One timeframe drives both streams — a 5m strategy gets 5m premium bars."""
    ticks = [*_entry_tape(), *_premium_tape(CE_CONTRACT, _PREMIUM_WALK)]
    engine, _strategy, _positions = _build_engine(ticks)
    assert engine._option_candles is not None
    assert engine._option_candles.interval == 5
    assert engine.candles.interval == 5
    assert engine._premium_candle_interval == 5
    assert timedelta(minutes=engine._premium_candle_interval) == timedelta(minutes=5)
