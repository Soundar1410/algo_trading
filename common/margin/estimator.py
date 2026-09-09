"""``MarginEstimator`` — the one door a strategy's margin-utilization gate
goes through (spec section 3.7/6.3). Real Dhan margin-calculator result,
summed per leg, in production; :class:`~common.margin.models.
MarginUnavailable` on any failure — never a fabricated number.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from common.logging import get_logger

from .model import ConservativeMarginModel
from .models import LegMarginRequest, MarginEstimate, MarginUnavailable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from common.market_data.dhan_margin import BasketMargin

_log = get_logger(__name__)

#: How old a just-computed estimate may be before it is treated as stale.
#: Margin does not move tick-by-tick the way an option quote does, but this
#: still exists as a real structural safety property (spec: "missing,
#: stale, invalid or failed margin estimation blocks entry"), not merely a
#: theoretical one — see tests/unit/test_margin_estimator.py's stale case,
#: which constructs a :class:`~common.margin.models.MarginEstimate` fixture
#: directly rather than waiting on a real clock.
DEFAULT_MAX_MARGIN_AGE_SECONDS = 30.0


class MarginEstimator:
    """Estimate one basket's margin requirement.

Sources, in the order this class prefers them:

    ``basket_margin_fetcher`` — **the production default.** One call to
    Dhan's hedged multi-leg calculator
    (:func:`common.market_data.dhan_margin.build_dhan_basket_margin_fetcher`),
    which prices the legs against each other.

    ``margin_fetcher`` — one call per leg, results **summed**: no
    cross-margin/hedge netting assumed. Long presented as a merely
    conservative choice, and it is conservative, but for a defined-risk
    structure it is not *usefully* conservative: it prices an iron condor as
    two uncovered short options. Measured on 9 September 2026, same legs,
    same quantity — summed Rs 61,07,252 against hedged Rs 15,92,934, which
    was the difference between a strategy that could never enter and one
    that could. Kept as the fallback for a caller with no basket source.

    ``fallback_model`` — **production must never set this.** It exists only
    for an explicit offline/test caller (see
    :class:`~common.margin.model.ConservativeMarginModel`'s own docstring)
    and is consulted **only** when ``margin_fetcher`` is ``None`` — i.e.
    "no live source configured at all". A *configured* ``margin_fetcher``
    that fails, raises, or returns invalid/non-finite data always raises
    :class:`~common.margin.models.MarginUnavailable`, regardless of whether
    a ``fallback_model`` happens to be set: a live-source failure is never
    silently downgraded to the offline approximation.
    """

    def __init__(
        self,
        *,
        margin_fetcher: Callable[[LegMarginRequest], float] | None,
        basket_margin_fetcher: Callable[[list[LegMarginRequest]], BasketMargin] | None = None,
        fallback_model: ConservativeMarginModel | None = None,
        max_age_seconds: float = DEFAULT_MAX_MARGIN_AGE_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._margin_fetcher = margin_fetcher
        self._basket_margin_fetcher = basket_margin_fetcher
        self._fallback_model = fallback_model
        self._max_age_seconds = max_age_seconds
        self._now: Callable[[], datetime] = clock if clock is not None else self._utc_now

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(UTC)

    def estimate_basket(
        self,
        legs: list[LegMarginRequest],
        *,
        spot: float,
        allocated_capital: float,
        now: datetime | None = None,
    ) -> MarginEstimate:
        """Every leg's margin, summed, with full provenance.

        Raises :class:`~common.margin.models.MarginUnavailable` — never
        returns a fabricated or partial value — when no usable estimate can
        be produced. The caller (the strategy's entry gate) must block
        entry and record an incident on this exception; it must never
        suppress a risk-reducing exit on it (spec section 3.7 is an
        entry-only gate).
        """
        evaluated_at = now if now is not None else self._now()

        if self._basket_margin_fetcher is not None:
            return self._from_basket_fetcher(
                legs, allocated_capital=allocated_capital, now=evaluated_at
            )

        if self._margin_fetcher is not None:
            return self._from_fetcher(legs, allocated_capital=allocated_capital, now=evaluated_at)

        if self._fallback_model is not None:
            total = self._fallback_model.estimate(legs, spot=spot)
            return MarginEstimate(
                estimated_margin=total,
                source="conservative_model_v1",
                estimated_at=evaluated_at,
                allocated_capital=allocated_capital,
            )

        raise MarginUnavailable(
            "no margin_fetcher configured and no fallback_model was explicitly injected — "
            "this is the fail-closed default; a caller that wants an offline/test estimate "
            "must inject ConservativeMarginModel itself"
        )

    def _from_basket_fetcher(
        self, legs: list[LegMarginRequest], *, allocated_capital: float, now: datetime
    ) -> MarginEstimate:
        """One hedged call. A failure raises rather than quietly re-summing.

        Falling back to ``margin_fetcher`` here would be the worst possible
        behaviour: it would silently restore the naked per-leg total, which
        for a defined-risk basket reads as three to four times the real
        requirement, and block entry with no explanation — precisely the
        failure this whole path exists to end. The caller blocks entry and
        records an incident on ``MarginUnavailable``, so a broken basket
        source is loud.
        """
        assert self._basket_margin_fetcher is not None
        try:
            basket = self._basket_margin_fetcher(legs)
            total = basket.total_margin
            if total is None or not math.isfinite(total) or total < 0:
                raise MarginUnavailable(
                    f"multi-leg margin calculator returned an invalid total: {total!r}"
                )
        except MarginUnavailable:
            raise
        except Exception as exc:
            _log.warning("multi-leg margin-calculator fetch failed: %s", exc)
            raise MarginUnavailable(f"multi-leg margin-calculator fetch failed: {exc}") from exc

        estimate = MarginEstimate(
            estimated_margin=float(total),
            source="dhan_margin_calculator_multi",
            estimated_at=now,
            allocated_capital=allocated_capital,
            components=basket.components,
        )
        if not estimate.is_fresh(now=now, max_age_seconds=self._max_age_seconds):
            raise MarginUnavailable(
                f"margin estimate is stale (age={estimate.age_seconds(now=now):.1f}s > "
                f"{self._max_age_seconds:.1f}s)"
            )
        return estimate

    def _from_fetcher(
        self, legs: list[LegMarginRequest], *, allocated_capital: float, now: datetime
    ) -> MarginEstimate:
        assert self._margin_fetcher is not None
        per_leg: list[tuple[str, float]] = []
        total = 0.0
        try:
            for leg in legs:
                value = self._margin_fetcher(leg)
                if value is None or not math.isfinite(value) or value < 0:
                    raise MarginUnavailable(
                        f"margin-calculator returned an invalid value for {leg.security_id}: "
                        f"{value!r}"
                    )
                per_leg.append((leg.security_id, float(value)))
                total += float(value)
        except MarginUnavailable:
            raise
        except Exception as exc:
            # A configured live source that fails always blocks — it is
            # never silently downgraded to the offline model, whether or
            # not one happens to be set (see this class's own docstring).
            _log.warning("real margin-calculator fetch failed: %s", exc)
            raise MarginUnavailable(f"real margin-calculator fetch failed: {exc}") from exc

        estimate = MarginEstimate(
            estimated_margin=total,
            source="dhan_margin_calculator_summed_legs",
            estimated_at=now,
            allocated_capital=allocated_capital,
            per_leg=tuple(per_leg),
        )
        if not estimate.is_fresh(now=now, max_age_seconds=self._max_age_seconds):
            raise MarginUnavailable(
                f"margin estimate is stale (age={estimate.age_seconds(now=now):.1f}s > "
                f"{self._max_age_seconds:.1f}s)"
            )
        return estimate
