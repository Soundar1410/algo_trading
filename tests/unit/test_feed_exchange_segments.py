"""Per-instrument exchange segments and subscription modes.

**Segments (Phase 4 Part 1).** The second half of runbook limitation 17, and the
half the limitation's own text did not mention. Resolving a real ``security_id``
is not enough to subscribe it: ``DhanMarketFeedAdapter`` held **one** segment for
every instrument, while an options runtime needs two at once — the underlying
index in ``IDX_I`` (0) and its contracts in ``NSE_FNO`` (2).

**Modes (Phase 4 Part 5).** The identical problem one field over. The adapter held
one *mode* for every instrument too, and an options runtime needs two of those as
well: the underlying on Ticker (an index has no order book to stream) and its
contracts on Full (the only mode that carries one, and the one the paper fill
model prices against).

Why this is worth its own suite: neither mistake raises. A wrong segment is
accepted and then delivers nothing, so it presents as a quiet market; a wrong mode
delivers ticks with no book at all, so it presents as a fill model that silently
prices every order off last price. No test could have caught either before,
because every test drove one instrument type in one mode.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from common.feed.hub import SharedFeedHub, WorkerChannel, build_channel
from common.feed.queues import BoundedWorkerQueue
from common.market_data.dhan import (
    FULL_MODE,
    QUOTE_MODE,
    TICKER_MODE,
    DhanFeedError,
    DhanMarketFeedAdapter,
)
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

    def modes(self) -> dict[str, int]:
        """security_id → subscription mode across every call."""
        return {sid: mode for batch in self.sent for _, sid, mode in batch}


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


# -------------------------------------------------------------- mixed modes
def test_an_instrument_with_no_named_mode_uses_the_adapter_default():
    adapter = _adapter(feed_mode=QUOTE_MODE)
    adapter.subscribe(["49081"])
    assert adapter.mode_for("49081") == QUOTE_MODE


def test_the_underlying_stays_on_ticker_while_its_option_goes_full():
    """The property the mode split exists for, and the reason Part 1 had to come
    first: only a real ``NSE_FNO`` option in Full mode carries a book at all."""
    adapter = _adapter()  # default IDX_I / Ticker, for the underlying
    feed = _FakeFeed()
    adapter._feed = feed

    adapter.subscribe(["13"])
    adapter.subscribe(["49081"], segment=NSE_FNO, mode=FULL_MODE)

    assert feed.modes() == {"13": TICKER_MODE, "49081": FULL_MODE}
    assert feed.flat() == {"13": IDX_I, "49081": NSE_FNO}
    assert adapter.mode_for("13") == TICKER_MODE
    assert adapter.mode_for("49081") == FULL_MODE


def test_a_reconnect_restores_each_instrument_to_its_own_mode():
    """Without this, a reconnect silently demotes the traded contract to Ticker
    and the fill model goes on calling itself depth-driven while pricing every
    fill off last price."""
    adapter = _adapter()
    adapter.subscribe(["13"])
    adapter.subscribe(["49081"], segment=NSE_FNO, mode=FULL_MODE)

    feed = _FakeFeed()
    adapter._feed = feed
    adapter.resubscribe_all()

    assert feed.modes() == {"13": TICKER_MODE, "49081": FULL_MODE}


def test_an_instrument_may_be_promoted_to_a_richer_mode():
    """Unlike a segment, this is not a contradiction — the same instrument in a
    richer mode is the same instrument — so it overwrites rather than refusing."""
    adapter = _adapter()
    adapter.subscribe(["49081"], segment=NSE_FNO)
    adapter.subscribe(["49081"], segment=NSE_FNO, mode=FULL_MODE)
    assert adapter.mode_for("49081") == FULL_MODE


@pytest.mark.parametrize("bad", [0, 19, 21.0, 99])
def test_a_mode_the_protocol_does_not_accept_is_refused(bad: object):
    """v2 accepts 15/17/21 only. Mode 19 (20-level depth) is v1-only, and letting
    it through would fail inside the SDK's batching, where the message names no
    instrument."""
    adapter = _adapter()
    with pytest.raises(DhanFeedError, match="Unsupported feed mode"):
        adapter.subscribe(["49081"], mode=bad)  # type: ignore[arg-type]


def test_an_unusable_default_mode_is_refused_at_construction():
    with pytest.raises(DhanFeedError, match="Unsupported feed mode"):
        _adapter(feed_mode=19)


def test_the_engine_and_the_adapter_agree_on_the_mode_numbers():
    """Two declarations of the same protocol constants — the ported engine's
    ``SubscriptionMode`` and the adapter's — must not drift apart."""
    from common.engine.feed import SubscriptionMode

    assert (int(SubscriptionMode.TICKER), int(SubscriptionMode.QUOTE)) == (
        TICKER_MODE,
        QUOTE_MODE,
    )
    assert int(SubscriptionMode.FULL) == FULL_MODE


def test_the_capture_script_agrees_on_the_mode_numbers():
    """``capture_live_tape`` repeats them rather than importing the adapter, which
    would undo the lazy SDK import. This is what keeps the repetition honest."""
    from scripts.capture_live_tape import _MODES

    assert _MODES == {"ticker": TICKER_MODE, "quote": QUOTE_MODE, "full": FULL_MODE}


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

    hub.request_subscription("s1", "49081", segment=NSE_FNO, mode=FULL_MODE)
    hub._apply_pending_subscriptions()

    assert adapter.requested_segments == {"49081": NSE_FNO}
    assert adapter.requested_modes == {"49081": FULL_MODE}
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
    assert adapter.requested_modes == {}, "no mode should have been asserted"
    assert "49081" in adapter.subscribed


# ------------------------------------------------ hub.start(): the initial subscription
def test_hub_start_subscribes_the_underlying_on_its_own_declared_segment():
    """The regression this whole module exists to prevent: before this,
    ``SharedFeedHub.start()`` sent the *entire* union through one
    ``adapter.subscribe(sorted(union))`` call with no segment at all, so a
    channel's underlying went out under the adapter's own default — tuned for
    its *option* subscriptions — and silently received nothing."""
    adapter = RecordedFeedAdapter([])
    hub = SharedFeedHub(adapter)
    hub.register(
        WorkerChannel(
            strategy_id="s1",
            security_ids=frozenset({"13"}),
            queue=BoundedWorkerQueue.in_process("s1"),
            segment=IDX_I,
        )
    )

    hub.start()

    assert adapter.requested_segments == {"13": IDX_I}


def test_hub_start_groups_two_channels_by_their_declared_segment():
    """Two channels naming different segments must reach the adapter as two
    separate calls, not one flattened under whichever segment happened to be
    named last."""
    adapter = RecordedFeedAdapter([])
    hub = SharedFeedHub(adapter)
    hub.register(
        WorkerChannel(
            strategy_id="s1",
            security_ids=frozenset({"13"}),
            queue=BoundedWorkerQueue.in_process("s1"),
            segment=IDX_I,
        )
    )
    hub.register(
        WorkerChannel(
            strategy_id="s2",
            security_ids=frozenset({"49081"}),
            queue=BoundedWorkerQueue.in_process("s2"),
            segment=NSE_FNO,
            mode=FULL_MODE,
        )
    )

    hub.start()

    assert adapter.requested_segments == {"13": IDX_I, "49081": NSE_FNO}
    assert adapter.requested_modes == {"49081": FULL_MODE}
    assert adapter.subscribed == frozenset({"13", "49081"})


def test_hub_start_with_no_declared_segments_is_one_call_as_before():
    """Every channel leaving segment/mode unset must collapse to exactly the
    call this module's fix replaces — the union, one call, no kwargs asserted."""
    adapter = RecordedFeedAdapter([])
    hub = SharedFeedHub(adapter)
    hub.register(build_channel("s1", ["A", "B"], in_process=True))
    hub.register(build_channel("s2", ["B", "C"], in_process=True))

    hub.start()

    assert adapter.subscribed == frozenset({"A", "B", "C"})
    assert adapter.requested_segments == {}
    assert adapter.requested_modes == {}


def test_hub_start_refuses_one_instrument_under_two_segments():
    adapter = RecordedFeedAdapter([])
    hub = SharedFeedHub(adapter)
    hub.register(
        WorkerChannel(
            strategy_id="s1",
            security_ids=frozenset({"13"}),
            queue=BoundedWorkerQueue.in_process("s1"),
            segment=IDX_I,
        )
    )
    hub.register(
        WorkerChannel(
            strategy_id="s2",
            security_ids=frozenset({"13"}),
            queue=BoundedWorkerQueue.in_process("s2"),
            segment=NSE_FNO,
        )
    )

    with pytest.raises(RuntimeError, match="conflicting"):
        hub.start()


# ------------------------------------------------------- through the reconnect layer
class _Recorder:
    """A minimal ``MarketFeedAdapter`` that records what it was asked to subscribe."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], int | None, int | None]] = []
        self.is_running = False

    def subscribe(
        self,
        security_ids: Sequence[str],
        *,
        segment: int | None = None,
        mode: int | None = None,
    ) -> None:
        self.calls.append((tuple(str(s) for s in security_ids), segment, mode))

    def start(self, on_tick: object) -> None: ...
    def request_stop(self) -> None: ...
    def stop(self) -> None: ...


def test_the_reconnect_wrapper_carries_the_segment_through():
    """``ReconnectingFeed`` sits between the hub and the adapter in a live
    deployment, so a segment it dropped would never reach the socket."""
    from common.feed.reconnect import ReconnectingFeed

    inner = _Recorder()
    feed = ReconnectingFeed(inner)  # type: ignore[arg-type]
    feed.subscribe(["13"])
    feed.subscribe(["49081"], segment=NSE_FNO)

    assert inner.calls == [(("13",), None, None), (("49081",), NSE_FNO, None)]


def test_the_reconnect_wrapper_carries_the_mode_through():
    """Phase 4 Part 5: a dropped mode would silently demote an option contract to
    Ticker after a reconnect, and the fill model would go on reporting itself as
    depth-driven while pricing every fill off last price."""
    from common.engine.feed import SubscriptionMode
    from common.feed.reconnect import ReconnectingFeed

    inner = _Recorder()
    feed = ReconnectingFeed(inner)  # type: ignore[arg-type]
    feed.subscribe(["13"])
    feed.subscribe(["49081"], segment=NSE_FNO, mode=int(SubscriptionMode.FULL))

    assert inner.calls[-1] == (("49081",), NSE_FNO, 21)


def test_the_reconnect_wrapper_does_not_relabel_earlier_instruments():
    """Re-sending the whole set under one segment would move the underlying
    onto the option segment — the exact silent failure being prevented."""
    from common.feed.reconnect import ReconnectingFeed

    inner = _Recorder()
    feed = ReconnectingFeed(inner)  # type: ignore[arg-type]
    feed.subscribe(["13"])
    feed.subscribe(["49081"], segment=NSE_FNO)

    ids, last_segment, _ = inner.calls[-1]
    assert "13" not in ids, "the underlying was relabelled"
    assert last_segment == NSE_FNO
