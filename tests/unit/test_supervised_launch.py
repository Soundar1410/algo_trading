"""orchestration.process_control.supervised_launch: bounded-restart classification.

Most tests here stub both `validate_environment.main` and the runtime's own
`main` — the retry/classification logic is what is under test, not the real
preflight or the real supervisor, both already covered by their own suites
(`test_scripts_are_read_only.py`, `tests/end_to_end/test_supervisor.py`). One
test leaves `_record_attempt` real and checks the actual `errors` rows it
writes, so the audit-visibility claim in the module docstring is proven, not
just assumed.

The runtime is resolved through `scripts._runtimes`, so a stub is installed by
replacing that runtime's registry entry rather than by patching an imported
module — which is the whole point of the change these tests cover: this module
no longer treats `intraday_options` as the universal entrypoint *or* as the
universal exit-code vocabulary.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from common.config import load_paths
from common.persistence import connect_readonly
from orchestration.process_control import supervised_launch as sl
from scripts import _runtimes
from scripts._runtimes import RuntimeEntrypoint

# The intraday exit codes these tests speak in, named once.
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_NO_CREDENTIALS = 2
EXIT_RUNTIME_DISABLED = 3
EXIT_STRATEGY_NOT_FOUND = 4
EXIT_LEGACY_SYSTEM_ACTIVE = 5
EXIT_SAFETY_SHUTDOWN = 6


def _install(monkeypatch, runtime_id, supervisor):
    """Point one registry entry at `supervisor`, keeping its own code table."""
    original = _runtimes.RUNTIMES[runtime_id]
    monkeypatch.setitem(
        _runtimes.RUNTIMES,
        runtime_id,
        RuntimeEntrypoint(
            main=supervisor,
            terminal_exit_codes=original.terminal_exit_codes,
            retryable_exit_codes=original.retryable_exit_codes,
        ),
    )


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

    _install(monkeypatch, "intraday_options", _must_not_be_called)

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
    _install(monkeypatch, "intraday_options", supervisor)

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
    supervisor = _CountingSupervisor([EXIT_FAILED])
    _install(monkeypatch, "intraday_options", supervisor)

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
        [EXIT_NO_CREDENTIALS, EXIT_OK]
    )
    _install(monkeypatch, "intraday_options", supervisor)

    result = sl.run(
        runtime_id="intraday_options",
        config_root=Path("config"),
        max_attempts=5,
        backoff_seconds=0.0,
    )

    assert result == EXIT_OK
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
    supervisor = _CountingSupervisor([ValueError("transient bug"), EXIT_OK])
    _install(monkeypatch, "intraday_options", supervisor)

    result = sl.run(
        runtime_id="intraday_options",
        config_root=Path("config"),
        max_attempts=3,
        backoff_seconds=0.0,
    )

    assert result == EXIT_OK
    assert supervisor.calls == 2


def test_an_exception_on_every_attempt_gives_up_after_max_attempts(
    monkeypatch: pytest.MonkeyPatch,
):
    _stub_preflight(monkeypatch, passes=True)
    supervisor = _CountingSupervisor([RuntimeError("always broken")])
    _install(monkeypatch, "intraday_options", supervisor)

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
    _install(monkeypatch, "intraday_options", supervisor)

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
    _install(monkeypatch, "intraday_options", supervisor)

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
    supervisor = _CountingSupervisor([EXIT_FAILED])
    _install(monkeypatch, "intraday_options", supervisor)

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
        [EXIT_FAILED, EXIT_FAILED]
    )
    _install(monkeypatch, "intraday_options", supervisor)

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


# ==================================================== genuine runtime genericity
def test_the_module_no_longer_imports_intraday_as_the_universal_entrypoint():
    """The regression this whole change exists to close.

    ``--runtime-id positional_options`` used to run *intraday's* composition
    root, admitting positional strategies under the wrong supervisor entirely.
    """
    source = Path(sl.__file__).read_text(encoding="utf-8")
    assert "from runtimes.intraday_options import __main__" not in source
    assert "import runtimes.intraday_options" not in source
    assert not hasattr(sl, "supervisor_main")


def test_each_runtime_runs_its_own_composition_root(monkeypatch: pytest.MonkeyPatch):
    _stub_preflight(monkeypatch, passes=True)
    called: list[str] = []

    for runtime_id in ("intraday_options", "positional_options"):
        def supervisor(argv, _rid=runtime_id):
            called.append(_rid)
            return 0

        _install(monkeypatch, runtime_id, supervisor)

    for runtime_id in ("intraday_options", "positional_options"):
        sl.run(
            runtime_id=runtime_id,
            config_root=Path("config"),
            max_attempts=1,
            backoff_seconds=0.0,
        )

    assert called == ["intraday_options", "positional_options"]


def test_the_registry_carries_both_real_runtimes():
    assert set(_runtimes.RUNTIMES) == {"intraday_options", "positional_options"}


def test_the_two_runtimes_genuinely_disagree_about_their_exit_codes():
    """If they agreed, the bug this fixes would have been invisible."""
    import runtimes.intraday_options.__main__ as intraday
    import runtimes.positional_options.__main__ as positional

    assert intraday.EXIT_RUNTIME_DISABLED != positional.EXIT_RUNTIME_DISABLED
    assert intraday.EXIT_NO_CREDENTIALS != positional.EXIT_NO_CREDENTIALS


def test_positionals_disabled_code_is_terminal_not_retried(monkeypatch: pytest.MonkeyPatch):
    """Read through intraday's table, positional's 10 is unknown and its 11
    would have been misread as EXIT_STRATEGY_NOT_FOUND. A deliberately disabled
    runtime would have been retried until the attempt budget ran out."""
    import runtimes.positional_options.__main__ as positional

    _stub_preflight(monkeypatch, passes=True)
    supervisor = _CountingSupervisor([positional.EXIT_RUNTIME_DISABLED])
    _install(monkeypatch, "positional_options", supervisor)

    result = sl.run(
        runtime_id="positional_options",
        config_root=Path("config"),
        max_attempts=3,
        backoff_seconds=0.0,
    )

    assert result == positional.EXIT_RUNTIME_DISABLED
    assert supervisor.calls == 1, "a disabled runtime must not be retried"


def test_positionals_no_credentials_code_is_retryable(monkeypatch: pytest.MonkeyPatch):
    import runtimes.positional_options.__main__ as positional

    _stub_preflight(monkeypatch, passes=True)
    supervisor = _CountingSupervisor([positional.EXIT_NO_CREDENTIALS, positional.EXIT_OK])
    _install(monkeypatch, "positional_options", supervisor)

    result = sl.run(
        runtime_id="positional_options",
        config_root=Path("config"),
        max_attempts=3,
        backoff_seconds=0.0,
    )

    assert result == positional.EXIT_OK
    assert supervisor.calls == 2


def test_an_unsupported_runtime_id_is_terminal_and_never_falls_back(
    monkeypatch: pytest.MonkeyPatch,
):
    def _must_not_be_called(argv):
        raise AssertionError("no runtime may run for an unknown id")

    _install(monkeypatch, "intraday_options", _must_not_be_called)

    result = sl.run(
        runtime_id="not_a_runtime",
        config_root=Path("config"),
        max_attempts=3,
        backoff_seconds=0.0,
    )

    assert result == sl.EXIT_PREFLIGHT_FAILED


def test_an_unclassified_exit_code_is_treated_as_terminal(monkeypatch: pytest.MonkeyPatch):
    """Looping on a code nobody has classified would hide it behind the budget."""
    _stub_preflight(monkeypatch, passes=True)
    supervisor = _CountingSupervisor([99])
    _install(monkeypatch, "intraday_options", supervisor)

    result = sl.run(
        runtime_id="intraday_options",
        config_root=Path("config"),
        max_attempts=3,
        backoff_seconds=0.0,
    )

    assert result == 99
    assert supervisor.calls == 1


def test_the_docstring_no_longer_claims_launchd_restarts_this_process():
    """KeepAlive=false means it does not. The comment said otherwise."""
    doc = sl.__doc__ or ""
    assert "Exhaustion is final for the day" in doc
    assert "ThrottleInterval the" not in doc
