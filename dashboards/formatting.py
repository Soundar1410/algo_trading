"""Presentation helpers shared by every dashboard page.

Pure functions only — no Streamlit, no database access — so every page can
import this module without affecting any of the AST-based import-boundary
checks in ``tests/unit/test_dashboard.py``, and every formatting rule is
testable without a fixture database.

Timestamps are stored throughout this project as UTC ``datetime.isoformat()``
strings (see ``common.execution.repository._now``). :func:`format_ist` is the
one place that turns one of those into what an operator actually reads —
every page must go through it rather than printing a raw UTC string, per the
spec's "Timestamps shown in IST" requirement.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime

from common.utils.timeutils import DEFAULT_TZ, get_tz

#: The literal shown for a field that is genuinely not available — never a
#: fabricated zero, never blank (blank looks like a rendering bug).
MISSING = "—"  # em dash

#: Explicit mode labels. An operator must never have to guess whether a
#: number is real money.
PAPER_LABEL = "PAPER — simulated"
LIVE_LABEL = "LIVE — real money"
BLOCKED_LABEL = "BLOCKED — fail-closed"
NOT_CONFIGURED_LABEL = "NOT CONFIGURED"
DEGRADED_LABEL = "DEGRADED"
HEALTHY_LABEL = "HEALTHY"

_IST = get_tz(DEFAULT_TZ)


def mode_label(execution_mode: str | None) -> str:
    """Always explicit — reused everywhere a mode badge is shown."""
    if execution_mode == "paper":
        return PAPER_LABEL
    if execution_mode == "live":
        return LIVE_LABEL
    return "UNKNOWN"


def parse_utc(value: str | None) -> datetime | None:
    """Parse one of this project's stored UTC ISO timestamps, or ``None``."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed


def format_ist(value: str | None, *, with_seconds: bool = True) -> str:
    """Render a stored UTC timestamp in IST. :data:`MISSING` if absent/unparseable."""
    parsed = parse_utc(value)
    if parsed is None:
        return MISSING
    local = parsed.astimezone(_IST)
    pattern = "%Y-%m-%d %H:%M:%S IST" if with_seconds else "%Y-%m-%d %H:%M IST"
    return local.strftime(pattern)


def format_ist_time_only(value: str | None) -> str:
    """Just the clock time in IST — for dense tables with a separate date column."""
    parsed = parse_utc(value)
    if parsed is None:
        return MISSING
    return parsed.astimezone(_IST).strftime("%H:%M:%S")


def format_age(seconds: float | None) -> str:
    """Human-scale age: ``"3s"``, ``"4m 12s"``, ``"2h 05m"``. :data:`MISSING` if unknown."""
    if seconds is None:
        return MISSING
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def format_inr(value: float | None, *, decimals: int = 2) -> str:
    """Indian rupee grouping: 3 digits, then groups of 2 (e.g. ``1,23,456.78``).

    :data:`MISSING` for ``None`` — a missing value must never render as
    ``₹0.00``, which looks like a real, computed zero.
    """
    if value is None:
        return MISSING
    sign = "-" if value < 0 else ""
    magnitude = abs(value)
    whole, _, frac = f"{magnitude:,.{decimals}f}".replace(",", "").partition(".")
    if len(whole) > 3:
        last3 = whole[-3:]
        rest = whole[:-3]
        groups: list[str] = []
        while len(rest) > 2:
            groups.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            groups.insert(0, rest)
        whole = ",".join([*groups, last3])
    formatted = f"₹{sign}{whole}"
    if decimals:
        formatted += f".{frac}"
    return formatted


def format_signed_inr(value: float | None, *, decimals: int = 2) -> str:
    """Same as :func:`format_inr` but with an explicit ``+``/``-`` sign."""
    if value is None:
        return MISSING
    text = format_inr(abs(value), decimals=decimals)
    if value > 0:
        return f"+{text}"
    if value < 0:
        return f"-{text}"
    return text


def format_pct(value: float | None, *, decimals: int = 1) -> str:
    if value is None:
        return MISSING
    return f"{value:.{decimals}f}%"


def pnl_color(value: float | None) -> str:
    """Streamlit markdown color token — ``green``/``red``/``gray``."""
    if value is None or value == 0:
        return "gray"
    return "green" if value > 0 else "red"


def colored(text: str, color: str) -> str:
    """Streamlit's ``:color[text]`` markdown syntax, as a plain string builder."""
    return f":{color}[{text}]"


def health_badge(health_state: str | None) -> str:
    """A short colored badge for a runtime/strategy health state."""
    state = (health_state or "").upper()
    if state in {"RUNNING_PAPER", "RUNNING_LIVE", "RUNNING"}:
        return colored(f"● {state}", "green")
    if state in {"DEGRADED", "STALE"}:
        return colored(f"● {state}", "orange")
    if state in {"FAILED", "CRASHED"}:
        return colored(f"● {state}", "red")
    if not state or state == "STOPPED":
        return colored("● STOPPED", "gray")
    return colored(f"● {state}", "gray")


def to_csv_bytes(rows: list[dict[str, object]]) -> bytes:
    """Plain ``list[dict] -> CSV bytes``, no server-side file write — fed
    directly into ``st.download_button``. Returns an empty-but-valid CSV
    (a friendly placeholder header) when ``rows`` is empty, matching the
    reference dashboard's "never crash the export on an empty table" habit.
    """
    if not rows:
        return b"no_data\n"
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def freshness_note(seconds: float | None, *, stale_after_seconds: float) -> str | None:
    """``None`` when fresh; a caption-ready warning when a mark/heartbeat is stale.

    Used everywhere the spec insists a stale value must never be shown as
    current — never omit the age, always say plainly when it is too old to
    trust.
    """
    if seconds is None:
        return "no reading yet"
    if seconds > stale_after_seconds:
        return f"stale — last updated {format_age(seconds)} ago"
    return None
