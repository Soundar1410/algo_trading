"""The trading-day and start-window gate — the cheapest, first decision.

Everything here is pure apart from one ``/etc/localtime`` read. That matters:
on a weekend or a configured holiday this gate must return before anything
touches credentials, the network or the broker, so a Saturday wake-up costs a
filesystem stat and an exit code.

Weekend and holiday rules are **not** reimplemented here. They come from
:class:`common.engine.session.MarketSession`, built from a
:class:`common.engine.config.SessionConfig` carrying the operator's holiday
list, so the calendar the engine trades on and the calendar the auto-start
consults are the same object.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from common.config import AutoStartConfig
from common.engine.config import SessionConfig
from common.engine.session import MarketSession
from common.logging import get_logger
from common.utils.timeutils import local_time_in, now_tz, parse_hhmm

_log = get_logger(__name__)

#: Where macOS records the system zone, as a symlink into the zoneinfo tree.
LOCALTIME_LINK = Path("/etc/localtime")


@dataclass(frozen=True)
class StartDecision:
    """Whether an automatic start may proceed, and why."""

    eligible: bool
    reason: str
    #: True when this is a *refusal* (a safety problem an operator must fix),
    #: as opposed to the ordinary "not yet" / "not today" answers. Only a
    #: terminal decision is worth a Telegram alert; a weekend is not.
    terminal: bool = False

    @property
    def quiet(self) -> bool:
        """True when the right response is a silent, successful exit."""
        return not self.eligible and not self.terminal


def build_session(cfg: AutoStartConfig) -> MarketSession:
    """A :class:`MarketSession` over the auto-start window and holiday list.

    ``end_time``/``square_off_time`` are set from the configured deadline
    because :class:`MarketSession` validates ``start < end <= square_off``; the
    only predicates this module uses are the calendar ones
    (:meth:`~MarketSession.is_trading_day`, :meth:`~MarketSession.is_holiday`),
    which read the holiday set and the weekday, not those boundaries.
    """
    start = cfg.startup_time
    deadline = cfg.session_deadline_time
    # A degenerate window (deadline == startup) would trip MarketSession's own
    # ordering check. Nudge only the *derived* end, never the real boundaries
    # this module compares against.
    if parse_hhmm(deadline) <= parse_hhmm(start):
        deadline = "23:59"
    return MarketSession(
        SessionConfig(
            timezone=cfg.timezone,
            start_time=start,
            end_time=deadline,
            square_off_time=deadline,
            holidays=cfg.holidays,
        )
    )


def system_timezone_name() -> str | None:
    """The Mac's configured IANA zone, or ``None`` if it cannot be determined.

    Read from the ``/etc/localtime`` symlink, which macOS points at
    ``/var/db/timezone/zoneinfo/<Area>/<City>``. ``TZ`` wins when set, because
    it is what this process's own ``datetime`` calls would honour.
    """
    env_zone = os.environ.get("TZ")
    if env_zone:
        return env_zone
    try:
        target = LOCALTIME_LINK.resolve()
    except OSError:
        return None
    parts = target.parts
    if "zoneinfo" not in parts:
        return None
    index = len(parts) - 1 - parts[::-1].index("zoneinfo")
    tail = parts[index + 1 :]
    return "/".join(tail) if tail else None


def system_timezone_matches(configured: str, *, at: datetime | None = None) -> bool:
    """Whether the Mac's clock agrees with the configured trading timezone.

    Two independent checks, either of which is enough to pass:

    1. the resolved zone *name* matches, or
    2. the system's current UTC offset equals the configured zone's.

    The offset fallback exists because a name mismatch is not always a real
    one — ``Asia/Calcutta`` is the same zone as ``Asia/Kolkata``, and a Mac
    with an unreadable ``/etc/localtime`` still has a usable offset. What both
    checks failing means is that ``StartCalendarInterval``'s 09:00 is not the
    configured zone's 09:00, which is exactly the condition this exists for.
    """
    moment = at if at is not None else now_tz(configured)
    name = system_timezone_name()
    if name == configured:
        return True
    system_offset = moment.astimezone().utcoffset()
    configured_offset = moment.astimezone(now_tz(configured).tzinfo).utcoffset()
    return system_offset is not None and system_offset == configured_offset


def evaluate_start_window(
    cfg: AutoStartConfig,
    *,
    now: datetime,
    session: MarketSession,
    check_system_timezone: bool = True,
) -> StartDecision:
    """The whole time/calendar decision, in the order that costs least.

    ``now`` must be timezone-aware; :func:`common.utils.timeutils.local_time_in`
    refuses a naive one rather than guessing at a zone, which is the bug class
    this project has already paid for once.
    """
    if not cfg.enabled:
        return StartDecision(False, "auto_start.enabled is false")

    if (
        check_system_timezone
        and cfg.require_system_timezone_match
        and not system_timezone_matches(cfg.timezone, at=now)
    ):
        detected = system_timezone_name() or "unknown"
        return StartDecision(
            False,
            (
                f"system timezone {detected!r} does not match the configured "
                f"{cfg.timezone!r}. launchd's StartCalendarInterval fires in the "
                "Mac's local zone, so the scheduled trigger would not be at the "
                "configured start time. Set the Mac's timezone, or set "
                "auto_start.require_system_timezone_match: false to accept the risk."
            ),
            terminal=True,
        )

    if session.is_holiday(now):
        return StartDecision(False, f"{now.date().isoformat()} is a configured trading holiday")
    if not session.is_trading_day(now):
        return StartDecision(False, f"{now.date().isoformat()} is not a trading day")

    clock = local_time_in(now, cfg.timezone, argument="now")
    if clock < parse_hhmm(cfg.startup_time):
        return StartDecision(
            False,
            (
                f"{clock.strftime('%H:%M')} is before the configured start "
                f"{cfg.startup_time}; the scheduled trigger will start it"
            ),
        )
    if clock > parse_hhmm(cfg.latest_start_time):
        return StartDecision(
            False,
            (
                f"{clock.strftime('%H:%M')} is past the latest start "
                f"{cfg.latest_start_time}; no new automatic session today"
            ),
        )

    return StartDecision(True, f"eligible at {clock.strftime('%H:%M')} {cfg.timezone}")


def session_deadline(cfg: AutoStartConfig, *, now: datetime) -> datetime:
    """The instant every retry loop must stop at, on ``now``'s trading date."""
    from common.utils.timeutils import combine, local_date_in

    day = local_date_in(now, cfg.timezone, argument="now")
    return combine(day, parse_hhmm(cfg.session_deadline_time), cfg.timezone)
