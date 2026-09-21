"""Phase 1. The three operator CSV loaders (spec 6.3-6.5), fail-closed.

These files are the only place a human decision enters an otherwise
deterministic strategy, so most of this file is about the ways they can be
wrong. A renamed column, a duplicated symbol or a date typed the American way
must stop the run, not be guessed at — a guess here trades something the
operator did not approve.

The one deliberate permission is a blank ``nifty100``, which reads as ``false``.
That is the fail-closed reading, because the flag only ever *widens* what may
be entered (spec 4.3 restricts Red-regime entries to ``nifty100`` symbols), and
the shipped universe file is seeded from a constituent list that does not carry
it. ``test_a_blank_nifty100_reads_as_false_and_is_reported`` pins both halves.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from strategies.positional_stocks.wsr1_weekly_stochrsi.inputs import (
    InputFileError,
    load_quality_gate,
    load_results_calendar,
    load_universe,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.models import OnExit, QualityStatus

UNIVERSE_HEADER = "symbol,isin,company,industry,nifty100,group,on_exit,as_of"
QUALITY_HEADER = "symbol,status,checked_on,valid_until,notes"
RESULTS_HEADER = "symbol,results_date"

REPO_UNIVERSE = Path(__file__).resolve().parents[2] / "config" / "positional_stocks"


def _write(tmp_path: Path, name: str, *lines: str) -> Path:
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _universe(tmp_path: Path, *rows: str) -> Path:
    return _write(tmp_path, "universe.csv", UNIVERSE_HEADER, *rows)


def _quality(tmp_path: Path, *rows: str) -> Path:
    return _write(tmp_path, "quality_gate.csv", QUALITY_HEADER, *rows)


def _results(tmp_path: Path, *rows: str) -> Path:
    return _write(tmp_path, "results_calendar.csv", RESULTS_HEADER, *rows)


# ============================================================ universe.csv
def test_a_well_formed_universe_row_parses_every_column(tmp_path: Path) -> None:
    path = _universe(
        tmp_path,
        "RELIANCE,INE002A01018,Reliance Industries Ltd.,Oil Gas,"
        "true,RELIANCE GROUP,hold,2026-07-22",
    )

    universe = load_universe(path)

    row = universe.rows[0]
    assert row.symbol == "RELIANCE"
    assert row.isin == "INE002A01018"
    assert row.company == "Reliance Industries Ltd."
    assert row.industry == "Oil Gas"
    assert row.nifty100 is True
    assert row.group == "RELIANCE GROUP"
    assert row.on_exit is OnExit.HOLD
    assert row.as_of == date(2026, 7, 22)


def test_symbols_are_upper_cased(tmp_path: Path) -> None:
    path = _universe(tmp_path, "reliance,INE002A01018,R,Oil,true,,hold,2026-07-22")

    assert load_universe(path).symbols == ("RELIANCE",)


def test_a_blank_nifty100_reads_as_false_and_is_reported(tmp_path: Path) -> None:
    """Fail-closed in effect: spec 4.3 only ever *widens* eligibility with this
    flag, so an unfilled row is simply never eligible in a Red regime. Reported
    so the run can warn rather than it being silent."""
    path = _universe(
        tmp_path,
        "RELIANCE,INE002A01018,R,Oil,,,hold,2026-07-22",
        "HDFCBANK,INE040A01034,H,Financial Services,true,,hold,2026-07-22",
    )

    universe = load_universe(path)

    assert universe.by_symbol["RELIANCE"].nifty100 is False
    assert universe.symbols_missing_nifty100 == ("RELIANCE",)
    assert universe.nifty100_symbols == ("HDFCBANK",)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("true", True),
        ("TRUE", True),
        ("false", False),
        ("FALSE", False),
        ("yes", True),
        ("no", False),
        ("1", True),
        ("0", False),
    ],
)
def test_nifty100_accepts_the_usual_spellings(tmp_path: Path, raw: str, expected: bool) -> None:
    path = _universe(tmp_path, f"RELIANCE,INE002A01018,R,Oil,{raw},,hold,2026-07-22")

    assert load_universe(path).rows[0].nifty100 is expected


def test_an_uninterpretable_nifty100_is_refused(tmp_path: Path) -> None:
    path = _universe(tmp_path, "RELIANCE,INE002A01018,R,Oil,maybe,,hold,2026-07-22")

    with pytest.raises(InputFileError, match="nifty100"):
        load_universe(path)


def test_a_blank_group_means_the_symbol_is_its_own_group(tmp_path: Path) -> None:
    """Spec 6.3. Keeps the per-promoter-group limit meaningful without the
    operator naming a group for every standalone company."""
    path = _universe(tmp_path, "RELIANCE,INE002A01018,R,Oil,true,,hold,2026-07-22")

    row = load_universe(path).rows[0]

    assert row.group is None
    assert row.effective_group == "RELIANCE"
    assert load_universe(path).symbols_missing_group == ("RELIANCE",)


def test_two_symbols_in_one_group_share_an_effective_group(tmp_path: Path) -> None:
    path = _universe(
        tmp_path,
        "ADANIENSOL,INE931S01010,Adani Energy,Power,true,ADANI,hold,2026-07-22",
        "ADANIPORTS,INE742F01042,Adani Ports,Services,true,ADANI,hold,2026-07-22",
    )

    groups = {row.effective_group for row in load_universe(path).rows}

    assert groups == {"ADANI"}


def test_a_blank_on_exit_defaults_to_hold(tmp_path: Path) -> None:
    path = _universe(tmp_path, "RELIANCE,INE002A01018,R,Oil,true,,,2026-07-22")

    assert load_universe(path).rows[0].on_exit is OnExit.HOLD


def test_on_exit_exit_is_accepted(tmp_path: Path) -> None:
    path = _universe(tmp_path, "RELIANCE,INE002A01018,R,Oil,true,,exit,2026-07-22")

    assert load_universe(path).rows[0].on_exit is OnExit.EXIT


def test_an_unknown_on_exit_is_refused(tmp_path: Path) -> None:
    path = _universe(tmp_path, "RELIANCE,INE002A01018,R,Oil,true,,liquidate,2026-07-22")

    with pytest.raises(InputFileError, match="on_exit"):
        load_universe(path)


def test_a_duplicated_symbol_is_refused(tmp_path: Path) -> None:
    """Which row is authoritative is not a loader's decision to make."""
    path = _universe(
        tmp_path,
        "RELIANCE,INE002A01018,R,Oil,true,,hold,2026-07-22",
        "RELIANCE,INE002A01018,R,Oil,false,,exit,2026-07-22",
    )

    with pytest.raises(InputFileError, match="appears twice"):
        load_universe(path)


def test_a_blank_symbol_is_refused(tmp_path: Path) -> None:
    path = _universe(tmp_path, ",INE002A01018,R,Oil,true,,hold,2026-07-22")

    with pytest.raises(InputFileError, match="symbol is blank"):
        load_universe(path)


def test_a_universe_with_no_rows_is_refused(tmp_path: Path) -> None:
    """Unlike the quality gate, an empty universe is meaningless rather than
    merely restrictive — there is nothing to evaluate."""
    path = _universe(tmp_path)

    with pytest.raises(InputFileError, match="no rows"):
        load_universe(path)


def test_a_missing_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(InputFileError, match="does not exist"):
        load_universe(tmp_path / "absent.csv")


def test_a_renamed_column_is_refused_and_named(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "universe.csv",
        "symbol,isin,company,sector,nifty100,group,on_exit,as_of",
        "RELIANCE,INE002A01018,R,Oil,true,,hold,2026-07-22",
    )

    with pytest.raises(InputFileError, match="industry"):
        load_universe(path)


def test_an_extra_column_is_refused(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "universe.csv",
        UNIVERSE_HEADER + ",weight",
        "RELIANCE,INE002A01018,R,Oil,true,,hold,2026-07-22,3.5",
    )

    with pytest.raises(InputFileError, match="weight"):
        load_universe(path)


def test_reordered_columns_are_refused(tmp_path: Path) -> None:
    """Position matters as well as membership: a reordered header means the
    file was rebuilt by something that does not know the contract."""
    path = _write(
        tmp_path,
        "universe.csv",
        "isin,symbol,company,industry,nifty100,group,on_exit,as_of",
        "INE002A01018,RELIANCE,R,Oil,true,,hold,2026-07-22",
    )

    with pytest.raises(InputFileError, match="expected order"):
        load_universe(path)


def test_an_ambiguous_date_is_refused_rather_than_guessed(tmp_path: Path) -> None:
    """01/02/2026 is January in one convention and February in another. The
    loader refuses both readings."""
    path = _universe(tmp_path, "RELIANCE,INE002A01018,R,Oil,true,,hold,01/02/2026")

    with pytest.raises(InputFileError, match="ISO date"):
        load_universe(path)


def test_a_blank_as_of_is_tolerated(tmp_path: Path) -> None:
    path = _universe(tmp_path, "RELIANCE,INE002A01018,R,Oil,true,,hold,")

    assert load_universe(path).rows[0].as_of is None


def test_blank_lines_are_skipped_not_treated_as_rows(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "universe.csv",
        UNIVERSE_HEADER,
        "RELIANCE,INE002A01018,R,Oil,true,,hold,2026-07-22",
        ",,,,,,,",
        "HDFCBANK,INE040A01034,H,Fin,true,,hold,2026-07-22",
    )

    assert len(load_universe(path)) == 2


def test_a_utf8_bom_does_not_break_the_header(tmp_path: Path) -> None:
    """Excel writes one, and the operator maintains these by hand."""
    path = tmp_path / "universe.csv"
    path.write_text(
        UNIVERSE_HEADER + "\nRELIANCE,INE002A01018,R,Oil,true,,hold,2026-07-22\n",
        encoding="utf-8-sig",
    )

    assert load_universe(path).symbols == ("RELIANCE",)


# ======================================================== quality_gate.csv
def test_a_quality_row_parses(tmp_path: Path) -> None:
    path = _quality(tmp_path, "RELIANCE,PASS,2026-09-01,2026-12-31,checked by hand")

    row = load_quality_gate(path).rows[0]

    assert row.symbol == "RELIANCE"
    assert row.status is QualityStatus.PASS
    assert row.checked_on == date(2026, 9, 1)
    assert row.valid_until == date(2026, 12, 31)
    assert row.notes == "checked by hand"


def test_an_empty_gate_is_valid_and_admits_nothing(tmp_path: Path) -> None:
    """Spec 4.2: an entry needs a valid row, so an empty gate means no symbol
    may be entered. That is the correct fail-closed starting state, not an
    error — which is why the shipped file is header-only."""
    gate = load_quality_gate(_quality(tmp_path))

    assert len(gate) == 0
    assert gate.status_on("RELIANCE", date(2026, 9, 21)) is None


@pytest.mark.parametrize("status", ["PASS", "EVENT_RISK", "FAIL"])
def test_every_documented_status_is_accepted(tmp_path: Path, status: str) -> None:
    path = _quality(tmp_path, f"RELIANCE,{status},2026-09-01,2026-12-31,")

    assert load_quality_gate(path).rows[0].status is QualityStatus(status)


def test_an_unknown_status_is_refused(tmp_path: Path) -> None:
    path = _quality(tmp_path, "RELIANCE,MAYBE,2026-09-01,2026-12-31,")

    with pytest.raises(InputFileError, match="PASS, EVENT_RISK or FAIL"):
        load_quality_gate(path)


def test_an_expired_row_reads_as_no_status_at_all(tmp_path: Path) -> None:
    """Spec 4.2 treats missing and expired identically: no entry, no add."""
    path = _quality(tmp_path, "RELIANCE,PASS,2026-01-01,2026-09-20,")
    gate = load_quality_gate(path)

    assert gate.status_on("RELIANCE", date(2026, 9, 20)) is QualityStatus.PASS
    assert gate.status_on("RELIANCE", date(2026, 9, 21)) is None


def test_valid_until_on_the_execution_date_itself_still_counts(tmp_path: Path) -> None:
    """ "on or after" — the boundary day is inclusive."""
    path = _quality(tmp_path, "RELIANCE,PASS,2026-01-01,2026-09-21,")

    assert load_quality_gate(path).status_on("RELIANCE", date(2026, 9, 21)) is QualityStatus.PASS


def test_a_missing_valid_until_is_refused(tmp_path: Path) -> None:
    """A row with no expiry would never stop being trusted."""
    path = _quality(tmp_path, "RELIANCE,PASS,2026-09-01,,")

    with pytest.raises(InputFileError, match="valid_until is blank"):
        load_quality_gate(path)


def test_a_blank_checked_on_is_tolerated(tmp_path: Path) -> None:
    path = _quality(tmp_path, "RELIANCE,PASS,,2026-12-31,")

    assert load_quality_gate(path).rows[0].checked_on is None


def test_a_duplicated_quality_symbol_is_refused(tmp_path: Path) -> None:
    path = _quality(
        tmp_path,
        "RELIANCE,PASS,2026-09-01,2026-12-31,",
        "RELIANCE,FAIL,2026-09-02,2026-12-31,",
    )

    with pytest.raises(InputFileError, match="appears twice"):
        load_quality_gate(path)


def test_a_missing_quality_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(InputFileError, match="does not exist"):
        load_quality_gate(tmp_path / "absent.csv")


def test_status_lookup_is_case_insensitive_on_the_symbol(tmp_path: Path) -> None:
    path = _quality(tmp_path, "RELIANCE,PASS,2026-09-01,2026-12-31,")

    assert load_quality_gate(path).status_on("reliance", date(2026, 9, 21)) is QualityStatus.PASS


# ===================================================== results_calendar.csv
def test_a_results_row_parses(tmp_path: Path) -> None:
    path = _results(tmp_path, "RELIANCE,2026-10-15")

    calendar = load_results_calendar(path)

    assert calendar.rows[0].results_date == date(2026, 10, 15)
    assert calendar.absent is False


def test_several_results_dates_for_one_symbol_are_expected(tmp_path: Path) -> None:
    """Quarterly results: four rows a year per symbol is the normal case."""
    path = _results(tmp_path, "RELIANCE,2026-10-15", "RELIANCE,2027-01-20", "RELIANCE,2027-04-18")

    assert load_results_calendar(path).dates_for("RELIANCE") == (
        date(2026, 10, 15),
        date(2027, 1, 20),
        date(2027, 4, 18),
    )


def test_an_exact_duplicate_row_is_still_refused(tmp_path: Path) -> None:
    path = _results(tmp_path, "RELIANCE,2026-10-15", "RELIANCE,2026-10-15")

    with pytest.raises(InputFileError, match="duplicate results row"):
        load_results_calendar(path)


def test_results_in_the_execution_week_are_found(tmp_path: Path) -> None:
    """Spec 4.6 item 6: no entry when results fall Monday-Friday of the
    execution week."""
    path = _results(tmp_path, "RELIANCE,2026-09-23")
    calendar = load_results_calendar(path)

    assert calendar.has_results_between("RELIANCE", date(2026, 9, 21), date(2026, 9, 25)) is True
    assert calendar.has_results_between("RELIANCE", date(2026, 9, 28), date(2026, 10, 2)) is False


def test_knowing_a_symbol_is_distinct_from_it_having_no_results_this_week(
    tmp_path: Path,
) -> None:
    """Spec 4.6 item 6 separates "no results this week" from "results date
    unknown", and only the first is a clean pass."""
    path = _results(tmp_path, "RELIANCE,2026-12-01")
    calendar = load_results_calendar(path)

    assert calendar.knows("RELIANCE") is True
    assert calendar.has_results_between("RELIANCE", date(2026, 9, 21), date(2026, 9, 25)) is False
    assert calendar.knows("HDFCBANK") is False


def test_an_absent_calendar_is_tolerated_in_paper_mode(tmp_path: Path) -> None:
    calendar = load_results_calendar(tmp_path / "absent.csv")

    assert calendar.absent is True
    assert len(calendar) == 0
    assert calendar.knows("RELIANCE") is False


def test_no_calendar_configured_at_all_is_tolerated_in_paper_mode() -> None:
    assert load_results_calendar(None).absent is True


def test_an_absent_calendar_is_refused_when_required(tmp_path: Path) -> None:
    """Spec 4.6 item 6: "a live mode must fail closed"."""
    with pytest.raises(InputFileError, match="required"):
        load_results_calendar(tmp_path / "absent.csv", required=True)


def test_no_calendar_configured_is_refused_when_required() -> None:
    with pytest.raises(InputFileError, match="required"):
        load_results_calendar(None, required=True)


def test_a_malformed_calendar_is_refused_even_though_the_file_is_optional(
    tmp_path: Path,
) -> None:
    """Optional means "may be absent", never "may be wrong"."""
    path = _results(tmp_path, "RELIANCE,15-10-2026")

    with pytest.raises(InputFileError, match="ISO date"):
        load_results_calendar(path)


# ====================================================== the shipped files
def test_the_committed_universe_file_loads_and_has_two_hundred_rows() -> None:
    universe = load_universe(REPO_UNIVERSE / "universe.csv")

    assert len(universe) == 200
    assert "RELIANCE" in universe.by_symbol
    assert "M&M" in universe.by_symbol
    assert "BAJAJ-AUTO" in universe.by_symbol


def test_the_committed_universe_is_honest_about_its_unfilled_columns() -> None:
    """Seeded from a constituent list that carries neither flag. Both are the
    operator's to fill before Phase 4, and until then the loader reports them
    rather than the run assuming they were considered."""
    universe = load_universe(REPO_UNIVERSE / "universe.csv")

    assert len(universe.symbols_missing_nifty100) == 200
    assert universe.nifty100_symbols == (), "nothing is eligible in a Red regime yet"
    assert len(universe.symbols_missing_group) == 200


def test_the_committed_universe_holds_every_symbol_on_removal() -> None:
    universe = load_universe(REPO_UNIVERSE / "universe.csv")

    assert {row.on_exit for row in universe.rows} == {OnExit.HOLD}


def test_the_committed_quality_gate_is_empty_and_admits_nothing() -> None:
    gate = load_quality_gate(REPO_UNIVERSE / "quality_gate.csv")

    assert len(gate) == 0
    assert gate.status_on("RELIANCE", date(2026, 9, 21)) is None


def test_the_committed_results_calendar_loads_and_is_empty() -> None:
    calendar = load_results_calendar(REPO_UNIVERSE / "results_calendar.csv")

    assert calendar.absent is False
    assert len(calendar) == 0
