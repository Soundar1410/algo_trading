"""The market-feed contract.

One Protocol, two implementations: a recorded tape for every automated test and
a Dhan WebSocket adapter for the opt-in live smoke test. The hub depends only on
this Protocol, so no test needs a network, credentials or market hours — and the
live adapter cannot quietly acquire behaviour the recorded one lacks, because
the hub can only use what is declared here.

A Protocol rather than an ABC: the recorded adapter is not conceptually a
subclass of a WebSocket client, and structural typing keeps the test double free
of inherited machinery it would only have to stub out.

Thread ownership
----------------
An adapter's ``start()`` may block for as long as the feed is live, so a real
deployment runs it on a worker thread and stops it from somewhere else. That
makes the threading contract part of *this* interface rather than a detail of any
one implementation:

    **The thread that called** ``start()`` **owns the connection, and is the only
    thread permitted to close it. Every other thread may only signal intent, via**
    ``request_stop()``.

This is not a theoretical rule. ``dhanhq``'s ``MarketFeed`` drives a private
``asyncio`` loop synchronously from whichever thread called in, and its
``close_connection()`` waits — with no timeout — on a loop only that thread can
advance. Calling it from anywhere else while the owner is blocked in ``recv()``
hangs the process, which is what a live capture run did in Phase 2 Block 2.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Protocol, runtime_checkable

from common.models import Tick

#: Called by the adapter for each normalised tick. Implementations must return
#: quickly: the spec requires the feed callback only to validate minimally and
#: publish into a bounded queue, never to do strategy work.
TickCallback = Callable[[Tick], None]


@runtime_checkable
class MarketFeedAdapter(Protocol):
    """One live or recorded source of normalised ticks."""

    def subscribe(self, security_ids: Sequence[str]) -> None:
        """Register interest. Called once with the union of worker requirements.

        Must be safe to call again after a reconnect without creating duplicate
        subscriptions.
        """
        ...

    def start(self, on_tick: TickCallback) -> None:
        """Begin delivering ticks to ``on_tick``."""
        ...

    def request_stop(self) -> None:
        """Ask the feed to finish. Safe to call from **any** thread.

        Sets a flag and returns immediately. Must **never** touch the connection:
        the owning thread notices at its next frame boundary and closes there.
        Idempotent, and safe before ``start()`` or after it has returned.

        This is the only stop that a thread which does not own the loop may use.
        """
        ...

    def stop(self) -> None:
        """Stop delivering and release the connection. Must be idempotent.

        Only for the thread that called ``start()``, or for any thread when no
        ``start()`` is in flight. From anywhere else use :meth:`request_stop`.
        """
        ...

    @property
    def is_running(self) -> bool: ...
