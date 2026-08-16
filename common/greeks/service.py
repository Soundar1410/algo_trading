"""``GreeksService`` — the one door every strategy's Greek requirement goes
through (spec section 4.2). Dhan option-chain Greeks first, when the
response is complete/correctly mapped/fresh; the vetted fallback model
(:mod:`common.greeks.model`) second; :class:`~common.greeks.models.
GreeksUnavailable` when neither produces a usable value — which the caller
must treat as a block on entry/normal-adjustment risk, never on an exit.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from common.logging import get_logger
from common.market_data.chain_view import ChainPayloadError, ChainView, parse_chain_payload
from common.market_data.option_chain import ChainSnapshot, OptionChainService
from common.models import OptionType

from .model import GreeksModelUnavailable, black_scholes_merton_greeks
from .models import GreekInputs, GreekSnapshot, GreekSource, GreeksUnavailable

_log = get_logger(__name__)

#: Spec section 3.6 / 4.2: a decision-grade Greek/quote snapshot older than
#: this is stale and must block a risk-increasing decision.
DEFAULT_MAX_GREEK_AGE_SECONDS = 5.0


@dataclass(frozen=True)
class ModelAssumptions:
    """Configuration/version-controlled rate and carry assumptions for the
    fallback model — never hardcoded inline at the call site (spec section
    4.2: "rate and dividend assumptions are configuration/version
    controlled")."""

    risk_free_rate: float
    dividend_yield: float = 0.0


class GreeksService:
    """Resolves one option's Greeks, chain-first, model-second.

    Every candidate leg in one entry/adjustment evaluation must share a
    single, consistent snapshot (spec section 4.2) — a caller evaluating
    several legs for the same decision therefore calls
    :meth:`chain_snapshot` **once** and passes the resulting
    :class:`~common.market_data.chain_view.ChainView` into :meth:`resolve`
    for every leg, rather than letting each leg fetch its own.
    """

    def __init__(
        self,
        chain_service: OptionChainService,
        *,
        assumptions: ModelAssumptions,
        max_age_seconds: float = DEFAULT_MAX_GREEK_AGE_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._chain_service = chain_service
        self._assumptions = assumptions
        self._max_age_seconds = max_age_seconds
        self._now: Callable[[], datetime] = clock if clock is not None else self._utc_now

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(UTC)

    def chain_snapshot(
        self, *, underlying_security_id: int, underlying_segment: str, expiry: str
    ) -> ChainView:
        """Fetch (or reuse the cached) chain snapshot for one underlying/
        expiry — the shared basis for every leg in one decision."""
        snapshot: ChainSnapshot = self._chain_service.get(
            underlying_security_id, underlying_segment, expiry
        )
        try:
            return parse_chain_payload(
                snapshot.payload,
                snapshot_at=snapshot.snapshot_at,
                received_at=snapshot.received_at,
            )
        except ChainPayloadError as exc:
            raise GreeksUnavailable(f"option chain payload could not be parsed: {exc}") from exc

    def resolve(
        self,
        *,
        chain: ChainView,
        security_id: str,
        option_type: OptionType,
        strike: float,
        spot: float,
        expiry_at: datetime,
    ) -> GreekSnapshot:
        """One option's Greeks — chain first, model second, both subject to
        the same freshness gate.

        Raises :class:`~common.greeks.models.GreeksUnavailable` when neither
        source produces a usable, fresh, finite snapshot. The caller decides
        what "unusable" means for its own action (block entry/adjustment;
        never block an exit) — this method never makes that judgement call
        itself, it only ever returns a good snapshot or raises.
        """
        now = self._now()
        chain_snapshot = self._from_chain(
            chain=chain, security_id=security_id, option_type=option_type, strike=strike, now=now
        )
        if chain_snapshot is not None:
            return chain_snapshot

        try:
            model_inputs = GreekInputs(
                spot=spot,
                strike=strike,
                option_type=option_type,
                implied_volatility=self._implied_volatility_for_model(
                    chain=chain, option_type=option_type, strike=strike
                ),
                risk_free_rate=self._assumptions.risk_free_rate,
                dividend_yield=self._assumptions.dividend_yield,
                evaluation_timestamp=now,
                expiry_at=expiry_at,
            )
            return black_scholes_merton_greeks(model_inputs, security_id=security_id, now=now)
        except GreeksModelUnavailable as exc:
            raise GreeksUnavailable(
                f"no usable Greeks for {security_id} ({option_type.value} {strike}): "
                f"chain source unavailable/stale/incomplete, and the fallback model "
                f"could not produce one: {exc}"
            ) from exc

    def _from_chain(
        self,
        *,
        chain: ChainView,
        security_id: str,
        option_type: OptionType,
        strike: float,
        now: datetime,
    ) -> GreekSnapshot | None:
        row = chain.strike(strike)
        if row is None:
            return None
        quote = row.side(option_type)
        # has_complete_greeks (Phase 4A correction) already requires a
        # present, finite, positive implied_volatility on top of every
        # Greek-sanity check it performs — no separate IV check needed
        # here.
        if not quote.has_complete_greeks:
            return None
        snapshot = GreekSnapshot(
            security_id=security_id,
            option_type=option_type,
            strike=strike,
            delta=quote.delta,  # type: ignore[arg-type]
            gamma=quote.gamma,  # type: ignore[arg-type]
            theta=quote.theta,  # type: ignore[arg-type]
            vega=quote.vega,  # type: ignore[arg-type]
            implied_volatility=quote.implied_volatility,  # type: ignore[arg-type]
            source=GreekSource.BROKER_CHAIN,
            source_timestamp=chain.snapshot_at,
            received_at=chain.received_at,
        )
        if not snapshot.is_fresh(now=now, max_age_seconds=self._max_age_seconds):
            _log.info(
                "chain Greeks for %s stale (age=%.1fs > %.1fs); falling back to the model",
                security_id,
                snapshot.age_seconds(now=now),
                self._max_age_seconds,
            )
            return None
        return snapshot

    def _implied_volatility_for_model(
        self, *, chain: ChainView, option_type: OptionType, strike: float
    ) -> float:
        """The IV the fallback model prices with — read from the chain's own
        reported IV for this strike when available (the same market
        expectation the chain's own Greeks would have used), never a
        hardcoded constant. Raises if the chain has no usable IV either —
        the model has no independent way to estimate one without a market
        price to invert, which is out of this service's scope."""
        row = chain.strike(strike)
        if row is not None:
            iv = row.side(option_type).implied_volatility
            # Dhan reports implied_volatility as a percentage (e.g. 15.2 means
            # 15.2%, per this module's own documented payload shape in
            # common.market_data.chain_view) — always converted to a fraction
            # here, never assumed already-fractional.
            if iv is not None and iv > 0:
                return iv / 100.0
        raise GreeksModelUnavailable(
            f"no usable implied_volatility on the chain for strike {strike} — the fallback "
            "model has no independent way to estimate one"
        )
