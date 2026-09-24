"""Value types for ``wsr1_weekly_stochrsi`` (spec sections 3, 4, 6, 9).

Phase 1 added the bars and the three operator input rows. Phase 3 adds what the
rules core (``rules.py``) takes and returns: its parameters, positions and
their fills, pending orders, closed trades, the drawdown-brake state, and the
decisions and funnel entries it reports.

**Money is ``Decimal``, rounded to the paisa.** A float level such as
``1000 * (1 - 0.108)`` is 891.9999…, not ₹892, and a weekly low of exactly
₹892 must count as a touch of L1. Indicator values stay floats (they are
compared with thresholds, never added to cash); prices from bars are converted
with :func:`money`, which goes through the float's shortest ``repr`` so ₹1226.40
stays exactly 1226.40.

Every type here is frozen and validates itself on construction. That is the
same judgement ``common.models.Candle`` makes and for the same reason: a bar
whose ``high`` is below its ``low`` is a source-data defect, and catching it
where the bar is built names the real problem, rather than letting it surface
five weeks later as an impossible ATR.

Deliberately **not** ``common.models.Candle``: that type is intraday-shaped
(``start_at``/``end_at`` datetimes, a minutes interval, a ``security_id`` and
an ``instrument``), and spec section 13 keeps weekly bars out of
``common/candles/`` and ``common/warmup/`` entirely — ``parse_timeframe_minutes``
rejects ``1d`` and ``1W``, and the whole warm-up stack buckets by intraday
minutes within one session.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum

from .iso_weeks import WeekKey, week_of

PAISA = Decimal("0.01")


def money(value: float | int | Decimal) -> Decimal:
    """``value`` as an exact ``Decimal``, rounded to the paisa.

    A float goes through ``str`` (its shortest repr), so a price parsed from a
    bar as ``1226.4`` becomes ``Decimal("1226.40")`` and not the binary
    expansion of that float.
    """
    if isinstance(value, float):
        if value != value:  # NaN
            raise ValueError("money() refuses NaN")
        value = Decimal(str(value))
    return Decimal(value).quantize(PAISA, rounding=ROUND_HALF_UP)


class BarError(ValueError):
    """A bar could not be built because its values are not internally consistent."""


def _validate_ohlc(open_: float, high: float, low: float, close: float, label: str) -> None:
    values = {"open": open_, "high": high, "low": low, "close": close}
    for name, value in values.items():
        if value != value:  # NaN is the one float that is not equal to itself
            raise BarError(f"{label}: {name} is NaN")
        if value <= 0:
            raise BarError(f"{label}: {name} must be positive, got {value}")
    if high < low:
        raise BarError(f"{label}: high {high} is below low {low}")
    if not (low <= open_ <= high):
        raise BarError(f"{label}: open {open_} is outside [{low}, {high}]")
    if not (low <= close <= high):
        raise BarError(f"{label}: close {close} is outside [{low}, {high}]")


@dataclass(frozen=True, slots=True)
class DailyBar:
    """One trading session, as Dhan's daily-candle endpoint reports it.

    ``session`` is the session's date in IST. There is no time component: the
    endpoint's timestamps are converted once, at parse time, and every
    downstream comparison in this strategy is date-to-date.
    """

    session: date
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def __post_init__(self) -> None:
        _validate_ohlc(self.open, self.high, self.low, self.close, f"daily bar {self.session}")
        if self.volume < 0:
            raise BarError(f"daily bar {self.session}: volume {self.volume} is negative")

    @property
    def traded_value(self) -> float:
        """Close x volume — the liquidity measure of spec 4.1, which says to use
        this when the source gives no traded value. Dhan's daily candle does not."""
        return self.close * self.volume


@dataclass(frozen=True, slots=True)
class WeeklyBar:
    """One completed ISO week, aggregated from that week's sessions (spec 3).

    ``week_ending`` is the date of the week's **last trading session**, not its
    Friday — spec section 3 defines it that way, and in a holiday week the two
    differ. ``sessions`` carries how many sessions the week actually had, so a
    short week stays visible to anything that cares.

    ``expected_last_session`` is what the *calendar* says the week should have
    ended on (spec 6.2 v1.2b), and it is here because the two answers
    disagreeing is the defect the Phase 1 review found. When Dhan has not yet
    published Friday's candle, a week built from the sessions that happen to be
    present ends on Thursday and looks perfectly well-formed. Carrying the
    calendar's answer next to the data's makes that condition a value
    (:attr:`is_truncated`) rather than something a later phase has to remember
    to re-derive.
    """

    week_ending: date
    iso_year: int
    iso_week: int
    open: float
    high: float
    low: float
    close: float
    #: The calendar's answer for this week — see :attr:`is_truncated`.
    expected_last_session: date
    volume: float = 0.0
    sessions: int = 0

    def __post_init__(self) -> None:
        label = f"weekly bar {self.iso_year}-W{self.iso_week:02d}"
        _validate_ohlc(self.open, self.high, self.low, self.close, label)
        if self.sessions <= 0:
            raise BarError(f"{label}: a weekly bar needs at least one session")
        if self.volume < 0:
            raise BarError(f"{label}: volume {self.volume} is negative")

    @property
    def iso_key(self) -> tuple[int, int]:
        """``(iso_year, iso_week)`` — the identity a week is compared on.

        Never ``week_ending``: two symbols' bars for the same week end on
        different dates when one of them did not trade on the week's last day.
        """
        return (self.iso_year, self.iso_week)

    @property
    def is_truncated(self) -> bool:
        """The week is missing the session the calendar expected it to end on.

        Either the vendor has not published it yet, or the exchange closed on a
        day the holiday list does not carry, or this symbol alone did not trade
        that session. All three are reportable and none of them is safe to
        decide on — spec 6.2 stops the run when the *index* is in this state
        and skips the symbol when only it is.

        A truncated bar is still built. Dropping it would leave a hole in the
        middle of a 260-week history that StochRSI(14) would compute straight
        through without noticing, which is a worse failure than a visible flag.
        """
        return self.week_ending < self.expected_last_session


class QualityStatus(Enum):
    """The operator's verdict for one symbol (spec 4.2, 6.4)."""

    PASS = "PASS"
    EVENT_RISK = "EVENT_RISK"
    FAIL = "FAIL"


class OnExit(Enum):
    """What happens to a held symbol the operator removed from the universe (spec 6.3)."""

    HOLD = "hold"
    EXIT = "exit"


@dataclass(frozen=True, slots=True)
class UniverseRow:
    """One row of ``universe.csv`` (spec 6.3)."""

    symbol: str
    isin: str
    company: str
    industry: str
    nifty100: bool
    group: str | None
    on_exit: OnExit
    as_of: date | None

    @property
    def effective_group(self) -> str:
        """The promoter group this symbol counts against for the limit of spec 4.12.

        A blank ``group`` means "its own group" (spec 6.3), so it resolves to
        the symbol itself — which keeps the per-group limit meaningful without
        the operator having to name a group for every standalone company.
        """
        return self.group or self.symbol


@dataclass(frozen=True, slots=True)
class QualityRow:
    """One row of ``quality_gate.csv`` (spec 6.4)."""

    symbol: str
    status: QualityStatus
    checked_on: date | None
    valid_until: date
    notes: str = ""

    def is_valid_on(self, execution_date: date) -> bool:
        """Spec 4.2: usable only while ``valid_until`` is on or after the
        execution date. Expiry alone decides this; ``status`` is a separate
        question the caller asks next."""
        return execution_date <= self.valid_until


@dataclass(frozen=True, slots=True)
class ResultsRow:
    """One row of ``results_calendar.csv`` (spec 6.5)."""

    symbol: str
    results_date: date


# ===========================================================================
# Phase 3 — the rules core's inputs and outputs (spec sections 4, 9, 12)
# ===========================================================================


class Regime(Enum):
    """Spec 4.3. ``UNKNOWN`` (v1.2f) blocks entries: an undefined input must
    never read as "not Red"."""

    NORMAL = "normal"
    RED = "red"
    UNKNOWN = "unknown"


class PositionState(Enum):
    OPEN = "OPEN"
    HALF_SOLD = "HALF_SOLD"
    CLOSED = "CLOSED"


class OrderAction(Enum):
    """Spec 9's ``stock_pending_orders.action`` vocabulary."""

    BUY_T1 = "BUY_T1"
    BUY_T2 = "BUY_T2"
    BUY_T3 = "BUY_T3"
    SELL_HALF = "SELL_HALF"
    SELL_ALL = "SELL_ALL"

    @property
    def is_buy(self) -> bool:
        return self in (OrderAction.BUY_T1, OrderAction.BUY_T2, OrderAction.BUY_T3)

    @property
    def tranche(self) -> int:
        """1, 2 or 3 for a buy. Raises for a sell."""
        tranches = {OrderAction.BUY_T1: 1, OrderAction.BUY_T2: 2, OrderAction.BUY_T3: 3}
        if self not in tranches:
            raise ValueError(f"{self.value} is not a buy")
        return tranches[self]

    @classmethod
    def buy(cls, tranche: int) -> OrderAction:
        return {1: cls.BUY_T1, 2: cls.BUY_T2, 3: cls.BUY_T3}[tranche]


class FunnelStage(Enum):
    """How far one symbol got through spec 4.5-4.7 in one week (spec 9's
    ``stock_signals.stage``), furthest last."""

    SKIPPED = "skipped"  # not evaluated: held, pending, stale, no data
    UNDEFINED = "undefined"  # K/D undefined in the window — flagged (4.4, 4.5)
    NOT_ARMED = "not_armed"
    ARMED = "armed"  # armed, no valid trigger this week
    FILTERED = "filtered"  # triggered, but a 4.6 filter failed
    NOT_TAKEN = "not_taken"  # passed every filter; a 4.7/4.12 cap refused it
    TAKEN = "taken"


@dataclass(frozen=True, slots=True)
class RulesParameters:
    """Spec 4.12 / section 12's values, paper-book defaults.

    Percentages are percentages (``committed_cap_pct=100`` means 100%), as in
    the configuration. Binding this to the YAML is Phase 5; the rules take the
    object, never the file.
    """

    capital: Decimal = Decimal("1000000")
    base_allocation: Decimal = Decimal("100000")
    max_positions: int = 10
    committed_cap_pct: Decimal = Decimal("100")
    buffer_pct: Decimal = Decimal("0")
    max_per_sector: int = 2
    max_per_group: int = 1
    # stoch / trigger (4.5)
    arm_level: float = 20.0
    arm_window_weeks: int = 8
    max_k_at_cross: float = 50.0
    overbought: float = 90.0
    # filters (4.1, 4.6)
    ema_trend: int = 50
    min_pct_of_52w_high: float = 60.0
    max_atr_pct: float = 12.0
    min_history_weeks: int = 200
    min_traded_value_cr: float = 20.0
    repeat_lookback_weeks: int = 26
    # sizing (4.8)
    spacing_floor_pct: Decimal = Decimal("10")
    spacing_atr_mult: Decimal = Decimal("1.5")
    tranches_pct: tuple[Decimal, Decimal, Decimal] = (Decimal("40"), Decimal("30"), Decimal("30"))
    stop_spacing_mult: int = 3
    event_risk_alloc_mult: Decimal = Decimal("0.5")
    # adds, exits, re-entry (4.9-4.11)
    add_ema: int = 200
    trail_ema: int = 10
    time_exit_weeks: int = 52
    trail_time_weeks: int = 52
    cooling_off_weeks: int = 26
    # regime (4.3)
    regime_ema: int = 40
    regime_slope_lookback_weeks: int = 4
    red_max_entries: int = 1
    normal_max_entries: int = 2
    # brakes (4.12)
    dd1_pct: Decimal = Decimal("10")
    dd1_pause_weeks: int = 4
    dd2_pct: Decimal = Decimal("20")
    brake_2_cleared_on: date | None = None
    # costs (8)
    cost_bps_buy: Decimal = Decimal("12")
    cost_bps_sell: Decimal = Decimal("11")
    fixed_cost_per_sell_rs: Decimal = Decimal("15")

    def __post_init__(self) -> None:
        if self.capital <= 0 or self.base_allocation <= 0:
            raise ValueError("capital and base_allocation must be positive")
        if sum(self.tranches_pct) != Decimal("100"):
            raise ValueError(f"tranches_pct must sum to 100, got {self.tranches_pct}")
        for name in (
            "max_positions",
            "arm_window_weeks",
            "dd1_pause_weeks",
            "repeat_lookback_weeks",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1")
        if not (Decimal(0) <= self.buffer_pct < Decimal(100)):
            raise ValueError("buffer_pct must be in [0, 100)")

    @property
    def committed_cap(self) -> Decimal:
        return money(self.capital * self.committed_cap_pct / 100)

    @property
    def buffer(self) -> Decimal:
        return money(self.capital * self.buffer_pct / 100)


@dataclass(frozen=True, slots=True)
class Sizing:
    """Spec 4.8, fixed for the whole trade at the trigger week's ATR%."""

    spacing: Decimal
    allocation: Decimal
    tranche_amounts: tuple[Decimal, Decimal, Decimal]
    event_risk: bool = False

    @property
    def max_tranches(self) -> int:
        """3, or 2 for an EVENT_RISK entry (T3 disabled)."""
        return 2 if self.event_risk else 3


@dataclass(frozen=True, slots=True)
class BuyFill:
    """One filled tranche. ``fees`` are the modelled buy costs (spec 8)."""

    tranche: int
    session: date
    price: Decimal
    shares: int
    fees: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if self.tranche not in (1, 2, 3):
            raise ValueError(f"tranche must be 1, 2 or 3, got {self.tranche}")
        if self.shares <= 0 or self.price <= 0:
            raise ValueError("a buy fill needs positive shares and price")

    @property
    def week(self) -> WeekKey:
        return week_of(self.session)

    @property
    def value(self) -> Decimal:
        return self.price * self.shares


@dataclass(frozen=True, slots=True)
class SaleFill:
    """One filled sale (``SELL_HALF`` or ``SELL_ALL``)."""

    action: OrderAction
    session: date
    price: Decimal
    shares: int
    fees: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if self.action.is_buy:
            raise ValueError("a sale fill needs a sell action")
        if self.shares <= 0 or self.price <= 0:
            raise ValueError("a sale fill needs positive shares and price")

    @property
    def week(self) -> WeekKey:
        return week_of(self.session)

    @property
    def value(self) -> Decimal:
        return self.price * self.shares


@dataclass(frozen=True, slots=True)
class Position:
    """One trade in one symbol, from its T1 fill until its last share is sold.

    ``p1``, ``l1``, ``l2`` and ``stop`` are fixed at the T1 fill and never
    recalculated (spec 4.8). ``touch_week`` is spec 4.9's touch memory for the
    *next* level: the first week, on or after the previous buy's fill week, whose
    low reached it — cleared by a close at or above P1 and consumed by a fill.
    ``sector`` and ``group`` are captured at entry, so the limits of spec 4.12
    still count a position whose symbol has since left the universe file.
    """

    position_id: str
    symbol: str
    sector: str
    group: str
    sizing: Sizing
    p1: Decimal
    l1: Decimal
    l2: Decimal
    stop: Decimal
    buys: tuple[BuyFill, ...]
    sales: tuple[SaleFill, ...] = ()
    state: PositionState = PositionState.OPEN
    touch_week: WeekKey | None = None
    #: v1.2f: a PASS entry whose row later turned EVENT_RISK keeps its sizing,
    #: but T3 is disabled from then on.
    t3_disabled: bool = False
    #: v1.2g: set only when a partial sale was due on a 1-share position, which
    #: sells nothing but still switches to the trail. It is the week a real
    #: SELL_HALF would have filled, so the trail-time clock matches any other.
    half_sold_week: WeekKey | None = None

    def __post_init__(self) -> None:
        if not self.buys or self.buys[0].tranche != 1:
            raise ValueError(f"{self.position_id}: a position starts with its T1 fill")
        if [buy.tranche for buy in self.buys] != list(range(1, len(self.buys) + 1)):
            raise ValueError(f"{self.position_id}: tranches must be filled in order")
        if self.shares_held < 0:
            raise ValueError(f"{self.position_id}: more shares sold than bought")
        if (self.state is PositionState.CLOSED) != (self.shares_held == 0):
            raise ValueError(f"{self.position_id}: CLOSED exactly when no shares are held")
        if self.state is PositionState.HALF_SOLD and self.partial_week is None:
            raise ValueError(f"{self.position_id}: HALF_SOLD needs a partial sale")
        if self.state is PositionState.OPEN and self.half_sold_week is not None:
            raise ValueError(f"{self.position_id}: an OPEN position has no half_sold_week")

    # ------------------------------------------------------------ shares
    @property
    def shares_bought(self) -> int:
        return sum(buy.shares for buy in self.buys)

    @property
    def shares_held(self) -> int:
        return self.shares_bought - sum(sale.shares for sale in self.sales)

    @property
    def tranches_used(self) -> int:
        return len(self.buys)

    @property
    def max_tranches(self) -> int:
        """3, or 2 for EVENT_RISK. A disabled T3 counts only while unfilled
        (v1.2g): once T3 has filled, the position commits its full A."""
        if self.t3_disabled and self.tranches_used < 3:
            return 2
        return self.sizing.max_tranches

    @property
    def next_level(self) -> Decimal | None:
        """L1 for T2, L2 for T3; ``None`` once no further add is possible."""
        if self.state is not PositionState.OPEN or self.tranches_used >= self.max_tranches:
            return None
        return self.l1 if self.tranches_used == 1 else self.l2

    # ------------------------------------------------------------- weeks
    @property
    def t1_fill_week(self) -> WeekKey:
        return self.buys[0].week

    @property
    def last_buy_week(self) -> WeekKey:
        return self.buys[-1].week

    @property
    def partial_week(self) -> WeekKey | None:
        """The SELL_HALF fill week, or ``half_sold_week`` after a 0-share partial."""
        sold = next((s.week for s in self.sales if s.action is OrderAction.SELL_HALF), None)
        return sold if sold is not None else self.half_sold_week

    @property
    def exit_week(self) -> WeekKey | None:
        return self.sales[-1].week if self.state is PositionState.CLOSED else None

    # ------------------------------------------------------------- money
    @property
    def buy_value(self) -> Decimal:
        return sum((buy.value for buy in self.buys), Decimal("0"))

    @property
    def average_cost(self) -> Decimal:
        """Average buy price, before fees."""
        return self.buy_value / self.shares_bought

    @property
    def cost_of_held(self) -> Decimal:
        """Spec 4.12: "the cost of the shares still held", at average cost."""
        return money(self.average_cost * self.shares_held)

    @property
    def fees(self) -> Decimal:
        buys = sum((buy.fees for buy in self.buys), Decimal("0"))
        return buys + sum((sale.fees for sale in self.sales), Decimal("0"))

    @property
    def sale_value(self) -> Decimal:
        return sum((sale.value for sale in self.sales), Decimal("0"))

    @property
    def committed(self) -> Decimal:
        """Spec 4.12: A (only the usable tranches for EVENT_RISK or a disabled
        T3) until the partial sale; afterwards the cost of the shares held."""
        if self.state is PositionState.OPEN:
            usable = self.sizing.tranche_amounts[: self.max_tranches]
            return sum(usable, Decimal("0"))
        if self.state is PositionState.HALF_SOLD:
            return self.cost_of_held
        return Decimal("0")

    @property
    def net_pnl(self) -> Decimal:
        """Realised P&L net of every modelled cost. Final once CLOSED."""
        return self.sale_value - self.buy_value - self.fees


@dataclass(frozen=True, slots=True)
class PendingOrder:
    """A decision to act at the next session's open (spec 9).

    A ``BUY_T1`` carries everything the fill needs to open the position;
    later buys carry the tranche amount, sells the share quantity.
    """

    action: OrderAction
    symbol: str
    decided_week: WeekKey
    execute_on_or_after: date
    reason: str
    position_id: str | None = None
    amount: Decimal | None = None
    quantity: int | None = None
    sizing: Sizing | None = None
    sector: str | None = None
    group: str | None = None

    def __post_init__(self) -> None:
        if self.action.is_buy:
            if self.amount is None or self.amount <= 0:
                raise ValueError(f"{self.action.value} {self.symbol}: a buy needs an amount")
        elif self.quantity is None or self.quantity <= 0:
            raise ValueError(f"{self.action.value} {self.symbol}: a sell needs a quantity")
        if self.action is OrderAction.BUY_T1:
            if self.sizing is None or self.sector is None or self.group is None:
                raise ValueError(f"BUY_T1 {self.symbol}: needs sizing, sector and group")
        elif self.position_id is None:
            raise ValueError(f"{self.action.value} {self.symbol}: needs its position_id")


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    """What spec 4.11 needs to remember about a finished trade."""

    symbol: str
    position_id: str
    exit_week: WeekKey
    net_pnl: Decimal

    @property
    def is_loss(self) -> bool:
        """Net of costs (v1.2f): slightly positive gross, negative net, is a loss."""
        return self.net_pnl < 0


@dataclass(frozen=True, slots=True)
class BrakeState:
    """Spec 4.12's drawdown brakes (v1.2f semantics), carried week to week."""

    peak: Decimal | None = None
    #: Brake 1 pauses entries through this ISO week, inclusive.
    brake1_until: WeekKey | None = None
    #: Brake 1 may fire only after equity has closed above 90% of peak since
    #: it last fired, so a long drawdown pauses once, not forever.
    brake1_can_fire: bool = True
    #: Brake 2 is active from this week-ending date until cleared.
    brake2_fired_on: date | None = None

    @property
    def brake2_active(self) -> bool:
        return self.brake2_fired_on is not None


@dataclass(frozen=True, slots=True)
class Book:
    """The paper book as the rules see it at the start of step 3 (spec 4.13).

    ``positions`` holds every position not yet CLOSED; ``closed`` every
    finished trade, for re-entry (4.11). ``pending`` lists orders still
    unfilled after step 1 (a symbol that did not trade), which the rules never
    stack a second order on.
    """

    cash: Decimal
    positions: tuple[Position, ...] = ()
    closed: tuple[ClosedTrade, ...] = ()
    pending: tuple[PendingOrder, ...] = ()
    brakes: BrakeState = field(default_factory=BrakeState)

    def __post_init__(self) -> None:
        symbols = [p.symbol for p in self.positions]
        if len(symbols) != len(set(symbols)):
            raise ValueError("one open position per symbol (spec 4.11)")
        if any(p.state is PositionState.CLOSED for p in self.positions):
            raise ValueError("a CLOSED position belongs in `closed`, not `positions`")

    def last_closed(self, symbol: str) -> ClosedTrade | None:
        trades = [t for t in self.closed if t.symbol == symbol]
        return max(trades, key=lambda t: t.exit_week) if trades else None


@dataclass(frozen=True, slots=True)
class FunnelEntry:
    """One symbol's progress through entries this week (spec 4.7, 9)."""

    symbol: str
    stage: FunnelStage
    reason: str
    rs: float | None = None
    flags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PositionReview:
    """What step 3 decided for one held position, and why."""

    position_id: str
    symbol: str
    reason: str
    order: PendingOrder | None = None
    flags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WeekDecision:
    """Everything one weekly decision produces (spec 4.13 steps 2-4)."""

    week: WeekKey
    regime: Regime
    equity: Decimal
    brakes: BrakeState
    orders: tuple[PendingOrder, ...]
    #: Held positions after step 3's bookkeeping (touch memory, T3 disabling).
    positions: tuple[Position, ...]
    reviews: tuple[PositionReview, ...]
    funnel: tuple[FunnelEntry, ...]
    #: Why no entry could be taken this week at all, if that is the case.
    entries_blocked: str | None = None
