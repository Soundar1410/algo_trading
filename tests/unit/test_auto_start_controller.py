"""orchestration.auto_start.controller: the ordered chain, end to end.

The property this file exists to pin down is an ordering one: **no runtime may
be started before Dhan has validated the token**. Several tests below assert it
from the other side — by making authentication fail, or the network raise, and
then insisting the launcher was never called at all.

Nothing here reaches a network, a broker, a real process or a real clock.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from common.authentication import (
    InvalidCredentialsError,
    TokenGenerationError,
    TokenOutcome,
    TokenRateLimitedError,
)
from common.config import AutoStartConfig, ProjectPaths
from common.notifications import NotificationEvent
from orchestration.auto_start import controller as ctl
from orchestration.auto_start.day_claim import (
    KIND_AUTH_SUCCESS,
    DailyNotificationClaim,
)
from orchestration.auto_start.gate import build_session
from orchestration.auto_start.paper_safety import PaperSafetyReport, RuntimePlan
from orchestration.auto_start.retry import DeadlineWaiter, ProjectUnavailableError
from orchestration.auto_start.runtime_launcher import LaunchResult

IST = ZoneInfo("Asia/Kolkata")
START = datetime(2026, 8, 20, 9, 0, tzinfo=IST)  # a Thursday
SATURDAY = datetime(2026, 8, 22, 9, 30, tzinfo=IST)


class _Clock:
    def __init__(self, start: datetime = START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class _AdvancingEvent(threading.Event):
    def __init__(self, clock: _Clock) -> None:
        super().__init__()
        self._clock = clock

    def wait(self, timeout: float | None = None) -> bool:  # type: ignore[override]
        self._clock.advance(float(timeout or 0))
        return self.is_set()


class _RecordingNotifier:
    channel = "recording"

    def __init__(self, *, succeeds: bool = True, raises: bool = False) -> None:
        self.events: list[NotificationEvent] = []
        self._succeeds = succeeds
        self._raises = raises

    def send(self, event: NotificationEvent) -> bool:
        self.events.append(event)
        if self._raises:
            raise RuntimeError("telegram unreachable")
        return self._succeeds


class _FakeLauncher:
    def __init__(self, *, started: dict[str, bool] | None = None) -> None:
        self.launched: list[str] = []
        self.supervised = False
        self._started = started or {}

    def launch(self, runtime_ids):
        self.launched.extend(runtime_ids)
        return {
            rid: LaunchResult(
                runtime_id=rid, started=self._started.get(rid, True), detail="test"
            )
            for rid in runtime_ids
        }

    def supervise(self):
        self.supervised = True
        return {}


class _FakeBootstrap:
    def __init__(self, *, token: str = "t", accepts: bool = True, raises=None) -> None:
        self._token = token
        self._accepts = accepts
        self._raises = raises
        self.cooldown = None

    def get_token(self):
        if self._raises is not None:
            raise self._raises
        return self._token, TokenOutcome("cache", "2026-08-21T09:00:00", 0, False)

    def refresh(self, current_token=None):
        return "fresh", TokenOutcome("generated", "2026-08-21T09:00:00", 1, False)

    def validate(self, token: str) -> bool:
        return self._accepts


_REPORT = PaperSafetyReport(
    plans=(
        RuntimePlan("intraday_options", ("c921_ema_cross_buy",)),
        RuntimePlan("positional_options", ("weekly_delta_neutral",)),
    )
)


def _cfg(**overrides) -> AutoStartConfig:
    base = {"enabled": True, "require_system_timezone_match": False}
    base.update(overrides)
    return AutoStartConfig(**base)


def _controller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    cfg: AutoStartConfig | None = None,
    clock: _Clock | None = None,
    event: threading.Event | None = None,
    notifier=None,
    bootstrap=None,
    launcher=None,
    report: PaperSafetyReport | Exception = _REPORT,
    probe=None,
) -> tuple[ctl.AutoStartController, _FakeLauncher, _RecordingNotifier]:
    cfg = cfg or _cfg()
    clock = clock or _Clock()
    event = event if event is not None else threading.Event()
    notifier = notifier or _RecordingNotifier()
    launcher = launcher or _FakeLauncher()

    def _verify(config_root, **kwargs):
        if isinstance(report, Exception):
            raise report
        return report

    monkeypatch.setattr(ctl, "verify_paper_only", _verify)

    controller = ctl.AutoStartController(
        cfg=cfg,
        config_root=tmp_path / "config",
        paths=ProjectPaths(project_root=tmp_path),
        session=build_session(cfg),
        clock=clock,
        waiter=DeadlineWaiter(
            interval_seconds=30.0,
            max_interval_seconds=300.0,
            multiplier=2.0,
            clock=clock,
            stop_event=event,
        ),
        stop_event=event,
        notifier=notifier,
        claim=DailyNotificationClaim(tmp_path / "notifications.json"),
        bootstrap_factory=lambda: bootstrap or _FakeBootstrap(),
        launcher_factory=lambda: launcher,
        project_probe=probe or (lambda: None),
        check_system_timezone=False,
    )
    return controller, launcher, notifier


# ---------------------------------------------------- the ordering guarantee
def test_a_successful_chain_notifies_then_starts_both_runtimes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    controller, launcher, _ = _controller(tmp_path, monkeypatch)
    outcome = controller.run()

    assert outcome.exit_code == ctl.EXIT_OK
    assert outcome.notified
    assert launcher.launched == ["intraday_options", "positional_options"]
    assert launcher.supervised, "the controller must stay with its children"


def test_no_runtime_starts_when_dhan_rejects_the_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The ordering guarantee, asserted from the failing side."""
    controller, launcher, notifier = _controller(
        tmp_path, monkeypatch, bootstrap=_FakeBootstrap(accepts=False)
    )
    outcome = controller.run()

    assert outcome.exit_code == ctl.EXIT_TERMINAL_REFUSAL
    assert launcher.launched == []
    assert not any(e.event_type == "auto_start_auth_success" for e in notifier.events)


def test_the_success_notification_is_sent_before_any_runtime_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    order: list[str] = []

    class _OrderedNotifier(_RecordingNotifier):
        def send(self, event):
            order.append("notify")
            return super().send(event)

    class _OrderedLauncher(_FakeLauncher):
        def launch(self, runtime_ids):
            order.append("launch")
            return super().launch(runtime_ids)

    controller, _, _ = _controller(
        tmp_path, monkeypatch, notifier=_OrderedNotifier(), launcher=_OrderedLauncher()
    )
    controller.run()
    assert order == ["notify", "launch"]


# ---------------------------------------------------------- calendar and window
def test_a_weekend_exits_cleanly_without_touching_the_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    def _must_not_run() -> None:
        raise AssertionError("nothing may touch the project or network on a weekend")

    controller, launcher, notifier = _controller(
        tmp_path,
        monkeypatch,
        clock=_Clock(SATURDAY),
        bootstrap=_ExplodingBootstrap(),
        probe=_must_not_run,
    )
    outcome = controller.run()

    assert outcome.exit_code == ctl.EXIT_OK
    assert launcher.launched == []
    assert notifier.events == []


def test_a_configured_holiday_exits_cleanly_without_touching_the_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    def _must_not_run() -> None:
        raise AssertionError("nothing may touch the project or network on a holiday")

    controller, _, notifier = _controller(
        tmp_path,
        monkeypatch,
        cfg=_cfg(holidays=("2026-08-20",)),
        bootstrap=_ExplodingBootstrap(),
        probe=_must_not_run,
    )
    outcome = controller.run()

    assert outcome.exit_code == ctl.EXIT_OK
    assert notifier.events == []


def test_an_early_login_starts_nothing_and_alerts_nobody(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    controller, launcher, notifier = _controller(
        tmp_path, monkeypatch, clock=_Clock(datetime(2026, 8, 20, 8, 59, tzinfo=IST))
    )
    outcome = controller.run()

    assert outcome.exit_code == ctl.EXIT_OK
    assert launcher.launched == []
    assert notifier.events == [], "an early login is not an incident"


def test_a_late_login_at_1100_still_starts_the_runtimes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A positional runtime with an open cycle must be recoverable at 11:00."""
    controller, launcher, _ = _controller(
        tmp_path, monkeypatch, clock=_Clock(datetime(2026, 8, 20, 11, 0, tzinfo=IST))
    )
    outcome = controller.run()

    assert outcome.exit_code == ctl.EXIT_OK
    assert "positional_options" in launcher.launched


class _ExplodingBootstrap:
    def get_token(self):
        raise AssertionError("authentication must not be reached")

    def validate(self, token):
        raise AssertionError("validation must not be reached")


# ----------------------------------------------------------------------- retry
def test_a_transient_failure_recovers_and_then_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    clock = _Clock()
    attempts = {"n": 0}

    def probe() -> None:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionError("no network yet")

    controller, launcher, _ = _controller(
        tmp_path, monkeypatch, clock=clock, event=_AdvancingEvent(clock), probe=probe
    )
    outcome = controller.run()

    assert outcome.exit_code == ctl.EXIT_OK
    assert attempts["n"] == 3
    assert launcher.launched


def test_a_volume_that_mounts_after_five_minutes_still_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A fixed five-minute cap would have lost the whole day here."""
    clock = _Clock()
    mounted_at = START + timedelta(minutes=6)

    def probe() -> None:
        if clock() < mounted_at:
            raise ProjectUnavailableError("/Volumes/Trading not mounted yet")

    controller, launcher, _ = _controller(
        tmp_path, monkeypatch, clock=clock, event=_AdvancingEvent(clock), probe=probe
    )
    outcome = controller.run()

    assert outcome.exit_code == ctl.EXIT_OK
    assert clock() >= mounted_at
    assert launcher.launched


def test_a_volume_that_never_mounts_gives_up_at_the_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    clock = _Clock()

    def probe() -> None:
        raise ProjectUnavailableError("/Volumes/Trading never mounted")

    controller, launcher, _ = _controller(
        tmp_path, monkeypatch, clock=clock, event=_AdvancingEvent(clock), probe=probe
    )
    outcome = controller.run()

    assert outcome.exit_code == ctl.EXIT_DEADLINE_EXPIRED
    assert launcher.launched == []
    assert clock() >= datetime(2026, 8, 20, 15, 15, tzinfo=IST)


def test_a_transient_auth_endpoint_failure_is_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    clock = _Clock()
    calls = {"n": 0}

    class _Flaky(_FakeBootstrap):
        def get_token(self):
            calls["n"] += 1
            if calls["n"] < 2:
                raise TokenGenerationError("dhan 503")
            return "t", TokenOutcome("generated", None, 1, False)

    controller, _, _ = _controller(
        tmp_path,
        monkeypatch,
        clock=clock,
        event=_AdvancingEvent(clock),
        bootstrap=_Flaky(),
    )
    outcome = controller.run()

    assert outcome.exit_code == ctl.EXIT_OK
    assert calls["n"] == 2


def test_invalid_credentials_stop_immediately_without_a_retry_storm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    clock = _Clock()
    calls = {"n": 0}

    class _Bad(_FakeBootstrap):
        def get_token(self):
            calls["n"] += 1
            raise InvalidCredentialsError("wrong PIN")

    controller, launcher, _ = _controller(
        tmp_path, monkeypatch, clock=clock, event=_AdvancingEvent(clock), bootstrap=_Bad()
    )
    outcome = controller.run()

    assert outcome.exit_code == ctl.EXIT_TERMINAL_REFUSAL
    assert calls["n"] == 1, "a wrong PIN must cost exactly one attempt"
    assert launcher.launched == []


def test_a_rate_limited_start_recovers_after_its_cooldown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    clock = _Clock()
    calls = {"n": 0}
    ready_at = START + timedelta(seconds=600)

    class _Limited(_FakeBootstrap):
        def get_token(self):
            calls["n"] += 1
            if clock() < ready_at:
                raise TokenRateLimitedError("too many requests")
            return "t", TokenOutcome("generated", None, 1, False)

    monkeypatch.setattr(ctl, "cooldown_ready_at", lambda bootstrap, now: ready_at)

    controller, launcher, _ = _controller(
        tmp_path, monkeypatch, clock=clock, event=_AdvancingEvent(clock), bootstrap=_Limited()
    )
    outcome = controller.run()

    assert outcome.exit_code == ctl.EXIT_OK
    assert clock() >= ready_at
    assert launcher.launched


def test_sigterm_during_a_retry_stops_the_whole_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    clock = _Clock()
    event = _AdvancingEvent(clock)
    calls = {"n": 0}

    def probe() -> None:
        calls["n"] += 1
        event.set()  # a SIGTERM arrives mid-retry
        raise ConnectionError("no network")

    controller, launcher, _ = _controller(
        tmp_path, monkeypatch, clock=clock, event=event, probe=probe
    )
    outcome = controller.run()

    assert outcome.exit_code == ctl.EXIT_OK
    assert "shutdown requested" in outcome.reason
    assert launcher.launched == []


# ---------------------------------------------------------------- notifications
def test_exactly_one_success_notification_across_duplicate_triggers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """RunAtLoad and the 09:00 calendar trigger both firing."""
    notifier = _RecordingNotifier()
    claim_path = tmp_path / "notifications.json"

    for _ in range(2):
        controller, _, _ = _controller(tmp_path, monkeypatch, notifier=notifier)
        controller._claim = DailyNotificationClaim(claim_path)
        controller.run()

    successes = [e for e in notifier.events if e.event_type == "auto_start_auth_success"]
    assert len(successes) == 1


def test_a_retry_recovery_still_sends_only_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    clock = _Clock()
    attempts = {"n": 0}

    def probe() -> None:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionError("flaky")

    controller, _, notifier = _controller(
        tmp_path, monkeypatch, clock=clock, event=_AdvancingEvent(clock), probe=probe
    )
    controller.run()

    successes = [e for e in notifier.events if e.event_type == "auto_start_auth_success"]
    assert len(successes) == 1


def test_a_failed_telegram_delivery_does_not_block_paper_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    controller, launcher, _ = _controller(
        tmp_path, monkeypatch, notifier=_RecordingNotifier(succeeds=False)
    )
    outcome = controller.run()

    assert outcome.exit_code == ctl.EXIT_OK
    assert not outcome.notified
    assert launcher.launched, "an unreachable Telegram must not stop a safe paper start"


def test_a_failed_delivery_is_not_recorded_as_delivered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    claim_path = tmp_path / "notifications.json"
    controller, _, _ = _controller(
        tmp_path, monkeypatch, notifier=_RecordingNotifier(succeeds=False)
    )
    controller._claim = DailyNotificationClaim(claim_path)
    controller.run()

    assert not DailyNotificationClaim(claim_path).already_delivered(
        day=START.date(), kind=KIND_AUTH_SUCCESS
    )


def test_a_raising_notifier_does_not_stop_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    controller, launcher, _ = _controller(
        tmp_path, monkeypatch, notifier=_RecordingNotifier(raises=True)
    )
    outcome = controller.run()
    assert outcome.exit_code == ctl.EXIT_OK
    assert launcher.launched


def test_delivery_is_retried_a_bounded_number_of_times(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    clock = _Clock()
    notifier = _RecordingNotifier(succeeds=False)
    controller, _, _ = _controller(
        tmp_path,
        monkeypatch,
        cfg=_cfg(telegram_retry_attempts=3),
        clock=clock,
        event=_AdvancingEvent(clock),
        notifier=notifier,
    )
    controller.run()

    successes = [e for e in notifier.events if e.event_type == "auto_start_auth_success"]
    assert len(successes) == 3, "bounded, not unbounded"


def test_the_success_message_contains_no_credential_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    secrets = {
        "token": "SUPERSECRETTOKEN",
        "pin": "1234",
        "totp": "987654",
        "bot": "123456:AAbotTOKEN",
        "client": "1100112233",
    }
    controller, _, notifier = _controller(
        tmp_path, monkeypatch, bootstrap=_FakeBootstrap(token=secrets["token"])
    )
    controller.run()

    body = "\n".join(e.message + e.rendered() for e in notifier.events)
    for label, value in secrets.items():
        assert value not in body, f"the {label} leaked into the notification"


def test_the_success_message_states_the_paper_posture_and_what_will_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    controller, _, notifier = _controller(tmp_path, monkeypatch)
    controller.run()

    message = notifier.events[0].message
    assert "Dhan authentication successful" in message
    assert "PAPER ONLY" in message
    assert "Token source: cache" in message
    assert "intraday_options" in message and "positional_options" in message
    assert "c921_ema_cross_buy" in message and "weekly_delta_neutral" in message


def test_the_give_up_alert_is_sent_once_not_per_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    clock = _Clock()

    def probe() -> None:
        raise ProjectUnavailableError("never mounts")

    controller, _, notifier = _controller(
        tmp_path, monkeypatch, clock=clock, event=_AdvancingEvent(clock), probe=probe
    )
    outcome = controller.run()

    give_ups = [e for e in notifier.events if e.event_type == "auto_start_gave_up"]
    assert outcome.exit_code == ctl.EXIT_DEADLINE_EXPIRED
    assert len(give_ups) == 1, "one alert for the day, not one per attempt"


def test_a_second_give_up_the_same_day_does_not_alert_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    claim_path = tmp_path / "notifications.json"
    notifier = _RecordingNotifier()

    for _ in range(2):
        controller, _, _ = _controller(
            tmp_path,
            monkeypatch,
            notifier=notifier,
            bootstrap=_FakeBootstrap(accepts=False),
        )
        controller._claim = DailyNotificationClaim(claim_path)
        controller.run()

    give_ups = [e for e in notifier.events if e.event_type == "auto_start_gave_up"]
    assert len(give_ups) == 1


# ------------------------------------------------------------------- safety
def test_a_paper_safety_violation_blocks_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    unsafe = PaperSafetyReport(
        plans=(RuntimePlan("intraday_options", ()),),
        violations=("global.live_trading_enabled is true",),
    )
    controller, launcher, _ = _controller(tmp_path, monkeypatch, report=unsafe)
    outcome = controller.run()

    assert outcome.exit_code == ctl.EXIT_TERMINAL_REFUSAL
    assert launcher.launched == []


def test_no_enabled_runtime_is_a_clean_no_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    controller, launcher, notifier = _controller(
        tmp_path, monkeypatch, report=PaperSafetyReport(plans=())
    )
    outcome = controller.run()

    assert outcome.exit_code == ctl.EXIT_OK
    assert launcher.launched == []
    assert notifier.events == [], "nothing enabled is not an incident"


def test_one_runtime_failing_to_start_still_leaves_the_other_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    launcher = _FakeLauncher(
        started={"intraday_options": False, "positional_options": True}
    )
    controller, _, _ = _controller(tmp_path, monkeypatch, launcher=launcher)
    outcome = controller.run()

    assert outcome.exit_code == ctl.EXIT_OK
    assert "positional_options" in outcome.reason
    assert launcher.supervised
