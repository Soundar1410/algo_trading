"""Indicator warm-up requirements.

Ported from the reference repository's ``framework/warmup/requirements.py``.
Part 2a brought across only :class:`IndicatorScope` and :class:`WarmupRequirement`
— the two the ported indicators' ``warmup_requirement()`` methods construct — and
recorded that ``validate_warmup_config``, ``InvalidWarmupConfig`` and
``StrategyWarmupSpec`` were engine-level and would follow when Part 2b gave them a
caller. Part 2b-i is that port, so they arrive here now.

The warm-up **manager** that replays history is still not ported: it is a
market-data concern (it fetches prior sessions) and the engine treats it as an
injected, duck-typed collaborator, exactly as the reference did. With no manager
injected the engine cold-starts, and :meth:`StrategyWarmupSpec.entry_blocked_by`
is what keeps a continuity-required strategy from trading on that cold seed.

Kept dependency-free (stdlib only), as in the reference, so the indicator base
classes can import it with no import-order risk.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any


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


class InvalidWarmupConfig(ValueError):
    """``warmup_from_history: false`` on a strategy that cannot be cold-started."""


def validate_warmup_config(strategy: Any, cfg: Any) -> None:
    """Reject a config that disables warm-up for a continuity-required strategy.

    Turning warm-up off for a path-dependent indicator is an invalid
    configuration, not an implicit operator waiver: there is no history at all, so
    the indicator seeds itself off its first live candle and can trade a
    manufactured trend with nothing to catch it. Fail at startup, loudly, rather
    than at 09:16 in a log line nobody reads.

    Keyed on the *config flag*, deliberately — not on whether a warm-up manager
    was actually built. An offline stack legitimately has no manager while the
    config still asks for warm-up; that is not a config error, and the engine's
    runtime gate fails it closed anyway. Only an explicit
    ``warmup_from_history: false`` is a config error.

    A future intentional opt-out should be an explicit ``allow_cold_start: true``
    with an audit warning -- not the absence of a flag.
    """
    if getattr(cfg, "warmup_from_history", False):
        return
    try:
        spec = strategy.warmup_spec()
    except Exception:  # never let validation itself break startup
        return
    if spec is None or not spec.continuity_required:
        return
    raise InvalidWarmupConfig(
        f"strategy '{getattr(strategy, 'name', type(strategy).__name__)}' holds a "
        f"continuity-required indicator (e.g. SuperTrend), which cannot be "
        f"cold-started: its trend is path-dependent and a cold seed has no "
        f"guaranteed convergence to the historically continuous state. "
        f"Set warmup_from_history true on the engine config."
    )


@dataclass(frozen=True)
class StrategyWarmupSpec:
    """A strategy's aggregate warm-up need, derived from its indicators.

    Only the session-spanning aggregate is acted on; ``has_session_local`` and
    ``requires_volume`` are surfaced so a manager can *safely skip* (rather than
    corrupt) a strategy that mixes in a session-local or volume-weighted
    indicator.
    """

    #: Max ``min_bars`` across the strategy's session-spanning indicators (0 if it
    #: has none — nothing session-spanning to warm).
    min_bars: int
    #: Any session-spanning indicator needs live volume (blocks warm-up on a
    #: volume-less stream).
    requires_volume: bool = False
    #: The strategy also holds a session-local indicator (e.g. VWAP).
    has_session_local: bool = False
    #: Any indicator here is path-dependent (see
    #: :attr:`WarmupRequirement.continuity_required`). When True the engine must
    #: refuse to ENTER unless warm-up returned ``WARMED``.
    continuity_required: bool = False

    @property
    def is_empty(self) -> bool:
        """True when there is no session-spanning indicator to warm."""
        return self.min_bars <= 0

    def entry_blocked_by(self, status: str) -> bool:
        """True when ``status`` must fail this spec closed (no new entries).

        The predicate keys on ``continuity_required`` FIRST and only then looks at
        the status -- that ordering is the spec, not an implementation detail.
        ``SKIPPED_EMPTY`` passes for an ordinary strategy because it needs no
        continuity, NOT because the status is intrinsically safe: if it ever
        arrives alongside ``continuity_required`` that is an inconsistent state,
        and this returns True (fail closed) rather than trusting it.

        Deliberately not a "warmed" predicate that counts ``PARTIAL`` as success.
        A partial replay leaves a *gap* between the history and the live candles,
        so a latched indicator is left mid-path -- worse than cold, because it
        looks warm.
        """
        if not self.continuity_required:
            return False
        return status != "WARMED"

    @classmethod
    def from_indicators(cls, indicators: Iterable[Any]) -> StrategyWarmupSpec:
        """Aggregate the requirements of the given indicators.

        ``indicators`` is any iterable of objects exposing ``warmup_requirement()``
        (every :class:`~common.indicators.base.StatefulIndicator` does).
        """
        reqs = [ind.warmup_requirement() for ind in indicators]
        spanning = [r for r in reqs if r.scope is IndicatorScope.SESSION_SPANNING]
        return cls(
            min_bars=max((r.min_bars for r in spanning), default=0),
            requires_volume=any(r.requires_volume for r in spanning),
            has_session_local=any(r.scope is IndicatorScope.SESSION_LOCAL for r in reqs),
            # Across ALL indicators, not just the session-spanning ones: a
            # path-dependent indicator that got itself classified session-local
            # still cannot be approximated, and must not lose the flag here.
            continuity_required=any(r.continuity_required for r in reqs),
        )
