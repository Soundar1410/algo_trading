"""Per-instrument exchange segments (Phase 4 Part 1).

The second half of runbook limitation 17, and the half the limitation's own text
did not mention. Resolving a real ``security_id`` is not enough to subscribe it:
``DhanMarketFeedAdapter`` held **one** segment for every instrument, while an
options runtime needs two at once — the underlying index in ``IDX_I`` (0) and its
contracts in ``NSE_FNO`` (2).

Why this is worth its own suite: a wrong segment does not raise. Dhan accepts the
subscription and delivers nothing, so the failure presents as a quiet market. No
existing test could have caught it, because every test drove one instrument type.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from common.feed.hub import SharedFeedHub, WorkerChannel
from common.feed.queues import BoundedWorkerQueue
from common.market_data.dhan import DhanFeedError, DhanMarketFeedAdapter
from common.market_data.recorded import RecordedFeedAdapter
from common.market_data.scrip_master import segment_code

IDX_I = segment_code("IDX_I")  # 0 — the underlying
NSE_FNO = segment_code("NSE_FNO")  # 2 — its options


def _adapter(**kwargs: object) -> DhanMarketFeedAdapter:
    defaults: dict[str, object] = {
        "client_id": "test-client",
        "access_token": "test-token",
        "exchange_segment": IDX_I,
    }
    return DhanMarketFeedAdapter(**{**defaults, **kwargs})  # type: ignore[arg-type]


class _FakeFeed:
    """Captures what would go to the SDK's ``subscribe_symbols``."""

    def __init__(self) -> None:
        self.sent: list[list[tuple[int, str, int]]] = []

    def subscribe_symbols(self, symbols: list[tuple[int, str, int]]) -> None:
        self.sent.append(symbols)

    def flat(self) -> dict[str, int]:
        """security_id → segment across every call."""
        return {sid: seg for batch in self.sent for seg, sid, _ in batch}


# ------------------------------------------------------------------- defaults
def test_an_instrument_with_no_named_segment_uses_the_adapter_default():
    adapter = _adapter(exchange_segment=NSE_FNO)
    adapter.subscribe(["49081"])
    assert adapter.segment_for("49081") == NSE_FNO


def test_the_pre_phase_four_call_shape_is_unchanged():
    """Every existing caller passes no segment and must keep working."""
    adapter = _adapter(exchange_segment=NSE_FNO)
    feed = _FakeFeed()
    adapter._feed = feed
    adapter.subscribe(["49081", "13"])
    assert feed.flat() == {"49081": NSE_FNO, "13": NSE_FNO}


# ------------------------------------------------------------ mixed segments
def test_an_underlying_and_its_option_are_subscribed_on_different_segments():
    """The property the whole change exists for."""
    adapter = _adapter()  # default IDX_I, for the underlying
    feed = _FakeFeed()
    adapter._feed = feed

    adapter.subscribe(["13"])  # NIFTY spot, default segment
    adapter.subscribe(["49081"], segment=NSE_FNO)  # a real option contract

    assert feed.flat() == {"13": IDX_I, "49081": NSE_FNO}
    assert adapter.segment_for("13") == IDX_I
    assert adapter.segment_for("49081") == NSE_FNO


def test_a_reconnect_restores_each_instrument_to_its_own_segment():
    """The failure this prevents: the underlying comes back on the option
    segment and the feed reconnects into silence."""
    adapter = _adapter()
    adapter.subscribe(["13"])
    adapter.subscribe(["49081"], segment=NSE_FNO)

    feed = _FakeFeed()
    adapter._feed = feed
    adapter.resubscribe_all()

    assert feed.flat() == {"13": IDX_I, "49081": NSE_FNO}


def test_one_instrument_cannot_be_moved_to_a_second_segment():
    """A contradiction, not a preference: the same id in two segments is two
    different instruments, and silently keeping one would pick the wrong one."""
    adapter = _adapter()
    adapter.subscribe(["49081"], segment=NSE_FNO)
    with pytest.raises(DhanFeedError, match="already subscribed on a different segment"):
        adapter.subscribe(["49081"], segment=IDX_I)


def test_resubscribing_the_same_instrument_to_the_same_segment_is_fine():
    adapter = _adapter()
    adapter.subscribe(["49081"], segment=NSE_FNO)
    adapter.subscribe(["49081"], segment=NSE_FNO)  # must not raise
    assert adapter.subscribed == frozenset({"49081"})


def test_subscription_stays_a_union_across_segments():
    adapter = _adapter()
    adapter.subscribe(["13"])
    adapter.subscribe(["49081"], segment=NSE_FNO)
    adapter.subscribe(["13"])
    assert adapter.subscribed == frozenset({"13", "49081"})


def test_only_the_delta_is_sent_to_the_socket():
    """Two option legs, added one at a time: the second call must not re-send
    the first. (Both on the same segment — mixing them in one call is the
    contradiction the previous test pins.)"""
    adapter = _adapter()
    feed = _FakeFeed()
    adapter._feed = feed
    adapter.subscribe(["49081"], segment=NSE_FNO)
    adapter.subscribe(["49081", "49082"], segment=NSE_FNO)
    assert [sid for batch in feed.sent for _, sid, _ in batch] == ["49081", "49082"]


# ------------------------------------------------------------------ the hub
def test_the_hub_forwards_the_segment_of_a_runtime_subscription():
    """The mid-session path: the engine picks a contract and the segment must
    travel with it, or the hub subscribes an option on the index's segment."""
    adapter = RecordedFeedAdapter([])
    hub = SharedFeedHub(adapter)
    hub.register(
        WorkerChannel(
            strategy_id="s1",
            security_ids=frozenset({"13"}),
            queue=BoundedWorkerQueue.in_process("s1"),
        )
    )

    hub.request_subscription("s1", "49081", segment=NSE_FNO)
    hub._apply_pending_subscriptions()

    assert adapter.requested_segments == {"49081": NSE_FNO}
    assert hub.subscriptions_applied == 1


def test_the_hub_defaults_to_the_adapter_segment_when_none_is_named():
    adapter = RecordedFeedAdapter([])
    hub = SharedFeedHub(adapter)
    hub.register(
        WorkerChannel(
            strategy_id="s1",
            security_ids=frozenset({"13"}),
            queue=BoundedWorkerQueue.in_process("s1"),
        )
    )

    hub.request_subscription("s1", "49081")
    hub._apply_pending_subscriptions()

    assert adapter.requested_segments == {}, "no segment should have been asserted"
    assert "49081" in adapter.subscribed


# ------------------------------------------------------- through the reconnect layer
def test_the_reconnect_wrapper_carries_the_segment_through():
    """``ReconnectingFeed`` sits between the hub and the adapter in a live
    deployment, so a segment it dropped would never reach the socket."""
    from common.feed.reconnect import ReconnectingFeed

    class _Recorder:
        def __init__(self) -> None:
            self.calls: list[tuple[tuple[str, ...], int | None]] = []
            self.is_running = False

        def subscribe(self, security_ids: Sequence[str], *, segment: int | None = None) -> None:
            self.calls.append((tuple(str(s) for s in security_ids), segment))

        def start(self, on_tick: object) -> None: ...
        def request_stop(self) -> None: ...
        def stop(self) -> None: ...

    inner = _Recorder()
    feed = ReconnectingFeed(inner)  # type: ignore[arg-type]
    feed.subscribe(["13"])
    feed.subscribe(["49081"], segment=NSE_FNO)

    assert inner.calls == [(("13",), None), (("49081",), NSE_FNO)]


def test_the_reconnect_wrapper_does_not_relabel_earlier_instruments():
    """Re-sending the whole set under one segment would move the underlying
    onto the option segment — the exact silent failure being prevented."""
    from common.feed.reconnect import ReconnectingFeed

    class _Recorder:
        def __init__(self) -> None:
            self.calls: list[tuple[tuple[str, ...], int | None]] = []
            self.is_running = False

        def subscribe(self, security_ids: Sequence[str], *, segment: int | None = None) -> None:
            self.calls.append((tuple(str(s) for s in security_ids), segment))

        def start(self, on_tick: object) -> None: ...
        def request_stop(self) -> None: ...
        def stop(self) -> None: ...

    inner = _Recorder()
    feed = ReconnectingFeed(inner)  # type: ignore[arg-type]
    feed.subscribe(["13"])
    feed.subscribe(["49081"], segment=NSE_FNO)

    _, last_segment = inner.calls[-1]
    assert "13" not in inner.calls[-1][0], "the underlying was relabelled"
    assert last_segment == NSE_FNO
