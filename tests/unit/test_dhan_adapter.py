"""Dhan adapter: ratified normalisation and SDK isolation, without a network.

Two fixtures, deliberately kept separate rather than one being "replaced" by
the other:

* ``dhan_ticker_payloads_synthesised.json`` — built in Block 1 by packing
  Dhan's documented binary layouts and running them through the installed
  SDK's own ``process_data()``, so the shape is the SDK's, not something
  hand-written. It exists to exercise **every** normalisation branch —
  Ticker, Quote (for ``LTQ``), Previous Close, OI, a market-status string, and
  an untraded instrument — because a real single-instrument ticker-mode
  capture cannot naturally produce most of those: Quote Data needs a
  ``QUOTE_MODE`` subscription we don't use, OI/status frames may or may not
  appear in any given window, and a liquid index never stops trading.
* ``dhan_ticker_payloads_real.json`` — captured in Block 2 from a genuine
  Dhan connection (``scripts/capture_live_tape.py``). It exists to prove the
  shape *actually observed* matches what Block 1 inferred from source; it is
  not used for branch coverage because it cannot supply the branches above.

Three Phase 1 defects are pinned here so they cannot regress: the time-only
``LTT``, the non-existent ``last_quantity`` key, and non-dict frames being
miscounted as malformed.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from common.market_data.dhan import (
    DISCONNECT_REASONS,
    DISCONNECT_TOKEN_EXPIRED,
    DhanFeedError,
    DhanMarketFeedAdapter,
    reconstruct_exchange_time,
)

#: LTT is IST wall-clock (known limitation 20) -- tests that compare a
#: reconstructed exchange_time back against a raw LTT string must convert.
IST = ZoneInfo("Asia/Kolkata")

REPO_ROOT = Path(__file__).resolve().parents[2]
SYNTH_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "dhan_ticker_payloads_synthesised.json"
REAL_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "dhan_ticker_payloads_real.json"


def _grouped_by_label(path: Path) -> dict[str, list[dict[str, Any]]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    grouped: dict[str, list[dict[str, Any]]] = {}
    for frame in data["frames"]:
        grouped.setdefault(frame["label"], []).append(frame["payload"])
    return grouped


#: Exhaustive, synthesised — used by every branch-coverage test below.
SYNTH_FRAMES = _grouped_by_label(SYNTH_FIXTURE)


def _adapter(**kwargs: Any) -> DhanMarketFeedAdapter:
    return DhanMarketFeedAdapter(
        client_id="test-client",
        access_token="test-token",
        instrument_label="NIFTY",
        **kwargs,
    )


# ------------------------------------------------------------ construction
def test_missing_credentials_are_refused_at_construction():
    with pytest.raises(DhanFeedError, match="client id and an access token"):
        DhanMarketFeedAdapter(client_id="", access_token="token")


def test_starting_without_a_subscription_is_refused():
    with pytest.raises(DhanFeedError, match="no subscriptions"):
        _adapter().start(lambda tick: None)


def test_subscriptions_are_a_union_not_a_list():
    adapter = _adapter()
    adapter.subscribe(["1", "2"])
    adapter.subscribe(["2", "3"])
    assert adapter.subscribed == {"1", "2", "3"}


def test_stopping_an_unstarted_adapter_is_safe():
    _adapter().stop()  # must not raise


# ---------------------------------------- the synthesised fixture's shape
def test_the_synthesised_fixture_carries_the_sdk_shape_not_a_guess():
    """Guards the assumptions every branch-coverage test here rests on.

    If a future SDK changes these, this test fails loudly instead of the
    normalisation quietly producing wrong ticks.
    """
    ticker = SYNTH_FRAMES["ticker"][0]
    assert ticker["type"] == "Ticker Data"
    assert isinstance(ticker["LTP"], str), "LTP is a formatted string, not a float"
    assert isinstance(ticker["security_id"], int), "security_id is an int, not a str"
    assert isinstance(ticker["LTT"], str) and len(ticker["LTT"]) == 8, "LTT is HH:MM:SS"
    assert "last_quantity" not in ticker, "Phase 1 read a key that does not exist"
    assert "LTQ" not in ticker, "quantity is Quote-only"
    assert "LTQ" in SYNTH_FRAMES["quote"][0], "Quote Data does carry LTQ"


#: The five depth levels packed into each synthesised Full frame, as
#: ``(bid_quantity, ask_quantity, bid_orders, ask_orders, bid_price, ask_price)``.
#: Keyed by the fixture label they produced.
_FULL_BOOKS: dict[str, list[tuple[int, int, int, int, float, float]]] = {
    "full": [
        (900, 750, 3, 2, 187.40, 187.50),
        (1200, 1500, 5, 6, 187.35, 187.55),
        (2100, 1800, 8, 7, 187.30, 187.60),
        (3000, 2400, 11, 9, 187.25, 187.65),
        (4500, 3900, 14, 12, 187.20, 187.70),
    ],
    "full_bid_only": [(900, 0, 3, 0, 187.40, 0.0)] + [(0, 0, 0, 0, 0.0, 0.0)] * 4,
    "full_empty_book": [(0, 0, 0, 0, 0.0, 0.0)] * 5,
}


def _sdk_full_frame(levels: list[tuple[int, int, int, int, float, float]]) -> dict[str, Any]:
    """Pack Dhan's documented 162-byte Full layout and let the **SDK** parse it.

    First byte 8, then ``'<BHBIfHIfIIIIIIffff100s'``: the trailing 100 bytes are
    five 20-byte depth levels in ``'<IIHHff'``. Nothing here hand-writes the
    resulting dict — ``process_data`` builds it — so this asserts against the
    pinned SDK's behaviour rather than against a belief about it.
    """
    import struct

    from dhanhq import marketfeed

    depth = b"".join(struct.pack("<IIHHff", *level) for level in levels)
    assert len(depth) == 100
    raw = struct.pack(
        "<BHBIfHIfIIIIIIffff100s",
        8, 162, 2, 49081, 187.45, 75, 1_754_381_700, 187.42,
        123456, 4000, 4200, 55000, 56000, 54000,
        180.0, 179.5, 191.0, 176.0, depth,
    )  # fmt: skip
    parser = marketfeed.MarketFeed.__new__(marketfeed.MarketFeed)
    return marketfeed.MarketFeed.process_data(parser, raw)  # type: ignore[no-any-return]


@pytest.mark.parametrize("label", sorted(_FULL_BOOKS))
def test_the_committed_full_frames_still_match_the_pinned_sdk(label: str):
    """The fixture cannot drift from the SDK it claims to describe.

    Phase 4 Part 5 added the Full frames, and the whole fill model rests on their
    shape — string prices, ``"0.00"`` for an empty level, five levels under
    ``depth``. Regenerating them here means an SDK bump that changes the layout
    fails *this* test, loudly, instead of leaving a stale JSON blob to disagree
    with the parser in production.
    """
    assert SYNTH_FRAMES[label][0] == _sdk_full_frame(_FULL_BOOKS[label])


def test_the_full_frame_prices_are_formatted_strings_not_numbers():
    top = SYNTH_FRAMES["full"][0]["depth"][0]
    assert isinstance(top["bid_price"], str) and isinstance(top["ask_price"], str)
    assert isinstance(top["bid_quantity"], int), "quantities are ints"
    assert len(SYNTH_FRAMES["full"][0]["depth"]) == 5


def test_a_ticker_frame_becomes_a_tick():
    adapter = _adapter()
    tick = adapter.normalise(SYNTH_FRAMES["ticker"][0])

    assert tick is not None
    assert tick.security_id == "49081"
    assert tick.last_price == pytest.approx(187.45)
    assert tick.instrument == "NIFTY"
    assert tick.exchange_time.tzinfo is not None
    assert adapter.counters.ticks == 1


def test_a_quote_frame_supplies_the_quantity_from_ltq():
    """Defect 2: Phase 1 read ``last_quantity``, which no frame contains, so live
    candle volume was always zero."""
    adapter = _adapter()
    tick = adapter.normalise(SYNTH_FRAMES["quote"][0])

    assert tick is not None
    assert tick.last_quantity == 75
    assert adapter.counters.ticks == 1


# --------------------------------------------------------------------- depth
def test_a_quote_frame_carries_no_depth_whatever_the_old_comment_said():
    """Ratified from SDK source in Part 5. ``dhan.py`` claimed "Quote/Full add
    depth"; ``process_quote`` returns volume and session OHLC and no book at all,
    so a Quote-mode subscription would have produced depth-less fills while the
    config believed otherwise."""
    quote = SYNTH_FRAMES["quote"][0]
    assert "depth" not in quote
    assert "depth" in SYNTH_FRAMES["full"][0], "only Full carries a book"

    tick = _adapter().normalise(quote)
    assert tick is not None
    assert tick.bid_price is None and tick.ask_price is None


def test_a_full_frame_becomes_a_tick_rather_than_a_silently_dropped_non_tick():
    """The defect that made mode 21 unusable. ``"Full Data"`` was in neither
    ``_TICK_TYPES`` nor ``_NON_TICK_TYPES``, so it fell through to the
    unrecognised-type branch: a **non-tick** count and a *debug* log. A Full-mode
    feed would have run connected and silent — no candles, no indicators, no
    orders — with nothing above debug level saying why."""
    adapter = _adapter()
    tick = adapter.normalise(SYNTH_FRAMES["full"][0])

    assert tick is not None, "a Full frame must produce a tick"
    assert adapter.counters.ticks == 1
    assert adapter.counters.non_tick_frames == 0


def test_a_full_frame_carries_the_top_of_book_onto_the_tick():
    adapter = _adapter()
    tick = adapter.normalise(SYNTH_FRAMES["full"][0])

    assert tick is not None
    assert tick.bid_price == pytest.approx(187.40)
    assert tick.ask_price == pytest.approx(187.50)
    assert tick.last_quantity == 75
    assert adapter.counters.ticks_with_depth == 1
    assert adapter.counters.depth_ratio == 1.0


def test_only_the_best_level_is_carried():
    """Five levels arrive; the fill model prices against the touch. Carrying all
    five across the IPC queue on every tick would cost pickle size on the hot path
    for data nothing consumes (spec section 6: no complex exchange simulator)."""
    top = SYNTH_FRAMES["full"][0]["depth"][0]
    second = SYNTH_FRAMES["full"][0]["depth"][1]
    assert float(second["bid_price"]) < float(top["bid_price"])

    tick = _adapter().normalise(SYNTH_FRAMES["full"][0])
    assert tick is not None and tick.bid_price == pytest.approx(float(top["bid_price"]))


def test_a_zero_price_level_means_absence_not_a_bid_of_zero():
    """The trap in the reference repository's normaliser, which this does not copy.

    An untraded or one-sided strike renders its empty side as ``"0.00"`` rather
    than omitting it. Read as a number, that makes the quote look two-sided with a
    bid of zero — and a sell would then price at ``0 - slippage`` and be refused by
    the simulator, i.e. every exit on a one-sided book would fail.
    """
    frame = SYNTH_FRAMES["full_bid_only"][0]
    assert frame["depth"][0]["ask_price"] == "0.00", "the SDK renders absence as 0.00"

    adapter = _adapter()
    tick = adapter.normalise(frame)

    assert tick is not None
    assert tick.bid_price == pytest.approx(187.40)
    assert tick.ask_price is None, "a 0.00 ask is no ask"
    assert adapter.counters.ticks_with_depth == 0
    assert adapter.counters.ticks_one_sided_book == 1


def test_an_empty_book_still_produces_a_tradeable_tick():
    """The price is real even when nothing is resting: the fill model falls back to
    LTP and records that it did, rather than the tick being dropped."""
    adapter = _adapter()
    tick = adapter.normalise(SYNTH_FRAMES["full_empty_book"][0])

    assert tick is not None
    assert tick.last_price == pytest.approx(187.45)
    assert tick.bid_price is None and tick.ask_price is None
    assert adapter.counters.ticks_one_sided_book == 1


def test_a_ticker_frame_is_not_counted_as_a_one_sided_book():
    """Two different problems needing two different responses: an illiquid strike
    on Full, versus a subscription that carries no book in the first place."""
    adapter = _adapter()
    adapter.normalise(SYNTH_FRAMES["ticker"][0])
    assert adapter.counters.ticks_one_sided_book == 0


def test_per_instrument_labels_are_applied():
    adapter = _adapter(instrument_labels={"13": "NIFTY", "49081": "NIFTY24JUL25000CE"})
    assert adapter.normalise(SYNTH_FRAMES["ticker"][0]).instrument == "NIFTY24JUL25000CE"  # type: ignore[union-attr]
    assert adapter.normalise(SYNTH_FRAMES["ticker"][3]).instrument == "NIFTY"  # type: ignore[union-attr]


# ------------------------------------------------------- exchange timestamps
def test_the_time_only_ltt_is_reconstructed_and_not_a_fallback():
    """Defect 1, the most consequential one.

    ``LTT`` is time-only — no date. Phase 1 passed it to
    ``datetime.fromisoformat``, which raises, so *every* tick silently fell
    back to receipt time and candles were bucketed by arrival rather than by
    exchange time. That is precisely the opposite of the aggregator's
    contract.

    ``LTT`` is IST wall-clock, not UTC (known limitation 20, fixed 6 August
    2026), so the reconstructed ``exchange_time`` is genuinely UTC and must be
    converted back to IST before it matches the raw string byte-for-byte.
    """
    adapter = _adapter()
    tick = adapter.normalise(SYNTH_FRAMES["ticker"][0])

    assert tick is not None
    assert (
        tick.exchange_time.astimezone(IST).strftime("%H:%M:%S") == SYNTH_FRAMES["ticker"][0]["LTT"]
    )
    assert tick.exchange_time != tick.received_at
    assert adapter.counters.exchange_time_fallbacks == 0
    assert adapter.counters.fallback_ratio == 0.0


def test_a_real_captured_ist_ltt_converts_correctly_to_utc():
    """Fail-first regression for known limitation 20, using the actual values
    captured live on 6 August 2026. At a genuine receipt of
    2026-08-06 05:38:49.473969 UTC, Dhan's ``LTT`` read ``"11:08:48"`` — the
    IST wall clock, not UTC. The old code relabelled those digits as UTC and
    produced ``2026-08-06 11:08:48+00:00``, a timestamp 5:30 in the future
    relative to ``received_at``; that is what tripped
    ``tick.exchange_time <= tick.received_at`` live, the trip-wire this
    function's caller has always carried. The correct value, converting from
    IST, is one second *before* receipt — what real exchange latency actually
    looks like.
    """
    received = datetime(2026, 8, 6, 5, 38, 49, 473969, tzinfo=UTC)
    moment = reconstruct_exchange_time("11:08:48", received)
    assert moment == datetime(2026, 8, 6, 5, 38, 48, tzinfo=UTC)
    assert moment <= received, "exchange time must not be in the future relative to receipt"


def test_ltt_picks_the_date_closest_to_receipt():
    """``LTT`` is IST wall-clock (known limitation 20): ``"08:35:03"`` means
    08:35:03 IST, which converts to 03:05:03 UTC — not 08:35:03 UTC."""
    received = datetime(2026, 7, 30, 8, 35, 10, tzinfo=UTC)
    moment = reconstruct_exchange_time("08:35:03", received)
    assert moment == datetime(2026, 7, 30, 3, 5, 3, tzinfo=UTC)


def test_ltt_just_before_ist_midnight_resolves_to_the_previous_day():
    """A rollover guard, anchored on IST midnight — the zone ``LTT`` is
    actually in (known limitation 20; this used to anchor on UTC midnight,
    the wrong zone). It cannot arise for Indian equity/F&O — that session is
    09:15-15:30 IST, nowhere near midnight — but it costs nothing and does
    not assume other segments keep that property."""
    received = datetime(2026, 7, 30, 18, 30, 5, tzinfo=UTC)  # 2026-07-31 00:00:05 IST
    moment = reconstruct_exchange_time("23:59:58", received)
    assert moment == datetime(2026, 7, 30, 18, 29, 58, tzinfo=UTC)  # 2026-07-30 23:59:58 IST


def test_ltt_just_after_ist_midnight_resolves_to_the_next_day():
    received = datetime(2026, 7, 30, 18, 29, 58, tzinfo=UTC)  # 2026-07-30 23:59:58 IST
    moment = reconstruct_exchange_time("00:00:03", received)
    assert moment == datetime(2026, 7, 30, 18, 30, 3, tzinfo=UTC)  # 2026-07-31 00:00:03 IST


def test_an_epoch_timestamp_is_still_accepted():
    """Most likely future SDK change; accepted so it would not be a silent
    fallback."""
    moment = reconstruct_exchange_time(1_785_400_503, datetime.now(UTC))
    assert moment == datetime.fromtimestamp(1_785_400_503, tz=UTC)


def test_an_iso_timestamp_is_still_accepted():
    moment = reconstruct_exchange_time("2026-07-30T08:35:03+00:00", datetime.now(UTC))
    assert moment == datetime(2026, 7, 30, 8, 35, 3, tzinfo=UTC)


def test_a_naive_iso_timestamp_is_assumed_utc():
    moment = reconstruct_exchange_time("2026-07-30T08:35:03", datetime.now(UTC))
    assert moment == datetime(2026, 7, 30, 8, 35, 3, tzinfo=UTC)


@pytest.mark.parametrize("raw", [None, "", "   ", "garbage", "25:99:99", 0, -1, True, False])
def test_an_unusable_timestamp_yields_none_so_the_caller_can_count_it(raw: object):
    """Epoch 0 is "no trade yet on this instrument", not 1970."""
    assert reconstruct_exchange_time(raw, datetime.now(UTC)) is None


def test_a_missing_timestamp_falls_back_to_receipt_and_is_counted():
    adapter = _adapter()
    tick = adapter.normalise({"type": "Ticker Data", "security_id": "13", "LTP": "100.0"})

    assert tick is not None
    assert tick.exchange_time == tick.received_at
    assert adapter.counters.exchange_time_fallbacks == 1
    assert adapter.counters.fallback_ratio == 1.0, (
        "a 100% fallback rate is what Phase 1 had and could not see"
    )


def test_an_instrument_with_no_trades_yet_is_rejected_outright():
    """An LTT of epoch 0 renders as "00:00:00", and its LTP is not a trade price.

    Three behaviours were possible and only one is defensible. Reconstructing it
    as a real midnight UTC would emit a tick timestamped 05:30 IST — harmless
    only by luck, because the aggregator's session filter happens to drop it.
    Substituting receipt time would be worse: the non-price would land in the
    current candle. Rejecting it upholds the spec's rule that a stale price is
    never treated as a fresh unchanged price.
    """
    adapter = _adapter()
    assert adapter.normalise(SYNTH_FRAMES["ticker_no_trade_yet"][0]) is None
    assert adapter.counters.untraded_frames == 1
    assert adapter.counters.ticks == 0
    assert adapter.counters.malformed_payloads == 0, "not traded is not malformed"
    assert adapter.counters.exchange_time_fallbacks == 0


def test_a_numeric_zero_ltt_is_also_treated_as_never_traded():
    adapter = _adapter()
    payload = {"type": "Ticker Data", "security_id": "49082", "LTP": "0.05", "LTT": 0}
    assert adapter.normalise(payload) is None
    assert adapter.counters.untraded_frames == 1


def test_a_missing_ltt_still_falls_back_rather_than_being_rejected():
    """Absent is not the same as zero: "we do not know when this traded" is a
    legitimate fallback, whereas "it has not traded" is not a tick at all."""
    adapter = _adapter()
    tick = adapter.normalise({"type": "Ticker Data", "security_id": "13", "LTP": "100.0"})

    assert tick is not None
    assert tick.exchange_time == tick.received_at
    assert adapter.counters.untraded_frames == 0
    assert adapter.counters.exchange_time_fallbacks == 1


# ----------------------------------------------------- frame classification
def test_a_market_status_string_is_a_non_tick_not_a_malformed_payload():
    """Defect 3: ``process_status`` returns the bare string "Markets Open".

    Phase 1's ``isinstance(payload, dict)`` guard counted it as malformed, so
    ordinary traffic inflated the malformed counter and hid a genuine problem.
    """
    adapter = _adapter()
    assert adapter.normalise("Markets Open") is None
    assert adapter.counters.non_tick_frames == 1
    assert adapter.counters.malformed_payloads == 0


def test_an_empty_frame_is_counted_separately():
    """``process_data`` returns None for an unrecognised first byte, and for a
    server-disconnection frame."""
    adapter = _adapter()
    assert adapter.normalise(None) is None
    assert adapter.counters.empty_frames == 1
    assert adapter.counters.malformed_payloads == 0


@pytest.mark.parametrize("label", ["prev_close", "oi"])
def test_recognised_non_tick_frames_are_ignored_cleanly(label: str):
    adapter = _adapter()
    assert adapter.normalise(SYNTH_FRAMES[label][0]) is None
    assert adapter.counters.non_tick_frames == 1
    assert adapter.counters.malformed_payloads == 0
    assert adapter.counters.ticks == 0


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "Ticker Data", "security_id": "13"},
        {"type": "Ticker Data", "security_id": "13", "LTP": "0.00"},
        {"type": "Ticker Data", "security_id": "", "LTP": "100.0"},
        {"type": "Ticker Data", "LTP": "100.0"},
        {"type": "Ticker Data", "security_id": "13", "LTP": "not-a-number"},
    ],
)
def test_a_genuinely_malformed_tick_is_counted_not_raised(payload: object):
    """One bad frame must not tear down the feed for every worker in the group."""
    adapter = _adapter()
    assert adapter.normalise(payload) is None
    assert adapter.counters.malformed_payloads == 1
    assert adapter.counters.ticks == 0


@pytest.mark.parametrize("payload", [42, 3.14, [1, 2], ("a",), object()])
def test_an_unexpected_payload_type_is_malformed(payload: object):
    adapter = _adapter()
    assert adapter.normalise(payload) is None
    assert adapter.counters.malformed_payloads == 1


def test_replaying_the_whole_synthesised_fixture_produces_only_ticks_and_recognised_frames():
    """No frame the SDK can emit may land in the malformed bucket.

    Exact counts are pinned deliberately: this fixture was built to contain a
    known, fixed set of frame kinds, so a change in these numbers means either
    the fixture changed or normalise() started classifying something
    differently — both worth an explicit look, not a silent pass.
    """
    adapter = _adapter()
    for frame in json.loads(SYNTH_FIXTURE.read_text(encoding="utf-8"))["frames"]:
        adapter.normalise(frame["payload"])

    assert adapter.counters.malformed_payloads == 0
    assert adapter.counters.ticks == 8  # 4 ticker + 1 quote + 3 full
    assert adapter.counters.non_tick_frames == 3  # prev_close, oi, status
    assert adapter.counters.untraded_frames == 1
    assert adapter.counters.ticks_with_depth == 1  # only the two-sided Full frame
    assert adapter.counters.ticks_one_sided_book == 2  # bid-only and empty-book
    assert adapter.counters.exchange_time_fallbacks == 0, (
        "every tick in the fixture carries a reconstructable exchange timestamp"
    )


# --------------------------------------- the real fixture: shape ratification
def test_the_real_fixture_is_actually_captured_not_synthesised():
    """Guards against the two fixtures ever being silently swapped."""
    data = json.loads(REAL_FIXTURE.read_text(encoding="utf-8"))
    assert data["source"] == "captured"


def test_the_real_fixture_matches_the_shape_block_1_inferred_from_source():
    """The Block 2 ratification claim, checked directly: a genuine Dhan
    connection produces exactly the shape Block 1 inferred by reading SDK
    source, not merely something normalise() happens to tolerate."""
    real_frames = _grouped_by_label(REAL_FIXTURE)
    ticker = real_frames["ticker_data"][0]

    assert ticker["type"] == "Ticker Data"
    assert isinstance(ticker["LTP"], str), "LTP is a formatted string, not a float"
    assert isinstance(ticker["security_id"], int), "security_id is an int, not a str"
    assert isinstance(ticker["LTT"], str) and len(ticker["LTT"]) == 8, "LTT is HH:MM:SS"
    assert "last_quantity" not in ticker
    assert set(ticker.keys()) == {"type", "exchange_segment", "security_id", "LTP", "LTT"}


def test_replaying_the_whole_real_fixture_produces_no_malformed_or_fallback_frames():
    """Loose by design, unlike the synthesised replay above: a live capture's
    exact frame mix will differ every time it is re-run (Step 3 may be
    repeated), so this asserts the properties that must always hold — nothing
    malformed, no timestamp fallback, at least one real tick — rather than
    today's specific counts."""
    adapter = _adapter()
    frames = json.loads(REAL_FIXTURE.read_text(encoding="utf-8"))["frames"]
    for frame in frames:
        adapter.normalise(frame["payload"])

    assert adapter.counters.malformed_payloads == 0
    assert adapter.counters.empty_frames == 0
    assert adapter.counters.exchange_time_fallbacks == 0, (
        "a real LTT that fails to reconstruct means the format changed"
    )
    assert adapter.counters.ticks > 0, "the capture must have observed at least one real tick"
    assert (
        adapter.counters.ticks + adapter.counters.non_tick_frames + adapter.counters.untraded_frames
        == len(frames)
    )


@pytest.mark.parametrize("path", [SYNTH_FIXTURE, REAL_FIXTURE], ids=["synthesised", "real"])
def test_no_fixture_contains_a_credential_or_account_identifier(path: Path):
    raw = path.read_text(encoding="utf-8")
    for forbidden in ("accessToken", "access_token", "dhanClientId", "pin", "totp"):
        assert forbidden not in raw, (
            f"{forbidden} must never appear in a test fixture ({path.name})"
        )


# ------------------------------------------------------ disconnect handling
def test_token_expiry_is_the_reason_code_that_needs_a_new_token():
    assert DISCONNECT_TOKEN_EXPIRED == 807
    assert DISCONNECT_REASONS[807] == "access token expired"
    for code in (805, 806, 808, 809):
        assert code in DISCONNECT_REASONS


def test_token_expired_is_false_until_dhan_says_so():
    adapter = _adapter()
    assert adapter.token_expired is False
    adapter.last_disconnect_code = 805
    assert adapter.token_expired is False
    adapter.last_disconnect_code = DISCONNECT_TOKEN_EXPIRED
    assert adapter.token_expired is True


def test_the_disconnect_probe_recovers_the_reason_code_the_sdk_only_prints():
    """2.2.0's ``server_disconnection`` prints the reason and returns None, so the
    code never reaches the caller — a token-expiry disconnect would be
    indistinguishable from an unknown frame, and reconnecting with the same dead
    token would loop forever."""
    import struct

    adapter = _adapter()
    calls: list[bytes] = []

    class _FakeFeed:
        def server_disconnection(self, data: bytes) -> None:
            calls.append(data)

    adapter._feed = _FakeFeed()
    adapter._install_disconnect_probe()

    frame = struct.pack("<BHBIH", 50, 10, 2, 0, DISCONNECT_TOKEN_EXPIRED)
    adapter._feed.server_disconnection(frame)

    assert adapter.last_disconnect_code == DISCONNECT_TOKEN_EXPIRED
    assert adapter.token_expired is True
    assert adapter.counters.disconnects == 1
    assert adapter.counters.disconnect_codes == {DISCONNECT_TOKEN_EXPIRED: 1}
    assert calls, "the SDK's own handler must still run"


def test_a_truncated_disconnect_frame_does_not_raise():
    adapter = _adapter()

    class _FakeFeed:
        def server_disconnection(self, data: bytes) -> None:
            return None

    adapter._feed = _FakeFeed()
    adapter._install_disconnect_probe()
    adapter._feed.server_disconnection(b"\x32")

    assert adapter.last_disconnect_code is None
    assert adapter.counters.disconnects == 0


# --------------------------------------------------------------- shutdown
def test_stop_uses_the_synchronous_close_not_the_coroutine():
    """Phase 1 called ``disconnect()``, a coroutine, so it built an un-awaited
    coroutine object and never closed the socket. ``close_connection()`` is the
    sync wrapper."""
    adapter = _adapter()
    closed: list[str] = []

    class _FakeFeed:
        def close_connection(self) -> None:
            closed.append("close_connection")

        async def disconnect(self) -> None:  # pragma: no cover - must not be called
            closed.append("disconnect")

    adapter._feed = _FakeFeed()
    adapter._running = True
    adapter.stop()

    assert closed == ["close_connection"]
    assert adapter.is_running is False


def test_stop_is_idempotent_and_survives_a_failing_close():
    adapter = _adapter()

    class _Angry:
        def close_connection(self) -> None:
            raise RuntimeError("socket already gone")

    adapter._feed = _Angry()
    adapter.stop()  # must not raise
    adapter.stop()  # second call has no feed at all


# ----------------------------------------------------------- resubscription
def test_resubscribe_all_sends_the_whole_set_not_the_delta():
    """A new socket carries none of the old subscriptions, while ``subscribe``
    deliberately sends only what is new."""
    adapter = _adapter()
    sent: list[list[tuple[int, str, int]]] = []

    class _FakeFeed:
        def subscribe_symbols(self, symbols: list[tuple[int, str, int]]) -> None:
            sent.append(symbols)

    adapter.subscribe(["49081", "13"])
    adapter._feed = _FakeFeed()
    adapter.resubscribe_all()

    assert len(sent) == 1
    assert {sid for _, sid, _ in sent[0]} == {"49081", "13"}


def test_resubscribing_with_no_subscriptions_is_a_no_op():
    adapter = _adapter()
    adapter._feed = object()  # would raise if touched
    adapter.resubscribe_all()


# --------------------------------------------------------- SDK isolation
def test_only_the_dhan_adapter_imports_the_sdk():
    """The spec's rule that strategies never call the SDK, enforced structurally.

    Phase 2 adds authentication, which speaks to Dhan over httpx rather than
    through the SDK's ``DhanLogin`` precisely so this stays a one-file rule.
    """
    result = subprocess.run(
        [
            "grep",
            "-rlnE",
            # Actual import statements, not mentions. Matching the bare word
            # would flag the docstrings that explain *why* auth avoids the SDK,
            # which is the opposite of the intended signal.
            r"^[[:space:]]*(import[[:space:]]+dhanhq|from[[:space:]]+dhanhq)",
            "--include=*.py",
            "common",
            "strategies",
            "runtimes",
            "dashboards",
            "scripts",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    importers = {line for line in result.stdout.splitlines() if line}
    assert importers == {"common/market_data/dhan.py"}, (
        f"dhanhq must only be imported by the adapter, found: {sorted(importers)}"
    )


def test_the_sdk_is_not_imported_at_package_import_time():
    """A lazy import keeps credential-free test runs fast and offline."""
    code = (
        "import sys; import common.market_data.dhan; "
        "assert 'dhanhq' not in sys.modules, 'dhanhq imported eagerly'"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_authentication_does_not_import_the_sdk():
    """DhanLogin exists in dhanhq 2.2.0; using it would breach the one-file rule."""
    code = (
        "import sys; import common.authentication; "
        "assert 'dhanhq' not in sys.modules, 'auth pulled in the SDK'"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
