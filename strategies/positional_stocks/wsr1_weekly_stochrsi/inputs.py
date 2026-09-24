"""Loaders for the operator-maintained CSVs (spec 6.1, 6.3, 6.4, 6.5).

All three are **fail-closed**. These files are the only place a human decision
enters an otherwise deterministic strategy, and every way they can be wrong —
a renamed column, a duplicated symbol, a date typed the American way — is a way
to silently trade something the operator did not approve. So every deviation
from the column contract raises, and the run stops, rather than the loader
guessing.

Stdlib ``csv``, not pandas: the same choice ``common/market_data/scrip_master.py``
makes, and these files are hundreds of rows, not millions.

One column is deliberately permissive. ``nifty100`` may be **blank**, and a
blank reads as ``false``. The universe file is seeded from an NSE constituent
list that does not carry the flag, so refusing a blank would make the seeded
file unloadable; and ``false`` is the fail-closed reading, because the flag only
ever *widens* what may be entered — spec 4.3 restricts Red-regime entries to
``nifty100`` symbols, so an unfilled row is simply never eligible in Red.
:attr:`UniverseFile.symbols_missing_nifty100` carries them so the run can warn.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

from common.logging import get_logger

from .gaps import RATIO_PLACES
from .models import (
    GapAcknowledgement,
    OnExit,
    QualityRow,
    QualityStatus,
    ResultsRow,
    UniverseRow,
)

_log = get_logger(__name__)

UNIVERSE_COLUMNS = (
    "symbol",
    "isin",
    "company",
    "industry",
    "nifty100",
    "group",
    "on_exit",
    "as_of",
)
QUALITY_COLUMNS = ("symbol", "status", "checked_on", "valid_until", "notes")
RESULTS_COLUMNS = ("symbol", "results_date")
GAP_ACK_COLUMNS = ("symbol", "gap_session", "ratio", "acknowledged_on", "note")

_TRUE = frozenset({"true", "yes", "y", "1"})
_FALSE = frozenset({"false", "no", "n", "0", ""})


class InputFileError(RuntimeError):
    """An operator input file is missing, malformed, or violates its contract."""


@dataclass(frozen=True, slots=True)
class UniverseFile:
    """``universe.csv``, parsed (spec 6.3)."""

    path: Path
    rows: tuple[UniverseRow, ...]
    #: Rows whose ``nifty100`` column was blank. Read as ``false``, which
    #: excludes them from the Red-regime subset — reported, never silent.
    symbols_missing_nifty100: tuple[str, ...] = ()
    #: Rows whose ``group`` was blank, i.e. that stand as their own group.
    symbols_missing_group: tuple[str, ...] = ()

    def __len__(self) -> int:
        return len(self.rows)

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(row.symbol for row in self.rows)

    @property
    def by_symbol(self) -> dict[str, UniverseRow]:
        return {row.symbol: row for row in self.rows}

    @property
    def nifty100_symbols(self) -> tuple[str, ...]:
        return tuple(row.symbol for row in self.rows if row.nifty100)


@dataclass(frozen=True, slots=True)
class QualityGateFile:
    """``quality_gate.csv``, parsed (spec 6.4)."""

    path: Path
    rows: tuple[QualityRow, ...] = ()

    def __len__(self) -> int:
        return len(self.rows)

    @property
    def by_symbol(self) -> dict[str, QualityRow]:
        return {row.symbol: row for row in self.rows}

    def status_on(self, symbol: str, execution_date: date) -> QualityStatus | None:
        """The usable status for ``symbol``, or ``None``.

        ``None`` covers both "no row" and "the row has expired", because spec
        4.2 treats them identically: no entry, no add, and the symbol is listed
        under "needs quality check". Fail closed.
        """
        row = self.by_symbol.get(symbol.strip().upper())
        if row is None or not row.is_valid_on(execution_date):
            return None
        return row.status


@dataclass(frozen=True, slots=True)
class ResultsCalendarFile:
    """``results_calendar.csv``, parsed (spec 6.5). Optional in paper mode."""

    path: Path | None
    rows: tuple[ResultsRow, ...] = ()
    #: True when the file was absent and absence was permitted. Spec 4.6 item 6
    #: then flags "results date unknown" for every symbol.
    absent: bool = False
    _dates: dict[str, tuple[date, ...]] = field(default_factory=dict, compare=False)

    def __len__(self) -> int:
        return len(self.rows)

    def dates_for(self, symbol: str) -> tuple[date, ...]:
        return self._dates.get(symbol.strip().upper(), ())

    def has_results_between(self, symbol: str, start: date, end: date) -> bool:
        return any(start <= day <= end for day in self.dates_for(symbol))

    def knows(self, symbol: str) -> bool:
        """Whether the calendar has any row for this symbol at all.

        Spec 4.6 item 6 distinguishes "no results this week" from "we do not
        know this symbol's results date", and only the first is a pass.
        """
        return symbol.strip().upper() in self._dates


# ------------------------------------------------------------------- loading
def load_universe(path: Path | str) -> UniverseFile:
    """Parse ``universe.csv``. Raises :class:`InputFileError` on any violation."""
    location = Path(path)
    rows: list[UniverseRow] = []
    missing_flag: list[str] = []
    missing_group: list[str] = []
    seen: set[str] = set()

    for line, record in _records(location, UNIVERSE_COLUMNS, "universe"):
        symbol = _required(record, "symbol", location, line).upper()
        _reject_duplicate(symbol, seen, location, line, "universe")

        raw_flag = (record.get("nifty100") or "").strip()
        if not raw_flag:
            missing_flag.append(symbol)
        raw_group = (record.get("group") or "").strip()
        if not raw_group:
            missing_group.append(symbol)

        rows.append(
            UniverseRow(
                symbol=symbol,
                isin=(record.get("isin") or "").strip(),
                company=(record.get("company") or "").strip(),
                industry=(record.get("industry") or "").strip(),
                nifty100=_boolean(raw_flag, "nifty100", location, line),
                group=raw_group or None,
                on_exit=_on_exit(record.get("on_exit"), location, line),
                as_of=_optional_date(record.get("as_of"), "as_of", location, line),
            )
        )

    if not rows:
        raise InputFileError(f"{location}: the universe file has no rows. Fail closed.")

    if missing_flag:
        _log.warning(
            "%s: %d row(s) have a blank nifty100 flag; they are read as false and are "
            "therefore never eligible in a Red regime",
            location,
            len(missing_flag),
        )
    return UniverseFile(
        path=location,
        rows=tuple(rows),
        symbols_missing_nifty100=tuple(missing_flag),
        symbols_missing_group=tuple(missing_group),
    )


def load_quality_gate(path: Path | str) -> QualityGateFile:
    """Parse ``quality_gate.csv``. An empty file (header only) is valid.

    An empty gate means no symbol may be entered, which is the correct
    fail-closed starting state — not an error.
    """
    location = Path(path)
    rows: list[QualityRow] = []
    seen: set[str] = set()

    for line, record in _records(location, QUALITY_COLUMNS, "quality gate"):
        symbol = _required(record, "symbol", location, line).upper()
        _reject_duplicate(symbol, seen, location, line, "quality gate")
        rows.append(
            QualityRow(
                symbol=symbol,
                status=_status(record.get("status"), location, line),
                checked_on=_optional_date(record.get("checked_on"), "checked_on", location, line),
                valid_until=_required_date(
                    record.get("valid_until"), "valid_until", location, line
                ),
                notes=(record.get("notes") or "").strip(),
            )
        )
    return QualityGateFile(path=location, rows=tuple(rows))


def load_results_calendar(
    path: Path | str | None, *, required: bool = False
) -> ResultsCalendarFile:
    """Parse ``results_calendar.csv`` (spec 6.5).

    Optional in paper mode by decision 7; ``required=True`` is what a live mode
    would pass, per spec 4.6 item 6's "a live mode must fail closed".

    Raises:
        InputFileError: the file is absent and ``required``, or malformed.
    """
    if path is None:
        if required:
            raise InputFileError("No results calendar configured, but one is required.")
        return ResultsCalendarFile(path=None, absent=True)

    location = Path(path)
    if not location.exists():
        if required:
            raise InputFileError(f"{location}: results calendar is required but does not exist.")
        _log.warning(
            "%s: no results calendar; every symbol is flagged 'results date unknown'", location
        )
        return ResultsCalendarFile(path=location, absent=True)

    rows: list[ResultsRow] = []
    dates: dict[str, list[date]] = {}
    seen_pairs: set[tuple[str, date]] = set()

    for line, record in _records(location, RESULTS_COLUMNS, "results calendar"):
        symbol = _required(record, "symbol", location, line).upper()
        when = _required_date(record.get("results_date"), "results_date", location, line)
        # A symbol legitimately has several results dates across a year, so
        # only an exact repeat of the same pair is a defect.
        if (symbol, when) in seen_pairs:
            raise InputFileError(
                f"{location} line {line}: duplicate results row for {symbol} on {when}."
            )
        seen_pairs.add((symbol, when))
        rows.append(ResultsRow(symbol=symbol, results_date=when))
        dates.setdefault(symbol, []).append(when)

    return ResultsCalendarFile(
        path=location,
        rows=tuple(rows),
        _dates={symbol: tuple(sorted(days)) for symbol, days in dates.items()},
    )


def load_gap_acknowledgements(path: Path | str) -> tuple[GapAcknowledgement, ...]:
    """Parse ``gap_acknowledgements.csv`` (spec 6.1). Header-only is valid.

    Fail closed: a missing file raises like every other operator input, and a
    malformed row stops the run — a wrongly parsed acknowledgement would let a
    blocked symbol trade. ``ratio`` is close / previous close; it is rounded
    to 4 decimals, the precision gaps are matched on.

    Raises:
        InputFileError: missing file, wrong header, a bad value, or the same
            (symbol, gap_session) acknowledged twice.
    """
    location = Path(path)
    rows: list[GapAcknowledgement] = []
    seen: set[tuple[str, date]] = set()
    for line, record in _records(location, GAP_ACK_COLUMNS, "gap acknowledgements"):
        symbol = _required(record, "symbol", location, line).upper()
        session = _required_date(record.get("gap_session"), "gap_session", location, line)
        if (symbol, session) in seen:
            raise InputFileError(
                f"{location} line {line}: {symbol} {session} is acknowledged twice."
            )
        seen.add((symbol, session))
        raw_ratio = _required(record, "ratio", location, line)
        try:
            ratio = Decimal(raw_ratio)
        except InvalidOperation:
            raise InputFileError(
                f"{location} line {line}: ratio is {raw_ratio!r}; expected a number."
            ) from None
        if not ratio.is_finite() or ratio <= 0:
            raise InputFileError(f"{location} line {line}: ratio must be positive.")
        rows.append(
            GapAcknowledgement(
                symbol=symbol,
                gap_session=session,
                ratio=ratio.quantize(RATIO_PLACES, rounding=ROUND_HALF_UP),
                acknowledged_on=_required_date(
                    record.get("acknowledged_on"), "acknowledged_on", location, line
                ),
                note=(record.get("note") or "").strip(),
            )
        )
    return tuple(rows)


# ------------------------------------------------------------------- parsing
def _records(
    location: Path, expected: tuple[str, ...], label: str
) -> Iterator[tuple[int, Mapping[str, str]]]:
    if not location.exists():
        raise InputFileError(f"{location}: {label} file does not exist. Fail closed.")
    try:
        text = location.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise InputFileError(f"{location}: cannot read the {label} file: {exc}") from exc

    reader = csv.DictReader(io.StringIO(text))
    _check_header(reader.fieldnames, expected, location, label)
    # Line 1 is the header, so the first data row is line 2.
    for offset, record in enumerate(reader, start=2):
        if not any((value or "").strip() for value in record.values()):
            continue  # a wholly blank line is formatting, not data
        yield offset, record


def _check_header(
    fieldnames: Iterable[str] | None, expected: tuple[str, ...], location: Path, label: str
) -> None:
    if not fieldnames:
        raise InputFileError(f"{location}: the {label} file has no header row.")
    actual = tuple((name or "").strip() for name in fieldnames)
    if actual == expected:
        return
    missing = [name for name in expected if name not in actual]
    extra = [name for name in actual if name not in expected]
    detail = []
    if missing:
        detail.append(f"missing {missing}")
    if extra:
        detail.append(f"unexpected {extra}")
    if not detail:
        detail.append(f"expected order {list(expected)}, got {list(actual)}")
    raise InputFileError(f"{location}: {label} header is wrong — {'; '.join(detail)}.")


def _required(record: Mapping[str, str], column: str, location: Path, line: int) -> str:
    value = (record.get(column) or "").strip()
    if not value:
        raise InputFileError(f"{location} line {line}: {column} is blank.")
    return value


def _reject_duplicate(symbol: str, seen: set[str], location: Path, line: int, label: str) -> None:
    if symbol in seen:
        raise InputFileError(
            f"{location} line {line}: {symbol} appears twice in the {label} file. "
            "Which row is authoritative is not something this loader may decide."
        )
    seen.add(symbol)


def _boolean(raw: str, column: str, location: Path, line: int) -> bool:
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise InputFileError(
        f"{location} line {line}: {column} is {raw!r}; expected true or false (blank reads "
        "as false)."
    )


def _on_exit(raw: str | None, location: Path, line: int) -> OnExit:
    value = (raw or "").strip().lower()
    if not value:
        return OnExit.HOLD  # spec 6.3: hold is the default
    try:
        return OnExit(value)
    except ValueError:
        raise InputFileError(
            f"{location} line {line}: on_exit is {raw!r}; expected 'hold' or 'exit'."
        ) from None


def _status(raw: str | None, location: Path, line: int) -> QualityStatus:
    value = (raw or "").strip().upper()
    try:
        return QualityStatus(value)
    except ValueError:
        raise InputFileError(
            f"{location} line {line}: status is {raw!r}; expected PASS, EVENT_RISK or FAIL."
        ) from None


def _required_date(raw: str | None, column: str, location: Path, line: int) -> date:
    parsed = _optional_date(raw, column, location, line)
    if parsed is None:
        raise InputFileError(f"{location} line {line}: {column} is blank.")
    return parsed


def _optional_date(raw: str | None, column: str, location: Path, line: int) -> date | None:
    value = (raw or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise InputFileError(
            f"{location} line {line}: {column} is {raw!r}; expected an ISO date (YYYY-MM-DD). "
            "An ambiguous format such as 01/02/2026 is refused rather than guessed."
        ) from None
