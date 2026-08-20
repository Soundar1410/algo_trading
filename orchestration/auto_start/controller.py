"""The daily start controller — one ordered chain, in one process.

    trading-day / time gate
      -> project and .venv availability
      -> environment and paper-safety validation
      -> internet / auth retry
      -> Dhan token validation
      -> Telegram auth-success notification
      -> start the enabled runtime supervisors

A runtime cannot start before validated authentication because there is one
call site for it, at the end of one function, after the validation step. That
is deliberately structural: the previous design expressed the same intent as
two LaunchAgents fifteen minutes apart, which is a hope about scheduling rather
than a dependency.

Everything that can wait is injected — the clock, the backoff waiter, the
notifier, the auth bootstrap, the launcher — so the whole of this file is
exercised without a real clock, a real network call or a real process.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from common.config import AutoStartConfig, ProjectPaths
from common.engine.session import MarketSession
from common.logging import get_logger
from common.notifications import NotificationEvent, Notifier
from common.utils.timeutils import local_date_in

from .auth_flow import ValidatedToken, authenticate_and_validate, cooldown_ready_at
from .day_claim import KIND_AUTH_SUCCESS, KIND_GIVE_UP, DailyNotificationClaim
from .gate import StartDecision, evaluate_start_window, session_deadline
from .paper_safety import PaperSafetyReport, verify_paper_only
from .retry import (
    DeadlineWaiter,
    ProjectUnavailableError,
    Retryability,
    classify,
)
from .runtime_launcher import LaunchResult, RuntimeLauncher

_log = get_logger(__name__)

EXIT_OK = 0
EXIT_TERMINAL_REFUSAL = 2
EXIT_DEADLINE_EXPIRED = 3

#: The ``runtime_id`` every notification from this layer carries. Not a real
#: runtime: these events are about the platform coming up, before any runtime
#: group exists to attribute them to.
NOTIFY_RUNTIME_ID = "auto_start"


@dataclass(frozen=True)
class StartupOutcome:
    exit_code: int
    reason: str
    launched: dict[str, LaunchResult] = field(default_factory=dict)
    notified: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == EXIT_OK


class AutoStartController:
    """Runs the daily start chain exactly once, then supervises what it started."""

    def __init__(
        self,
        *,
        cfg: AutoStartConfig,
        config_root: Path,
        paths: ProjectPaths,
        session: MarketSession,
        clock: Callable[[], datetime],
        waiter: DeadlineWaiter,
        stop_event: threading.Event,
        notifier: Notifier,
        claim: DailyNotificationClaim,
        bootstrap_factory: Callable[[], object],
        launcher_factory: Callable[[], RuntimeLauncher],
        project_probe: Callable[[], None] | None = None,
        check_system_timezone: bool = True,
        check_legacy: bool = True,
        check_environment: bool = True,
    ) -> None:
        self._cfg = cfg
        self._config_root = config_root
        self._paths = paths
        self._session = session
        self._clock = clock
        self._waiter = waiter
        self._stop_event = stop_event
        self._notifier = notifier
        self._claim = claim
        self._bootstrap_factory = bootstrap_factory
        self._launcher_factory = launcher_factory
        self._project_probe = project_probe if project_probe is not None else _default_probe
        self._check_system_timezone = check_system_timezone
        self._check_legacy = check_legacy
        self._check_environment = check_environment

    # -------------------------------------------------------------------- run
    def run(self) -> StartupOutcome:
        now = self._clock()
        decision = evaluate_start_window(
            self._cfg,
            now=now,
            session=self._session,
            check_system_timezone=self._check_system_timezone,
        )
        if decision.terminal:
            _log.error("automatic startup refused: %s", decision.reason)
            notified = self._notify_give_up(decision.reason, day=self._today(now))
            return StartupOutcome(EXIT_TERMINAL_REFUSAL, decision.reason, notified=notified)
        if not decision.eligible:
            # Not an error: a weekend, a holiday, an early login, a late one.
            # Nothing has touched the network at this point.
            _log.info("not starting: %s", decision.reason)
            return StartupOutcome(EXIT_OK, decision.reason)

        _log.info("automatic startup %s", decision.reason)
        deadline = session_deadline(self._cfg, now=now)
        return self._attempt_until(deadline=deadline, decision=decision)

    def _attempt_until(self, *, deadline: datetime, decision: StartDecision) -> StartupOutcome:
        """The retry loop. Transient failures wait; terminal ones stop now."""
        while True:
            if self._stop_event.is_set():
                return StartupOutcome(EXIT_OK, "shutdown requested before startup completed")

            try:
                report = self._preflight()
                if not report.plans:
                    reason = "no runtime is enabled; nothing to start"
                    _log.info("%s", reason)
                    return StartupOutcome(EXIT_OK, reason)
                validated = self._authenticate()
            except BaseException as exc:  # classified below; nothing swallowed
                if isinstance(exc, KeyboardInterrupt | SystemExit):
                    raise
                outcome = self._handle_failure(exc, deadline=deadline)
                if outcome is not None:
                    return outcome
                continue

            self._waiter.reset()
            return self._start_runtimes(report=report, validated=validated)

    def _handle_failure(self, exc: BaseException, *, deadline: datetime) -> StartupOutcome | None:
        """``None`` to retry; an outcome to stop."""
        kind = classify(exc)
        now = self._clock()
        day = self._today(now)

        if kind is Retryability.TERMINAL:
            reason = f"{type(exc).__name__}: {exc}"
            _log.error("automatic startup failed permanently: %s", reason)
            notified = self._notify_give_up(reason, day=day)
            return StartupOutcome(EXIT_TERMINAL_REFUSAL, reason, notified=notified)

        if kind is Retryability.COOLDOWN:
            ready_at = self._cooldown_boundary(now=now)
            if ready_at >= deadline:
                reason = (
                    f"{type(exc).__name__}: {exc} — the cooldown outlasts today's "
                    "session deadline"
                )
                _log.error("%s", reason)
                notified = self._notify_give_up(reason, day=day)
                return StartupOutcome(EXIT_DEADLINE_EXPIRED, reason, notified=notified)
            _log.warning("waiting out a provider cooldown: %s", exc)
            if not self._waiter.wait_until(ready_at, deadline=deadline):
                return self._deadline_outcome(exc, day=day)
            return None

        _log.warning("transient startup failure (%s); will retry: %s", type(exc).__name__, exc)
        if not self._waiter.wait(deadline=deadline):
            return self._deadline_outcome(exc, day=day)
        return None

    def _deadline_outcome(self, exc: BaseException, *, day: date) -> StartupOutcome:
        if self._stop_event.is_set():
            return StartupOutcome(EXIT_OK, "shutdown requested while retrying")
        reason = (
            f"gave up at the session deadline; last failure was "
            f"{type(exc).__name__}: {exc}"
        )
        _log.error("%s", reason)
        notified = self._notify_give_up(reason, day=day)
        return StartupOutcome(EXIT_DEADLINE_EXPIRED, reason, notified=notified)

    # ------------------------------------------------------------------ steps
    def _preflight(self) -> PaperSafetyReport:
        self._project_probe()
        report = verify_paper_only(
            self._config_root,
            check_legacy=self._check_legacy,
            check_environment=self._check_environment,
        )
        report.raise_if_unsafe()
        return report

    def _authenticate(self) -> ValidatedToken:
        bootstrap = self._bootstrap_factory()
        return authenticate_and_validate(bootstrap)  # type: ignore[arg-type]

    def _cooldown_boundary(self, *, now: datetime) -> datetime:
        try:
            return cooldown_ready_at(self._bootstrap_factory(), now=now)  # type: ignore[arg-type]
        except Exception:
            return now

    def _start_runtimes(
        self, *, report: PaperSafetyReport, validated: ValidatedToken
    ) -> StartupOutcome:
        """Notify once, then launch and own the enabled runtimes."""
        now = self._clock()
        notified = self._notify_success(validated, report=report, now=now)

        launcher = self._launcher_factory()
        results = launcher.launch(report.runtime_ids)

        started = [rid for rid, result in results.items() if result.started]
        failed = [rid for rid, result in results.items() if not result.started]
        if failed:
            # Isolation, stated plainly: the healthy runtime keeps running.
            _log.error("runtime(s) failed to start: %s", ", ".join(sorted(failed)))
        if not started:
            reason = f"no runtime started: {', '.join(sorted(failed)) or 'none attempted'}"
            return StartupOutcome(EXIT_TERMINAL_REFUSAL, reason, results, notified)

        # Stays alive for the whole session: caffeinate, launchd's view of the
        # job, and SIGTERM propagation all depend on it.
        launcher.supervise()
        reason = f"started {', '.join(sorted(started))}"
        if failed:
            reason += f"; failed {', '.join(sorted(failed))}"
        return StartupOutcome(EXIT_OK, reason, results, notified)

    # ---------------------------------------------------------- notifications
    def _today(self, now: datetime) -> date:
        return local_date_in(now, self._cfg.timezone, argument="now")

    def _deliver(self, event: NotificationEvent) -> bool:
        """Bounded delivery. Returns whether it genuinely got through."""
        attempts = self._cfg.telegram_retry_attempts
        for attempt in range(1, attempts + 1):
            try:
                if self._notifier.send(event):
                    return True
            except Exception as exc:
                _log.warning("notification attempt %d/%d raised: %s", attempt, attempts, exc)
            else:
                _log.warning("notification attempt %d/%d was not delivered", attempt, attempts)
            if attempt < attempts and not self._stop_event.wait(
                self._cfg.telegram_retry_delay_seconds
            ):
                continue
            break
        return False

    def _notify_success(
        self, validated: ValidatedToken, *, report: PaperSafetyReport, now: datetime
    ) -> bool:
        event = NotificationEvent(
            event_type="auto_start_auth_success",
            message=_success_message(
                validated, report=report, now=now, timezone=self._cfg.timezone
            ),
            runtime_id=NOTIFY_RUNTIME_ID,
        )
        result = self._claim.send_once(
            day=self._today(now),
            kind=KIND_AUTH_SUCCESS,
            deliver=lambda: self._deliver(event),
        )
        if result.delivered and result.sent_by_us:
            _log.info("sent the daily authentication-success notification")
        elif result.delivered:
            _log.info("authentication-success notification already sent today by another process")
        else:
            # Explicitly not fatal: a paper runtime that is otherwise safe to
            # start must not be held hostage by Telegram being unreachable.
            _log.error("authentication-success notification failed: %s", result.reason)
        return result.delivered

    def _notify_give_up(self, reason: str, *, day: date) -> bool:
        """One alert per trading date when startup stops for the day.

        Bounded and once-only on purpose: an alert per retry attempt would be
        an outage turned into a pager storm.
        """
        event = NotificationEvent(
            event_type="auto_start_gave_up",
            message=(
                "Automatic PAPER startup did not complete today.\n"
                f"Date: {day.isoformat()}\n"
                f"Reason: {reason}\n"
                "No runtime was started. Execution posture: PAPER ONLY."
            ),
            runtime_id=NOTIFY_RUNTIME_ID,
            required_action="Check logs/launchd/autostart.err.log and the auto-start log.",
        )
        result = self._claim.send_once(
            day=day, kind=KIND_GIVE_UP, deliver=lambda: self._deliver(event)
        )
        return result.delivered


def _success_message(
    validated: ValidatedToken,
    *,
    report: PaperSafetyReport,
    now: datetime,
    timezone: str,
) -> str:
    """Non-secret only.

    Carries no access token, no TOTP, no PIN, no bot token and no Dhan client
    id — not even a partial one. ``token source`` and ``expiry`` say everything
    an operator needs about *which* credential is in play without reproducing
    any part of it. :meth:`NotificationEvent.rendered` applies the process-wide
    redactor on top of this as a second, independent layer.
    """
    runtimes = ", ".join(report.runtime_ids) or "none"
    strategies = ", ".join(report.strategy_ids) or "none"
    stamp = now.astimezone(now.tzinfo).strftime("%Y-%m-%d %H:%M:%S")
    return (
        "Dhan authentication successful\n"
        f"Time: {stamp} ({timezone})\n"
        f"Token source: {validated.source}\n"
        f"Token expiry: {validated.expiry_time or 'unknown'}\n"
        "Execution posture: PAPER ONLY\n"
        f"Runtimes starting: {runtimes}\n"
        f"Strategies starting: {strategies}"
    )


def _default_probe() -> None:
    """Confirm the project root and its interpreter are actually present.

    Raises :class:`ProjectUnavailableError` — a *retryable* condition — because
    at login the Trading volume routinely mounts after ``launchd`` has fired.
    """
    from common.config.paths import resolve_project_root

    root = resolve_project_root()
    interpreter = root / ".venv" / "bin" / "python"
    if not interpreter.is_file():
        raise ProjectUnavailableError(
            f"{interpreter} is not present yet — is /Volumes/Trading mounted?"
        )
