"""The deterministic test-only signal fixture.

**This is not a trading strategy and must never be treated as one.** It contains
no market hypothesis, no indicator and no edge. Its only job is to make the
walking skeleton's plumbing observable: given candle N, emit a known signal, so
a test can assert that exactly one order, one fill and one position resulted.

Real strategies are Phase 9, and they arrive on the preserved engines
(``TradingEngine`` and friends), not on this class. The ``Strategy`` protocol
below is shaped like the engine's signal interface so that swap is mechanical,
but it is deliberately *not* derived from the reference implementation —
porting the engines is Phase 3 (deviation D10).

Determinism is by candle index, not by price or clock: the Nth completed candle
of the session triggers entry and the Mth triggers exit, so the same tape always
produces the same trades regardless of when it is replayed or how fast.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from common.config.models import ExecutionMode
from common.models import Candle, Side, Signal


@runtime_checkable
class Strategy(Protocol):
    """What a worker requires of any strategy.

    Shaped like the preserved engines' signal interface: the worker hands over
    one completed candle and receives at most one signal. A strategy never sees
    the broker, the database or the feed.
    """

    @property
    def strategy_id(self) -> str: ...

    @property
    def security_id(self) -> str: ...

    def on_candle(self, candle: Candle) -> Signal | None:
        """Evaluate one completed candle. Return a signal, or None to do nothing."""
        ...


@dataclass
class FixtureSignalStrategy:
    """Emits a BUY on candle ``entry_on_candle`` and a SELL on ``exit_on_candle``."""

    strategy_id: str
    security_id: str
    instrument: str
    execution_mode: ExecutionMode = ExecutionMode.PAPER
    quantity: int = 50
    entry_on_candle: int = 1
    exit_on_candle: int = 3
    #: Fractions of the entry price; recorded on the position so restart
    #: recovery has something concrete to restore and assert on.
    stop_loss_fraction: float = 0.80
    target_fraction: float = 1.20

    candles_seen: int = field(default=0, init=False)
    position_open: bool = field(default=False, init=False)
    entry_price: float | None = field(default=None, init=False)

    def on_candle(self, candle: Candle) -> Signal | None:
        if candle.security_id != self.security_id:
            return None

        self.candles_seen += 1

        if self.candles_seen == self.entry_on_candle and not self.position_open:
            self.position_open = True
            self.entry_price = candle.close
            return self._signal(candle, Side.BUY, "fixture entry")

        if self.candles_seen == self.exit_on_candle and self.position_open:
            self.position_open = False
            return self._signal(candle, Side.SELL, "fixture exit")

        return None

    def square_off_signal(self, candle: Candle) -> Signal | None:
        """Close an open position at the square-off boundary, if one exists."""
        if not self.position_open:
            return None
        self.position_open = False
        return self._signal(candle, Side.SELL, "square off")

    def stop_and_target(self) -> tuple[float | None, float | None]:
        if self.entry_price is None:
            return None, None
        return (
            round(self.entry_price * self.stop_loss_fraction, 2),
            round(self.entry_price * self.target_fraction, 2),
        )

    def restore(self, *, position_open: bool, entry_price: float | None, candles_seen: int) -> None:
        """Re-establish state after a restart, from persisted position data."""
        self.position_open = position_open
        self.entry_price = entry_price
        self.candles_seen = candles_seen

    def _signal(self, candle: Candle, side: Side, reason: str) -> Signal:
        return Signal(
            strategy_id=self.strategy_id,
            execution_mode=self.execution_mode,
            instrument=self.instrument,
            security_id=self.security_id,
            side=side,
            quantity=self.quantity,
            candle=candle,
            reference_price=candle.close,
            evaluated_at=_evaluated_at(candle),
            reason=reason,
        )


def _evaluated_at(candle: Candle) -> datetime:
    """Signals are evaluated at the candle close, never at wall-clock time."""
    return candle.end_at
