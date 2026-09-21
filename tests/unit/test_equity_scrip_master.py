"""Phase 1. :class:`~common.market_data.scrip_master.EquityScripMaster` and
:func:`~common.market_data.scrip_master.resolve_index` — the D34 port.

No network: every test parses fixture CSV text, exactly as
``test_scrip_master.py`` does for the options half.

The fixture reproduces the real column set and the row *kinds* that share the
file — a BSE listing, a non-``EQ`` series, an ``OPTSTK``/``FUTSTK`` derivative,
a spot ``INDEX`` — because each is something the filter has to reject and each
exists in the live master in quantity (100,308 ``OPTSTK`` rows alone on
2026-09-21). Symbol values are the real ones, including ``BAJAJ-AUTO`` and
``M&M``, which is what makes the "don't strip punctuation" rule testable.
"""

from __future__ import annotations

import pytest

from common.market_data.scrip_master import (
    INDEX_REGISTRY,
    NSE_EQUITY_SEGMENT,
    EquityScripMaster,
    ScripMasterCache,
    ScripMasterError,
    resolve_index,
    segment_code,
)

_HEADER = (
    "SEM_EXM_EXCH_ID,SEM_SEGMENT,SEM_SMST_SECURITY_ID,SEM_INSTRUMENT_NAME,"
    "SEM_EXPIRY_CODE,SEM_TRADING_SYMBOL,SEM_LOT_UNITS,SEM_CUSTOM_SYMBOL,"
    "SEM_EXPIRY_DATE,SEM_STRIKE_PRICE,SEM_OPTION_TYPE,SEM_TICK_SIZE,"
    "SEM_EXPIRY_FLAG,SEM_EXCH_INSTRUMENT_TYPE,SEM_SERIES,SM_SYMBOL_NAME"
)


def _row(
    *,
    exch: str = "NSE",
    segment: str = "E",
    security_id: str = "2885",
    instrument: str = "EQUITY",
    trading: str = "RELIANCE",
    custom: str = "Reliance Industries",
    tick: str = "10.0000",
    series: str = "EQ",
    name: str = "RELIANCE INDUSTRIES LTD",
) -> str:
    return (
        f"{exch},{segment},{security_id},{instrument},,{trading},1.0,{custom},"
        f",,,{tick},,,{series},{name}"
    )


def _csv(*rows: str) -> str:
    return "\n".join([_HEADER, *rows]) + "\n"


_REAL_WORLD = _csv(
    _row(),
    _row(security_id="1333", trading="HDFCBANK", custom="HDFC Bank", tick="5.0000"),
    _row(security_id="16669", trading="BAJAJ-AUTO", custom="Bajaj Auto", tick="100.0000"),
    _row(security_id="2031", trading="M&M", custom="Mahindra & Mahindra"),
    _row(security_id="10217", trading="ADANIENSOL", custom="Adani Energy Solutions"),
    # --- everything below must be filtered out ---
    _row(exch="BSE", security_id="500325", trading="RELIANCE", custom="Reliance BSE"),
    _row(security_id="9999", trading="SOMEBOND", series="SG"),
    _row(security_id="9998", trading="SOMESME", series="SM"),
    _row(security_id="9997", trading="BLANKSERIES", series=""),
    _row(security_id="9996", instrument="OPTSTK", trading="RELIANCE-Oct2026-3000-CE", series=""),
    _row(security_id="9995", instrument="FUTSTK", trading="RELIANCE-Oct2026-FUT", series=""),
    _row(
        segment="I",
        security_id="13",
        instrument="INDEX",
        trading="NIFTY",
        custom="Nifty 50",
        series="X",
        name="NIFTY",
    ),
    _row(
        segment="I",
        security_id="17",
        instrument="INDEX",
        trading="NIFTY 100",
        custom="NIFTY 100",
        series="X",
        name="NIFTY 100",
    ),
    _row(
        segment="I",
        security_id="18",
        instrument="INDEX",
        trading="NIFTY 200",
        custom="NIFTY 200",
        series="X",
        name="NIFTY 200",
    ),
)


def _master() -> EquityScripMaster:
    return EquityScripMaster().load_from_text(_REAL_WORLD)


# ------------------------------------------------------------------- resolution
def test_nse_eq_rows_resolve_to_their_security_ids() -> None:
    master = _master()

    assert master.get("RELIANCE") is not None
    assert master.get("RELIANCE").security_id == "2885"  # type: ignore[union-attr]
    assert master.get("HDFCBANK").security_id == "1333"  # type: ignore[union-attr]
    assert master.get("ADANIENSOL").security_id == "10217"  # type: ignore[union-attr]


def test_symbols_with_punctuation_survive_normalisation() -> None:
    """``BAJAJ-AUTO`` and ``M&M`` are real NSE symbols. A normaliser that
    stripped a hyphen or an ampersand would drop two NIFTY 200 constituents."""
    master = _master()

    assert master.get("BAJAJ-AUTO").security_id == "16669"  # type: ignore[union-attr]
    assert master.get("M&M").security_id == "2031"  # type: ignore[union-attr]


def test_lookup_is_case_and_whitespace_insensitive() -> None:
    master = _master()

    assert master.get("reliance").security_id == "2885"  # type: ignore[union-attr]
    assert master.get("  Reliance  ").security_id == "2885"  # type: ignore[union-attr]


def test_an_unknown_symbol_returns_none_rather_than_raising() -> None:
    """Spec section 5: an unresolvable symbol is skipped and reported; it never
    blocks the rest of the run."""
    assert _master().get("NOTLISTED") is None


def test_the_company_name_comes_from_the_custom_symbol() -> None:
    assert _master().get("HDFCBANK").company_name == "HDFC Bank"  # type: ignore[union-attr]


def test_the_exchange_segment_is_nse_eq_and_is_a_known_feed_segment() -> None:
    row = _master().get("RELIANCE")
    assert row is not None
    assert row.exchange_segment == NSE_EQUITY_SEGMENT == "NSE_EQ"
    assert segment_code(row.exchange_segment) == 1


def test_tick_size_is_converted_from_paise_and_stays_advisory() -> None:
    """The column is paise (``TICK_SIZE_PAISE_PER_RUPEE``), and equity rows are
    exactly where that docstring records it as untrustworthy — RELIANCE's
    ``10.0000`` converts to ₹0.10, not the ₹0.05 it trades in. Converted and
    carried, never enforced."""
    assert _master().get("RELIANCE").tick_size == pytest.approx(0.10)  # type: ignore[union-attr]
    assert _master().get("HDFCBANK").tick_size == pytest.approx(0.05)  # type: ignore[union-attr]


# ----------------------------------------------------------------- filtering
def test_a_bse_listing_of_the_same_symbol_does_not_win() -> None:
    """Both exchanges list RELIANCE. Taking the BSE row would place every order
    for this strategy on the wrong exchange's security id."""
    assert _master().get("RELIANCE").security_id == "2885"  # type: ignore[union-attr]


@pytest.mark.parametrize("symbol", ["SOMEBOND", "SOMESME", "BLANKSERIES"])
def test_non_eq_series_rows_are_excluded(symbol: str) -> None:
    """Strict ``SEM_SERIES == "EQ"``. The reference let a *blank* series
    through; spec section 5 says series EQ, so ``BLANKSERIES`` is rejected too
    — that is the one deliberate tightening of the ported filter."""
    assert _master().get(symbol) is None


def test_stock_derivative_rows_are_excluded() -> None:
    master = _master()

    assert master.get("RELIANCE-Oct2026-3000-CE") is None
    assert master.get("RELIANCE-Oct2026-FUT") is None


def test_spot_index_rows_are_not_equities() -> None:
    assert _master().get("NIFTY") is None


def test_only_the_five_cash_equities_are_loaded() -> None:
    master = _master()

    assert master.symbols == ("ADANIENSOL", "BAJAJ-AUTO", "HDFCBANK", "M&M", "RELIANCE")
    assert len(master) == 5


# -------------------------------------------------------------- bulk resolution
def test_resolve_all_returns_both_halves_in_one_pass() -> None:
    resolved, unresolved = _master().resolve_all(["RELIANCE", "NOTLISTED", "m&m", "GONE"])

    assert sorted(resolved) == ["M&M", "RELIANCE"]
    assert unresolved == ["NOTLISTED", "GONE"]


def test_resolve_all_keys_the_map_by_the_normalised_symbol() -> None:
    resolved, _unresolved = _master().resolve_all(["bajaj-auto"])

    assert list(resolved) == ["BAJAJ-AUTO"]


# --------------------------------------------------------------- failure modes
def test_a_master_with_no_cash_equity_rows_raises() -> None:
    """An empty result is a configuration error wearing a data error's clothes
    — the same rule :class:`ScripMaster` applies to its own filters."""
    options_only = _csv(_row(instrument="OPTSTK", trading="X-Oct2026-100-CE", series=""))

    with pytest.raises(ScripMasterError, match="cash-equity"):
        EquityScripMaster().load_from_text(options_only)


def test_an_empty_file_raises_rather_than_loading_nothing() -> None:
    with pytest.raises(ScripMasterError):
        EquityScripMaster().load_from_text(_HEADER + "\n")


def test_a_row_missing_its_security_id_is_skipped_not_fatal() -> None:
    """One bad row in a 26 MB daily file must not cost the whole run."""
    text = _csv(_row(), _row(security_id="", trading="BROKEN"))
    master = EquityScripMaster().load_from_text(text)

    assert master.get("BROKEN") is None
    assert master.get("RELIANCE") is not None


def test_reloading_replaces_rather_than_accumulates() -> None:
    master = _master()
    master.load_from_text(_csv(_row(security_id="1333", trading="HDFCBANK", custom="HDFC Bank")))

    assert master.symbols == ("HDFCBANK",)


# ---------------------------------------------------------------- index lookup
def test_nifty_50_resolves_by_trading_symbol_and_by_custom_symbol() -> None:
    by_trading = resolve_index(_REAL_WORLD, "NIFTY")
    by_custom = resolve_index(_REAL_WORLD, "Nifty 50")

    assert by_trading.security_id == by_custom.security_id == "13"
    assert by_trading.exchange_segment == "IDX_I"
    assert segment_code(by_trading.exchange_segment) == 0
    assert by_trading.name == "Nifty 50"


def test_the_resolved_nifty_id_agrees_with_the_existing_index_registry() -> None:
    """``INDEX_REGISTRY`` has carried NIFTY's id since Phase 4. This asserts the
    two sources agree rather than this module hard-coding a second copy — if
    Dhan ever renumbered the index, one of them would be wrong and silent."""
    assert resolve_index(_REAL_WORLD, "NIFTY").security_id == INDEX_REGISTRY["NIFTY"].security_id


def test_index_matching_is_exact_not_a_prefix() -> None:
    """``NIFTY`` must not be satisfied by ``NIFTY 100``, and asking for
    ``NIFTY 100`` must not return ``NIFTY``. A ``startswith`` would do both,
    and would silently compute the market regime off the wrong index."""
    assert resolve_index(_REAL_WORLD, "NIFTY").security_id == "13"
    assert resolve_index(_REAL_WORLD, "NIFTY 100").security_id == "17"
    assert resolve_index(_REAL_WORLD, "NIFTY 200").security_id == "18"


def test_index_lookup_is_case_insensitive() -> None:
    assert resolve_index(_REAL_WORLD, "nIfTy 50").security_id == "13"


def test_an_unknown_index_raises_rather_than_returning_none() -> None:
    """Unlike an equity, a missing index is fatal: spec 6.2 stops the whole run
    when the regime cannot be determined."""
    with pytest.raises(ScripMasterError, match="No NSE spot-index row"):
        resolve_index(_REAL_WORLD, "NIFTY MIDCAP 999")


def test_an_equity_named_like_an_index_is_not_matched() -> None:
    with pytest.raises(ScripMasterError):
        resolve_index(_REAL_WORLD, "RELIANCE")


def test_two_index_rows_with_different_ids_refuse_to_be_guessed_between() -> None:
    ambiguous = _csv(
        _row(segment="I", security_id="13", instrument="INDEX", trading="NIFTY", custom="Nifty 50"),
        _row(segment="I", security_id="77", instrument="INDEX", trading="NIFTY", custom="Other"),
    )

    with pytest.raises(ScripMasterError, match="Refusing to guess"):
        resolve_index(ambiguous, "NIFTY")


# --------------------------------------------------------------- cache reuse
def test_load_goes_through_the_existing_day_stamped_cache(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The equity master reuses :class:`ScripMasterCache` rather than adding a
    second download path — and on a warm cache it makes no fetch at all, which
    is what the offline decision run of spec 10.3 depends on."""
    fetches: list[int] = []

    def fetcher() -> str:
        fetches.append(1)
        return _REAL_WORLD

    cache = ScripMasterCache(tmp_path, fetcher=fetcher)

    first = EquityScripMaster().load(cache=cache)
    second = EquityScripMaster().load(cache=cache)

    assert first.get("RELIANCE").security_id == "2885"  # type: ignore[union-attr]
    assert second.get("RELIANCE").security_id == "2885"  # type: ignore[union-attr]
    assert len(fetches) == 1, "the second load must be served from the day-stamped cache"
