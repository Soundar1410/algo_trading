"""Simulated execution.

Phase 4 Part 5 closes deviation D11 and runbook limitation 5. Phase 1 shipped the
honest subset — a fill at the submission-time quote plus flat slippage, a *recorded*
latency, and one rejection rule — and said so, because the only tape available then
carried no depth. Part 1 made a real ``NSE_FNO`` option resolvable and Part 5 put
Full mode on the feed, so the model can now be built against a real book.

What it does now
----------------
* **Prices against the touch.** A buy fills from the ask and a sell from the bid,
  each moved adversely by configured slippage and then rounded **away** from you
  to a valid tick. Falling back to last price is allowed only under the four
  conditions the spec attaches to it (section 5.1), one of which — a *conservative
  additional* slippage — Phase 1 did not implement at all.
* **Selects a quote at a latency deadline** rather than merely recording a latency
  number. See "What latency can and cannot mean here" below; this is honest about
  where it applies and where it does not, per fill.
* **Rests limit orders** in a working book and settles them from quotes that
  arrive *after* submission (section 5.3), which is the only way a limit fill can
  mean anything.
* **Supports partial fills** through a deterministic hook, so the model has them
  even though the default policy fills the whole quantity (section 5.4).
* **Applies nine rejection rules** (section 5.5), each carrying a
  :class:`PaperRejectionCode` so a test or a dashboard can count them without
  parsing prose.

Slippage is adverse in both directions by construction: a buy fills higher and a
sell lower. A simulator that can accidentally fill you at a better price is worse
than useless, because it makes a losing strategy look profitable. The tick
rounding follows the same rule, and the limit model refuses price improvement for
it: see :meth:`PaperBroker._settle`.

What latency can and cannot mean here (deviation D48)
------------------------------------------------------
Spec 5.2 asks the simulator to use "the quote available after the simulated
latency, not the signal-time quote". At the moment ``submit`` is called on a live
feed, that quote **does not exist yet** — the deadline is 250 ms in the future and
no amount of modelling conjures a price from it. The alternatives were to block the
feed callback thread for a quarter of a second, or to make the whole chain from
``PositionManager`` down asynchronous, which is a redesign of the Phase 3
execution port rather than a widening of this class.

So: a market order asks :class:`~common.broker.quotes.QuoteBook` for the first
quote at or after its deadline, uses it when one exists, and otherwise fills at the
submission quote. Every fill records which happened in ``Fill.latency_applied``, so
"latency was applied" is a per-fill fact rather than a claim the configuration
makes on the model's behalf. On a live feed a market order is nearly always the
second case; on a replayed tape and for every resting limit order it is the first.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from common.models import Fill, Order, OrderIntent, OrderStatus, OrderType, RiskDecision, Side

from .base import BrokerError, BrokerPosition, Quote
from .costs import ChargesCalculator, CostRates
from .quotes import QuoteBook

#: Tolerance for tick arithmetic. ``187.50 / 0.05`` is ``3749.999999999999`` in
#: binary floating point, so a price already sitting exactly on a tick would
#: otherwise be pushed a whole tick further away by the adverse rounding below.
#: Twelve orders of magnitude below one tick step, and three above the relative
#: error at these magnitudes.
_TICK_EPSILON = 1e-9


class PaperRejectionCode(StrEnum):
    """Why the simulator refused an order — spec section 5.5's nine rules.

    A code rather than only prose because ``orders.rejection_reason`` is a free
    TEXT column and counting rejections by reading English is how a dashboard
    starts disagreeing with a test. The code is prefixed onto the message, so the
    persisted string carries it without a schema change.
    """

    INVALID_INSTRUMENT = "INVALID_INSTRUMENT"
    INVALID_QUANTITY = "INVALID_QUANTITY"
    #: The spec names this "invalid tick-size price". It covers every price the
    #: simulator will not trade at: a limit price off the tick grid, a limit price
    #: that is not positive, a simulated fill driven to zero or below by an
    #: extreme slippage setting, and a ticks-based slippage configured for an
    #: instrument whose tick nobody knows.
    INVALID_TICK_PRICE = "INVALID_TICK_PRICE"
    STALE_QUOTE = "STALE_QUOTE"
    NO_DEPTH = "NO_DEPTH"
    MARKET_CLOSED = "MARKET_CLOSED"
    RISK_BLOCKED = "RISK_BLOCKED"
    DUPLICATE_CORRELATION_ID = "DUPLICATE_CORRELATION_ID"
    INJECTED_FAILURE = "INJECTED_FAILURE"


class PaperRejection(BrokerError):
    """Raised when the simulator refuses an order."""

    def __init__(self, code: PaperRejectionCode, message: str) -> None:
        super().__init__(f"{code.value}: {message}")
        self.code = code


#: What the exchange says about one instrument. Supplied by the caller because the
#: broker must not reach into the scrip master itself — see
#: :class:`PaperBroker`'s ``instrument_rules``.
@dataclass(frozen=True)
class InstrumentRules:
    """Lot size, tick size and quantity ceiling for one tradable instrument."""

    lot_size: int | None = None
    tick_size: float | None = None
    #: Exchange freeze quantity, or any configured ceiling. ``None`` disables it.
    max_quantity: int | None = None


#: ``security_id`` -> its exchange rules, or ``None`` when the instrument is not
#: recognised at all. Returning ``None`` is what triggers ``INVALID_INSTRUMENT``,
#: so a lookup that cannot answer must not be wired in as one that can.
InstrumentRulesLookup = Callable[[str], InstrumentRules | None]

#: How one order's quantity is broken into fills. Returns the fill sizes in order.
#: The default returns the whole quantity as a single fill; a test hook returns
#: several, or fewer than the whole, to exercise the partial-fill model.
FillQuantityPolicy = Callable[[OrderIntent, Quote], Sequence[int]]

_SLIPPAGE_MODES = frozenset({"ticks", "points", "basis_points"})


@dataclass(frozen=True)
class SlippageConfig:
    """Spec section 6's slippage block, options only.

    The spec offers two shapes — ``ticks`` for options and ``basis_points`` for
    stocks — and lists ``points`` nowhere; ``points`` exists here because it is
    what Phase 1's ``slippage_points`` meant, and dropping it would silently
    change every existing configuration's fills. All three are accepted; ``ticks``
    is the default because it is what the spec names for options and because a
    whole number of ticks keeps a fill on the tick grid by construction.
    """

    mode: str = "ticks"
    market_order_ticks: float = 1.0
    market_order_points: float = 0.05
    market_order_bps: float = 2.0

    def __post_init__(self) -> None:
        if self.mode not in _SLIPPAGE_MODES:
            raise ValueError(
                f"Unknown slippage mode {self.mode!r}; expected one of {sorted(_SLIPPAGE_MODES)}"
            )
        for name in ("market_order_ticks", "market_order_points", "market_order_bps"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must not be negative")

    @classmethod
    def from_mapping(cls, values: dict[str, Any] | None) -> SlippageConfig:
        """Build from the spec's nested block, which is keyed by asset class.

        ``{"options": {...}}`` is the only key accepted. ``stocks`` is refused
        rather than ignored: intraday stocks are Phase 5, and quietly accepting a
        block that does nothing is how a configuration comes to describe behaviour
        the system does not have.
        """
        if not values:
            return cls()
        unknown = set(values) - {"options"}
        if unknown:
            raise ValueError(
                f"Unknown slippage asset class(es) {sorted(unknown)}. Only 'options' is "
                "supported; stocks arrive with Phase 5."
            )
        return cls(**(values.get("options") or {}))

    def amount(self, *, base_price: float, tick_size: float | None) -> float:
        """The adverse move to apply to ``base_price``, in price points."""
        if self.mode == "points":
            return self.market_order_points
        if self.mode == "basis_points":
            return base_price * self.market_order_bps / 10_000.0
        if tick_size is None:
            raise PaperRejection(
                PaperRejectionCode.INVALID_TICK_PRICE,
                "slippage is configured in ticks but no tick size is known for this "
                "instrument; set paper_execution.tick_size or use mode: points",
            )
        return self.market_order_ticks * tick_size


@dataclass(frozen=True)
class PaperFillConfig:
    """Fill-model configuration. Values come from strategy YAML."""

    slippage: SlippageConfig = field(default_factory=SlippageConfig)
    submission_latency_ms: int = 250
    #: A quote older than this is refused (spec 5.1, "the quote is fresh"; spec
    #: section 10, a stale price is never treated as a fresh unchanged one).
    #:
    #: **Off by default, and that is a trade-off rather than an oversight.** The
    #: check compares the quote's exchange timestamp against the wall clock, which
    #: is meaningful on a live feed and meaningless on a replayed tape — where
    #: every timestamp is historical and *every* order would be refused. Tape
    #: replay is this repository's default execution path, and a setting that
    #: breaks every replay is one that gets switched off everywhere and then
    #: protects nothing. **Set it in any live-feed configuration**; the runbook
    #: records this as deviation D53.
    max_quote_age_ms: int | None = None
    #: Enforced tick size when the instrument lookup does not supply one. Defaults
    #: to the NSE/BSE index-option tick, which is what this runtime trades.
    #: ``None`` disables tick rounding and the tick-price rejection entirely.
    tick_size: float | None = 0.05
    #: Whether a fill may fall back to last price when there is no bid/ask.
    allow_ltp_fallback: bool = True
    #: The "conservative additional slippage rule" the spec attaches to that
    #: fallback (5.1) and Phase 1 never applied. In ticks, on top of the ordinary
    #: slippage, because a fill priced off a last trade rather than a resting
    #: order is a worse estimate and should cost more, not the same.
    ltp_fallback_extra_ticks: float = 1.0
    #: Whether to refuse orders outside the trading session. **Off by default, and
    #: that is deliberate**: the engine already gates *entries* on the session,
    #: while an exit or a square-off legitimately fires at or after the square-off
    #: time — so a broker that enforced this by default would refuse exactly the
    #: orders that must never be refused. Turn it on only with a session predicate
    #: that admits the square-off window.
    reject_when_market_closed: bool = False
    #: Correlation IDs to refuse on sight, for exercising the recovery path
    #: (spec 5.5, "configured failure injection"). Deterministic rather than
    #: probabilistic so a test asserts an outcome instead of a distribution.
    reject_correlation_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.submission_latency_ms < 0:
            raise ValueError("submission_latency_ms must not be negative")
        if self.max_quote_age_ms is not None and self.max_quote_age_ms <= 0:
            raise ValueError("max_quote_age_ms must be positive, or None to disable")
        if self.tick_size is not None and self.tick_size <= 0:
            raise ValueError("tick_size must be positive, or None to disable")
        if self.ltp_fallback_extra_ticks < 0:
            raise ValueError("ltp_fallback_extra_ticks must not be negative")

    @classmethod
    def from_mapping(cls, values: dict[str, Any] | None) -> PaperFillConfig:
        """Build from ``paper_execution:`` in strategy YAML.

        ``slippage_points`` is still accepted as Phase 1 spelt it, and maps onto
        ``mode: points``. Keeping it is not politeness: ``from_mapping`` refuses
        unknown keys, so dropping it would turn every pre-Part-5 strategy file
        into a startup failure. Supplying both spellings is refused, because there
        is no answer to which one was meant.
        """
        if not values:
            return cls()
        supplied = dict(values)
        legacy = supplied.pop("slippage_points", None)
        slippage = supplied.pop("slippage", None)
        if legacy is not None and slippage is not None:
            raise ValueError(
                "paper_execution sets both 'slippage_points' and 'slippage'. They are "
                "the same setting spelt two ways; keep one."
            )

        known = {f for f in cls.__dataclass_fields__}
        unknown = set(supplied) - known
        if unknown:
            raise ValueError(f"Unknown paper execution keys: {sorted(unknown)}")

        if legacy is not None:
            supplied["slippage"] = SlippageConfig(mode="points", market_order_points=float(legacy))
        else:
            supplied["slippage"] = SlippageConfig.from_mapping(slippage)
        if "reject_correlation_ids" in supplied:
            supplied["reject_correlation_ids"] = tuple(supplied["reject_correlation_ids"])
        return cls(**supplied)


def _whole_quantity(intent: OrderIntent, quote: Quote) -> Sequence[int]:
    """The default fill policy: one fill, the whole order."""
    return (intent.quantity,)


@dataclass
class _WorkingOrder:
    """A limit order resting in the book until a later quote makes it eligible."""

    intent: OrderIntent
    #: The exchange timestamp of the quote current at submission.
    submitted_at: datetime
    #: The earliest moment a quote may fill this order — submission plus latency.
    eligible_at: datetime


@dataclass
class PaperBroker:
    """Deterministic order simulator. Satisfies :class:`Broker`."""

    config: PaperFillConfig = field(default_factory=PaperFillConfig)
    charges: ChargesCalculator = field(default_factory=ChargesCalculator)
    #: Recent quotes, for latency selection and for settling resting orders.
    #: ``None`` means every market order fills at its submission quote and no
    #: limit order can ever fill — both recorded rather than hidden.
    quotes: QuoteBook | None = None
    #: Exchange rules per instrument. ``None`` leaves the instrument and lot-size
    #: rules inactive, which is what every offline construction wants: inventing a
    #: lot size to validate against would reject correct orders.
    instrument_rules: InstrumentRulesLookup | None = None
    #: Session predicate for the market-closed rule. Only consulted when
    #: ``config.reject_when_market_closed`` is set.
    is_market_open: Callable[[datetime], bool] | None = None
    #: How an order's quantity is split. The spec requires model support for
    #: partial fills with a test hook, not a default policy that produces them.
    fill_quantity_policy: FillQuantityPolicy = _whole_quantity
    _orders: dict[str, Order] = field(default_factory=dict, init=False)
    _working: dict[str, _WorkingOrder] = field(default_factory=dict, init=False)
    _fill_sequence: itertools.count[int] = field(
        default_factory=lambda: itertools.count(1), init=False
    )
    #: Market orders that could not be priced at a post-latency quote, i.e. the
    #: ordinary live case. Counted so D48's residual is a number someone can read
    #: rather than a paragraph someone has to remember.
    latency_not_applied: int = field(default=0, init=False)
    rejections: dict[PaperRejectionCode, int] = field(default_factory=dict, init=False)

    @classmethod
    def from_config(
        cls,
        *,
        paper_execution: dict[str, Any] | None = None,
        cost_rates: dict[str, Any] | None = None,
        quotes: QuoteBook | None = None,
        instrument_rules: InstrumentRulesLookup | None = None,
        is_market_open: Callable[[datetime], bool] | None = None,
    ) -> PaperBroker:
        return cls(
            config=PaperFillConfig.from_mapping(paper_execution),
            charges=ChargesCalculator(CostRates.from_mapping(cost_rates)),
            quotes=quotes,
            instrument_rules=instrument_rules,
            is_market_open=is_market_open,
        )

    @property
    def name(self) -> str:
        return "paper"

    def is_healthy(self) -> bool:
        return True

    def order_by_correlation_id(self, correlation_id: str) -> Order | None:
        return self._orders.get(correlation_id)

    @property
    def working_orders(self) -> tuple[str, ...]:
        """Correlation IDs of limit orders still resting. Public for assertions."""
        return tuple(self._working)

    # -------------------------------------------------- modify/cancel (Phase 10)
    def modify(
        self, correlation_id: str, *, quantity: int | None = None, limit_price: float | None = None
    ) -> Order:
        """Change a still-resting limit order's quantity and/or limit price.

        Only a resting order (:attr:`working_orders`) is modifiable — exactly
        matching what a real exchange allows and what :class:`DhanLiveBroker`
        can honestly support, so a strategy tested against paper cannot
        depend on a modify that live mode would refuse identically.
        """
        working = self._working.get(correlation_id)
        if working is None:
            raise BrokerError(
                f"cannot modify {correlation_id!r}: no resting order found "
                "(already filled, cancelled, or never existed)"
            )
        new_quantity = quantity if quantity is not None else working.intent.quantity
        new_price = limit_price if limit_price is not None else working.intent.limit_price
        self._working[correlation_id] = _WorkingOrder(
            intent=replace(working.intent, quantity=new_quantity, limit_price=new_price),
            submitted_at=working.submitted_at,
            eligible_at=working.eligible_at,
        )
        order = replace(self._orders[correlation_id], updated_at=datetime.now(UTC))
        self._orders[correlation_id] = order
        return order

    def cancel(self, correlation_id: str) -> Order:
        working = self._working.pop(correlation_id, None)
        if working is None:
            raise BrokerError(
                f"cannot cancel {correlation_id!r}: no resting order found "
                "(already filled, cancelled, or never existed)"
            )
        order = replace(
            self._orders[correlation_id],
            status=OrderStatus.CANCELLED,
            updated_at=datetime.now(UTC),
        )
        self._orders[correlation_id] = order
        return order

    # --------------------------------------------------- reconciliation reads
    def fetch_order_book(self) -> tuple[Order, ...]:
        return tuple(self._orders.values())

    def fetch_trades(self) -> tuple[Fill, ...]:
        return tuple(fill for order in self._orders.values() for fill in order.fills)

    def fetch_positions(self) -> tuple[BrokerPosition, ...]:
        """Always empty. Paper mode has no exchange-side position concept —
        that is :class:`~common.models.Position`'s job, tracked locally by
        the engine/repository, not simulated a second time here."""
        return ()

    # ------------------------------------------------------------------ submit
    def submit(self, intent: OrderIntent, quote: Quote) -> Order:
        """Validate, then fill a market order or rest a limit one."""
        rules = self._validate(intent, quote)

        if intent.order_type is OrderType.LIMIT:
            return self._rest(intent, quote)
        return self._fill_now(intent, quote, rules)

    def _fill_now(self, intent: OrderIntent, quote: Quote, rules: InstrumentRules) -> Order:
        selected, latency_applied = self._select_quote(quote)
        if not latency_applied:
            self.latency_not_applied += 1
        price, method = self._market_price(intent.side, selected, rules)
        order = self._build_order(
            intent,
            selected,
            fills=self._make_fills(
                intent,
                selected,
                price=price,
                method=method,
                latency_applied=latency_applied,
                reference_price=quote.last_price,
            ),
        )
        self._orders[intent.correlation_id] = order
        return order

    def _rest(self, intent: OrderIntent, quote: Quote) -> Order:
        """Accept a limit order into the working book, unfilled.

        Returned ``SUBMITTED`` with no fills, which is the truth: a limit order
        that filled off the quote current at submission would not be a limit order
        at all. :meth:`on_quote` settles it when an eligible price arrives.

        Nothing in the engine issues one — ``OrderLifecycle`` builds every intent
        as ``MARKET`` — and ``LifecycleGateway`` refuses a fill-less order, so this
        path cannot silently leak a phantom position into a strategy's book. It
        exists because spec 5.3 asks for the model and its test hooks.
        """
        self._working[intent.correlation_id] = _WorkingOrder(
            intent=intent,
            submitted_at=quote.quoted_at,
            eligible_at=quote.quoted_at + timedelta(milliseconds=self.config.submission_latency_ms),
        )
        order = Order(
            correlation_id=intent.correlation_id,
            strategy_id=intent.strategy_id,
            execution_mode=intent.execution_mode,
            status=OrderStatus.SUBMITTED,
            updated_at=datetime.now(UTC),
            broker_order_id=f"paper-{intent.correlation_id}",
        )
        self._orders[intent.correlation_id] = order
        return order

    # ------------------------------------------------------------- settlement
    def on_quote(self, quote: Quote) -> tuple[Order, ...]:
        """Offer one quote to the working book; return whatever it filled.

        Driven by the tick stream. A limit buy fills only when an eligible ask (or
        an eligible traded price, when the fallback is permitted) reaches or
        improves the limit *after* submission, and a limit sell mirrors it — spec
        5.3, and the reason the latency deadline and the resting book are the same
        mechanism rather than two.
        """
        filled: list[Order] = []
        for correlation_id in list(self._working):
            working = self._working[correlation_id]
            if working.intent.security_id != quote.security_id:
                continue
            if quote.quoted_at < working.eligible_at:
                continue
            order = self._settle(working, quote)
            if order is not None:
                del self._working[correlation_id]
                self._orders[correlation_id] = order
                filled.append(order)
        return tuple(filled)

    def _settle(self, working: _WorkingOrder, quote: Quote) -> Order | None:
        """Fill one resting order against ``quote``, or leave it resting.

        **The fill price is the limit price, never the better available one.** A
        real limit buy against an ask below the limit does get the improvement,
        but only if it reaches the front of the queue — and queue position is
        exactly what a simulator with top-of-book data cannot know. Assuming the
        improvement would hand every resting order a free edge, which is the same
        failure as favourable slippage.
        """
        intent = working.intent
        limit = intent.limit_price
        if limit is None:  # pragma: no cover - _validate refuses this at submission
            return None

        if intent.side is Side.BUY:
            candidate = quote.ask if quote.ask is not None else self._fallback_price(quote)
            eligible = candidate is not None and candidate <= limit
        else:
            candidate = quote.bid if quote.bid is not None else self._fallback_price(quote)
            eligible = candidate is not None and candidate >= limit
        if not eligible:
            return None

        return self._build_order(
            intent,
            quote,
            fills=self._make_fills(
                intent,
                quote,
                price=limit,
                method="limit",
                latency_applied=True,
                reference_price=quote.last_price,
            ),
        )

    def _fallback_price(self, quote: Quote) -> float | None:
        """Last price as a stand-in for a missing side, when that is permitted."""
        return quote.last_price if self.config.allow_ltp_fallback else None

    # -------------------------------------------------------------- validation
    def _validate(self, intent: OrderIntent, quote: Quote) -> InstrumentRules:
        """Run the rejection rules, in the order a real broker would.

        Returns the instrument's rules, which the fill model needs anyway — so the
        lookup happens once and the checks cannot drift from what is priced
        against.
        """
        if intent.correlation_id in self._orders:
            # Idempotency is the point: a retried submission must surface as a
            # duplicate rather than quietly producing a second position.
            self._refuse(
                PaperRejectionCode.DUPLICATE_CORRELATION_ID,
                f"an order with correlation ID {intent.correlation_id!r} has already "
                "been submitted",
            )
        if intent.correlation_id in self.config.reject_correlation_ids:
            self._refuse(
                PaperRejectionCode.INJECTED_FAILURE,
                f"{intent.correlation_id!r} is in paper_execution.reject_correlation_ids",
            )
        if intent.risk_decision is not RiskDecision.ALLOWED:
            self._refuse(
                PaperRejectionCode.RISK_BLOCKED,
                f"the intent arrived with risk_decision={intent.risk_decision.value} "
                f"({intent.risk_reason or 'no reason recorded'})",
            )
        if quote.security_id != intent.security_id:
            self._refuse(
                PaperRejectionCode.INVALID_INSTRUMENT,
                f"quote is for {quote.security_id!r} but the order is for {intent.security_id!r}",
            )

        rules = self._rules_for(intent.security_id)
        self._check_quantity(intent, rules)
        tick = self._tick_for(rules)
        self._check_limit_price(intent, tick)
        self._check_freshness(quote)
        self._check_session(quote)
        if not quote.has_depth and not self.config.allow_ltp_fallback:
            self._refuse(
                PaperRejectionCode.NO_DEPTH,
                f"no bid/ask for {quote.security_id!r} and LTP fallback is disabled",
            )
        return rules

    def _rules_for(self, security_id: str) -> InstrumentRules:
        """The instrument's exchange rules, refusing an unrecognised instrument.

        With no lookup wired in, every instrument is accepted and the rules that
        depend on one are simply not applied. That is the only safe default: a
        broker that rejected everything it could not verify would refuse every
        order in a runtime that has no scrip master, which is every offline test
        and every simulated-contract run.
        """
        if self.instrument_rules is None:
            return InstrumentRules()
        rules = self.instrument_rules(security_id)
        if rules is None:
            self._refuse(
                PaperRejectionCode.INVALID_INSTRUMENT,
                f"{security_id!r} is not a tradable instrument in the loaded instrument master",
            )
            raise AssertionError  # pragma: no cover - _refuse always raises
        return rules

    def _check_quantity(self, intent: OrderIntent, rules: InstrumentRules) -> None:
        if intent.quantity <= 0:  # pragma: no cover - OrderIntent has no such guard
            self._refuse(
                PaperRejectionCode.INVALID_QUANTITY,
                f"quantity {intent.quantity} is not positive",
            )
        if rules.lot_size is not None and intent.quantity % rules.lot_size:
            self._refuse(
                PaperRejectionCode.INVALID_QUANTITY,
                f"quantity {intent.quantity} is not a multiple of the {rules.lot_size} "
                f"lot size for {intent.security_id!r}",
            )
        if rules.max_quantity is not None and intent.quantity > rules.max_quantity:
            self._refuse(
                PaperRejectionCode.INVALID_QUANTITY,
                f"quantity {intent.quantity} exceeds the {rules.max_quantity} ceiling "
                f"for {intent.security_id!r}",
            )

    def _check_limit_price(self, intent: OrderIntent, tick: float | None) -> None:
        """Tick validation applies to a *submitted* price, so only to limits.

        A market order submits no price at all; the price it fills at is this
        class's own output and is put on the grid by :func:`_round_to_tick`
        rather than validated after the fact.

        With no tick known the rule is skipped rather than guessed at. That
        matters more than it looks: Dhan's ``SEM_TICK_SIZE`` is published in
        paise, so a value read at face value would make ₹0.05 look like ₹5 and
        refuse every order on the instrument.
        """
        if intent.order_type is not OrderType.LIMIT:
            return
        if intent.limit_price is None:
            self._refuse(
                PaperRejectionCode.INVALID_TICK_PRICE,
                "a limit order was submitted with no limit price",
            )
            raise AssertionError  # pragma: no cover - _refuse always raises
        if intent.limit_price <= 0:
            self._refuse(
                PaperRejectionCode.INVALID_TICK_PRICE,
                f"limit price {intent.limit_price} is not positive",
            )
        if tick is None:
            return
        steps = intent.limit_price / tick
        if abs(steps - round(steps)) > _TICK_EPSILON * max(1.0, abs(steps)):
            self._refuse(
                PaperRejectionCode.INVALID_TICK_PRICE,
                f"limit price {intent.limit_price} is not a multiple of the {tick} "
                f"tick size for {intent.security_id!r}",
            )

    def _check_freshness(self, quote: Quote) -> None:
        max_age = self.config.max_quote_age_ms
        if max_age is None:
            return
        age_ms = self._age_ms(quote)
        if age_ms is not None and age_ms > max_age:
            self._refuse(
                PaperRejectionCode.STALE_QUOTE,
                f"the quote for {quote.security_id!r} is {age_ms:.0f} ms old, over the "
                f"{max_age} ms limit; filling against it would treat a stale price as a "
                "fresh unchanged one",
            )

    def _check_session(self, quote: Quote) -> None:
        if not self.config.reject_when_market_closed or self.is_market_open is None:
            return
        if not self.is_market_open(quote.quoted_at):
            self._refuse(
                PaperRejectionCode.MARKET_CLOSED,
                f"the market is closed at {quote.quoted_at.isoformat()}",
            )

    @staticmethod
    def _age_ms(quote: Quote) -> float | None:
        """How old this quote is, in milliseconds, or ``None`` if it is not yet.

        A negative age means the quote's exchange timestamp is ahead of our clock,
        which happens with a small clock skew and on a replayed tape whose
        timestamps are in the future relative to the run. Neither is staleness, so
        neither is reported as an age.
        """
        age = (datetime.now(UTC) - quote.quoted_at).total_seconds() * 1000.0
        return age if age >= 0 else None

    def _refuse(self, code: PaperRejectionCode, message: str) -> None:
        self.rejections[code] = self.rejections.get(code, 0) + 1
        raise PaperRejection(code, message)

    # -------------------------------------------------------------- the fill
    def _select_quote(self, submission: Quote) -> tuple[Quote, bool]:
        """The quote to price against, and whether the latency deadline was met.

        See "What latency can and cannot mean here" in the module docstring: the
        deadline is in the future at submission time on a live feed, so this
        usually returns the submission quote and ``False``, which the fill records.
        """
        latency_ms = self.config.submission_latency_ms
        if self.quotes is None or latency_ms <= 0:
            return submission, False
        deadline = submission.quoted_at + timedelta(milliseconds=latency_ms)
        later = self.quotes.after(submission.security_id, deadline)
        if later is None:
            return submission, False
        return later, True

    def _market_price(self, side: Side, quote: Quote, rules: InstrumentRules) -> tuple[float, str]:
        """Price one market fill, always adversely."""
        tick = self._tick_for(rules)
        if quote.has_depth:
            assert quote.ask is not None and quote.bid is not None
            base = quote.ask if side is Side.BUY else quote.bid
            method = "bid_ask"
            extra = 0.0
        else:
            if not self.config.allow_ltp_fallback:  # pragma: no cover - _validate refuses
                self._refuse(
                    PaperRejectionCode.NO_DEPTH,
                    f"no bid/ask for {quote.security_id!r} and LTP fallback is disabled",
                )
            base = quote.last_price
            method = "ltp_fallback"
            # Spec 5.1's "conservative additional slippage rule". A price derived
            # from the last *trade* rather than from a resting order is a worse
            # estimate of what we would have paid, and must cost more than one
            # taken off the touch — otherwise the fallback is free and the
            # simulator quietly prefers illiquid instruments.
            extra = self.config.ltp_fallback_extra_ticks * (tick if tick is not None else 0.0)

        slippage = self.config.slippage.amount(base_price=base, tick_size=tick) + extra
        price = base + slippage if side is Side.BUY else base - slippage
        if tick is not None:
            price = _round_to_tick(price, tick, side)
        if price <= 0:
            self._refuse(
                PaperRejectionCode.INVALID_TICK_PRICE,
                f"simulated fill price {price} for {quote.security_id!r} is not positive",
            )
        return round(price, 4), method

    def _tick_for(self, rules: InstrumentRules) -> float | None:
        """The instrument's tick if the exchange supplied one, else the configured
        default. Instrument first: configuration is a fallback, not an override."""
        return rules.tick_size if rules.tick_size is not None else self.config.tick_size

    def _make_fills(
        self,
        intent: OrderIntent,
        quote: Quote,
        *,
        price: float,
        method: str,
        latency_applied: bool,
        reference_price: float,
    ) -> tuple[Fill, ...]:
        """Split the order into fills and price each one.

        Every fill of one order shares a price. Modelling a different price per
        slice would mean modelling depth consumption, which spec section 6
        explicitly defers ("later, allow optional quantity/depth-aware slippage;
        do not build a complex exchange simulator initially").
        """
        quantities = self._fill_quantities(intent, quote)
        filled_at = datetime.now(UTC)
        age_ms = self._age_ms(quote)
        return tuple(
            Fill(
                correlation_id=intent.correlation_id,
                broker_fill_id=f"paper-fill-{next(self._fill_sequence):06d}",
                strategy_id=intent.strategy_id,
                execution_mode=intent.execution_mode,
                quantity=quantity,
                price=price,
                filled_at=filled_at,
                reference_price=reference_price,
                slippage_amount=round(abs(price - reference_price), 6),
                latency_ms=self.config.submission_latency_ms,
                fill_method=method,
                charges=self.charges.total_for_leg(
                    side=intent.side, price=price, quantity=quantity
                ),
                latency_applied=latency_applied,
                quote_bid=quote.bid,
                quote_ask=quote.ask,
                quote_age_ms=age_ms,
            )
            for quantity in quantities
        )

    def _fill_quantities(self, intent: OrderIntent, quote: Quote) -> tuple[int, ...]:
        """Ask the policy how to split the order, and refuse an impossible answer.

        A policy returning more than the order's quantity would put a position on
        the book that was never requested, so it is a programming error here and
        not a rejection: no broker sent it, and no operator configured it.
        """
        quantities = tuple(int(q) for q in self.fill_quantity_policy(intent, quote))
        if not quantities or any(q <= 0 for q in quantities):
            raise ValueError(
                f"fill_quantity_policy returned {quantities!r} for {intent.correlation_id!r}; "
                "every fill must be a positive quantity"
            )
        if sum(quantities) > intent.quantity:
            raise ValueError(
                f"fill_quantity_policy returned {sum(quantities)} units for an order of "
                f"{intent.quantity}; a simulator must not over-fill"
            )
        return quantities

    def _build_order(self, intent: OrderIntent, quote: Quote, *, fills: tuple[Fill, ...]) -> Order:
        """Assemble the order from its fills, accumulating rather than overwriting.

        ``filled_quantity`` is the running total and ``average_fill_price`` the
        quantity-weighted mean, so a partially filled order reports what actually
        happened. ``ExecutionRepository.apply_fill`` does the same arithmetic on
        the persisted row; before Part 5 it wrote the *last* fill's values, which
        was invisible only because nothing produced two.
        """
        filled = sum(fill.quantity for fill in fills)
        average = sum(fill.price * fill.quantity for fill in fills) / filled
        status = OrderStatus.FILLED if filled >= intent.quantity else OrderStatus.PARTIALLY_FILLED
        return Order(
            correlation_id=intent.correlation_id,
            strategy_id=intent.strategy_id,
            execution_mode=intent.execution_mode,
            status=status,
            updated_at=fills[0].filled_at,
            broker_order_id=f"paper-{intent.correlation_id}",
            filled_quantity=filled,
            average_fill_price=round(average, 4),
            fills=fills,
        )


def _round_to_tick(price: float, tick: float, side: Side) -> float:
    """Put ``price`` on the tick grid, moving **away** from the trader.

    A buy rounds up and a sell rounds down, so rounding can never improve a fill.
    The epsilon keeps a price already sitting on a tick where it is; without it
    ``187.50`` would round up to ``187.55`` on a buy, because ``187.50 / 0.05`` is
    ``3749.999999999999`` in binary floating point.
    """
    steps = price / tick
    whole = (
        math.ceil(steps - _TICK_EPSILON) if side is Side.BUY else math.floor(steps + _TICK_EPSILON)
    )
    return round(whole * tick, 6)
