"""Strike resolution: ITM / ATM / OTM CE/PE selection.

Ported from the reference repository's ``framework/market_data/option_selector.py``
plus the simulated resolver from its ``option_chain.py`` (Phase 3 Part 2b-i).

Given the underlying spot and the strike step, find the nearest strike and build
the CE/PE contract to trade. The mapping from strike+expiry to a broker
``security_id`` is broker-specific, so it comes from an injected
:class:`OptionChainResolver`.

ITM/OTM offsets are sign-sensitive per option type: for a **call**, ITM means a
*lower* strike and OTM a *higher* one; for a **put** it is reversed.
:func:`resolve_strike` encodes that once, so strategies never reason about the
sign themselves — they declare ``moneyness`` and ``steps``.

Two resolvers ship here. :class:`SimulatedOptionChainResolver` manufactures
consistent synthetic contracts for offline runs and remains the default.
:class:`DhanOptionChainResolver` resolves **real** contracts out of Dhan's daily
instrument master (:mod:`common.market_data.scrip_master`), which is what closed
runbook limitation 17 in Phase 4 Part 1.

Part 2b-i shipped the simulated resolver alone and recorded that the Dhan one
"needs the scrip master", pointing at :mod:`common.market_data.option_chain` as
the surface to wire in. That pointer was wrong and Part 1 corrected it: the
option-chain response is keyed by strike and carries no per-strike
``security_id``, so it cannot name a tradable contract at all. The chain service
keeps its own job — live per-strike quotes and greeks — and is not involved in
resolution. See :mod:`common.market_data.scrip_master` for the full reasoning.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from common.broker.paper import InstrumentRules

from .models import Moneyness, OptionContract, OptionType

if TYPE_CHECKING:  # annotation only — keeps the engine import graph unchanged
    from common.market_data.scrip_master import ScripMaster


def nearest_atm_strike(spot: float, strike_step: int, offset: int = 0) -> int:
    """Round ``spot`` to the nearest tradable strike, applying an optional offset.

    Args:
        spot: underlying price.
        strike_step: gap between strikes (e.g. 50 for NIFTY).
        offset: number of strikes away from ATM (0 = pure ATM). Applied as
            ``offset * strike_step``; the sign convention is the caller's
            responsibility — see :func:`resolve_strike` for the CE/PE-aware form.
    """
    if strike_step <= 0:
        raise ValueError("strike_step must be positive")
    base = round(spot / strike_step) * strike_step
    return int(base + offset * strike_step)


def resolve_strike(
    spot: float,
    strike_step: int,
    option_type: OptionType,
    moneyness: Moneyness = Moneyness.ATM,
    steps: int = 0,
) -> int:
    """Resolve a strike for the given moneyness, correct for CE vs PE.

    For a call: ITM = lower strike (negative offset), OTM = higher strike
    (positive offset). For a put the directions invert. ``steps`` is always a
    non-negative "how many strikes away from ATM"; this applies the correct sign.
    """
    if moneyness is Moneyness.ATM or steps == 0:
        offset = 0
    elif option_type is OptionType.CE:
        offset = steps if moneyness is Moneyness.OTM else -steps
    else:  # PE
        offset = -steps if moneyness is Moneyness.OTM else steps
    return nearest_atm_strike(spot, strike_step, offset)


class OptionChainResolver(ABC):
    """Resolves a (strike, option_type, expiry) into a tradable contract."""

    @abstractmethod
    def resolve(
        self, strike: int, option_type: OptionType, expiry: str | None = None
    ) -> OptionContract: ...


class SimulatedOptionChainResolver(OptionChainResolver):
    """Builds synthetic but consistent contracts for paper/offline trading."""

    def __init__(self, underlying: str, lot_size: int, default_expiry: str = "WEEKLY") -> None:
        self._underlying = underlying
        self._lot_size = lot_size
        self._default_expiry = default_expiry

    def resolve(
        self, strike: int, option_type: OptionType, expiry: str | None = None
    ) -> OptionContract:
        exp = expiry or self._default_expiry
        return OptionContract(
            symbol=f"{self._underlying} {exp} {strike} {option_type.value}",
            security_id=f"SIM:{self._underlying}:{exp}:{strike}:{option_type.value}",
            strike=float(strike),
            option_type=option_type,
            expiry=exp,
            lot_size=self._lot_size,
        )


class ContractNotListed(KeyError):
    """No contract exists for the requested strike/expiry.

    A distinct type because the engine must treat this as "do not enter" rather
    than as a crash: a strategy asking for a strike outside the listed band is a
    normal market condition near the edges of the chain, not a defect.
    """


class DhanOptionChainResolver(OptionChainResolver):
    """Resolves real Dhan contracts from the loaded instrument master.

    The expiry is fixed for the session at construction — to the nearest listed
    expiry unless one is given — so every contract a run selects belongs to the
    same series even if the run crosses an expiry boundary at midnight. Strike
    resolution is a dict lookup against the already-loaded master, so entering a
    position costs **no** API call and cannot be delayed or refused by a rate
    limit at the worst possible moment.
    """

    def __init__(self, scrip_master: ScripMaster, *, expiry: str | None = None) -> None:
        self._master = scrip_master
        self._expiry = expiry or scrip_master.nearest_expiry()
        self._log_expiry()

    def _log_expiry(self) -> None:
        from common.logging import get_logger

        get_logger(__name__).info(
            "option resolver using %s expiry %s", self._master.underlying, self._expiry
        )

    @property
    def expiry(self) -> str:
        return self._expiry

    @property
    def lot_size(self) -> int | None:
        """The exchange's lot size, for callers that must not trust configuration."""
        return self._master.lot_size

    def instrument_rules(self, security_id: str) -> InstrumentRules | None:
        """This instrument's exchange rules, or ``None`` if it is not listed.

        The lookup the paper broker's rejection rules take (Phase 4 Part 5). It
        lives here rather than in the broker because the broker must not know what
        a scrip master is — a ``Callable[[str], InstrumentRules | None]`` is the
        whole of the contract between them.

        ``tick_size`` comes from the exchange's own ``SEM_TICK_SIZE``, converted
        from paise. The fill model treats it as advisory and warns on
        disagreement rather than enforcing it, because that column's unit is not
        uniform across instrument types — see
        :data:`~common.market_data.scrip_master.TICK_SIZE_PAISE_PER_RUPEE`.
        """
        row = self._master.by_security_id(security_id)
        if row is None:
            return None
        return InstrumentRules(lot_size=row.lot_size, tick_size=row.tick_size)

    def resolve(
        self, strike: int, option_type: OptionType, expiry: str | None = None
    ) -> OptionContract:
        exp = expiry or self._expiry
        row = self._master.get(strike, option_type, exp)
        if row is None:
            listed = self._master.strikes_for_expiry(exp)
            bounds = f"{listed[0]:g}-{listed[-1]:g}" if listed else "none listed"
            raise ContractNotListed(
                f"No {self._master.underlying} {option_type.value} contract at strike "
                f"{strike} for expiry {exp} in the scrip master (listed strikes: {bounds})."
            )
        return OptionContract(
            symbol=row.symbol,
            security_id=row.security_id,
            strike=row.strike,
            option_type=row.option_type,
            expiry=row.expiry,
            lot_size=row.lot_size,
        )


class OptionSelector:
    """Picks a CE/PE contract for a given spot using a chain resolver.

    ``default_moneyness``/``default_steps`` come from the strategy's own
    ``option_selection``; a per-signal override carried on a
    :class:`~common.engine.models.StrategySignal` takes precedence when provided.
    """

    def __init__(
        self,
        resolver: OptionChainResolver,
        strike_step: int,
        *,
        default_moneyness: Moneyness = Moneyness.ATM,
        default_steps: int = 0,
        expiry: str | None = None,
    ) -> None:
        self._resolver = resolver
        self._strike_step = strike_step
        self._default_moneyness = default_moneyness
        self._default_steps = default_steps
        self._expiry = expiry

    @property
    def resolver(self) -> OptionChainResolver:
        """The chain resolver behind this selector.

        Public because the wiring needs to ask a *real* resolver for the exchange
        rules the paper broker validates against, and reaching into ``_resolver``
        from the runtime would be the same access with less honesty about it.
        """
        return self._resolver

    def select(
        self,
        spot: float,
        option_type: OptionType,
        moneyness: Moneyness | None = None,
        steps: int | None = None,
    ) -> OptionContract:
        strike = resolve_strike(
            spot,
            self._strike_step,
            option_type,
            moneyness if moneyness is not None else self._default_moneyness,
            steps if steps is not None else self._default_steps,
        )
        return self._resolver.resolve(strike, option_type, self._expiry)
