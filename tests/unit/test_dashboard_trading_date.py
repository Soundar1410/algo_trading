"""``dashboards._shared.trading_date_today``: the dashboards' one "today".

Every page's trading date comes from here, and it is the **exchange's**
calendar date, never the host machine's. The distinction is invisible on an
IST developer Mac and decides the whole 00:00 to 05:30 IST window everywhere
else — see D88, and ``tests/unit/test_dashboard_apptest.py`` for the
behavioural proof at page level.

Nothing here reads the wall clock.
"""

from __future__ import annotations

import os
import time as _time
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from common.utils import timeutils
from dashboards._shared import trading_date_today

REPO_ROOT = Path(__file__).resolve().parents[2]

#: 00:30 IST on 2026-09-14 — 19:00 UTC on 2026-09-13. Inside the window the
#: bug lived in, and a day on which the two zones disagree about the date.
FROZEN_UTC = datetime(2026, 9, 13, 19, 0, tzinfo=UTC)


@pytest.fixture
def host_clock_in_utc() -> Iterator[None]:
    """Force this process's local zone to UTC, restoring it afterwards.

    ``datetime.astimezone()`` with no argument — what a naive ``date.today()``
    is equivalent to — reads libc's parsed zone, so ``TZ`` alone is not enough
    without ``tzset()``.
    """
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    if hasattr(_time, "tzset"):
        _time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        if hasattr(_time, "tzset"):
            _time.tzset()


def test_the_host_and_the_exchange_really_do_disagree_at_the_frozen_instant(
    host_clock_in_utc: None,
):
    """Guards the two tests below: if the frozen instant ever stopped
    straddling midnight, they would both pass vacuously."""
    assert FROZEN_UTC.astimezone().date() == date(2026, 9, 13)  # the host's answer
    assert FROZEN_UTC.astimezone(ZoneInfo("Asia/Kolkata")).date() == date(2026, 9, 14)


def test_the_trading_date_is_the_exchanges_day_not_the_hosts(
    host_clock_in_utc: None, monkeypatch: pytest.MonkeyPatch
):
    """The regression itself: 00:30 IST is still *that* IST day's session,
    while the UTC host is still on the day before."""
    monkeypatch.setattr(
        timeutils,
        "now_tz",
        lambda tz_name=timeutils.DEFAULT_TZ: FROZEN_UTC.astimezone(ZoneInfo(tz_name)),
    )
    assert trading_date_today() == date(2026, 9, 14)


def test_an_explicit_zone_is_honoured(host_clock_in_utc: None, monkeypatch: pytest.MonkeyPatch):
    """``tz_name`` exists so a future non-IST ``global.timezone`` has exactly
    one place to be wired in; it must actually be used, not decoration."""
    monkeypatch.setattr(
        timeutils,
        "now_tz",
        lambda tz_name=timeutils.DEFAULT_TZ: FROZEN_UTC.astimezone(ZoneInfo(tz_name)),
    )
    assert trading_date_today("UTC") == date(2026, 9, 13)
    assert trading_date_today("America/New_York") == date(2026, 9, 13)


def test_the_default_zone_is_the_one_the_committed_config_trades_in():
    """The helper defaults to :data:`common.utils.timeutils.DEFAULT_TZ` rather
    than loading YAML — a dashboard must degrade to a message, not raise a
    ``ConfigError``, and threading config loading into a date computation on
    five pages buys nothing while the two values agree. This test is what
    keeps them agreeing: change ``global.timezone`` and it fails here rather
    than silently shipping pages on the wrong calendar.
    """
    import yaml

    raw = yaml.safe_load((REPO_ROOT / "config" / "global.yaml").read_text(encoding="utf-8"))
    assert raw["global"]["timezone"] == timeutils.DEFAULT_TZ
