"""Dhan WebSocket market-feed adapter.

**This is the only module in the repository that imports ``dhanhq``.** Every
other component depends on :class:`~common.market_data.adapter.MarketFeedAdapter`,
so the SDK cannot leak into strategy, execution or persistence code — the spec's
rule that "the strategy must never call the Dhan SDK directly" is enforced by
import topology rather than by review.

The import is deliberately *lazy*, inside ``start()``. Phase 1's automated tests
must run with no credentials and no network, and a module-level import would
also make every test process pay the SDK's import cost.

**Status: unratified.** The API surface below was read from the installed
``dhanhq==2.1.0`` (``MarketFeed(dhan_context, instruments, version)`` with
``run_forever`` / ``get_data`` / ``subscribe_symbols`` / ``disconnect``), but the
*payload shape* returned by ``get_data`` has not been observed against a live
connection. Normalisation is therefore defensive and every unparseable payload
is counted rather than guessed at. The compatibility spike that ratifies this —
including the 2.1.0-versus-2.2.0 decision — is **Phase 2**. Reconnection with
bounded backoff, resubscription and staleness detection are Phase 2 as well;
this adapter is intentionally the thinnest thing that can prove one live tick
reaches a worker.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from common.logging import get_logger
from common.models import Tick

from .adapter import TickCallback

_log = get_logger(__name__)

#: Subscription mode: ticker gives last price and time, which is all a candle
#: needs. Quote/Full add depth and are a Phase 4 concern (bid/ask fill model).
_TICKER_MODE = 15


class DhanFeedError(RuntimeError):
    """Raised when the live feed cannot be constructed or authorised."""


class DhanMarketFeedAdapter:
    """Live Dhan feed. Satisfies :class:`MarketFeedAdapter`.

    Not used by any default test. The opt-in smoke test in ``tests/smoke/`` is
    the only automated caller, and it only asserts that a tick arrives — it
    never places an order.
    """

    def __init__(
        self,
        *,
        client_id: str,
        access_token: str,
        exchange_segment: int = 2,
        instrument_label: str = "UNKNOWN",
    ) -> None:
        if not client_id or not access_token:
            raise DhanFeedError("Dhan feed requires both a client id and an access token")
        # Held only for the SDK handshake; never logged. The logging redactor
        # masks these values everywhere regardless.
        self._client_id = client_id
        self._access_token = access_token
        self._exchange_segment = exchange_segment
        self._instrument_label = instrument_label
        self._security_ids: set[str] = set()
        self._feed: Any = None
        self._running = False
        self.malformed_payloads = 0

    @property
    def is_running(self) -> bool:
        return self._running

    def subscribe(self, security_ids: Sequence[str]) -> None:
        """Union semantics — a resubscribe must not duplicate instruments."""
        new = {str(s) for s in security_ids} - self._security_ids
        self._security_ids.update(new)
        if self._feed is not None and new:
            self._feed.subscribe_symbols(self._instrument_tuples(new))

    def _instrument_tuples(self, security_ids: set[str]) -> list[tuple[int, str, int]]:
        return [(self._exchange_segment, sid, _TICKER_MODE) for sid in sorted(security_ids)]

    def start(self, on_tick: TickCallback) -> None:
        """Connect and pump ticks into ``on_tick`` until :meth:`stop`."""
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

        self._running = True
        _log.info("dhan feed starting instruments=%d", len(self._security_ids))
        try:
            while self._running:
                self._feed.run_forever()
                payload = self._feed.get_data()
                tick = self._normalise(payload)
                if tick is not None:
                    on_tick(tick)
        finally:
            self._running = False

    def stop(self) -> None:
        self._running = False
        if self._feed is not None:
            try:
                self._feed.disconnect()
            except Exception as exc:
                _log.warning("dhan feed disconnect failed: %s", exc)
            self._feed = None

    def _normalise(self, payload: Any) -> Tick | None:
        """Convert one SDK payload into a :class:`Tick`, or count it as malformed.

        Returns None rather than raising: one unrecognised frame must not tear
        down the feed for every worker in the group. The count is surfaced in
        the heartbeat so a persistently wrong shape is visible rather than
        merely quiet.
        """
        if not isinstance(payload, dict):
            self.malformed_payloads += 1
            return None
        try:
            price = float(payload.get("LTP") or payload.get("ltp") or 0.0)
            security_id = str(payload.get("security_id") or payload.get("securityId") or "")
            if price <= 0 or not security_id:
                self.malformed_payloads += 1
                return None
            received = datetime.now(UTC)
            exchange_time = _parse_exchange_time(payload) or received
            return Tick(
                security_id=security_id,
                instrument=self._instrument_label,
                last_price=price,
                exchange_time=exchange_time,
                received_at=received,
                last_quantity=int(payload.get("last_quantity") or 0),
            )
        except (TypeError, ValueError):
            self.malformed_payloads += 1
            return None


def _parse_exchange_time(payload: dict[str, Any]) -> datetime | None:
    """Best-effort exchange timestamp; falls back to receipt time when absent."""
    raw = payload.get("LTT") or payload.get("ltt") or payload.get("exchange_time")
    if raw is None:
        return None
    if isinstance(raw, int | float):
        return datetime.fromtimestamp(float(raw), tz=UTC)
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
