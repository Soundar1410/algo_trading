"""Phase 1. The symbol-keyed daily cache, the overlap check and staleness
(spec 6.1, 6.2, 10.3).

Two groups of tests carry the weight.

**The cache key.** Spec 10.3's first rule for keeping the Monday decision run
offline is that the cache must be readable without resolving anything:
``ScripMasterCache`` keys its file by the IST date, so a Monday call to it
would download. ``test_the_filename_is_the_symbol_and_nothing_else`` and
``test_a_series_is_readable_without_any_identifier_but_the_symbol`` are what
hold that.

**The overlap check.** Dhan's daily history is back-adjusted — verified across
2,160 sessions spanning the Reliance 1:1 (Oct 2024) and HDFC Bank 1:1 (Aug
2025) bonuses. Back-adjustment is exactly what makes a naive tail-only refetch
unsafe: after a split, cached bars and freshly fetched bars are on different
scales, and appending one to the other invents a 50% gap. The re-adjustment
tests below reproduce a 1:1 bonus directly.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from strategies.positional_stocks.wsr1_weekly_stochrsi.daily_cache import (
    MAX_OVERLAP_DRIFT,
    OVERLAP_SESSIONS,
    SCHEMA_VERSION,
    DailyBarCache,
    MergeStatus,
    UnsafeSymbolError,
    assess_index_publication,
    assess_staleness,
    check_symbol,
    merge_tail,
    refetch_from,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.models import DailyBar
from strategies.positional_stocks.wsr1_weekly_stochrsi.trading_calendar import TradingCalendar

IST = ZoneInfo("Asia/Kolkata")
FETCHED_AT = datetime(2026, 9, 18, 18, 0, tzinfo=IST)

#: The real committed calendar. These tests assert against the holiday list the
#: platform actually runs on, not a fixture — a spec 6.2 case that passes only
#: against invented holidays proves nothing about next Friday.
CONFIG_ROOT = Path(__file__).resolve().parents[2] / "config"


def _series(start: date, count: int, *, close: float = 100.0, step: float = 1.0) -> list[DailyBar]:
    """``count`` consecutive weekday sessions from ``start``."""
    bars: list[DailyBar] = []
    day = start
    price = close
    while len(bars) < count:
        if day.isoweekday() <= 5:
            bars.append(
                DailyBar(
                    session=day,
                    open=price,
                    high=price + 2.0,
                    low=price - 2.0,
                    close=price,
                    volume=1000.0,
                )
            )
            price += step
        day += timedelta(days=1)
    return bars


def _rescaled(bars: list[DailyBar], factor: float) -> list[DailyBar]:
    """The same sessions after a corporate action restates every price."""
    return [
        DailyBar(
            session=bar.session,
            open=bar.open * factor,
            high=bar.high * factor,
            low=bar.low * factor,
            close=bar.close * factor,
            volume=bar.volume / factor,
        )
        for bar in bars
    ]


def _cache(tmp_path: Path) -> DailyBarCache:
    return DailyBarCache(tmp_path / "daily")


# ------------------------------------------------------------- symbol safety
@pytest.mark.parametrize("symbol", ["RELIANCE", "M&M", "BAJAJ-AUTO", "360ONE", "L&TFH"])
def test_real_nse_symbols_are_accepted_unchanged(symbol: str) -> None:
    assert check_symbol(symbol) == symbol


def test_symbols_are_normalised_to_upper_case() -> None:
    assert check_symbol("  reliance  ") == "RELIANCE"


@pytest.mark.parametrize(
    "symbol",
    ["", "   ", "../ESCAPE", "A/B", "WITH SPACE", "DOT.DOT/..", "a" * 40, "_LEADING"],
)
def test_an_unsafe_symbol_is_refused_rather_than_sanitised(symbol: str) -> None:
    """Mangling a symbol into a filename would let two symbols share one cache
    file, which is a wrong-price bug wearing a filesystem bug's clothes."""
    with pytest.raises(UnsafeSymbolError):
        check_symbol(symbol)


def test_an_unsafe_symbol_cannot_reach_a_path(tmp_path: Path) -> None:
    with pytest.raises(UnsafeSymbolError):
        _cache(tmp_path).path_for("../../etc/passwd")


# ------------------------------------------------------------- round tripping
def test_a_written_series_reads_back_identically(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    bars = _series(date(2026, 9, 1), 12)

    cache.write("RELIANCE", bars, fetched_at=FETCHED_AT, security_id="2885")

    assert cache.read("RELIANCE") == bars


def test_the_filename_is_the_symbol_and_nothing_else(tmp_path: Path) -> None:
    """Not the instrument id, not the run date. Spec 10.3: the decision run
    reads this cache having resolved nothing and authenticated nowhere."""
    cache = _cache(tmp_path)
    cache.write("M&M", _series(date(2026, 9, 1), 3), fetched_at=FETCHED_AT, security_id="2031")

    assert cache.path_for("M&M").name == "M&M.json"
    assert cache.symbols() == ("M&M",)


def test_a_series_is_readable_without_any_identifier_but_the_symbol(tmp_path: Path) -> None:
    """A second cache object, told nothing about instrument ids or run dates,
    reads the same series."""
    _cache(tmp_path).write(
        "RELIANCE",
        _series(date(2026, 9, 1), 5),
        fetched_at=FETCHED_AT,
        security_id="2885",
        exchange_segment="NSE_EQ",
        instrument="EQUITY",
    )

    assert len(DailyBarCache(tmp_path / "daily").read("RELIANCE")) == 5


def test_the_security_id_is_stored_as_provenance_only(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    cache.write(
        "RELIANCE",
        _series(date(2026, 9, 1), 3),
        fetched_at=FETCHED_AT,
        security_id="2885",
        exchange_segment="NSE_EQ",
        instrument="EQUITY",
    )

    meta = cache.metadata("RELIANCE")
    assert meta["security_id"] == "2885"
    assert meta["exchange_segment"] == "NSE_EQ"
    assert meta["schema_version"] == SCHEMA_VERSION
    assert meta["first_session"] == "2026-09-01"
    assert "bars" not in meta


def test_writing_replaces_rather_than_appends(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    cache.write("RELIANCE", _series(date(2026, 9, 1), 10), fetched_at=FETCHED_AT)
    cache.write("RELIANCE", _series(date(2026, 9, 1), 3), fetched_at=FETCHED_AT)

    assert len(cache.read("RELIANCE")) == 3


def test_bars_are_stored_and_returned_in_session_order(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    bars = _series(date(2026, 9, 1), 6)

    cache.write("RELIANCE", list(reversed(bars)), fetched_at=FETCHED_AT)

    assert cache.read("RELIANCE") == bars


def test_reading_an_absent_symbol_gives_an_empty_series(tmp_path: Path) -> None:
    assert _cache(tmp_path).read("NOTFETCHED") == []
    assert _cache(tmp_path).metadata("NOTFETCHED") == {}


def test_discard_removes_the_file_and_reports_whether_one_was_there(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    cache.write("RELIANCE", _series(date(2026, 9, 1), 3), fetched_at=FETCHED_AT)

    assert cache.discard("RELIANCE") is True
    assert cache.discard("RELIANCE") is False
    assert cache.read("RELIANCE") == []


# ------------------------------------------------------------ corrupt files
def test_a_corrupt_file_is_treated_as_absent_not_as_an_error(tmp_path: Path) -> None:
    """The answer to a bad cache file is to refetch the symbol. In ``decide``
    mode an empty series is then caught by the staleness check, which already
    fails closed with a clear reason."""
    cache = _cache(tmp_path)
    cache.write("RELIANCE", _series(date(2026, 9, 1), 3), fetched_at=FETCHED_AT)
    cache.path_for("RELIANCE").write_text("{not json", encoding="utf-8")

    assert cache.read("RELIANCE") == []


def test_a_file_from_an_older_schema_is_treated_as_absent(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    cache.write("RELIANCE", _series(date(2026, 9, 1), 3), fetched_at=FETCHED_AT)
    path = cache.path_for("RELIANCE")
    payload = json.loads(path.read_text())
    payload["schema_version"] = SCHEMA_VERSION - 1
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert cache.read("RELIANCE") == []


def test_a_file_with_an_impossible_bar_is_treated_as_absent(tmp_path: Path) -> None:
    """A partially usable series is worse than none: it parses and computes."""
    cache = _cache(tmp_path)
    cache.write("RELIANCE", _series(date(2026, 9, 1), 3), fetched_at=FETCHED_AT)
    path = cache.path_for("RELIANCE")
    payload = json.loads(path.read_text())
    payload["bars"][1][2] = 1.0  # high below low
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert cache.read("RELIANCE") == []


def test_a_failed_write_leaves_no_partial_file_behind(tmp_path: Path) -> None:
    """Atomic replace: a crash mid-write leaves the old file or the new one,
    never a truncated series that parses cleanly."""
    cache = _cache(tmp_path)
    good = _series(date(2026, 9, 1), 5)
    cache.write("RELIANCE", good, fetched_at=FETCHED_AT)

    class Exploding(datetime):
        def isoformat(self, *args: object, **kwargs: object) -> str:
            raise OSError("disk full")

    with pytest.raises(OSError):
        cache.write("RELIANCE", good, fetched_at=Exploding(2026, 9, 18, tzinfo=IST))

    assert cache.read("RELIANCE") == good, "the previous series survives"
    assert list(cache.root.glob(".daily_*.tmp")) == [], "no temp file is left"


# ------------------------------------------------------------ tail refetching
def test_refetch_from_backs_up_by_the_overlap(tmp_path: Path) -> None:
    cached = _series(date(2026, 8, 3), 30)

    start = refetch_from(cached)

    assert start == cached[-OVERLAP_SESSIONS].session
    assert sum(1 for bar in cached if bar.session >= start) == OVERLAP_SESSIONS


def test_refetch_from_on_a_short_series_starts_at_its_beginning() -> None:
    cached = _series(date(2026, 9, 1), 3)

    assert refetch_from(cached) == cached[0].session


def test_refetch_from_nothing_is_none_meaning_fetch_full_history() -> None:
    assert refetch_from([]) is None


def test_an_agreeing_overlap_merges_and_appends_the_new_tail() -> None:
    full = _series(date(2026, 8, 3), 30)
    cached, fetched = full[:20], full[10:]

    outcome = merge_tail(cached, fetched)

    assert outcome.status is MergeStatus.MERGED
    assert outcome.needs_full_refetch is False
    assert outcome.compared == 10
    assert list(outcome.bars) == full


def test_the_fetched_values_win_over_the_cached_ones_in_the_overlap() -> None:
    """The fetched copy is the more recently restated view. Preferring the
    cached one would preserve exactly the stale scale this check exists to
    detect."""
    cached = _series(date(2026, 8, 3), 20)
    fetched = [
        DailyBar(
            session=bar.session,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            # Inside the drift tolerance, so this is a revision, not a restatement.
            close=bar.close * (1 + MAX_OVERLAP_DRIFT / 2),
            volume=bar.volume,
        )
        for bar in cached[10:]
    ]

    outcome = merge_tail(cached, fetched)

    assert outcome.status is MergeStatus.MERGED
    assert outcome.bars[15].close == fetched[5].close


def test_a_tiny_price_revision_is_not_mistaken_for_a_corporate_action() -> None:
    cached = _series(date(2026, 8, 3), 20)
    fetched = [
        DailyBar(
            session=bar.session,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close + 0.01,  # two-decimal publication noise
            volume=bar.volume,
        )
        for bar in cached[10:]
    ]

    assert merge_tail(cached, fetched).status is MergeStatus.MERGED


# --------------------------------------------------------- re-adjustment path
def test_a_one_for_one_bonus_is_detected_as_a_restated_series() -> None:
    """The Reliance and HDFC Bank case: every historical price halves. The
    cached history is then on a pre-adjustment scale, and appending a
    post-adjustment tail to it would invent a 50% overnight gap."""
    cached = _series(date(2026, 8, 3), 20)
    fetched = _rescaled(cached[10:], 0.5)

    outcome = merge_tail(cached, fetched)

    assert outcome.status is MergeStatus.RE_ADJUSTED
    assert outcome.needs_full_refetch is True
    assert outcome.bars == (), "a partially rescaled series must not be offered"
    assert len(outcome.drifts) == 10
    assert outcome.drifts[0].relative == pytest.approx(0.5)


def test_the_re_adjustment_message_names_the_symbol_and_the_drift() -> None:
    cached = _series(date(2026, 8, 3), 20)
    outcome = merge_tail(cached, _rescaled(cached[10:], 0.5))

    described = outcome.describe("RELIANCE")

    assert "RELIANCE" in described
    assert "restated" in described
    assert "50.0%" in described


def test_a_single_drifted_session_is_enough_to_condemn_the_series() -> None:
    """Not a majority vote: one restated close means the scale changed."""
    cached = _series(date(2026, 8, 3), 20)
    fetched = list(cached[10:])
    fetched[3] = DailyBar(
        session=fetched[3].session,
        open=fetched[3].open,
        high=fetched[3].high * 2,
        low=fetched[3].low,
        close=fetched[3].close * 2,
        volume=fetched[3].volume,
    )

    outcome = merge_tail(cached, fetched)

    assert outcome.status is MergeStatus.RE_ADJUSTED
    assert len(outcome.drifts) == 1


def test_drift_exactly_at_the_tolerance_still_merges() -> None:
    cached = _series(date(2026, 8, 3), 20)
    fetched = [
        DailyBar(
            session=bar.session,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close * (1 + MAX_OVERLAP_DRIFT),
            volume=bar.volume,
        )
        for bar in cached[10:]
    ]

    assert merge_tail(cached, fetched).status is MergeStatus.MERGED


def test_a_first_fetch_with_nothing_cached_merges_trivially() -> None:
    fetched = _series(date(2026, 8, 3), 20)

    outcome = merge_tail([], fetched)

    assert outcome.status is MergeStatus.MERGED
    assert outcome.compared == 0
    assert list(outcome.bars) == fetched


def test_the_full_refetch_after_a_restatement_replaces_everything(tmp_path: Path) -> None:
    """End to end: detect, discard, refetch, and the cache holds one scale."""
    cache = _cache(tmp_path)
    original = _series(date(2026, 8, 3), 20)
    cache.write("RELIANCE", original, fetched_at=FETCHED_AT)

    outcome = merge_tail(cache.read("RELIANCE"), _rescaled(original[10:], 0.5))
    assert outcome.needs_full_refetch

    cache.discard("RELIANCE")
    cache.write("RELIANCE", _rescaled(original, 0.5), fetched_at=FETCHED_AT)

    stored = cache.read("RELIANCE")
    assert len(stored) == 20
    assert stored[0].close == pytest.approx(original[0].close * 0.5)
    gaps = [
        abs(stored[i].close - stored[i - 1].close) / stored[i - 1].close
        for i in range(1, len(stored))
    ]
    assert max(gaps) < 0.30, "one scale end to end, no invented gap"


# ------------------------------------------------------------------ staleness
def test_a_series_reaching_the_weeks_last_session_is_current() -> None:
    bars = _series(date(2026, 9, 14), 5)

    verdicts = assess_staleness({"RELIANCE": bars}, required_session=date(2026, 9, 18))

    assert verdicts[0].is_current is True
    assert verdicts[0].reason == ""


def test_a_series_stopping_short_is_stale_and_says_why() -> None:
    bars = _series(date(2026, 9, 14), 3)  # Mon..Wed

    verdict = assess_staleness({"RELIANCE": bars}, required_session=date(2026, 9, 18))[0]

    assert verdict.is_current is False
    assert "2026-09-16" in verdict.reason
    assert "2026-09-18" in verdict.reason


def test_an_empty_series_is_stale_rather_than_silently_current() -> None:
    verdict = assess_staleness({"RELIANCE": []}, required_session=date(2026, 9, 18))[0]

    assert verdict.is_current is False
    assert "no cached sessions" in verdict.reason


def test_a_symbol_that_traded_past_the_required_session_is_current() -> None:
    """A symbol whose series runs further than the week being decided is not
    stale — the check is a floor, not an equality."""
    bars = _series(date(2026, 9, 14), 10)

    verdict = assess_staleness({"RELIANCE": bars}, required_session=date(2026, 9, 18))[0]

    assert verdict.is_current is True


def test_verdicts_come_back_in_symbol_order() -> None:
    bars = _series(date(2026, 9, 14), 5)

    verdicts = assess_staleness(
        {"ZEEL": bars, "ABB": bars, "M&M": bars}, required_session=date(2026, 9, 18)
    )

    assert [v.symbol for v in verdicts] == ["ABB", "M&M", "ZEEL"]


# ----------------------------------------------- the index publication gate
#
# Phase 1 asked NIFTY 50's own data what the week's last session was
# (``reference_last_session``, now deleted). The Phase 1 review proved that
# unsafe: 21 Sep 2026 was a trading day, yet Dhan had not published its candle
# by 22:20 IST. The expected session now comes from the calendar and the index
# is checked against it. These tests are spec 6.2 v1.2b's four cases.


def test_the_index_having_the_expected_session_is_what_publishes_a_week() -> None:
    index = _series(date(2026, 9, 14), 5)  # Mon..Fri, W38

    verdict = assess_index_publication(index, week=(2026, 38), expected_session=date(2026, 9, 18))

    assert verdict.is_published is True
    assert verdict.last_session == date(2026, 9, 18)
    assert verdict.reason == ""


def test_a_missing_friday_candle_fails_closed_instead_of_making_thursday_the_week() -> None:
    """The exact defect. Dhan has not published Friday 18 Sep, so the index
    reaches Thursday. Phase 1 concluded "Thursday is the week's last session,
    everything is current". The calendar says Friday, so this is *unpublished*
    and the run stops."""
    index = _series(date(2026, 9, 14), 4)  # Mon..Thu only

    verdict = assess_index_publication(index, week=(2026, 38), expected_session=date(2026, 9, 18))

    assert verdict.is_published is False
    assert verdict.last_session == date(2026, 9, 17)
    assert "2026-09-18" in verdict.reason
    assert "not published it yet" in verdict.reason


def test_a_week_the_index_has_no_sessions_for_at_all_is_unpublished() -> None:
    verdict = assess_index_publication(
        _series(date(2026, 9, 14), 5), week=(2026, 39), expected_session=date(2026, 9, 25)
    )

    assert verdict.is_published is False
    assert verdict.last_session is None
    assert "none at all" in verdict.reason


def test_a_holiday_friday_week_is_published_by_its_thursday() -> None:
    """Good Friday 2026-04-03. The calendar expects Thursday 04-02, so a series
    reaching Thursday is current — not stale for missing a session that was
    never scheduled."""
    calendar = TradingCalendar.from_config(CONFIG_ROOT)
    expected = calendar.expected_last_session((2026, 14))
    assert expected == date(2026, 4, 2)

    index = _series(date(2026, 3, 30), 4)  # Mon..Thu; Friday is the holiday

    verdict = assess_index_publication(
        index, week=(2026, 14), expected_session=expected, calendar=calendar
    )
    assert verdict.is_published is True

    symbol = assess_staleness({"RELIANCE": index}, required_session=expected)[0]
    assert symbol.is_current is True


def test_an_unlisted_weekday_closure_fails_closed_and_is_reported() -> None:
    """The calendar has no entry for Friday 2026-09-25, so it is expected. If
    the exchange in fact closed that day, no amount of waiting produces it —
    the run fails closed and says so, and the operator adds the date."""
    calendar = TradingCalendar.from_config(CONFIG_ROOT)
    expected = calendar.expected_last_session((2026, 39))
    assert expected == date(2026, 9, 25)

    index = _series(date(2026, 9, 21), 4)  # Mon..Thu; Friday never happened

    verdict = assess_index_publication(
        index, week=(2026, 39), expected_session=expected, calendar=calendar
    )

    assert verdict.is_published is False
    assert "the verified holiday list does not carry" in verdict.reason


def test_a_session_on_a_listed_holiday_is_reported_and_moves_nothing() -> None:
    """The Diwali Muhurat session, Sunday 2026-11-08, is inside ISO week 45.
    It is reported, and the expected last session stays Friday 11-06."""
    calendar = TradingCalendar.from_config(CONFIG_ROOT)
    expected = calendar.expected_last_session((2026, 45))
    assert expected == date(2026, 11, 6)

    index = [
        *_series(date(2026, 11, 2), 5),  # Mon..Fri
        DailyBar(
            session=date(2026, 11, 8),  # Sunday Muhurat
            open=100.0,
            high=102.0,
            low=98.0,
            close=101.0,
            volume=500.0,
        ),
    ]

    verdict = assess_index_publication(
        index, week=(2026, 45), expected_session=expected, calendar=calendar
    )

    assert verdict.is_published is True
    assert verdict.expected_session == date(2026, 11, 6)
    assert verdict.unlisted_sessions == (date(2026, 11, 8),)
