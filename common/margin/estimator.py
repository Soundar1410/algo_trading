"""``MarginEstimator`` — the one door a strategy's margin-utilization gate
goes through (spec section 3.7/6.3). Real Dhan margin-calculator result,
summed per leg, in production; :class:`~common.margin.models.
MarginUnavailable` on any failure — never a fabricated number.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import UTC, datetime

from common.logging import get_logger

from .model import ConservativeMarginModel
from .models import LegMarginRequest, MarginEstimate, MarginUnavailable

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

    ``margin_fetcher`` — when provided — is called once per leg against the
    real, read-only Dhan margin-calculator endpoint
    (:func:`common.market_data.dhan_margin.build_dhan_margin_fetcher`) and
    the results are **summed**: a deliberately conservative choice (no
    cross-margin/hedge netting between legs is assumed), so the estimate
    never *understates* margin relative to what real hedge netting could
    achieve.

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
        fallback_model: ConservativeMarginModel | None = None,
        max_age_seconds: float = DEFAULT_MAX_MARGIN_AGE_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._margin_fetcher = margin_fetcher
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
