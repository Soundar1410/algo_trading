"""Mode-namespaced correlation IDs.

Format::

    p_io_st01_20260729_0001
    │  │   │     │        └── per-strategy, per-day sequence
    │  │   │     └─────────── trading date
    │  │   └───────────────── strategy token
    │  └───────────────────── runtime token
    └──────────────────────── mode namespace: p=paper, l=live

The leading mode namespace is the point. The spec requires a mode-specific
namespace so paper and live records cannot be confused, and putting it in the
*identifier itself* means the separation survives being copied into a log line,
a Telegram message or a support ticket — places where the ``execution_mode``
column does not travel.

Dhan constrains correlation IDs to a short alphanumeric-with-underscore string,
so tokens are truncated and sanitised rather than trusted. The sequence is
allocated by the database inside the same transaction that inserts the intent
(see :class:`~common.execution.repository.ExecutionRepository`), never by an
in-memory counter that a restart would reset.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from common.config.models import ExecutionMode

#: Dhan's documented limit is 25 characters. Staying under it is a constraint on
#: the format, not something to discover at submission time.
MAX_LENGTH = 25

#: How many characters of a sanitised strategy_id become its correlation-ID
#: token. Short enough to keep the whole ID inside MAX_LENGTH; see
#: strategy_token's own docstring for the collision this creates and how a
#: caller admitting strategies is expected to guard against it.
STRATEGY_TOKEN_LENGTH = 4

_NAMESPACE_BY_MODE = {ExecutionMode.PAPER: "p", ExecutionMode.LIVE: "l"}
_MODE_BY_NAMESPACE = {v: k for k, v in _NAMESPACE_BY_MODE.items()}

_SANITISE_RE = re.compile(r"[^a-z0-9]+")
_PATTERN = re.compile(
    r"^(?P<ns>[pl])_(?P<runtime>[a-z0-9]+)_(?P<strategy>[a-z0-9]+)"
    r"_(?P<date>\d{8})_(?P<seq>\d{4,})$"
)


class CorrelationIdError(ValueError):
    """Raised when a correlation ID cannot be built or parsed."""


@dataclass(frozen=True)
class ParsedCorrelationId:
    execution_mode: ExecutionMode
    runtime_token: str
    strategy_token: str
    trading_date: str
    sequence_number: int


def _token(value: str, limit: int) -> str:
    """Reduce an identifier to a short lowercase alphanumeric token."""
    cleaned = _SANITISE_RE.sub("", value.lower())
    if not cleaned:
        raise CorrelationIdError(f"{value!r} has no alphanumeric characters to build a token from")
    return cleaned[:limit]


def strategy_token(strategy_id: str) -> str:
    """The exact token :func:`build_correlation_id` will embed for this
    strategy — public so a caller admitting strategies can check for a
    collision *before* either one ever places an order.

    **Not guaranteed unique.** Two strategy IDs agreeing on their first
    :data:`STRATEGY_TOKEN_LENGTH` alphanumeric characters (case-insensitive)
    — ``io_skelone`` and ``io_skeltwo``, say — produce the identical token,
    and therefore the identical ``correlation_id`` string on each one's
    first order of the day. Discovered exactly this way: two strategies in
    one test whose IDs happened to share a four-character prefix collided
    on ``order_intents.correlation_id``'s UNIQUE constraint, surfacing as a
    plain worker crash rather than a controlled refusal (Phase 7 Part 4,
    D78). ``IntradayOptionsSupervisor.add_worker`` calls this on every
    admission and refuses a strategy whose token collides with one already
    admitted, the same way it already refuses a live-mode strategy the gate
    blocks — before this function existed, nothing checked at all.
    """
    return _token(strategy_id, limit=STRATEGY_TOKEN_LENGTH)


def build_correlation_id(
    *,
    execution_mode: ExecutionMode,
    runtime_id: str,
    strategy_id: str,
    trading_date: str,
    sequence_number: int,
) -> str:
    """Build a correlation ID. ``trading_date`` is ``YYYY-MM-DD`` or ``YYYYMMDD``."""
    if sequence_number < 0:
        raise CorrelationIdError(f"sequence_number must not be negative, got {sequence_number}")

    compact_date = trading_date.replace("-", "")
    if not (len(compact_date) == 8 and compact_date.isdigit()):
        raise CorrelationIdError(
            f"trading_date must be YYYY-MM-DD or YYYYMMDD, got {trading_date!r}"
        )
    try:
        # Digit-counting alone would accept "29-07-2026": eight digits, but
        # day-first. That produces a valid-looking ID for the year 2907, which
        # no query would ever match and no operator would notice.
        datetime.strptime(compact_date, "%Y%m%d")
    except ValueError as exc:
        raise CorrelationIdError(
            f"trading_date {trading_date!r} is not a real YYYY-MM-DD date: {exc}"
        ) from exc

    namespace = _NAMESPACE_BY_MODE[execution_mode]
    runtime_token = _initials(runtime_id, limit=3)
    strategy_id_token = _token(strategy_id, limit=STRATEGY_TOKEN_LENGTH)
    candidate = (
        f"{namespace}_{runtime_token}_{strategy_id_token}_{compact_date}_{sequence_number:04d}"
    )

    if len(candidate) > MAX_LENGTH:
        raise CorrelationIdError(
            f"Correlation ID {candidate!r} is {len(candidate)} characters, "
            f"over the {MAX_LENGTH}-character broker limit"
        )
    return candidate


def _initials(value: str, *, limit: int) -> str:
    """``intraday_options`` becomes ``io`` — keeps the ID inside the length limit."""
    parts = [p for p in _SANITISE_RE.split(value.lower()) if p]
    if not parts:
        raise CorrelationIdError(f"{value!r} has no alphanumeric characters to build a token from")
    if len(parts) == 1:
        return parts[0][:limit]
    return "".join(part[0] for part in parts)[:limit]


def parse_correlation_id(correlation_id: str) -> ParsedCorrelationId:
    """Recover the structured parts, including which mode produced the ID."""
    match = _PATTERN.match(correlation_id)
    if match is None:
        raise CorrelationIdError(f"{correlation_id!r} is not a valid correlation ID")
    return ParsedCorrelationId(
        execution_mode=_MODE_BY_NAMESPACE[match.group("ns")],
        runtime_token=match.group("runtime"),
        strategy_token=match.group("strategy"),
        trading_date=match.group("date"),
        sequence_number=int(match.group("seq")),
    )


def is_paper(correlation_id: str) -> bool:
    """True when the ID itself declares paper execution."""
    return correlation_id.startswith("p_")
