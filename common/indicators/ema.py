"""Exponential Moving Average, plus the crossover-confirmation gate.

Ported from the reference repository's ``framework/indicators/ema.py``
(Phase 4 Part 2), behaviour unchanged.

Two things live here because they live together in the reference, and splitting
them would be a change to internals made before their tests were ported:

* :class:`EMA` — the stateful indicator.
* :class:`ConfirmedCrossover` — hysteresis plus consecutive-close confirmation
  for an EMA *spread*. Not an indicator and not a ``StatefulIndicator``; it is
  the gate that stops a one-bar whipsaw becoming an order. It has **no consumer
  in this repository yet** — EMA crossover strategies are Phase 9 — and is
  ported anyway because it carries the only four genuine regression tests in the
  reference's entire indicator package, replays of real 2026-07-22 production
  spreads.

Seeding, and why the pandas-ta cross-check is not exact
------------------------------------------------------
This EMA seeds on the **first close** and then applies ``alpha = 2/(period+1)``
from bar two. ``pandas_ta_classic.ema`` seeds differently, so the two disagree
early and converge exponentially — the gap decays as ``(1-alpha)**n``. Measured
at ``2.98e-09`` maximum relative difference over the last 150 bars of a 300-bar
series; the cross-check asserts ``1e-8``. See
:mod:`common.indicators.vectorised`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .base import OHLC, StatefulIndicator


@dataclass
class EMAState:
    value: float


class EMA(StatefulIndicator):
    """Stateful EMA over closing price, fed one closed candle at a time."""

    def __init__(self, period: int) -> None:
        if period < 1:
            raise ValueError("period must be >= 1")
        self.period = period
        self._alpha = 2.0 / (period + 1.0)
        self.reset()

    def reset(self) -> None:
        self._value: float | None = None
        self._count = 0

    @property
    def is_ready(self) -> bool:
        return self._count >= self.period

    def update(self, candle: OHLC) -> EMAState:
        c = candle.close
        self._value = c if self._value is None else self._value + self._alpha * (c - self._value)
        self._count += 1
        return self.state

    @property
    def state(self) -> EMAState:
        if self._value is None:
            raise RuntimeError("EMA has no value yet; call update() first.")
        return EMAState(value=float(self._value))


class ConfirmedCrossover:
    """Hysteresis + consecutive-close confirmation for an EMA spread.

    A raw ``fast - slow`` sign flip is not enough to change direction.  The
    spread must first clear ``minimum_separation`` and then remain on that side
    for ``confirmation_candles`` consecutive completed bars.  Values inside
    the deadband cancel a pending candidate but retain the last confirmed side.

    ``update`` returns ``1`` only for a newly confirmed bullish crossover,
    ``-1`` only for a newly confirmed bearish crossover, and ``None`` while the
    direction is unchanged or still pending.
    """

    def __init__(self, minimum_separation: float, confirmation_candles: int) -> None:
        if not math.isfinite(minimum_separation) or minimum_separation < 0:
            raise ValueError("minimum_separation must be finite and >= 0")
        if confirmation_candles < 1:
            raise ValueError("confirmation_candles must be >= 1")
        self.minimum_separation = float(minimum_separation)
        self.confirmation_candles = int(confirmation_candles)
        self.reset()

    def reset(self) -> None:
        self.confirmed_side: int | None = None
        self.candidate_side: int | None = None
        self.candidate_count = 0
        self._initialized = False

    def update(self, spread: float) -> int | None:
        if not math.isfinite(spread):
            raise ValueError("EMA spread must be finite")
        raw_side = (
            1
            if spread > self.minimum_separation
            else -1
            if spread < -self.minimum_separation
            else None
        )

        # The first ready EMA pair establishes context only.  A strategy that
        # starts while a trend already exists must not enter a stale signal.
        if not self._initialized:
            self._initialized = True
            self.confirmed_side = raw_side
            return None

        if raw_side is None or raw_side == self.confirmed_side:
            self.candidate_side = None
            self.candidate_count = 0
            return None

        if raw_side != self.candidate_side:
            self.candidate_side = raw_side
            self.candidate_count = 1
        else:
            self.candidate_count += 1

        if self.candidate_count < self.confirmation_candles:
            return None

        self.confirmed_side = raw_side
        self.candidate_side = None
        self.candidate_count = 0
        return raw_side

    @property
    def confirmed_label(self) -> str:
        if self.confirmed_side == 1:
            return "bullish"
        if self.confirmed_side == -1:
            return "bearish"
        return "unconfirmed"

    @property
    def candidate_label(self) -> str:
        if self.candidate_side == 1:
            return "bullish"
        if self.candidate_side == -1:
            return "bearish"
        return "none"
