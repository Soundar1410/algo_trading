"""Parse Dhan's daily-candle response into :class:`DailyBar` (spec 6.1).

The wire shape was confirmed against a real call on 2026-09-21: top-level
parallel arrays ``open``/``high``/``low``/``close``/``volume``/``timestamp``,
the timestamps being epoch seconds. That is the same shape ``/charts/intraday``
returns, so this keeps ``common.warmup.historical.parse_intraday_response``'s
defensive fallback for a ``data``-nested body — it costs one ``isinstance``
and covers a wrapper shape neither endpoint is documented to rule out.

Why this lives here and not in ``common/warmup/``: that module parses into
``common.models.Candle``, which is intraday-shaped, and spec section 13 keeps
this strategy's bar types out of the shared candle/warm-up stack entirely.

**Timestamps are converted in IST, once, here.** Dhan's daily candle stamps a
session at its start; read in the host's zone a session could land on the
neighbouring date, which would then place it in the wrong ISO week — the exact
class of defect D88+ exists for. Every date downstream of this function is
already an IST calendar date.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from common.logging import get_logger
from common.utils.timeutils import DEFAULT_TZ, get_tz

from .models import BarError, DailyBar

_log = get_logger(__name__)


class DailyResponseError(ValueError):
    """The response carried no usable candle arrays at all."""


def parse_daily_response(
    response: dict[str, Any], *, tz_name: str = DEFAULT_TZ, label: str = ""
) -> list[DailyBar]:
    """Parse one daily-candle response body into ascending :class:`DailyBar`s.

    Individually unusable rows are **skipped, not fatal** — including a row
    Dhan itself returned with an inconsistent OHLC, which :class:`DailyBar`'s
    own validation catches. A whole response with no candle arrays does raise:
    that is "the request did not return data", which must never be mistaken
    for "this symbol had no sessions".

    Duplicate sessions are collapsed to the last occurrence, so a response that
    repeats a session cannot produce two bars for one day and silently double
    that day's weight in a weekly aggregate.

    Raises:
        DailyResponseError: the body has no candle arrays, or no timestamps.
    """
    if not isinstance(response, dict):
        raise DailyResponseError(f"Unexpected daily response type: {type(response).__name__}")

    block = response.get("data") if isinstance(response.get("data"), dict) else response
    if not isinstance(block, dict) or "close" not in block:
        status = response.get("status")
        remarks = response.get("remarks")
        raise DailyResponseError(
            f"Daily response has no candle arrays{_suffix(label)} "
            f"(status={status!r}, remarks={remarks!r})."
        )

    # Presence, not truthiness: a symbol with no sessions in the requested
    # range legitimately returns every array empty, and that is an empty
    # series, not a malformed response. Only a wholly absent timestamp array
    # means the body is not the shape this parser understands.
    stamp_key = next((key for key in ("timestamp", "start_Time") if key in block), None)
    if stamp_key is None:
        raise DailyResponseError(f"Daily response is missing a timestamp array{_suffix(label)}.")
    stamps = _array(block, stamp_key)

    opens = _array(block, "open")
    highs = _array(block, "high")
    lows = _array(block, "low")
    closes = _array(block, "close")
    volumes = _array(block, "volume")

    tz = get_tz(tz_name)
    count = min(len(opens), len(highs), len(lows), len(closes), len(stamps))
    by_session: dict[Any, DailyBar] = {}
    skipped = 0

    for i in range(count):
        try:
            session = datetime.fromtimestamp(float(stamps[i]), tz).date()
            bar = DailyBar(
                session=session,
                open=float(opens[i]),
                high=float(highs[i]),
                low=float(lows[i]),
                close=float(closes[i]),
                volume=float(volumes[i]) if i < len(volumes) else 0.0,
            )
        except (TypeError, ValueError, OSError, OverflowError, BarError):
            skipped += 1
            continue
        by_session[bar.session] = bar

    if skipped:
        _log.warning("daily response%s: skipped %d unusable row(s)", _suffix(label), skipped)

    return sorted(by_session.values(), key=lambda bar: bar.session)


def _array(block: dict[str, Any], key: str) -> Sequence[Any]:
    value = block.get(key)
    return value if isinstance(value, Sequence) and not isinstance(value, str | bytes) else []


def _suffix(label: str) -> str:
    return f" for {label}" if label else ""
