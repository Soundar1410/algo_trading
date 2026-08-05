"""The Dhan instrument master and the resolver built on it (Phase 4 Part 1).

The first test is the reference repository's own regression test, ported
unmodified in its assertions and its fixture — ``test_existing_index_option_
master_behaviour_is_preserved`` from its ``test_market_data_scanner_foundation.py``.
Only the import path and the ``ScripMaster`` construction differ, because that is
what porting means here. Everything after it is new coverage for behaviour the
reference had no test for: expiry selection, exchange filtering, the prefix
collision, the cache, and the resolver.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from common.engine.selection import (
    ContractNotListed,
    DhanOptionChainResolver,
    OptionSelector,
)
from common.market_data.scrip_master import (
    INDEX_REGISTRY,
    SEGMENT_CODES,
    OptionRow,
    ScripMaster,
    ScripMasterCache,
    ScripMasterError,
    resolve_index_meta,
    segment_code,
)
from common.models import OptionType

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SAMPLE = FIXTURES / "dhan_scrip_master_sample.csv"
MULTI = FIXTURES / "dhan_scrip_master_multi.csv"


def _multi(underlying: str = "NIFTY", **kwargs: object) -> ScripMaster:
    return ScripMaster(underlying, **kwargs).load_from_text(  # type: ignore[arg-type]
        MULTI.read_text()
    )


# ---------------------------------------------------------------- ported regression
def test_existing_index_option_master_behaviour_is_preserved():
    """The reference's own regression test, assertions unchanged."""
    master = ScripMaster("NIFTY").load_from_text(SAMPLE.read_text())
    row = master.get(24150, OptionType.CE, "2026-07-30")
    assert row is not None
    assert row.security_id == "8001"
    assert master.lot_size == 65


# ------------------------------------------------------------------------- parsing
def test_both_option_types_resolve_to_different_contracts():
    master = _multi()
    call = master.get(24150, OptionType.CE, "2026-08-04")
    put = master.get(24150, OptionType.PE, "2026-08-04")
    assert call is not None and put is not None
    assert call.security_id == "8103"
    assert put.security_id == "8104"


def test_the_lot_size_comes_from_the_exchange_not_from_configuration():
    """Half of limitation 17: a configured lot size can silently be wrong."""
    master = _multi()
    row = master.get(24150, OptionType.CE, "2026-08-04")
    assert row is not None
    assert row.lot_size == 75
    assert master.lot_size == 75


def test_a_lookalike_underlying_never_matches_through_a_shared_prefix():
    """NIFTYNXT50 rows sit in the same file and start with the same six letters."""
    master = _multi()
    ids = {row.security_id for row in master.atm_band(24150, "2026-08-04", 50, 0)}
    assert ids == {"8103", "8104"}
    assert "8301" not in ids and "8302" not in ids

    nxt = _multi("NIFTYNXT50")
    row = nxt.get(24150, OptionType.CE, "2026-08-04")
    assert row is not None and row.security_id == "8301"
    assert row.lot_size == 25


def test_an_nse_underlying_never_matches_a_bse_row():
    master = _multi("SENSEX")  # registry says BSE
    assert master.exchange == "BSE"
    row = master.get(80000, OptionType.CE, "2026-08-04")
    assert row is not None and row.security_id == "8401"
    # The same underlying forced onto NSE must find nothing at all.
    with pytest.raises(ScripMasterError, match="No SENSEX"):
        ScripMaster("SENSEX", exchange="NSE").load_from_text(MULTI.read_text())


def test_unparseable_rows_are_skipped_rather_than_fatal():
    """One bad row in a multi-megabyte daily file must not cost the session."""
    master = _multi()
    assert master.get(24250, OptionType.CE, "2026-08-04") is None  # option type "XX"
    assert 24100 in master.strikes_for_expiry("2026-08-04")  # the good rows survived


def test_non_option_instruments_are_ignored():
    """A FUTIDX row shares the underlying and the expiry but is not an option.

    Asserted on the row's *security id* rather than on its option type: every
    lookup is keyed by CE/PE, so a type-based assertion would hold even if the
    future had been indexed, and would pass for the wrong reason.
    """
    master = _multi()
    assert "8601" not in {row.security_id for row in _all_rows(master)}


def test_a_master_matching_nothing_raises_rather_than_loading_empty():
    """An empty result is a configuration error wearing a data error's clothes."""
    with pytest.raises(ScripMasterError, match="No BANKNIFTY"):
        ScripMaster("BANKNIFTY").load_from_text(MULTI.read_text())


def test_an_empty_csv_raises():
    with pytest.raises(ScripMasterError):
        ScripMaster("NIFTY").load_from_text("SEM_SMST_SECURITY_ID,SEM_INSTRUMENT_NAME\n")


# -------------------------------------------------------------------------- expiry
def test_expiries_are_sorted_and_complete():
    assert _multi().expiries == ["2026-07-28", "2026-08-04", "2026-08-11"]


def test_nearest_expiry_skips_one_that_has_already_passed():
    master = _multi()
    assert master.nearest_expiry(on=date(2026, 8, 1)) == "2026-08-04"


def test_nearest_expiry_includes_expiry_day_itself():
    """On expiry morning the expiring series is still the one being traded."""
    assert _multi().nearest_expiry(on=date(2026, 8, 4)) == "2026-08-04"


def test_nearest_expiry_rolls_to_the_next_series_the_day_after():
    assert _multi().nearest_expiry(on=date(2026, 8, 5)) == "2026-08-11"


def test_a_stale_master_raises_rather_than_returning_a_dead_expiry():
    """The reference fell back to the last (past) expiry. That resolves contracts
    that can no longer be traded, so it fails towards a silent bad entry."""
    master = _multi()
    with pytest.raises(ScripMasterError, match="stale"):
        master.nearest_expiry(on=date(2027, 1, 1))


def test_nearest_expiry_defaults_to_today_in_ist_not_utc():
    """At 23:30 UTC it is already tomorrow in Mumbai; the reference used a naive
    ``datetime.now()`` and would have picked the wrong series for half an hour."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    import common.market_data.scrip_master as module

    master = _multi()
    late_utc = datetime(2026, 8, 4, 23, 30, tzinfo=ZoneInfo("UTC"))
    assert late_utc.astimezone(ZoneInfo("Asia/Kolkata")).date() == date(2026, 8, 5)
    # Default path must agree with the IST date, i.e. roll to the next series.
    original = module.now_ist
    module.now_ist = lambda: late_utc.astimezone(ZoneInfo("Asia/Kolkata"))  # type: ignore[assignment]
    try:
        assert master.nearest_expiry() == "2026-08-11"
    finally:
        module.now_ist = original  # type: ignore[assignment]


# ---------------------------------------------------------------------- atm band
def test_the_atm_band_spans_both_option_types_around_the_money():
    rows = _multi().atm_band(24148, "2026-08-04", strike_step=50, half_width=1)
    assert {(r.strike, r.option_type) for r in rows} == {
        (24100.0, OptionType.CE),
        (24100.0, OptionType.PE),
        (24150.0, OptionType.CE),
        (24150.0, OptionType.PE),
        (24200.0, OptionType.CE),
        (24200.0, OptionType.PE),
    }


def test_the_atm_band_silently_omits_strikes_the_exchange_does_not_list():
    """Near the edge of the chain a band is naturally short. That is not an error."""
    rows = _multi().atm_band(24150, "2026-08-11", strike_step=50, half_width=2)
    assert [r.security_id for r in rows] == ["8201", "8202"]


def test_a_zero_strike_step_is_refused():
    with pytest.raises(ValueError, match="strike_step must be positive"):
        _multi().atm_band(24150, "2026-08-04", strike_step=0, half_width=1)


# --------------------------------------------------------------------- segments
def test_an_index_and_its_options_resolve_to_different_segments():
    """The reason a single adapter-wide segment cannot serve an options runtime."""
    meta = resolve_index_meta("NIFTY")
    assert meta.segment == "IDX_I"
    assert meta.fno_segment == "NSE_FNO"
    assert segment_code(meta.segment) == 0
    assert segment_code(meta.fno_segment) == 2


def test_sensex_options_resolve_to_the_bse_segment():
    meta = resolve_index_meta("SENSEX")
    assert (meta.segment, meta.fno_segment) == ("IDX_I", "BSE_FNO")
    assert segment_code(meta.fno_segment) == 8


def test_an_unknown_segment_raises_rather_than_defaulting():
    """A guessed segment subscribes to nothing and fails by delivering silence."""
    with pytest.raises(KeyError, match="Unknown exchange segment"):
        segment_code("NSE_FNO_TYPO")


def test_an_unknown_underlying_needs_all_three_overrides():
    with pytest.raises(ValueError, match="no explicit index_security_id"):
        resolve_index_meta("MIDCPNIFTY")
    meta = resolve_index_meta(
        "MIDCPNIFTY", index_security_id="442", index_segment="IDX_I", fno_segment="NSE_FNO"
    )
    assert meta.security_id == "442"


def test_every_registry_entry_names_a_known_segment():
    for name, meta in INDEX_REGISTRY.items():
        assert meta.segment in SEGMENT_CODES, name
        assert meta.fno_segment in SEGMENT_CODES, name


# ----------------------------------------------------------------------- caching
def test_the_cache_fetches_once_per_day_and_serves_the_rest_from_disk(tmp_path: Path):
    calls: list[int] = []

    def fetcher() -> str:
        calls.append(1)
        return MULTI.read_text()

    cache = ScripMasterCache(tmp_path, today=lambda: date(2026, 8, 4), fetcher=fetcher)
    first = cache.text()
    second = cache.text()
    assert first == second
    assert len(calls) == 1, "a same-day restart refetched the master"


def test_a_new_trading_day_refetches(tmp_path: Path):
    calls: list[int] = []
    today = [date(2026, 8, 4)]

    def fetcher() -> str:
        calls.append(1)
        return MULTI.read_text()

    cache = ScripMasterCache(tmp_path, today=lambda: today[0], fetcher=fetcher)
    cache.text()
    today[0] = date(2026, 8, 5)
    cache.text()
    assert len(calls) == 2


def test_a_crashing_fetch_leaves_no_partial_file_behind(tmp_path: Path):
    def fetcher() -> str:
        raise RuntimeError("network went away")

    cache = ScripMasterCache(tmp_path, today=lambda: date(2026, 8, 4), fetcher=fetcher)
    with pytest.raises(RuntimeError):
        cache.text()
    assert list(tmp_path.glob("*")) == [], "a failed fetch left a file on disk"


def test_a_truncated_cache_file_is_not_served_as_empty(tmp_path: Path):
    """An empty cached file must miss, not resolve zero contracts."""
    cache = ScripMasterCache(tmp_path, today=lambda: date(2026, 8, 4), fetcher=MULTI.read_text)
    cache.path_for(date(2026, 8, 4)).parent.mkdir(parents=True, exist_ok=True)
    cache.path_for(date(2026, 8, 4)).write_text("")
    assert cache.cached_text() is None
    assert cache.text() == MULTI.read_text()


def test_pruning_keeps_only_the_newest_masters(tmp_path: Path):
    cache = ScripMasterCache(tmp_path, today=lambda: date(2026, 8, 4))
    for day in (1, 2, 3, 4, 5):
        cache.path_for(date(2026, 8, day)).write_text("x")
    assert cache.prune(keep=2) == 3
    assert sorted(p.name for p in tmp_path.glob("*.csv")) == [
        "dhan_scrip_master_2026-08-04.csv",
        "dhan_scrip_master_2026-08-05.csv",
    ]


def test_loading_through_the_cache_needs_no_network(tmp_path: Path):
    cache = ScripMasterCache(
        tmp_path, today=lambda: date(2026, 8, 4), fetcher=lambda: MULTI.read_text()
    )
    master = ScripMaster("NIFTY").load(cache=cache)
    assert master.get(24150, OptionType.CE, "2026-08-04") is not None


# ---------------------------------------------------------------------- resolver
def test_the_resolver_returns_a_real_security_id_not_a_synthetic_one():
    """Limitation 17 in one assertion."""
    resolver = DhanOptionChainResolver(_multi(), expiry="2026-08-04")
    contract = resolver.resolve(24150, OptionType.CE)
    assert contract.security_id == "8103"
    assert not contract.security_id.startswith("SIM:")
    assert contract.lot_size == 75
    assert contract.strike == 24150.0
    assert contract.expiry == "2026-08-04"


def test_the_resolver_fixes_its_expiry_for_the_session():
    resolver = DhanOptionChainResolver(_multi(), expiry="2026-08-11")
    assert resolver.expiry == "2026-08-11"
    assert resolver.resolve(24150, OptionType.PE).security_id == "8202"


def test_the_resolver_defaults_to_the_nearest_listed_expiry():
    master = _multi()
    resolver = DhanOptionChainResolver(master, expiry=master.nearest_expiry(on=date(2026, 8, 1)))
    assert resolver.expiry == "2026-08-04"


def test_an_unlisted_strike_raises_a_distinct_type_the_engine_can_catch():
    """Not a crash: asking outside the listed band is a normal market condition."""
    resolver = DhanOptionChainResolver(_multi(), expiry="2026-08-04")
    with pytest.raises(ContractNotListed, match="listed strikes: 24100-24200"):
        resolver.resolve(30000, OptionType.CE)


def test_contract_not_listed_is_catchable_as_key_error():
    """Callers written against the reference's ``KeyError`` keep working."""
    assert issubclass(ContractNotListed, KeyError)


def test_the_resolver_exposes_the_exchange_lot_size():
    assert DhanOptionChainResolver(_multi(), expiry="2026-08-04").lot_size == 75


def test_the_selector_drives_the_real_resolver_end_to_end():
    """The seam that matters: OptionSelector is unchanged and now yields real ids."""
    selector = OptionSelector(
        DhanOptionChainResolver(_multi(), expiry="2026-08-04"),
        strike_step=50,
        expiry="2026-08-04",
    )
    contract = selector.select(24138.4, OptionType.CE)
    assert contract.security_id == "8103", "spot should have rounded to the 24150 strike"


def test_the_selector_still_applies_moneyness_against_real_contracts():
    from common.engine.models import Moneyness

    selector = OptionSelector(
        DhanOptionChainResolver(_multi(), expiry="2026-08-04"),
        strike_step=50,
        expiry="2026-08-04",
    )
    otm_call = selector.select(24150, OptionType.CE, moneyness=Moneyness.OTM, steps=1)
    otm_put = selector.select(24150, OptionType.PE, moneyness=Moneyness.OTM, steps=1)
    assert otm_call.strike == 24200.0
    assert otm_put.strike == 24100.0


# ------------------------------------------------------------------------- helper
def _all_rows(master: ScripMaster) -> list[OptionRow]:
    rows: list[OptionRow] = []
    for expiry in master.expiries:
        for strike in master.strikes_for_expiry(expiry):
            for option_type in (OptionType.CE, OptionType.PE):
                row = master.get(strike, option_type, expiry)
                if row is not None:
                    rows.append(row)
    return rows


# ------------------------------------------------------------------ tick size
#: A master carrying `SEM_TICK_SIZE`, in the units Dhan actually publishes.
#: Assembled here rather than as a fixture file so the paise values sit next to
#: the rupee assertions that depend on them.
_TICK_HEADER = (
    "SEM_SMST_SECURITY_ID,SEM_INSTRUMENT_NAME,SEM_TRADING_SYMBOL,SEM_CUSTOM_SYMBOL,"
    "SEM_EXPIRY_DATE,SEM_STRIKE_PRICE,SEM_OPTION_TYPE,SEM_LOT_UNITS,SEM_TICK_SIZE,"
    "SEM_EXM_EXCH_ID,SEM_SEGMENT"
)

#: ``(security_id, strike, option_type, raw SEM_TICK_SIZE)``. 5.0000 is what the
#: live master carries for a NIFTY option whose real tick is ₹0.05 — the paise
#: unit this file exists to pin. The empty and "rubbish" values are the two ways
#: the column can fail to say anything.
_TICK_ROWS = [
    ("9001", 24100, "CE", "5.0000"),
    ("9002", 24100, "PE", "5.0000"),
    ("9003", 24200, "CE", ""),
    ("9004", 24200, "PE", "rubbish"),
]

_WITH_TICKS = "\n".join(
    [
        _TICK_HEADER,
        *(
            f"{sid},OPTIDX,NIFTY-Jul2026-{strike}-{kind},"
            f"NIFTY 28 JUL {strike} {kind},2026-07-28 00:00:00,"
            f"{strike},{kind},75,{tick},NSE,D"
            for sid, strike, kind, tick in _TICK_ROWS
        ),
        "",
    ]
)


def _ticked() -> ScripMaster:
    return ScripMaster("NIFTY").load_from_text(_WITH_TICKS)


def test_the_tick_size_is_converted_from_paise_to_rupees():
    """The finding that shaped the whole tick rule (Phase 4 Part 5).

    Verified against the live master: NIFTY and SENSEX ``OPTIDX`` rows carry
    ``5.0000`` where the real tick is ₹0.05, and ``FUTCUR`` USDINR carries
    ``0.2500`` for a real ₹0.0025. Read at face value as rupees, NIFTY options
    would sit on a ₹5 grid and every order would be refused as off-tick.
    """
    row = _ticked().get(24100.0, OptionType.CE, "2026-07-28")
    assert row is not None
    assert row.tick_size == pytest.approx(0.05)


@pytest.mark.parametrize("strike", [24200.0])
def test_a_missing_or_unparseable_tick_size_is_none_not_a_default(strike: float):
    """Not knowing an instrument's tick and knowing it is 0.05 must stay
    distinguishable: the fill model skips its tick rule for the first and enforces
    it for the second."""
    master = _ticked()
    assert master.get(strike, OptionType.CE, "2026-07-28").tick_size is None  # type: ignore[union-attr]
    assert master.get(strike, OptionType.PE, "2026-07-28").tick_size is None  # type: ignore[union-attr]


def test_a_master_without_the_column_at_all_still_loads():
    """The column is absent from both committed fixtures, and from any master
    Dhan might trim — that must not be a load failure."""
    for row in _all_rows(_multi()):
        assert row.tick_size is None


def test_contracts_are_indexed_by_security_id_for_the_broker():
    """An order carries a ``security_id`` and nothing else, so the strike/expiry
    key cannot answer "what are this instrument's exchange rules?"."""
    master = _ticked()
    row = master.by_security_id("9001")
    assert row is not None and row.strike == 24100.0 and row.option_type is OptionType.CE
    assert master.by_security_id("does-not-exist") is None


def test_the_security_id_index_is_cleared_on_reload():
    master = _ticked()
    assert master.by_security_id("9001") is not None
    master.load_from_text(MULTI.read_text())
    assert master.by_security_id("9001") is None, "a reload must not keep stale ids"


def test_the_resolver_hands_the_broker_the_exchange_rules():
    """The whole contract between the scrip master and the paper broker is a
    ``Callable[[str], InstrumentRules | None]`` — the broker must not know what a
    scrip master is."""
    resolver = DhanOptionChainResolver(_ticked(), expiry="2026-07-28")
    rules = resolver.instrument_rules("9001")
    assert rules is not None
    assert rules.lot_size == 75
    assert rules.tick_size == pytest.approx(0.05)


def test_an_unlisted_security_id_has_no_rules_which_is_what_refuses_it():
    """``None`` is what makes the broker's INVALID_INSTRUMENT rule mean something:
    an order for an id the exchange's own daily master does not list is an order
    that could not have been placed."""
    resolver = DhanOptionChainResolver(_ticked(), expiry="2026-07-28")
    assert resolver.instrument_rules("99999") is None


def test_the_distinct_tick_sizes_are_reportable():
    assert _ticked().tick_sizes() == frozenset({0.05})
    assert _multi().tick_sizes() == frozenset()
