"""orchestration.auto_start.runtime_launcher: owning the runtime children.

No real process is created anywhere in this file. ``_FakeProcess`` implements
the ``ProcessHandle`` protocol the launcher actually depends on, and ``_World``
models the one piece of shared state that decides everything else: which
runtimes currently hold their supervisor lock. Together they let the whole
ownership lifecycle — handshake, supervision, signal propagation, escalation to
SIGKILL — be asserted deterministically.
"""

from __future__ import annotations

import signal
import threading
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from common.config import ProjectPaths
from orchestration.auto_start import runtime_launcher as rl

IST = ZoneInfo("Asia/Kolkata")
START = datetime(2026, 8, 20, 9, 0, tzinfo=IST)


class _Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class _FakeProcess:
    """A child we fully control: when it dies, and how it answers signals."""

    _next_pid = 1000

    def __init__(self, *, exit_after_polls: int | None = None, ignores_sigterm: bool = False):
        _FakeProcess._next_pid += 1
        self._pid = _FakeProcess._next_pid
        self._exit_after_polls = exit_after_polls
        self._ignores_sigterm = ignores_sigterm
        self._polls = 0
        self._exit_code: int | None = None
        self.signals: list[int] = []
        self.killed = False

    @property
    def pid(self) -> int:
        return self._pid

    def poll(self) -> int | None:
        self._polls += 1
        if (
            self._exit_code is None
            and self._exit_after_polls is not None
            and self._polls >= self._exit_after_polls
        ):
            self._exit_code = 0
        return self._exit_code

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)
        if not self._ignores_sigterm:
            self._exit_code = 0

    def kill(self) -> None:
        self.killed = True
        self._exit_code = -9


class _World:
    """Which runtimes hold their supervisor lock, and what spawning does.

    ``takes_lock`` is the set of runtimes whose child successfully hands
    shake — i.e. acquires the lock the moment it is spawned. Everything else
    spawns and never appears, which is what a runtime dying on startup looks
    like from out here.
    """

    def __init__(
        self,
        *,
        already_locked: set[str] | None = None,
        takes_lock: set[str] | None = None,
        processes: dict[str, _FakeProcess] | None = None,
        spawn_error: dict[str, Exception] | None = None,
    ) -> None:
        self.locked: set[str] = set(already_locked or set())
        self.takes_lock: set[str] = set(takes_lock if takes_lock is not None else set())
        self.processes: dict[str, _FakeProcess] = dict(processes or {})
        self.spawn_error = dict(spawn_error or {})
        self.spawned: list[list[str]] = []

    def spawn(self, command):
        command = list(command)
        runtime_id = command[command.index("--runtime-id") + 1]
        if runtime_id in self.spawn_error:
            raise self.spawn_error[runtime_id]
        self.spawned.append(command)
        process = self.processes.setdefault(runtime_id, _FakeProcess())
        if runtime_id in self.takes_lock:
            self.locked.add(runtime_id)
        return process

    def is_running(self, runtime_id: str) -> bool:
        return runtime_id in self.locked


class _AdvancingEvent(threading.Event):
    """Waiting advances the fake clock rather than sleeping."""

    def __init__(self, clock: _Clock) -> None:
        super().__init__()
        self._clock = clock

    def wait(self, timeout: float | None = None) -> bool:  # type: ignore[override]
        self._clock.advance(float(timeout or 0))
        return self.is_set()


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rl, "_sleep", lambda seconds: None)


def _build(
    tmp_path: Path,
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
    *,
    clock: _Clock | None = None,
    event: threading.Event | None = None,
    **kwargs,
) -> rl.RuntimeLauncher:
    clock = clock or _Clock()
    event = event if event is not None else threading.Event()
    params = {
        "python_bin": Path("/nonexistent/python"),
        "paths": ProjectPaths(project_root=tmp_path),
        "config_root": tmp_path / "config",
        "clock": clock,
        "stop_event": event,
        "handshake_seconds": 60.0,
        "shutdown_grace_seconds": 30.0,
        "spawn": world.spawn,
        "poll_interval_seconds": 1.0,
    }
    params.update(kwargs)
    launcher = rl.RuntimeLauncher(**params)
    monkeypatch.setattr(launcher, "is_running", world.is_running)
    return launcher


# ------------------------------------------------------------------ handshake
def test_a_runtime_that_takes_its_lock_is_reported_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    world = _World(takes_lock={"intraday_options"})
    launcher = _build(tmp_path, world, monkeypatch)

    results = launcher.launch(["intraday_options"])

    assert results["intraday_options"].started
    assert results["intraday_options"].detail == "supervisor lock acquired"
    assert not results["intraday_options"].already_running


def test_popen_returning_is_not_treated_as_a_successful_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A child that dies before taking its lock must be reported as failed —
    the spawn succeeding says nothing about whether the runtime started."""
    world = _World(processes={"intraday_options": _FakeProcess(exit_after_polls=1)})
    launcher = _build(tmp_path, world, monkeypatch)

    results = launcher.launch(["intraday_options"])

    assert not results["intraday_options"].started
    assert "before taking its lock" in results["intraday_options"].detail
    assert results["intraday_options"].exit_code == 0


def test_a_handshake_timeout_stops_the_unverified_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    clock = _Clock()
    world = _World()  # nothing ever takes the lock
    launcher = _build(
        tmp_path,
        world,
        monkeypatch,
        clock=clock,
        event=_AdvancingEvent(clock),
        handshake_seconds=5.0,
    )

    results = launcher.launch(["intraday_options"])

    assert not results["intraday_options"].started
    assert "did not take its supervisor lock" in results["intraday_options"].detail
    process = world.processes["intraday_options"]
    assert signal.SIGTERM in process.signals, "an unverified child must not be left running"


# ---------------------------------------------------------------- idempotency
def test_an_already_running_runtime_is_not_duplicated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    world = _World(already_locked={"intraday_options"})
    launcher = _build(tmp_path, world, monkeypatch)

    results = launcher.launch(["intraday_options"])

    assert results["intraday_options"].started
    assert results["intraday_options"].already_running
    assert world.spawned == [], "a live verified owner means no second worker"


def test_a_duplicate_trigger_cannot_produce_two_supervisors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """RunAtLoad and the 09:00 calendar trigger arriving together."""
    world = _World(takes_lock={"intraday_options"})
    launcher = _build(tmp_path, world, monkeypatch)

    launcher.launch(["intraday_options"])
    launcher.launch(["intraday_options"])

    assert len(world.spawned) == 1


# ------------------------------------------------------------------ isolation
def test_one_runtime_failing_does_not_prevent_the_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    world = _World(
        takes_lock={"positional_options"},
        processes={"intraday_options": _FakeProcess(exit_after_polls=1)},
    )
    launcher = _build(tmp_path, world, monkeypatch)

    results = launcher.launch(["intraday_options", "positional_options"])

    assert not results["intraday_options"].started
    assert results["positional_options"].started


def test_a_spawn_that_raises_is_isolated_to_its_own_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    world = _World(
        takes_lock={"positional_options"},
        spawn_error={"intraday_options": OSError("fork failed")},
    )
    launcher = _build(tmp_path, world, monkeypatch)

    results = launcher.launch(["intraday_options", "positional_options"])

    assert not results["intraday_options"].started
    assert "OSError" in results["intraday_options"].detail
    assert results["positional_options"].started


def test_both_real_runtimes_start_through_supervised_launch_not_intraday(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    world = _World(takes_lock={"intraday_options", "positional_options"})
    launcher = _build(tmp_path, world, monkeypatch)

    launcher.launch(["intraday_options", "positional_options"])

    assert len(world.spawned) == 2
    launched_ids = {cmd[cmd.index("--runtime-id") + 1] for cmd in world.spawned}
    assert launched_ids == {"intraday_options", "positional_options"}
    for command in world.spawned:
        assert "orchestration.process_control.supervised_launch" in command
        assert "runtimes.intraday_options" not in command


# ----------------------------------------------------------------- supervision
def test_supervise_blocks_until_every_child_has_exited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """This is what keeps caffeinate — and launchd's view of the job — alive."""
    clock = _Clock()
    world = _World(
        takes_lock={"intraday_options"},
        processes={"intraday_options": _FakeProcess(exit_after_polls=6)},
    )
    launcher = _build(
        tmp_path, world, monkeypatch, clock=clock, event=_AdvancingEvent(clock)
    )

    launcher.launch(["intraday_options"])
    results = launcher.supervise()

    assert results["intraday_options"].exit_code == 0
    assert clock.now > START, "supervision must actually have spanned time"


def test_a_normal_child_exit_never_triggers_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    clock = _Clock()
    world = _World(
        takes_lock={"intraday_options"},
        processes={"intraday_options": _FakeProcess(exit_after_polls=3)},
    )
    launcher = _build(
        tmp_path, world, monkeypatch, clock=clock, event=_AdvancingEvent(clock)
    )

    launcher.launch(["intraday_options"])
    launcher.supervise()

    assert len(world.spawned) == 1, "a deliberate shutdown must stay shut down"


def test_one_child_exiting_leaves_its_healthy_sibling_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    clock = _Clock()
    dying = _FakeProcess(exit_after_polls=3)
    healthy = _FakeProcess(exit_after_polls=40)
    world = _World(
        takes_lock={"intraday_options", "positional_options"},
        processes={"intraday_options": dying, "positional_options": healthy},
    )
    launcher = _build(
        tmp_path, world, monkeypatch, clock=clock, event=_AdvancingEvent(clock)
    )

    launcher.launch(["intraday_options", "positional_options"])
    results = launcher.supervise()

    assert results["intraday_options"].exit_code == 0
    assert results["positional_options"].exit_code == 0
    # The sibling was never signalled: it finished on its own schedule.
    assert healthy.signals == [], "one runtime exiting must not stop the other"
    assert not healthy.killed


# ------------------------------------------------------------- signal handling
def test_sigterm_is_propagated_to_every_owned_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    world = _World(takes_lock={"intraday_options", "positional_options"})
    launcher = _build(tmp_path, world, monkeypatch)
    launcher.launch(["intraday_options", "positional_options"])

    launcher.shutdown()

    for runtime_id in ("intraday_options", "positional_options"):
        assert world.processes[runtime_id].signals == [signal.SIGTERM]


def test_shutdown_is_bounded_and_escalates_to_sigkill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    clock = _Clock()
    stubborn = _FakeProcess(ignores_sigterm=True)
    world = _World(
        takes_lock={"intraday_options"}, processes={"intraday_options": stubborn}
    )
    launcher = _build(
        tmp_path, world, monkeypatch, clock=clock, shutdown_grace_seconds=5.0
    )
    launcher.launch(["intraday_options"])

    # The bounded wait must terminate on its own; let the clock run past it.
    monkeypatch.setattr(rl, "_sleep", lambda seconds: clock.advance(1.0))
    launcher.shutdown()

    assert signal.SIGTERM in stubborn.signals
    assert stubborn.killed, "the grace period must be bounded, not indefinite"


def test_supervise_shuts_children_down_when_the_stop_event_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    event = threading.Event()
    world = _World(takes_lock={"intraday_options"})
    launcher = _build(tmp_path, world, monkeypatch, event=event)
    launcher.launch(["intraday_options"])

    event.set()
    launcher.supervise()

    assert world.processes["intraday_options"].signals == [signal.SIGTERM]


def test_a_shutdown_during_the_handshake_is_reported_not_claimed_as_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    event = threading.Event()
    event.set()
    world = _World()  # never takes the lock
    launcher = _build(tmp_path, world, monkeypatch, event=event)

    results = launcher.launch(["intraday_options"])

    assert not results["intraday_options"].started
    assert "shutdown requested" in results["intraday_options"].detail
