"""Phase 5 B3: the operator's eight verified real moves (spec 6.1, 16 v1.3).

Built from the twelve blocking gaps the first real preview listed
(2026-09-25-preview.md), each keyed exactly as it was reported, so the test
depends on the committed CSV and not on the local (gitignored) cache. The
Part C dry run shows the same result on the real cache.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from strategies.positional_stocks.wsr1_weekly_stochrsi.gaps import Gap, blocking_gaps
from strategies.positional_stocks.wsr1_weekly_stochrsi.inputs import load_gap_acknowledgements

CSV = (
    Path(__file__).resolve().parents[2]
    / "config"
    / "positional_stocks"
    / "gap_acknowledgements.csv"
)

ACKNOWLEDGED = {
    ("BANKBARODA", "2017-10-25", "1.3144"),
    ("BANKINDIA", "2017-10-25", "1.3412"),
    ("CANBK", "2017-10-25", "1.3873"),
    ("PNB", "2017-10-25", "1.4620"),
    ("UNIONBANK", "2017-10-25", "1.3417"),
    ("INDUSINDBK", "2020-03-26", "1.4467"),
    ("TATACOMM", "2019-09-17", "0.6519"),
    ("POLICYBZR", "2026-09-24", "0.6400"),
}
#: The latest gap of each symbol that stays blocked (spec 16 v1.3).
STILL_BLOCKED = {
    ("MOTHERSON", "2024-04-30", "0.6687"),
    ("PATANJALI", "2020-01-27", "5.0625"),
    ("IDEA", "2020-03-19", "0.6804"),
    ("YESBANK", "2020-03-17", "1.5809"),
}


def _gap(symbol: str, session: str, ratio: str) -> Gap:
    day = date.fromisoformat(session)
    return Gap(
        symbol=symbol,
        session=day,
        previous_session=day - timedelta(days=1),
        previous_close=100.0,
        close=float(Decimal("100") * Decimal(ratio)),
        ratio=Decimal(ratio),
    )


def test_the_eight_rows_load_exactly_as_committed() -> None:
    rows = load_gap_acknowledgements(CSV)
    keys = {(r.symbol, r.gap_session.isoformat(), f"{r.ratio}") for r in rows}
    assert keys == ACKNOWLEDGED
    assert all(r.acknowledged_on == date(2026, 9, 27) for r in rows)
    assert len(rows) == 8


def test_only_motherson_patanjali_idea_and_yesbank_still_block() -> None:
    acknowledgements = load_gap_acknowledgements(CSV)
    gaps = [_gap(*key) for key in sorted(ACKNOWLEDGED | STILL_BLOCKED)]
    assert all(g.flagged for g in gaps)  # every one is a >= 30% move
    blocking = blocking_gaps(gaps, acknowledgements, window_start=date.min)
    assert {g.symbol for g in blocking} == {"MOTHERSON", "PATANJALI", "IDEA", "YESBANK"}


def test_an_acknowledgement_never_covers_another_session_or_ratio() -> None:
    """Spec 6.1 v1.2b: keyed to (symbol, session, ratio), never the symbol."""
    acknowledgements = load_gap_acknowledgements(CSV)
    other_day = _gap("POLICYBZR", "2026-09-25", "0.6400")
    other_ratio = _gap("POLICYBZR", "2026-09-24", "0.6500")
    assert blocking_gaps([other_day, other_ratio], acknowledgements, window_start=date.min) == [
        other_day,
        other_ratio,
    ]
