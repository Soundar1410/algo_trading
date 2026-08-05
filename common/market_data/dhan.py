"""Dhan WebSocket market-feed adapter.

**This is the only module in the repository that imports ``dhanhq``.** Every
other component depends on :class:`~common.market_data.adapter.MarketFeedAdapter`,
so the SDK cannot leak into strategy, execution or persistence code — the spec's
rule that "the strategy must never call the Dhan SDK directly" is enforced by
import topology rather than by review. A test asserts it.

The import is deliberately *lazy*, inside :meth:`DhanMarketFeedAdapter.start`, so
the automated suite runs with no credentials and no network, and no test process
pays the SDK's import cost.

Payload shape: **ratified** (Phase 2)
-------------------------------------
``get_data()`` returns whatever ``marketfeed.process_data()`` builds. That method
constructs the dict itself from the binary frame, so the *shape* is determined
entirely by SDK code rather than by the wire — and ``process_ticker``,
``process_quote`` and ``utc_time`` are byte-identical between ``dhanhq`` 2.1.0
and 2.2.0. Ticker Data (request code 15, first byte 2) is:

.. code-block:: python

    {"type": "Ticker Data", "exchange_segment": int, "security_id": int,
     "LTP": "1234.55",       # str, via "{:.2f}".format(...)
     "LTT": "09:15:03"}      # str, utcfromtimestamp(e).strftime('%H:%M:%S')

Phase 1 wrote this defensively and got three things wrong, all corrected here:

1. **``LTT`` carries no date.** ``datetime.fromisoformat("09:15:03")`` raises,
   so the old code fell back to receipt time on *every* tick — meaning candles
   were bucketed by local arrival rather than exchange time, the opposite of
   :mod:`common.candles.aggregator`'s contract. See :func:`reconstruct_exchange_time`.
2. **``last_quantity`` is not a key.** Quantity is ``LTQ``, and only in Quote
   Data, so live volume was always zero.
3. **Not every payload is a dict.** ``process_status`` returns the bare string
   ``"Markets Open"`` and ``process_data`` returns ``None`` for an unrecognised
   first byte. Both failed the old ``isinstance(payload, dict)`` guard and
   inflated ``malformed_payloads``, making a genuine shape problem
   indistinguishable from ordinary traffic.

Depth: **ratified** (Phase 4 Part 5)
------------------------------------
Only ``process_full`` (request code 21, first byte 8) carries a book. Verified
against the pinned SDK: ``process_quote`` (code 17) returns LTQ, volume, buy/sell
quantity totals and session OHLC and **no depth at all**, and ``MarketFeed``
refuses any v2 mode outside ``{15, 17, 21}``, so the 20-level depth code 19 is not
available to us. Full Data is:

.. code-block:: python

    {"type": "Full Data", ..., "LTP": "1234.55", "LTQ": int, "LTT": "09:15:03",
     "depth": [{"bid_quantity": int, "ask_quantity": int, "bid_orders": int,
                "ask_orders": int, "bid_price": "1234.50", "ask_price": "1234.60"},
               ...5 levels...]}

Two properties of that structure decide how it is read here:

1. **Prices are formatted strings**, built by ``"{:.2f}".format(...)`` exactly as
   ``LTP`` is, so they are coerced rather than assumed numeric.
2. **A level with no order carries ``"0.00"``**, not a missing key. Zero is
   therefore *absence*, and :func:`_price_or_none` maps it to ``None``. Reading it
   as a real price would make :attr:`~common.broker.base.Quote.has_depth` true with
   a bid of zero, and a sell would then price at ``0 - slippage`` — i.e. every exit
   on a one-sided book refused by the simulator. The reference repository's
   normaliser does exactly that; it is not copied.

Adding ``"Full Data"`` to :data:`_TICK_TYPES` is what makes mode 21 usable at all.
Without it the frame falls through to the unrecognised-type branch, which counts a
*non-tick* and logs at **debug** — so a Full-mode feed would run connected and
silent, producing no candles, no indicators and no orders, with nothing above debug
level to say why.
"""

from __future__ import annotations

import struct
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from common.logging import get_logger
from common.models import Tick

from .adapter import TickCallback

_log = get_logger(__name__)

#: Subscription modes, as the v2 protocol numbers them. Ticker gives last price
#: and time, which is all a candle needs. Quote adds traded quantity, volume and
#: session OHLC — **not** depth, despite what this comment claimed before Phase 4
#: Part 5. Only Full carries a bid/ask book, which is what the paper fill model
#: prices against. ``MarketFeed`` rejects any other value on v2.
TICKER_MODE = 15
QUOTE_MODE = 17
FULL_MODE = 21

#: Every mode the SDK accepts on v2. Checked here so an unusable mode fails at
#: subscription rather than inside the SDK's batching, where the message names no
#: instrument.
FEED_MODES = frozenset({TICKER_MODE, QUOTE_MODE, FULL_MODE})


def _is_feed_mode(value: object) -> bool:
    """True only for an exact ``int`` naming a v2 mode.

    Exact on purpose. ``21.0 in FEED_MODES`` is ``True`` and the SDK's own
    batching would then key a ``defaultdict`` by a float, which happens to work
    and would leave a wrongly-typed value travelling to the socket — the sort of
    thing that survives until the day it does not. ``bool`` is excluded for the
    usual reason: ``True`` is ``1``, and no mode is 1.
    """
    return type(value) is int and value in FEED_MODES


#: ``type`` values that carry a tradeable price. Everything else is a legitimate
#: non-tick frame, not a parse failure.
_TICK_TYPES = frozenset({"Ticker Data", "Quote Data", "Full Data"})

#: ``type`` values we recognise and intentionally ignore.
_NON_TICK_TYPES = frozenset({"Previous Close", "OI Data", "Market Depth"})

#: Dhan's server-disconnection reason codes, from ``marketfeed.server_disconnection``.
DISCONNECT_REASONS: dict[int, str] = {
    805: "too many active websocket connections",
    806: "data APIs not subscribed on this account",
    807: "access token expired",
    808: "invalid client id",
    809: "authentication failed",
}

#: Reason code that a new token can fix. Reconnecting with the same token cannot.
DISCONNECT_TOKEN_EXPIRED = 807

#: What ``LTT`` looks like for an instrument that has not traded yet: the SDK
#: renders epoch 0 through ``strftime('%H:%M:%S')``, giving midnight UTC.
#:
#: Such a frame carries an ``LTP`` that is not a real trade price, so it is
#: rejected outright rather than admitted with a substituted timestamp — the
#: spec's rule that a stale price is never treated as a fresh unchanged price
#: (section 10). Midnight UTC is 05:30 IST, and no Dhan segment trades then, so
#: this sentinel cannot collide with a genuine exchange timestamp.
NEVER_TRADED_LTT = "00:00:00"


class DhanFeedError(RuntimeError):
    """Raised when the live feed cannot be constructed or authorised."""


@dataclass
class FeedCounters:
    """Observability for the normalisation path.

    ``exchange_time_fallbacks`` exists because of defect 1 above: a silent
    100 % fallback rate looked exactly like healthy operation. Now it is a
    number someone can read.
    """

    ticks: int = 0
    non_tick_frames: int = 0
    malformed_payloads: int = 0
    exchange_time_fallbacks: int = 0
    empty_frames: int = 0
    #: Frames for instruments that have not traded today. Expected and benign at
    #: the open, especially for far-out-of-the-money strikes; a count that stays
    #: high all session means we are subscribed to something illiquid.
    untraded_frames: int = 0
    #: Ticks carrying a two-sided book, i.e. the ones the fill model can price
    #: against bid/ask instead of falling back to last price.
    ticks_with_depth: int = 0
    #: Ticks from a Full-mode frame whose top of book had one side or neither.
    #: Ordinary for an illiquid strike and *not* an error, but a rate that stays
    #: high means paper fills are being priced off LTP while the config believes
    #: they are being priced off the book.
    ticks_one_sided_book: int = 0
    disconnects: int = 0
    disconnect_codes: dict[int, int] = field(default_factory=dict)

    @property
    def fallback_ratio(self) -> float:
        """Share of ticks whose exchange timestamp could not be reconstructed."""
        return self.exchange_time_fallbacks / self.ticks if self.ticks else 0.0

    @property
    def depth_ratio(self) -> float:
        """Share of ticks that carried a two-sided book."""
        return self.ticks_with_depth / self.ticks if self.ticks else 0.0


def reconstruct_exchange_time(
    raw: object,
    received_at: datetime,
) -> datetime | None:
    """Rebuild a full UTC timestamp from Dhan's time-only ``LTT``.

    The SDK renders the exchange epoch as ``strftime('%H:%M:%S')`` against UTC,
    discarding the date. The date is recovered by picking whichever of yesterday,
    today or tomorrow places the wall-clock time closest to ``received_at`` —
    correct for any true latency under twelve hours, and robust across a UTC
    midnight rollover without special-casing it.

    (For Indian equity and F&O segments the rollover cannot arise: the session
    runs 09:15 to 15:30 IST, i.e. 03:45 to 10:00 UTC, so the UTC date is constant
    throughout. The general form costs nothing and does not depend on that
    remaining true for other segments.)

    Also accepts a numeric epoch, which is what a future SDK version would most
    likely switch to, and an ISO-8601 string. Returns ``None`` when the value is
    absent or unusable, so the caller can count the fallback.
    """
    if raw is None:
        return None

    if isinstance(raw, int | float) and not isinstance(raw, bool):
        # A real epoch. Zero means "no trade yet on this instrument", not 1970.
        if raw <= 0:
            return None
        return datetime.fromtimestamp(float(raw), tz=UTC)

    text = str(raw).strip()
    if not text:
        return None

    parsed_time: datetime | None = None
    try:
        parsed_time = datetime.strptime(text, "%H:%M:%S")
    except ValueError:
        # Not the SDK's format. Try full ISO-8601 before giving up.
        try:
            iso = datetime.fromisoformat(text)
        except ValueError:
            return None
        return iso if iso.tzinfo else iso.replace(tzinfo=UTC)

    anchor = received_at.astimezone(UTC)
    candidates = [
        datetime.combine(
            (anchor + timedelta(days=offset)).date(),
            parsed_time.time(),
            tzinfo=UTC,
        )
        for offset in (-1, 0, 1)
    ]
    return min(candidates, key=lambda moment: abs((moment - anchor).total_seconds()))


class DhanMarketFeedAdapter:
    """Live Dhan feed. Satisfies :class:`MarketFeedAdapter`.

    Not used by any default test. Reconnection is owned by
    :class:`~common.feed.reconnect.ReconnectingFeed`, which wraps this adapter —
    see that module for why the SDK's own retry loop is not used.
    """

    def __init__(
        self,
        *,
        client_id: str,
        access_token: str,
        exchange_segment: int = 2,
        instrument_label: str = "UNKNOWN",
        instrument_labels: dict[str, str] | None = None,
        feed_mode: int = TICKER_MODE,
    ) -> None:
        if not client_id or not access_token:
            raise DhanFeedError("Dhan feed requires both a client id and an access token")
        # Held only for the SDK handshake; never logged. The logging redactor
        # masks these values everywhere regardless.
        self._client_id = client_id
        self._access_token = access_token
        self._exchange_segment = exchange_segment
        self._instrument_label = instrument_label
        self._instrument_labels = dict(instrument_labels or {})
        if not _is_feed_mode(feed_mode):
            raise DhanFeedError(
                f"Unsupported feed mode {feed_mode!r}. The v2 protocol accepts only "
                f"{sorted(FEED_MODES)} (ticker/quote/full)."
            )
        self._feed_mode = feed_mode
        self._security_ids: set[str] = set()
        #: security_id → exchange segment, for ids that do not live in the
        #: adapter's default. An options runtime always has some: the underlying
        #: index sits in ``IDX_I`` (0) and its contracts in ``NSE_FNO`` (2), and
        #: one adapter carries both at once. Ids absent here use the default.
        self._segments: dict[str, int] = {}
        #: security_id → subscription mode, for the same reason and with the same
        #: shape as ``_segments``. An options runtime wants two at once: the
        #: underlying index on Ticker, because an index has no order book to
        #: stream, and its contracts on Full, because that is the only mode that
        #: carries one. ``MarketFeed.validate_and_process_tuples`` already batches
        #: a mixed instrument list by mode, so both travel on one socket.
        self._modes: dict[str, int] = {}
        self._feed: Any = None
        self._running = False
        #: Ident of the thread currently inside :meth:`start`, i.e. the thread
        #: that owns the SDK's asyncio loop. ``None`` when no loop is running,
        #: which is the only state in which any thread may close the socket.
        self._owner_thread: int | None = None
        self.counters = FeedCounters()
        #: Set when Dhan sends a server-disconnection frame. Read by the
        #: reconnect layer to decide whether a new token is required.
        self.last_disconnect_code: int | None = None

    # ------------------------------------------------------------- properties
    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def subscribed(self) -> frozenset[str]:
        """The current subscription set — asserted on after a resubscribe."""
        return frozenset(self._security_ids)

    @property
    def token_expired(self) -> bool:
        return self.last_disconnect_code == DISCONNECT_TOKEN_EXPIRED

    # ------------------------------------------------------------ subscription
    def subscribe(
        self,
        security_ids: Sequence[str],
        *,
        segment: int | None = None,
        mode: int | None = None,
    ) -> None:
        """Union semantics — a resubscribe must not duplicate instruments.

        ``segment`` and ``mode`` are both remembered per security id, so a later
        :meth:`resubscribe_all` after a reconnect restores each instrument to the
        segment *and* mode it was actually subscribed on. Without the segment the
        underlying would come back on the option segment (or vice versa) and the
        feed would reconnect into silence — a failure that looks exactly like a
        quiet market. Without the mode a reconnect would silently demote the option
        contracts to Ticker, and the fill model would start pricing off last price
        while still reporting itself as depth-driven.
        """
        wanted = {str(s) for s in security_ids}
        if segment is not None:
            conflicting = {
                sid for sid in wanted & self._security_ids if self._segment_for(sid) != segment
            }
            if conflicting:
                raise DhanFeedError(
                    f"Cannot resubscribe {sorted(conflicting)} to segment {segment}: "
                    "already subscribed on a different segment. One instrument "
                    "cannot live in two segments at once."
                )
            for sid in wanted:
                self._segments[sid] = segment

        if mode is not None:
            if not _is_feed_mode(mode):
                raise DhanFeedError(
                    f"Unsupported feed mode {mode!r}. The v2 protocol accepts only "
                    f"{sorted(FEED_MODES)} (ticker/quote/full)."
                )
            # Unlike a segment, a mode change on an already-subscribed instrument
            # is legitimate — the SDK simply sends another subscription frame — so
            # this overwrites rather than refusing.
            for sid in wanted:
                self._modes[sid] = mode

        new = wanted - self._security_ids
        self._security_ids.update(new)
        if self._feed is not None and new:
            self._feed.subscribe_symbols(self._instrument_tuples(new))

    def resubscribe_all(self) -> None:
        """Re-send the full subscription set after a reconnect.

        Needed because a new socket carries none of the old subscriptions, while
        :meth:`subscribe` deliberately sends only the *delta*.
        """
        if self._feed is not None and self._security_ids:
            self._feed.subscribe_symbols(self._instrument_tuples(self._security_ids))

    def segment_for(self, security_id: str) -> int:
        """The segment this instrument is subscribed on. Public for assertions."""
        return self._segment_for(str(security_id))

    def mode_for(self, security_id: str) -> int:
        """The mode this instrument is subscribed in. Public for assertions."""
        return self._mode_for(str(security_id))

    def _segment_for(self, security_id: str) -> int:
        return self._segments.get(security_id, self._exchange_segment)

    def _mode_for(self, security_id: str) -> int:
        return self._modes.get(security_id, self._feed_mode)

    def _instrument_tuples(self, security_ids: set[str]) -> list[tuple[int, str, int]]:
        return [(self._segment_for(sid), sid, self._mode_for(sid)) for sid in sorted(security_ids)]

    # ---------------------------------------------------------------- lifecycle
    def start(self, on_tick: TickCallback) -> None:
        """Connect and pump ticks into ``on_tick`` until stopped.

        Blocks for the life of the feed, and the calling thread becomes the owner
        of the SDK's event loop: only it may close the socket. Another thread
        stops this loop with :meth:`request_stop`, never :meth:`stop`.
        """
        if not self._security_ids:
            raise DhanFeedError("Refusing to start the Dhan feed with no subscriptions")

        try:
            from dhanhq import DhanContext, marketfeed
        except ImportError as exc:  # pragma: no cover - dependency is pinned
            raise DhanFeedError(f"dhanhq is not importable: {exc}") from exc

        try:
            context = DhanContext(self._client_id, self._access_token)
            self._feed = marketfeed.MarketFeed(
                context,
                self._instrument_tuples(self._security_ids),
                version="v2",
            )
        except Exception as exc:
            raise DhanFeedError(f"Cannot construct the Dhan market feed: {exc}") from exc

        self._install_disconnect_probe()
        self._running = True
        self._owner_thread = threading.get_ident()
        _log.info("dhan feed starting instruments=%d", len(self._security_ids))
        try:
            while self._running:
                self._feed.run_forever()
                payload = self._feed.get_data()
                tick = self.normalise(payload)
                if tick is not None:
                    on_tick(tick)
        finally:
            self._running = False
            # This thread owns the SDK's event loop, so this thread closes the
            # socket — including when the loop is unwound by an exception, and
            # including when the exit was triggered by request_stop() from
            # somewhere else. No other thread may do this; see the module
            # docstring of common.market_data.adapter.
            self.stop()
            self._owner_thread = None

    def request_stop(self) -> None:
        """Ask the feed loop to finish. Safe from any thread; closes nothing.

        Sets the flag :meth:`start`'s loop already tests, so the loop exits at its
        next frame boundary and closes the socket itself. That is the whole point:
        ``MarketFeed.close_connection()`` called from a foreign thread waits on
        ``run_coroutine_threadsafe(...).result()``, which cannot complete while
        the owning thread is blocked in ``recv()``.

        The cost is that the stop lands at the *next frame*, so a socket that is
        delivering nothing at all will not notice. Nothing can safely force that
        case from outside the owning thread; the supervisor bounds it with a grace
        period instead of reaching across.
        """
        self._running = False

    def stop(self) -> None:
        """Stop delivering and close the socket. Idempotent.

        For the owning thread, or for any thread when no ``start()`` is running.
        From another thread while the feed is live, use :meth:`request_stop` —
        this method would hang there, as a live capture run proved in Phase 2.
        """
        self._running = False
        feed, self._feed = self._feed, None
        if feed is None:
            return
        owner = self._owner_thread
        if owner is not None and owner != threading.get_ident():
            # Refuse rather than hang. Reaching a live SDK loop from a foreign
            # thread is the defect this contract exists to prevent, so it fails
            # loudly instead of silently reproducing it. The flag is still
            # cleared above, so the owner will close on its way out.
            self._feed = feed
            _log.error(
                "refusing to close the dhan feed from thread %s; it is owned by %s. "
                "Use request_stop() from another thread.",
                threading.get_ident(),
                owner,
            )
            return
        try:
            # close_connection() is the *synchronous* wrapper. Phase 1 called
            # disconnect() directly, which is a coroutine — so it returned an
            # un-awaited coroutine object and the socket was never closed. In
            # 2.1.0 disconnect() also never called ws.close() at all; 2.2.0 does.
            feed.close_connection()
        except Exception as exc:
            _log.warning("dhan feed close failed: %s", exc)

    def _install_disconnect_probe(self) -> None:
        """Capture Dhan's server-disconnection reason code.

        ``process_data`` dispatches first byte 50 to ``server_disconnection``,
        which in 2.2.0 *prints* the reason and returns ``None`` — so the code
        never reaches the caller, and a token-expiry disconnect is
        indistinguishable from an unknown frame. Overriding the bound method on
        our own instance (never on the class) recovers it, which is what lets a
        807 trigger a token refresh instead of a futile reconnect with the same
        dead token.
        """
        feed = self._feed
        original = feed.server_disconnection

        def _probe(data: bytes) -> Any:
            try:
                code = int(struct.unpack("<BHBIH", data[0:10])[4])
            except (struct.error, IndexError, TypeError):
                code = 0
            if code:
                self.last_disconnect_code = code
                self.counters.disconnects += 1
                self.counters.disconnect_codes[code] = (
                    self.counters.disconnect_codes.get(code, 0) + 1
                )
                _log.warning(
                    "dhan server disconnection code=%s reason=%s",
                    code,
                    DISCONNECT_REASONS.get(code, "unknown"),
                )
            return original(data)

        feed.server_disconnection = _probe

    # -------------------------------------------------------------- normalising
    def normalise(self, payload: object) -> Tick | None:
        """Convert one SDK payload into a :class:`Tick`, or classify and count it.

        Returns ``None`` for anything that is not a tick. One unrecognised frame
        must never tear down the feed for every worker in the group, so nothing
        here raises — but the *reason* is counted, and non-ticks are counted
        separately from malformed ones so a real shape problem stands out.
        """
        if payload is None:
            # process_data() returns None for an unrecognised first byte, and for
            # a server-disconnection frame (already recorded by the probe).
            self.counters.empty_frames += 1
            return None

        if isinstance(payload, str):
            # process_status() returns the bare string "Markets Open".
            self.counters.non_tick_frames += 1
            return None

        if not isinstance(payload, dict):
            self.counters.malformed_payloads += 1
            _log.warning("unexpected dhan payload type=%s", type(payload).__name__)
            return None

        frame_type = str(payload.get("type", ""))
        if frame_type in _NON_TICK_TYPES:
            self.counters.non_tick_frames += 1
            return None

        if frame_type and frame_type not in _TICK_TYPES:
            self.counters.non_tick_frames += 1
            _log.debug("ignoring dhan frame type=%s", frame_type)
            return None

        return self._to_tick(payload)

    def _to_tick(self, payload: dict[str, Any]) -> Tick | None:
        try:
            price = float(payload.get("LTP") or 0.0)
            security_id = str(payload.get("security_id") or "")
        except (TypeError, ValueError):
            self.counters.malformed_payloads += 1
            return None

        if price <= 0 or not security_id:
            self.counters.malformed_payloads += 1
            return None

        raw_ltt = payload.get("LTT")
        if _is_never_traded(raw_ltt):
            # No trade has occurred, so LTP is not a trade price. Admitting it
            # with a substituted timestamp would fold a non-price into a candle.
            self.counters.untraded_frames += 1
            return None

        received = datetime.now(UTC)
        exchange_time = reconstruct_exchange_time(raw_ltt, received)
        if exchange_time is None:
            # Honest fallback, and counted. A persistent fallback rate means the
            # timestamp format changed and candles are being bucketed by arrival.
            self.counters.exchange_time_fallbacks += 1
            exchange_time = received

        bid, ask = _top_of_book(payload)
        self.counters.ticks += 1
        if bid is not None and ask is not None:
            self.counters.ticks_with_depth += 1
        elif "depth" in payload:
            # A Full-mode frame arrived but the top of book was one-sided or empty.
            # Counted separately from "this mode carries no depth at all", because
            # the two need different responses: this one is an illiquid strike, the
            # other is a misconfigured subscription.
            self.counters.ticks_one_sided_book += 1
        return Tick(
            security_id=security_id,
            instrument=self._instrument_labels.get(security_id, self._instrument_label),
            last_price=price,
            exchange_time=exchange_time,
            received_at=received,
            last_quantity=_as_int(payload.get("LTQ")),
            bid_price=bid,
            ask_price=ask,
        )


def _is_never_traded(raw: object) -> bool:
    """True when ``LTT`` marks an instrument that has not traded yet.

    Distinct from a *missing* ``LTT``, which means "we do not know when this
    traded" and legitimately falls back to receipt time.
    """
    if raw is None:
        return False
    if isinstance(raw, bool):
        return False
    if isinstance(raw, int | float):
        return raw <= 0
    return str(raw).strip() == NEVER_TRADED_LTT


def _top_of_book(payload: dict[str, Any]) -> tuple[float | None, float | None]:
    """Best bid and ask from a Full-mode frame, or ``(None, None)``.

    The SDK renders every depth price through ``"{:.2f}".format(...)``, so these
    are strings, and a level with no resting order renders as ``"0.00"`` rather
    than being omitted. **Zero therefore means absence**, which is why this goes
    through :func:`_price_or_none` rather than ``float()``: a bid of ``0.0`` would
    make the quote look two-sided to the fill model, and a sell would then be
    priced at ``0 - slippage`` and refused. Each side is resolved independently,
    so a book with only an ask still yields that ask.

    Ticker and Quote frames have no ``depth`` key at all and fall out here with
    both sides ``None``, which is the same answer they gave before Part 5.
    """
    depth = payload.get("depth")
    if not isinstance(depth, list) or not depth:
        return None, None
    top = depth[0]
    if not isinstance(top, dict):
        return None, None
    return _price_or_none(top.get("bid_price")), _price_or_none(top.get("ask_price"))


def _price_or_none(value: object) -> float | None:
    """Coerce a depth price, mapping absent/zero/negative/unparseable to ``None``."""
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, int | float | str):
        return None
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def _as_int(value: object) -> int:
    """Coerce a quantity to int. Dhan sends these as ints, but Quote sends
    formatted strings for prices, so defensive coercion is cheap."""
    if value is None or isinstance(value, bool):
        return 0
    if not isinstance(value, int | float | str):
        return 0
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0
