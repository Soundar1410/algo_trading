"""A momentarily-refused order must cost one tick, not the whole trading day.

The 2026-09-08 incident, in one line: a NIFTY weekly option quote arrived
2064 ms old against a 2000 ms freshness limit, the paper broker correctly
refused to fill against it, and the resulting ``GatewayExecutionError``
propagated straight out of ``TradingEngine.run()`` — killing the worker.
``st05_supertrend_buy`` died that way at 09:45 on an *entry*;
``st12_supertrend_buy`` at 10:50 on an *exit*. Both were then blocked for the
rest of the day, because the restart could not re-warm (a separate fault, fixed
alongside this one).

The refusal itself was right and is unchanged — a fabricated fill would record a
trade that never happened. What changed is the escalation: a *transient* cause is
retried on the next tick, and only a standing one, or a transient one that
outlasts its bound, is still fatal.

These drive the real ``TradingEngine`` with a gateway that refuses on demand,
rather than asserting on the classification helper alone — the bug was in the
engine's reaction, so that is what has to be pinned.
"""

from __future__ import annotations

import contextlib
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from common.broker.paper import (
    TRANSIENT_REJECTION_CODES,
    PaperRejectionCode,
    is_transient_rejection,
)
from common.engine.config import EngineConfig, SessionConfig
from common.engine.engine import (
    EXIT_RETRY_MAX_ATTEMPTS,
    EXIT_RETRY_MAX_SECONDS,
    TradingEngine,
)
from common.engine.feed import SimulatedFeed
from common.engine.gateway import GatewayExecutionError
from common.engine.positions import InMemoryGateway, PositionManager
from common.engine.selection import OptionSelector, SimulatedOptionChainResolver
from common.engine.session import MarketSession
from common.models import Candle, Tick
from common.warmup.manager import WarmupManager
from common.warmup.session_buckets import session_bucket_starts
from common.warmup.source import WarmupSource
from strategies.intraday_options.st12_supertrend_buy.strategy import SupertrendBuy1x1p2Strategy

IST = ZoneInfo("Asia/Kolkata")
UNDERLYING = "INDEX"
LOT_SIZE = 75
_SOURCE = WarmupSource(security_id="13", exchange_segment="IDX_I", instrument_type="INDEX")

#: The live tape both supertrend strategies actually traded on 2026-09-08's
#: shape: a flat warm-up, then a DOWN flip that buys a PE.
LIVE_CLOSES = [19500.0, 20200.0]


def _dt(h: int, m: int, s: int = 0) -> datetime:
    return datetime(2026, 8, 20, h, m, s, tzinfo=IST)


def _tick(security_id: str, price: float, ts: datetime) -> Tick:
    return Tick(
        security_id=security_id, instrument=security_id,
        last_price=price, exchange_time=ts, received_at=ts,
    )


class _RefusingGateway(InMemoryGateway):
    """An ordinary in-memory gateway that refuses the first ``refusals`` calls.

    ``retryable`` mirrors what the real gateway sets from the broker's rejection
    code, so this exercises the engine's branch rather than re-implementing it.
    """

    def __init__(self, *, refusals: int, retryable: bool, side: str = "buy", **kw: object) -> None:
        super().__init__(**kw)  # type: ignore[arg-type]
        self._left = refusals
        self._retryable = retryable
        self._side = side
        self.attempts = 0

    def _maybe_refuse(self, side: str, contract: object) -> None:
        if side != self._side:
            return
        self.attempts += 1
        if self._left > 0:
            self._left -= 1
            raise GatewayExecutionError(
                f"{side.upper()} of {contract} did not trade: the order was rejected: "
                "STALE_QUOTE: the quote is 2064 ms old, over the 2000 ms limit.",
                retryable=self._retryable,
            )

    def buy(self, contract, lots, **kw):  # type: ignore[no-untyped-def]
        self._maybe_refuse("buy", contract.security_id)
        return super().buy(contract, lots, **kw)

    def sell(self, contract, lots, **kw):  # type: ignore[no-untyped-def]
        self._maybe_refuse("sell", contract.security_id)
        return super().sell(contract, lots, **kw)


def _warmup_candles(session: MarketSession) -> list[Candle]:
    starts = (
        session_bucket_starts(session, date(2026, 8, 18), 5)
        + session_bucket_starts(session, date(2026, 8, 19), 5)
    )[-75:]
    return [
        Candle(
            security_id="13", instrument="NIFTY", open=20000.0, high=20000.0,
            low=20000.0, close=20000.0, volume=0,
            start_at=s, end_at=s + timedelta(minutes=5),
        )
        for s in starts
    ]


def _build(ticks, gateway):
    cfg = SessionConfig(
        timezone="Asia/Kolkata", start_time="09:15",
        end_time="15:15", square_off_time="15:20",
    )
    strategy = SupertrendBuy1x1p2Strategy()
    positions = PositionManager(gateway, lots=strategy.quantity_lots)
    candles = _warmup_candles(MarketSession(cfg))

    def _fetch(source, *, session, timeframe_minutes, lookback_sessions, now=None):
        return candles

    engine = TradingEngine(
        EngineConfig(timeframe="5m", session=cfg, warmup_from_history=True),
        feed=SimulatedFeed(list(ticks)),
        option_selector=OptionSelector(
            SimulatedOptionChainResolver("NIFTY", lot_size=LOT_SIZE), strike_step=50
        ),
        strategy=strategy,
        position_manager=positions,
        underlying_security_id=UNDERLYING,
        warmup_manager=WarmupManager(_fetch, max_lookback_sessions=3),
        warmup_source=_SOURCE,
        clock=lambda: _dt(9, 15),
    )
    return engine, positions


def _entry_tape(option_ticks: int) -> tuple[list[Tick], str]:
    """A DOWN flip that queues a PE, then ``option_ticks`` ticks for that PE."""
    ticks = [
        _tick(UNDERLYING, 19500.0, _dt(9, 16)),
        _tick(UNDERLYING, 20200.0, _dt(9, 21)),  # closes the bar -> flip -> pending PE
    ]
    pe = "SIM:NIFTY:WEEKLY:20200:PE"
    for i in range(option_ticks):
        ticks.append(_tick(pe, 120.0 + i, _dt(9, 21, 10 + i)))
    return ticks, pe


# ------------------------------------------------------- classification itself
def test_only_a_stale_quote_is_treated_as_transient():
    assert {PaperRejectionCode.STALE_QUOTE} == TRANSIENT_REJECTION_CODES
    assert is_transient_rejection("STALE_QUOTE: the quote is 2064 ms old") is True
    for code in PaperRejectionCode:
        if code is PaperRejectionCode.STALE_QUOTE:
            continue
        assert is_transient_rejection(f"{code.value}: whatever") is False, code


def test_an_unrecognised_reason_is_never_treated_as_transient():
    """Fail-loud is the default: a reason from another broker, free text, or
    nothing at all must not be retried on a guess."""
    for reason in (None, "", "some free text", "not a code: at all", "stale_quote: lowercase"):
        assert is_transient_rejection(reason) is False, reason


def test_a_gateway_error_defaults_to_not_retryable():
    """Every existing raise site keeps the old behaviour without being touched."""
    assert GatewayExecutionError("boom").retryable is False


# ------------------------------------------------------------------- entries
def test_a_transiently_refused_entry_fills_on_the_next_tick():
    """st05's 09:45 death: the entry was refused once and the worker ended the
    day. Now the pending entry simply waits for the next tick, which is what
    ``_pending`` already existed to do."""
    ticks, pe = _entry_tape(option_ticks=2)
    gateway = _RefusingGateway(refusals=1, retryable=True, side="buy", slippage_points=0.0)
    engine, positions = _build(ticks, gateway)

    engine.run()  # must not raise

    assert gateway.attempts == 2, "the entry was not retried"
    (position,) = positions.positions
    assert position.contract.security_id == pe
    assert position.quantity == 10 * LOT_SIZE


def test_a_non_transient_refused_entry_still_ends_the_session():
    """The guard on the fix: only a momentary cause is survivable. A standing
    one must still surface loudly rather than be retried forever."""
    ticks, _ = _entry_tape(option_ticks=2)
    gateway = _RefusingGateway(refusals=1, retryable=False, side="buy", slippage_points=0.0)
    engine, positions = _build(ticks, gateway)

    with pytest.raises(GatewayExecutionError):
        engine.run()
    assert positions.positions == []


def test_a_retrying_entry_still_cannot_open_after_the_entry_cutoff():
    """The retry re-evaluates ``session.can_enter`` every attempt, so a pending
    entry cannot ride a refusal past 15:15 and open late."""
    pe = "SIM:NIFTY:WEEKLY:20200:PE"
    ticks = [
        _tick(UNDERLYING, 19500.0, _dt(15, 6)),
        _tick(UNDERLYING, 20200.0, _dt(15, 11)),   # bar closes 15:11 -> pending PE
        _tick(pe, 120.0, _dt(15, 14, 50)),          # refused, still inside the window
        _tick(pe, 121.0, _dt(15, 16)),              # retry — now past the 15:15 cutoff
    ]
    gateway = _RefusingGateway(refusals=1, retryable=True, side="buy", slippage_points=0.0)
    engine, positions = _build(ticks, gateway)

    engine.run()

    assert positions.positions == [], "a refused entry opened after the cutoff"


# --------------------------------------------------------------------- exits
def _exit_tape(extra_ticks: int = 0) -> tuple[list[Tick], str]:
    """Open a PE, then drive a momentum premium exit on it.

    Premium candles: the 09:20-09:25 bucket closes at 125 with a low of 125, and
    the next closes at 98 — under that low, so the combined exit fires. Extra
    trailing ticks give a refused exit something to retry against.
    """
    pe = "SIM:NIFTY:WEEKLY:20200:PE"
    ticks = [
        _tick(UNDERLYING, 19500.0, _dt(9, 16)),
        _tick(UNDERLYING, 20200.0, _dt(9, 21)),
        _tick(pe, 120.0, _dt(9, 21, 10)),   # entry fills here
        _tick(pe, 125.0, _dt(9, 23)),
        _tick(pe, 98.0, _dt(9, 27)),        # closes bucket 1 at 125, low 125
        _tick(pe, 98.0, _dt(9, 32)),        # closes bucket 2 at 98 -> momentum exit
    ]
    for i in range(extra_ticks):
        ticks.append(_tick(pe, 98.0, _dt(9, 33 + i)))
    return ticks, pe


def test_a_transiently_refused_exit_closes_on_a_later_tick():
    """st12's 10:50 death: the *exit* was refused and the worker ended the day.
    The position must now close a tick later instead, still on its own reason."""
    ticks, pe = _exit_tape(extra_ticks=2)
    gateway = _RefusingGateway(refusals=1, retryable=True, side="sell", slippage_points=0.0)
    engine, positions = _build(ticks, gateway)

    engine.run()  # must not raise

    assert gateway.attempts >= 2, "the exit was not retried"
    assert positions.positions == [], "the position was never closed"
    (trade,) = positions.trades
    assert trade.contract.security_id == pe


def test_an_exit_refused_past_the_attempt_bound_still_fails_loudly():
    """The bound is what keeps this fail-closed: a refusal that keeps repeating
    is not momentary, whatever code it carries, and must surface."""
    ticks, _ = _exit_tape(extra_ticks=6)
    gateway = _RefusingGateway(
        refusals=99, retryable=True, side="sell", slippage_points=0.0
    )
    engine, _positions = _build(ticks, gateway)

    with pytest.raises(GatewayExecutionError):
        engine.run()

    assert gateway.attempts <= EXIT_RETRY_MAX_ATTEMPTS + 1, (
        f"retried {gateway.attempts} times; the bound is {EXIT_RETRY_MAX_ATTEMPTS}"
    )


def test_a_non_transient_refused_exit_is_fatal_on_the_first_attempt():
    """Unchanged behaviour, and the reason the two committed fail-loud exit
    tests still pass: an injected rejection is not transient, so it is never
    retried at all.

    ``attempts == 2`` rather than 1 because the *second* sell is not a retry —
    it is ``TradingEngine.run()``'s own exception handler forcing a square-off
    before shutdown, which is the pre-existing safety net and must keep running.
    The distinguishing fact is proven against the transient case below: with
    ``retryable=True`` and a gateway that never relents, the engine reaches its
    full retry bound; with ``retryable=False`` it stops immediately.
    """
    ticks, _ = _exit_tape(extra_ticks=4)
    gateway = _RefusingGateway(refusals=99, retryable=False, side="sell", slippage_points=0.0)
    engine, _positions = _build(ticks, gateway)

    with pytest.raises(GatewayExecutionError):
        engine.run()
    assert gateway.attempts == 2, (
        "a non-transient exit refusal must not be retried; expected the refused "
        "exit plus run()'s own square-off attempt, and nothing more"
    )


def test_a_non_transient_exit_stops_sooner_than_a_transient_one():
    """The two paths, measured against each other on an identical tape — the
    cleanest statement that ``retryable`` is what actually drives the retry."""
    attempts: dict[bool, int] = {}
    for retryable in (False, True):
        ticks, _ = _exit_tape(extra_ticks=6)
        gateway = _RefusingGateway(
            refusals=99, retryable=retryable, side="sell", slippage_points=0.0
        )
        engine, _positions = _build(ticks, gateway)
        with pytest.raises(GatewayExecutionError):
            engine.run()
        attempts[retryable] = gateway.attempts
    assert attempts[False] < attempts[True], attempts


def test_the_retry_bounds_are_the_approved_ones():
    assert EXIT_RETRY_MAX_ATTEMPTS == 3
    assert EXIT_RETRY_MAX_SECONDS == 10.0


# ------------------------------------------------- the reversal safety property
def test_a_deferred_reversal_close_never_opens_the_replacement_leg():
    """The invariant the exit retry must not break.

    A reversal is "close, then open". If the close is deferred for a retry and
    the engine opened the replacement anyway, the strategy would hold two legs at
    once — the one thing ``TradingEngine`` guarantees it never does, and a far
    worse outcome than the crash this fix removes.

    The tape flips DOWN (buy PE), then UP (close PE, buy CE) while every sell is
    transiently refused. The PE close is deferred, so the CE must not open.
    """
    pe = "SIM:NIFTY:WEEKLY:20200:PE"
    ce = "SIM:NIFTY:WEEKLY:20300:CE"
    ticks = [
        _tick(UNDERLYING, 19500.0, _dt(9, 16)),
        _tick(UNDERLYING, 20200.0, _dt(9, 21)),   # DOWN flip -> pending PE
        _tick(pe, 120.0, _dt(9, 21, 10)),          # PE opens
        _tick(UNDERLYING, 20300.0, _dt(9, 26)),   # UP flip -> reversal
        _tick(ce, 140.0, _dt(9, 26, 10)),          # the CE would fill here
    ]
    gateway = _RefusingGateway(refusals=99, retryable=True, side="sell", slippage_points=0.0)
    engine, positions = _build(ticks, gateway)

    # The run may or may not reach the retry bound on this short tape; either
    # way the property below must hold.
    with contextlib.suppress(GatewayExecutionError):
        engine.run()

    opened = [p.contract.security_id for p in positions.positions]
    assert ce not in opened, (
        "the replacement leg was opened while the old one was still open — "
        "a deferred reversal close must never be followed by an entry"
    )
    assert len(positions.positions) <= 1, f"more than one leg open at once: {opened}"


# --------------------------------------------- the hard square-off stays absolute
def test_the_hard_square_off_never_defers_a_refused_close():
    """The backstop must stay all-or-raise, even for a transient cause.

    ``_handle_square_off`` latches ``_squared_off`` and stops the feed
    immediately after closing, so there is no "next tick" left to retry on. If a
    deferral were allowed here the position would be carried overnight with
    nothing raised — silently worse than the crash this whole change removes.
    A transient refusal at square-off must therefore still raise.
    """
    pe = "SIM:NIFTY:WEEKLY:20200:PE"
    ticks = [
        _tick(UNDERLYING, 19500.0, _dt(9, 16)),
        _tick(UNDERLYING, 20200.0, _dt(9, 21)),
        _tick(pe, 120.0, _dt(9, 21, 10)),        # PE opens
        _tick(UNDERLYING, 20200.0, _dt(15, 21)), # past the 15:20 square-off
    ]
    gateway = _RefusingGateway(refusals=99, retryable=True, side="sell", slippage_points=0.0)
    engine, positions = _build(ticks, gateway)

    with pytest.raises(GatewayExecutionError):
        engine.run()

    # The failure was surfaced rather than swallowed; the position is left
    # exactly as the book has it, which is what restart recovery adopts.
    assert positions.trades == [], "a close was recorded that never filled"
