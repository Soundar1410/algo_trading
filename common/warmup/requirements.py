"""Indicator warm-up requirements.

Ported from the reference repository's ``framework/warmup/requirements.py`` in
Phase 3 Part 2a. Only :class:`IndicatorScope` and :class:`WarmupRequirement` are
brought across — the two the ported indicators' ``warmup_requirement()`` methods
construct. ``validate_warmup_config``, ``InvalidWarmupConfig`` and
``StrategyWarmupSpec`` are engine-level and stay behind until Part 2b gives them
a caller, the same way only ``parse_hhmm`` came across from ``timeutils``.

Nothing in this repository *acts* on a ``WarmupRequirement`` yet — the warm-up
manager that consumes them is not ported. It is here so the ported indicators
remain complete and callable rather than carrying a method that would raise on
import; the fail-closed gate it describes arrives with the engine.

Kept dependency-free (stdlib only), as in the reference, so the indicator base
classes can import it with no import-order risk.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


class IndicatorScope(Enum):
    """Whether an indicator's state may span sessions or is reset each day."""

    #: Trend/MA/range indicators (SuperTrend, EMA, ATR). Prior-session history is
    #: valid and *desirable* — the state carries across the overnight gap, which
    #: is what makes a mid-session start match a bot that ran from the open.
    SESSION_SPANNING = auto()
    #: Session-cumulative indicators (VWAP). Reset every day — prior-session
    #: candles would corrupt them, so they must be warmed with today's data only.
    SESSION_LOCAL = auto()


@dataclass(frozen=True)
class WarmupRequirement:
    """One indicator's declared warm-up need."""

    #: Minimum closed candles for the indicator to be meaningfully "ready".
    min_bars: int
    scope: IndicatorScope = IndicatorScope.SESSION_SPANNING
    #: The indicator weights by volume (VWAP). Warm-up is only safe if the LIVE
    #: stream also carries volume; the manager refuses otherwise.
    requires_volume: bool = False
    #: This indicator's state is *path-dependent*, so an approximation built
    #: from live candles alone is not merely late — it can be wrong, with no
    #: guaranteed or bounded convergence to the historically continuous state.
    #:
    #: SuperTrend is the case that forced this flag: its direction is latched
    #: and only changes when price crosses a band, so a cold seed can hold the
    #: wrong trend indefinitely and then read the first crossing as a *fresh
    #: flip*. ``min_bars`` cannot express this -- the need is continuity with
    #: history, not a bar count (SuperTrend(period=1) declares min_bars=1, i.e.
    #: "one candle warms me", which is exactly the false comfort that let
    #: 2026-07-17's manufactured flips trade).
    #:
    #: Contrast EMA/ATR: session-spanning too, but they converge exponentially,
    #: so a cold start self-corrects within a few periods and this stays False.
    #: Consumers must fail closed on a non-``WARMED`` warm-up when this is set
    #: -- see ``md_docs/Warmup_Fail_Closed_Gate.md``.
    continuity_required: bool = False
