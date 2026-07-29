"""Transaction-cost calculation.

Rates are configuration, not code. The spec is explicit that current rates must
not be hard-coded into strategy logic — they change by regulation, and a paper
P&L computed with last year's STT is quietly wrong in a way no test will catch.

The defaults below are ordinary Indian equity-options retail rates and are
carried as *defaults for a simulation*, to be overridden from config. They are
not a claim about any particular broker's current tariff.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from common.models import Side


@dataclass(frozen=True)
class CostRates:
    """Per-leg cost inputs. All rates are fractions unless named otherwise."""

    brokerage_per_order: float = 20.0
    #: Applied to premium turnover.
    exchange_charge_rate: float = 0.0003503
    #: Securities transaction tax, charged on the sell leg only for options.
    stt_rate_on_sell: float = 0.000625
    sebi_charge_rate: float = 0.000001
    #: Goods and services tax, applied to brokerage plus exchange and SEBI fees.
    gst_rate: float = 0.18
    #: Stamp duty, charged on the buy leg only.
    stamp_duty_rate_on_buy: float = 0.00003

    @classmethod
    def from_mapping(cls, values: dict[str, Any] | None) -> CostRates:
        """Build from a config mapping, ignoring nothing silently."""
        if not values:
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(values) - known
        if unknown:
            raise ValueError(f"Unknown cost rate keys: {sorted(unknown)}")
        return cls(**values)


@dataclass(frozen=True)
class CostBreakdown:
    """Itemised so a dashboard can show why a paper fill cost what it did."""

    brokerage: float
    exchange_charges: float
    stt: float
    sebi_charges: float
    gst: float
    stamp_duty: float

    @property
    def total(self) -> float:
        return round(
            self.brokerage
            + self.exchange_charges
            + self.stt
            + self.sebi_charges
            + self.gst
            + self.stamp_duty,
            4,
        )


class ChargesCalculator:
    """Computes per-leg charges from configured rates."""

    def __init__(self, rates: CostRates | None = None) -> None:
        self._rates = rates or CostRates()

    @property
    def rates(self) -> CostRates:
        return self._rates

    def for_leg(self, *, side: Side, price: float, quantity: int) -> CostBreakdown:
        if price <= 0 or quantity <= 0:
            raise ValueError("charges require a positive price and quantity")

        turnover = price * quantity
        r = self._rates

        brokerage = min(r.brokerage_per_order, turnover)
        exchange = turnover * r.exchange_charge_rate
        sebi = turnover * r.sebi_charge_rate
        stt = turnover * r.stt_rate_on_sell if side is Side.SELL else 0.0
        stamp = turnover * r.stamp_duty_rate_on_buy if side is Side.BUY else 0.0
        gst = (brokerage + exchange + sebi) * r.gst_rate

        return CostBreakdown(
            brokerage=round(brokerage, 4),
            exchange_charges=round(exchange, 4),
            stt=round(stt, 4),
            sebi_charges=round(sebi, 4),
            gst=round(gst, 4),
            stamp_duty=round(stamp, 4),
        )

    def total_for_leg(self, *, side: Side, price: float, quantity: int) -> float:
        return self.for_leg(side=side, price=price, quantity=quantity).total
