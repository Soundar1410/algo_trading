"""A generic, minimal typed subscription for a market-data-only instrument.

Exists for exactly one reason: :data:`~common.market_data.scrip_master.
IndexMeta` describes a *tradable underlying* — it always carries an
``fno_segment`` because it exists to resolve that underlying's option chain.
India VIX has no option chain; it is a pure market-data auxiliary index for
strategies like ``straddle_920``'s VIX filter. Giving it a placeholder/fake
``fno_segment`` just to satisfy ``IndexMeta``'s shape would put false metadata
into validated configuration — rejected explicitly during this strategy's
design review.

:class:`MarketDataInstrument` instead carries only what a feed subscription
actually needs — ``security_id``, ``segment``, ``mode`` — plus a ``role`` label
for observability/routing. It has no ``fno_segment`` field at all, so it is
**structurally** incapable of being passed anywhere an option-chain resolver
expects an :class:`~common.market_data.scrip_master.IndexMeta` — a caller that
tries fails at the type checker, not at runtime with a wrong subscription.

Also serves as the typed shape for the underlying itself where a worker needs
to describe *both* the underlying and an auxiliary index uniformly (e.g. a
multi-leg engine's initial NIFTY + India VIX subscriptions) — see
:mod:`runtimes.intraday_options.multi_leg_engine_worker`.
"""

from __future__ import annotations

from dataclasses import dataclass

from .scrip_master import SEGMENT_CODES, segment_code


@dataclass(frozen=True)
class MarketDataInstrument:
    """A single instrument's feed-subscription identity — nothing more.

    ``segment``/``mode`` are numeric MarketFeed codes (see
    :data:`~common.market_data.scrip_master.SEGMENT_CODES`), ready to pass
    straight to :meth:`~common.feed.hub.SharedFeedHub.request_subscription` or
    a :class:`~common.feed.hub.WorkerChannel`'s initial ``segment``/``mode`` —
    no second conversion step, and no ``fno_segment`` to misuse.
    """

    security_id: str
    segment: int
    mode: int | None = None
    #: Free-form label for logs/observability/routing (e.g. "NIFTY_UNDERLYING",
    #: "INDIA_VIX"). Not interpreted by this module.
    role: str = ""

    @classmethod
    def from_named_segment(
        cls,
        *,
        security_id: str,
        segment: str,
        mode: int | None = None,
        role: str = "",
    ) -> MarketDataInstrument:
        """Build from a named segment (``"IDX_I"``) rather than its numeric code —
        the shape every strategy config already uses elsewhere in this repository."""
        return cls(security_id=security_id, segment=segment_code(segment), mode=mode, role=role)


__all__ = ["SEGMENT_CODES", "MarketDataInstrument"]
