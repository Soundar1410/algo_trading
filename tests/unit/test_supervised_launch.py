"""orchestration.process_control.supervised_launch: bounded-restart classification.

Most tests here stub both `validate_environment.main` and
`supervisor_main.main` — the retry/classification logic is what is under
test, not the real preflight or the real supervisor, both already covered by
their own suites (`test_scripts_are_read_only.py`,
`tests/end_to_end/test_supervisor.py`). One test (the last) leaves
`_record_attempt` real and checks the actual `errors` rows it writes, so the
audit-visibility claim in the module docstring is proven, not just assumed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from common.config import load_paths
from common.persistence import connect_readonly
from orchestration.process_control import supervised_launch as sl


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test should actually wait out a backoff."""
    monkeypatch.setattr(sl.time, "sleep", lambda seconds: None)


@pytest.fixture(autouse=True)
def _no_real_audit_writes(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Stub the one write this module performs, except in the dedicated test
    that checks it for real (opted out via a marker on that test)."""
    if "real_audit" in request.keywords:
        return
    monkeypatch.setattr(sl, "_record_attempt", lambda **kwargs: None)


def _stub_preflight(monkeypatch: pytest.MonkeyPatch, *, passes: bool) -> None:
    ok, problems = sl.validate_environment.EXIT_OK, sl.validate_environment.EXIT_PROBLEMS
    monkeypatch.setattr(sl.validate_environment, "main", lambda argv: ok if passes else problems)


class _CountingSupervisor:
    """Returns each code in `codes` in turn, then repeats the last forever.

    An entry may also be a `BaseException` instance, in which case it is
    raised instead of returned — used to exercise both the caught-and-retried
    path (a plain `Exception`) and the deliberately-never-caught path (a
    `SystemExit`), without a real bug inside the supervisor. Checked as
    `BaseException`, not `Exception`, precisely because `SystemExit` is not
    an `Exception` subclass and must still be raised, not returned as if it
    were a bogus exit code.
    """

    def __init__(self, codes: list[int | BaseException]) -> None:
        self._codes = codes
        self.calls = 0

    def __call__(self, argv: list[str]) -> int:
        code = self._codes[min(self.calls, len(self._codes) - 1)]
        self.calls += 1
        if isinstance(code, BaseException):
            raise code
        return code


def test_a_failed_preflight_short_circuits_before_any_supervisor_run(
    monkeypatch: pytest.MonkeyPatch,
):
    _stub_preflight(monkeypatch, passes=False)

    def _must_not_be_called(argv: list[str]) -> int:
        raise AssertionError("supervisor_main.main must not run after a failed preflight")

    monkeypatch.setattr(sl.supervisor_main, "main", _must_not_be_called)

    result = sl.run(
        runtime_id="intraday_options",
        config_root=Path("config"),
        max_attempts=3,
        backoff_seconds=0.0,
    )

    assert result == sl.EXIT_PREFLIGHT_FAILED


@pytest.mark.parametrize(
    "terminal_code",
    [
        pytest.param(0, id="EXIT_OK"),
        pytest.param(3, id="EXIT_RUNTIME_DISABLED"),
        pytest.param(4, id="EXIT_STRATEGY_NOT_FOUND"),
        pytest.param(5, id="EXIT_LEGACY_SYSTEM_ACTIVE"),
        pytest.param(6, id="EXIT_SAFETY_SHUTDOWN"),
    ],
)
def test_a_terminal_exit_code_stops_after_exactly_one_attempt(
    monkeypatch: pytest.MonkeyPatch, terminal_code: int
):
    _stub_preflight(monkeypatch, passes=True)
    supervisor = _CountingSupervisor([terminal_code])
    monkeypatch.setattr(sl.supervisor_main, "main", supervisor)

    result = sl.run(
        runtime_id="intraday_options",
        config_root=Path("config"),
        max_attempts=3,
        backoff_seconds=0.0,
    )

    assert result == terminal_code
    assert supervisor.calls == 1


def test_a_retryable_code_that_never_recovers_gives_up_after_max_attempts(
    monkeypatch: pytest.MonkeyPatch,
):
    _stub_preflight(monkeypatch, passes=True)
    supervisor = _CountingSupervisor([sl.supervisor_main.EXIT_FAILED])
    monkeypatch.setattr(sl.supervisor_main, "main", supervisor)

    result = sl.run(
        runtime_id="intraday_options",
        config_root=Path("config"),
        max_attempts=3,
        backoff_seconds=0.0,
    )

    assert result == sl.EXIT_GAVE_UP
    assert supervisor.calls == 3


def test_a_retryable_code_that_recovers_stops_retrying_immediately(
    monkeypatch: pytest.MonkeyPatch,
):
    _stub_preflight(monkeypatch, passes=True)
    supervisor = _CountingSupervisor(
        [sl.supervisor_main.EXIT_NO_CREDENTIALS, sl.supervisor_main.EXIT_OK]
    )
    monkeypatch.setattr(sl.supervisor_main, "main", supervisor)

    result = sl.run(
        runtime_id="intraday_options",
        config_root=Path("config"),
        max_attempts=5,
        backoff_seconds=0.0,
    )

    assert result == sl.supervisor_main.EXIT_OK
    assert supervisor.calls == 2


def test_an_unexpected_exception_is_retried_like_a_transient_failure(
    monkeypatch: pytest.MonkeyPatch,
):
    """The fail-first case: run against unmodified `supervised_launch.py` and
    this raises `ValueError` straight out of `sl.run(...)` uncaught — no
    retry, no `errors` row, the whole bounded-restart mechanism bypassed.
    With the fix, an unexpected exception is treated exactly like a
    retryable exit code."""
    _stub_preflight(monkeypatch, passes=True)
    supervisor = _CountingSupervisor([ValueError("transient bug"), sl.supervisor_main.EXIT_OK])
    monkeypatch.setattr(sl.supervisor_main, "main", supervisor)

    result = sl.run(
        runtime_id="intraday_options",
        config_root=Path("config"),
        max_attempts=3,
        backoff_seconds=0.0,
    )

    assert result == sl.supervisor_main.EXIT_OK
    assert supervisor.calls == 2


def test_an_exception_on_every_attempt_gives_up_after_max_attempts(
    monkeypatch: pytest.MonkeyPatch,
):
    _stub_preflight(monkeypatch, passes=True)
    supervisor = _CountingSupervisor([RuntimeError("always broken")])
    monkeypatch.setattr(sl.supervisor_main, "main", supervisor)

    result = sl.run(
        runtime_id="intraday_options",
        config_root=Path("config"),
        max_attempts=3,
        backoff_seconds=0.0,
    )

    assert result == sl.EXIT_GAVE_UP
    assert supervisor.calls == 3


def test_a_system_exit_is_never_caught_or_retried(monkeypatch: pytest.MonkeyPatch):
    """`except Exception`, never `except BaseException` — a deliberate
    `SystemExit` (or `KeyboardInterrupt`) must propagate untouched, not be
    miscategorized as a transient failure worth retrying."""
    _stub_preflight(monkeypatch, passes=True)
    supervisor = _CountingSupervisor([SystemExit(2)])
    monkeypatch.setattr(sl.supervisor_main, "main", supervisor)

    with pytest.raises(SystemExit):
        sl.run(
            runtime_id="intraday_options",
            config_root=Path("config"),
            max_attempts=3,
            backoff_seconds=0.0,
        )

    assert supervisor.calls == 1


@pytest.mark.real_audit
def test_an_exception_attempt_is_recorded_with_its_type_and_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    _stub_preflight(monkeypatch, passes=True)
    supervisor = _CountingSupervisor([ValueError("boom, distinctly")])
    monkeypatch.setattr(sl.supervisor_main, "main", supervisor)

    result = sl.run(
        runtime_id="intraday_options",
        config_root=Path("config"),
        max_attempts=1,
        backoff_seconds=0.0,
    )
    assert result == sl.EXIT_GAVE_UP

    paths = load_paths(tmp_path)
    conn = connect_readonly(paths.database_path("intraday_options"))
    try:
        rows = conn.execute(
            "SELECT severity, message FROM errors "
            "WHERE component = 'supervised_launch' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    assert [row[0] for row in rows] == ["WARNING", "ERROR"]
    assert all("exception=ValueError: boom, distinctly" in row[1] for row in rows)
    assert "attempt 1/1" in rows[0][1]


def test_a_single_max_attempt_never_sleeps(monkeypatch: pytest.MonkeyPatch):
    _stub_preflight(monkeypatch, passes=True)
    supervisor = _CountingSupervisor([sl.supervisor_main.EXIT_FAILED])
    monkeypatch.setattr(sl.supervisor_main, "main", supervisor)

    def _must_not_sleep(seconds: float) -> None:
        raise AssertionError("must not sleep when there is no further attempt")

    monkeypatch.setattr(sl.time, "sleep", _must_not_sleep)

    result = sl.run(
        runtime_id="intraday_options",
        config_root=Path("config"),
        max_attempts=1,
        backoff_seconds=30.0,
    )

    assert result == sl.EXIT_GAVE_UP
    assert supervisor.calls == 1


# ============================================================ real audit rows
@pytest.mark.real_audit
def test_errors_rows_are_written_for_each_attempt_and_the_final_give_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    _stub_preflight(monkeypatch, passes=True)
    supervisor = _CountingSupervisor(
        [sl.supervisor_main.EXIT_FAILED, sl.supervisor_main.EXIT_FAILED]
    )
    monkeypatch.setattr(sl.supervisor_main, "main", supervisor)

    result = sl.run(
        runtime_id="intraday_options",
        config_root=Path("config"),
        max_attempts=2,
        backoff_seconds=0.0,
    )
    assert result == sl.EXIT_GAVE_UP

    paths = load_paths(tmp_path)
    conn = connect_readonly(paths.database_path("intraday_options"))
    try:
        rows = conn.execute(
            "SELECT severity, component, message FROM errors "
            "WHERE component = 'supervised_launch' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    # One WARNING per retryable attempt (both, here), plus one final ERROR
    # recording the give-up itself.
    assert [row[0] for row in rows] == ["WARNING", "WARNING", "ERROR"]
    assert all(row[1] == "supervised_launch" for row in rows)
    assert "attempt 1/2" in rows[0][2]
    assert "attempt 2/2" in rows[1][2]
    assert "attempt 2/2" in rows[2][2]
