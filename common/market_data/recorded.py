"""Deterministic feed adapter driven by a recorded tick tape.

Every automated test uses this. It replays a JSON tape synchronously in
``start()`` — no threads, no sleeps, no wall-clock dependency — so a candle
boundary is crossed because the tape says so, not because a timer happened to
fire. That is what makes "one completed candle produces exactly one order" a
test that either passes or fails rather than one that flakes.

Duplicate and out-of-order tick suppression lives here rather than in the hub:
the spec requires it of every market-data component, and doing it at the source
means the hub's fan-out logic is tested against clean input.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from common.models import Tick

from .adapter import TickCallback


def load_tick_tape(path: Path | str) -> list[Tick]:
    """Load a recorded tape.

    Tape format is a JSON list of objects with ISO-8601 timestamps::

        [{"security_id": "43492", "instrument": "NIFTY", "last_price": 100.5,
          "exchange_time": "2026-07-29T09:15:00+05:30",
          "received_at":   "2026-07-29T09:15:00.05+05:30"}]
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"Tick tape {path} must contain a JSON list")

    ticks: list[Tick] = []
    for index, entry in enumerate(raw):
        try:
            ticks.append(
                Tick(
                    security_id=str(entry["security_id"]),
                    instrument=str(entry["instrument"]),
                    last_price=float(entry["last_price"]),
                    exchange_time=datetime.fromisoformat(entry["exchange_time"]),
                    received_at=datetime.fromisoformat(
                        entry.get("received_at", entry["exchange_time"])
                    ),
                    last_quantity=int(entry.get("last_quantity", 0)),
                    bid_price=_optional_float(entry.get("bid_price")),
                    ask_price=_optional_float(entry.get("ask_price")),
                    sequence=entry.get("sequence"),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Tick tape {path} entry {index} is invalid: {exc}") from exc
    return ticks


def _optional_float(value: str | float | None) -> float | None:
    """Coerce an optional JSON number. A wrong type raises, and the caller
    turns that into a tape-entry error naming the index."""
    return None if value is None else float(value)


class RecordedFeedAdapter:
    """Replays a fixed list of ticks. Satisfies :class:`MarketFeedAdapter`."""

    def __init__(self, ticks: Sequence[Tick]) -> None:
        self._ticks = list(ticks)
        self._subscribed: set[str] = set()
        self._running = False
        #: Highest exchange timestamp seen per instrument, for ordering checks.
        self._last_seen: dict[str, datetime] = {}
        self.duplicate_count = 0
        self.out_of_order_count = 0
        self.delivered_count = 0

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def subscribed(self) -> frozenset[str]:
        return frozenset(self._subscribed)

    def subscribe(self, security_ids: Sequence[str]) -> None:
        # Union semantics: re-subscribing after a simulated reconnect must not
        # duplicate, which is why this is a set rather than a list.
        self._subscribed.update(security_ids)

    def request_stop(self) -> None:
        """Ask the replay to finish. Safe from any thread; see the Protocol.

        Identical to :meth:`stop` here, and that is not a shortcut: this adapter
        holds no connection to close, so there is nothing that only the replaying
        thread may do. The distinction exists for adapters that own a socket.
        """
        self._running = False

    def stop(self) -> None:
        self._running = False

    def start(self, on_tick: TickCallback) -> None:
        """Replay the tape once, in order, to ``on_tick``."""
        self._running = True
        for tick in self._ticks:
            if not self._running:
                break
            if tick.security_id not in self._subscribed:
                continue
            if self._is_stale(tick):
                continue
            self._last_seen[tick.security_id] = tick.exchange_time
            self.delivered_count += 1
            on_tick(tick)
        self._running = False

    def _is_stale(self, tick: Tick) -> bool:
        """Reject a repeat or a backwards tick, counting each separately."""
        previous = self._last_seen.get(tick.security_id)
        if previous is None:
            return False
        if tick.exchange_time == previous:
            self.duplicate_count += 1
            return True
        if tick.exchange_time < previous:
            self.out_of_order_count += 1
            return True
        return False
