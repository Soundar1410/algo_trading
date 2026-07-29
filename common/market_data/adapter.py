"""The market-feed contract.

One Protocol, two implementations: a recorded tape for every automated test and
a Dhan WebSocket adapter for the opt-in live smoke test. The hub depends only on
this Protocol, so no test needs a network, credentials or market hours — and the
live adapter cannot quietly acquire behaviour the recorded one lacks, because
the hub can only use what is declared here.

A Protocol rather than an ABC: the recorded adapter is not conceptually a
subclass of a WebSocket client, and structural typing keeps the test double free
of inherited machinery it would only have to stub out.
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

    def stop(self) -> None:
        """Stop delivering and release the connection. Must be idempotent."""
        ...

    @property
    def is_running(self) -> bool: ...
