"""The recent-quote record behind the fill model (Phase 4 Part 5).

Small, but it decides two things the simulator's honesty rests on: which quote a
latency deadline selects, and whether a resting limit order can see a price that
arrived after it was submitted. Both are easy to get subtly wrong in the
*favourable* direction, which is the direction that flatters a strategy.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from common.broker import QuoteBook, quote_from_tick
from common.models import Tick

T0 = datetime(2026, 8, 5, 4, 30, 0, tzinfo=UTC)
SID = "49081"


def _tick(
    price: float,
    *,
    at: datetime,
    bid: float | None = None,
    ask: float | None = None,
    security_id: str = SID,
) -> Tick:
    return Tick(
        security_id=security_id,
        instrument="NIFTY 07 AUG 24000 CALL",
        last_price=price,
        exchange_time=at,
        received_at=at + timedelta(milliseconds=8),
        bid_price=bid,
        ask_price=ask,
    )


def test_a_tick_becomes_a_quote_with_its_book_intact():
    quote = quote_from_tick(_tick(187.45, at=T0, bid=187.40, ask=187.50))
    assert (quote.bid, quote.ask, quote.last_price) == (187.40, 187.50, 187.45)
    assert quote.has_depth


def test_the_quote_carries_exchange_time_not_receipt_time():
    """The market's own clock, matching what candles, sessions and square-off use
    since Part 3. A latency deadline is a statement about the market's timeline,
    and a staleness check should count the age of the *price* — which includes the
    time it spent reaching us."""
    tick = _tick(187.45, at=T0)
    assert quote_from_tick(tick).quoted_at == tick.exchange_time
    assert quote_from_tick(tick).quoted_at != tick.received_at


def test_latest_returns_the_newest_recorded_quote():
    book = QuoteBook()
    book.record(_tick(187.45, at=T0))
    book.record(_tick(188.05, at=T0 + timedelta(seconds=1)))
    assert book.latest(SID).last_price == 188.05  # type: ignore[union-attr]


def test_an_instrument_with_no_ticks_has_no_quote():
    assert QuoteBook().latest(SID) is None


def test_instruments_are_kept_apart():
    book = QuoteBook()
    book.record(_tick(187.45, at=T0))
    book.record(_tick(24000.0, at=T0, security_id="13"))
    assert book.latest("13").last_price == 24000.0  # type: ignore[union-attr]
    assert book.instruments() == {SID, "13"}


# ----------------------------------------------------------------- deadlines
def test_after_returns_the_first_quote_at_or_past_the_deadline():
    book = QuoteBook()
    for offset, price in ((0, 187.45), (100, 187.55), (300, 187.95), (900, 180.05)):
        book.record(_tick(price, at=T0 + timedelta(milliseconds=offset)))

    selected = book.after(SID, T0 + timedelta(milliseconds=250))
    assert selected is not None and selected.last_price == 187.95


def test_a_quote_exactly_on_the_deadline_counts():
    book = QuoteBook()
    book.record(_tick(187.45, at=T0))
    book.record(_tick(187.95, at=T0 + timedelta(milliseconds=250)))
    selected = book.after(SID, T0 + timedelta(milliseconds=250))
    assert selected is not None and selected.last_price == 187.95


def test_after_never_reaches_past_the_first_eligible_quote():
    """Oldest at or after, not *best* at or after. A simulated order becomes live
    at its deadline and executes against the price available *then*; choosing among
    later prices would be lookahead, and lookahead in a simulator always ends up
    favourable."""
    book = QuoteBook()
    book.record(_tick(187.45, at=T0))
    book.record(_tick(999.00, at=T0 + timedelta(milliseconds=300)))
    book.record(_tick(1.00, at=T0 + timedelta(milliseconds=400)))

    selected = book.after(SID, T0 + timedelta(milliseconds=250))
    assert selected is not None and selected.last_price == 999.00


def test_nothing_past_the_deadline_yields_none():
    """The ordinary live case: at submission time the future has not happened yet.
    The caller records that it fell back rather than inventing a quote."""
    book = QuoteBook()
    book.record(_tick(187.45, at=T0))
    assert book.after(SID, T0 + timedelta(milliseconds=250)) is None


def test_an_unknown_instrument_yields_none():
    assert QuoteBook().after(SID, T0) is None


# -------------------------------------------------------------------- bounds
def test_the_book_is_bounded_and_drops_the_oldest():
    """It is written on every tick for the whole session, so it must not grow
    without limit."""
    book = QuoteBook(depth=3)
    for offset in range(10):
        book.record(_tick(100.0 + offset, at=T0 + timedelta(seconds=offset)))

    assert book.latest(SID).last_price == 109.0  # type: ignore[union-attr]
    assert book.after(SID, T0) is not None
    # The first seven have aged out, so the oldest survivor is the eighth.
    oldest = book.after(SID, T0)
    assert oldest is not None and oldest.last_price == 107.0


def test_the_recorded_count_is_observable():
    """A book that is empty when an order arrives is a wiring failure, and a
    counter is how that gets told apart from a quiet market."""
    book = QuoteBook()
    book.record(_tick(187.45, at=T0))
    book.record(_tick(187.50, at=T0 + timedelta(seconds=1)))
    assert book.recorded == 2
