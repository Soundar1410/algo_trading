"""``scripts/`` splits into two tiers, and each gets a different safety proof.

Phase 2's central safety claim was that every live call ``scripts/`` makes is
read-only and cannot place, modify or cancel an order — worth a test rather
than a comment, because it is exactly the sort of thing a later refactor
erodes quietly. That claim still holds for the **read-only tier**
(``auth_bootstrap.py``/``authenticate.py``, ``capture_live_tape.py``,
``diagnose_live_feed.py``, ``status.py``, ``validate_environment.py``,
``verify_vix_security_id.py``): no
import path from any of them
reaches a broker, no order endpoint or verb appears in any of them, and
``--status`` is proven offline by making the socket layer raise.

Phase 7 Part 4 added a second, different kind of script: **the control
tier** (``stop_runtime.py``, ``stop_strategy.py``, ``square_off.py``,
``start_runtime.py``, ``start_strategy.py``, and the ``_operator_common.py``/
``_runtimes.py`` plumbing they share). These are not read-only — a stop sends ``SIGTERM``, a
square-off asks a worker to close a position, a start spawns a supervisor
that trades. What has to hold instead is the plan's own constraint: **no
control script opens a second writer against a trading table.** Every write
any of them performs goes through ``ExecutionRepository.record_audit_event``
— the audit trail — never a hand-rolled ``INSERT``/``UPDATE``/``DELETE``
against ``signals``, ``order_intents``, ``orders``, ``fills``, ``positions``
or ``strategy_state``. ``start_runtime.py``/``start_strategy.py`` are the
one pair that *does* eventually cause those tables to be written — through
the real supervisor and its spawned workers, each with their own connection
— but neither script performs, or contains, a write itself; they are proven
by the same "no SQL of its own" check as the rest of the tier, and the
supervisor's own writes are covered by every test in
``tests/end_to_end/test_supervisor.py`` and ``tests/end_to_end/test_walking_
skeleton.py``.

Phase 8 added ``start_dashboard.py`` to the read-only tier (execs
``streamlit``, no trading write of any kind — it does not even open a
database connection) and ``orchestration/process_control/`` as a third,
narrower tier: **launcher scripts**, swept by the same broker-import and
trading-table checks as the control tier, but never proven "no SQL of its
own" against the same table list, because ``supervised_launch.py`` does write
directly — to ``errors``, never a trading table (see its own module
docstring for why that table, not ``audit_events``).

Do not delete any tier's assertions when this file next changes — narrow
them further if a new script needs it, but a script that stops being checked
here is a safety property that stopped being proven.
"""

from __future__ import annotations

import ast
import re
import socket
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
#: ``__init__.py`` (Phase 10, added only so ``import scripts.auth_bootstrap``
#: below resolves to one unambiguous module under mypy's ``packages`` config
#: instead of colliding with the same file's bare-name inference — see
#: ``dashboards/Home.py``'s own ``__init__.py`` for the identical fix) carries
#: no script logic of its own and is excluded here the same way
#: ``dashboards``'s own directory-contents test excludes it.
SCRIPT_FILES = sorted(p for p in SCRIPTS.glob("*.py") if p.name != "__init__.py")
LAUNCHER_DIR = REPO_ROOT / "orchestration" / "process_control"
LAUNCHER_FILES = sorted(LAUNCHER_DIR.glob("*.py"))

#: Touches the network (Dhan's REST/WebSocket surface) or nothing at all;
#: never signals a process, never asks a worker to act.
READ_ONLY_SCRIPTS = {
    "assert_no_live_config_committed.py",
    "auth_bootstrap.py",
    "authenticate.py",
    "capture_live_tape.py",
    "diagnose_live_feed.py",
    "start_dashboard.py",
    "status.py",
    "validate_environment.py",
    "verify_dhan_option_chain.py",
    "verify_vix_security_id.py",
}

#: Phase 8's launcher tier: wraps a supervisor entrypoint with a bounded
#: retry, writing only to ``errors`` (see ``supervised_launch.py``'s own
#: docstring for why that table, not ``audit_events``). Swept like the
#: control tier, but not by "no SQL of its own" — it does write, just never
#: to a trading table.
LAUNCHER_SCRIPTS = {
    "__init__.py",
    "supervised_launch.py",
}

#: Signals a process, writes a request file a worker polls, or starts one —
#: live-impacting, per spec section 11's "must require explicit confirmation
#: and log an audit event". The only database write any of them performs is
#: that audit event.
CONTROL_SCRIPTS = {
    "_operator_common.py",
    "_runtimes.py",
    "square_off.py",
    "start_runtime.py",
    "start_strategy.py",
    "stop_runtime.py",
    "stop_strategy.py",
}

#: Manages only the short-lived confirmation gate in the account-shared DB.
#: It neither reaches a broker nor writes any trading table, but it is not
#: labelled read-only because issuing/revoking approval evidence is an
#: intentional operational mutation.
CONFIRMATION_SCRIPTS = {"live_confirmation.py"}

#: Installs, inspects and removes LaunchAgents. Not read-only — with
#: ``--execute`` it copies plists and calls ``launchctl`` — but it touches no
#: broker, no database and no trading table at all: the most it can do is make
#: the Mac run ``orchestration.auto_start`` on a schedule, which is itself
#: gated separately by ``auto_start.enabled``. What must hold is that it stays
#: dry-run by default and refuses while the legacy system is loaded, both
#: proven in ``tests/unit/test_install_launch_agents.py``.
AGENT_INSTALL_SCRIPTS = {"install_launch_agents.py"}

READ_ONLY_FILES = [p for p in SCRIPT_FILES if p.name in READ_ONLY_SCRIPTS]
CONTROL_FILES = [p for p in SCRIPT_FILES if p.name in CONTROL_SCRIPTS]
CONFIRMATION_FILES = [p for p in SCRIPT_FILES if p.name in CONFIRMATION_SCRIPTS]
AGENT_INSTALL_FILES = [p for p in SCRIPT_FILES if p.name in AGENT_INSTALL_SCRIPTS]

#: Tables a control script must never write directly — trades, positions and
#: the state that gates them. Contrast ``audit_events``, which every control
#: script writes through ``record_audit_event``, and the purely diagnostic
#: ``errors``/``notifications``/``runtime_heartbeats``/``runtime_sessions``,
#: none of which this file needs to police.
FORBIDDEN_TRADING_TABLES = (
    "signals",
    "order_intents",
    "orders",
    "fills",
    "positions",
    "strategy_state",
)


def test_the_scripts_directory_is_what_we_think_it_is():
    """Guards every parametrisation below: an empty glob would pass everything."""
    names = {path.name for path in SCRIPT_FILES}
    assert names == (
        READ_ONLY_SCRIPTS | CONTROL_SCRIPTS | CONFIRMATION_SCRIPTS | AGENT_INSTALL_SCRIPTS
    )
    tiers = (READ_ONLY_SCRIPTS, CONTROL_SCRIPTS, CONFIRMATION_SCRIPTS, AGENT_INSTALL_SCRIPTS)
    assert all(left.isdisjoint(right) for i, left in enumerate(tiers) for right in tiers[i + 1 :])


def test_the_launcher_directory_is_what_we_think_it_is():
    names = {path.name for path in LAUNCHER_FILES}
    assert names == LAUNCHER_SCRIPTS


@pytest.mark.parametrize("script", CONFIRMATION_FILES, ids=lambda p: p.name)
def test_confirmation_workflow_cannot_reach_a_broker_or_trading_table(script: Path):
    assert _broker_imports(script) == set(), f"{script.name} imports a broker"
    source = script.read_text(encoding="utf-8")
    for table in FORBIDDEN_TRADING_TABLES:
        assert not re.search(rf"\b{re.escape(table)}\b", source, re.IGNORECASE)
    assert "live_confirmations" in source
    assert "live_confirmation_events" in source


@pytest.mark.parametrize("script", AGENT_INSTALL_FILES, ids=lambda p: p.name)
def test_the_agent_installer_reaches_no_broker_database_or_trading_table(script: Path):
    assert _broker_imports(script) == set(), f"{script.name} imports a broker"
    source = script.read_text(encoding="utf-8")
    for table in FORBIDDEN_TRADING_TABLES:
        assert not re.search(rf"\b{re.escape(table)}\b", source, re.IGNORECASE)
    assert "ExecutionRepository" not in source
    assert "connect_readonly" not in source


@pytest.mark.parametrize("script", AGENT_INSTALL_FILES, ids=lambda p: p.name)
def test_the_agent_installer_never_loads_anything_without_execute(script: Path):
    """Loading a LaunchAgent is what turns files into a Mac that trades by
    itself. It must be an explicit act, never a default."""
    source = script.read_text(encoding="utf-8")
    assert "args.execute" in source or "execute=" in source
    assert "dry run" in source


# ============================================================== read-only tier
@pytest.mark.parametrize("script", READ_ONLY_FILES, ids=lambda p: p.name)
def test_no_read_only_script_imports_a_broker(script: Path):
    """A script that can reach a broker is one refactor away from ordering."""
    assert _broker_imports(script) == set(), f"{script.name} imports a broker"


@pytest.mark.parametrize("script", READ_ONLY_FILES, ids=lambda p: p.name)
def test_no_read_only_script_references_an_order_endpoint(script: Path):
    """Dhan's order surface lives under /orders. It must appear nowhere here.

    Comments and docstrings are stripped first so the prose explaining *why*
    orders are out of scope does not trip it.
    """
    source = script.read_text(encoding="utf-8")
    code_only = "\n".join(
        line.split("#", 1)[0] for line in source.splitlines() if not line.strip().startswith("#")
    )
    docstring_text = "\n".join(_string_literals(script))

    for forbidden in ("/orders", "/superorder", "/forever", "placeOrder", "cancelOrder"):
        in_code = forbidden in code_only and forbidden not in docstring_text
        assert not in_code, f"{script.name} references the order endpoint {forbidden!r}"


@pytest.mark.parametrize("script", READ_ONLY_FILES, ids=lambda p: p.name)
def test_no_read_only_script_uses_a_mutating_http_verb_against_the_trading_api(script: Path):
    """PUT, DELETE and PATCH have no read-only use in Dhan's API."""
    tree = ast.parse(script.read_text(encoding="utf-8"))
    calls = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    for verb in ("put", "delete", "patch"):
        assert verb not in calls, f"{script.name} calls .{verb}()"


def test_the_status_flag_makes_no_network_call(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """The one flag an operator can safely run outside market hours.

    Proven by replacing the socket constructor with something that raises, rather
    than by inspecting the code — so a transitive call through any library would
    still be caught.
    """
    monkeypatch.setenv("DHAN_CLIENT_ID", "1100000000")
    monkeypatch.setenv("DHAN_PIN", "1234")
    monkeypatch.setenv("DHAN_TOTP_SECRET", "JBSWY3DPEHPK3PXP")
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))

    class _Blocked(socket.socket):
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("a network call was attempted during --status")

    monkeypatch.setattr(socket, "socket", _Blocked)

    from scripts.auth_bootstrap import EXIT_OK, main

    assert main(["--status"]) == EXIT_OK


def test_a_missing_client_id_exits_before_any_network_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.delenv("DHAN_CLIENT_ID", raising=False)
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))

    class _Blocked(socket.socket):
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("a network call was attempted with no credentials")

    monkeypatch.setattr(socket, "socket", _Blocked)

    from scripts.auth_bootstrap import EXIT_NO_CREDENTIALS, main

    assert main([]) == EXIT_NO_CREDENTIALS


def test_the_capture_script_scrubs_credential_shaped_keys():
    """Payloads are filtered field by field before ever reaching the fixture."""
    from scripts.capture_live_tape import _scrub

    dirty = {
        "type": "Ticker Data",
        "LTP": "187.45",
        "accessToken": "eyJhbGciOi.should.not.survive",
        "dhanClientId": "1100000000",
        "nested": [{"pin": "1234", "LTQ": 75}],
    }
    clean = _scrub(dirty)

    assert clean == {"type": "Ticker Data", "LTP": "187.45", "nested": [{"LTQ": 75}]}


def test_the_capture_script_labels_every_frame_kind():
    from scripts.capture_live_tape import _label_for

    assert _label_for({"type": "Ticker Data"}) == "ticker_data"
    assert _label_for({"type": "Quote Data"}) == "quote_data"
    assert _label_for("Markets Open") == "status_string"
    assert _label_for(None) == "empty"
    assert _label_for(42) == "unexpected"
    assert _label_for({}) == "unknown"


def test_start_dashboard_refuses_cleanly_when_the_venv_has_no_streamlit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """No `os.execv` (and so no real streamlit process) when the interpreter
    it would exec does not exist — proven by letting the real check run
    against a tmp_path with no `.venv` at all, rather than mocking it away.

    Runs against an **isolated ephemeral port**, never the default 8501. The
    original version called ``main([])``, which probes 8501 — so on the
    operator's own machine, where the real dashboard is served by its
    LaunchAgent, ``start_dashboard`` correctly reported "already serving,
    nothing to do" and returned 0, and this test failed for a reason that had
    nothing to do with what it is about. The probe itself is still the real
    one: an ephemeral port that has just been released is genuinely closed, so
    the "not already serving" branch is exercised for real, and the running
    dashboard is neither contacted nor disturbed.
    """
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    from scripts.start_dashboard import main, port_is_serving

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
    assert free_port != 8501
    assert port_is_serving(free_port, timeout=0.1) is False, (
        "the chosen port was taken; this test needs a closed one"
    )

    assert main(["--port", str(free_port)]) == 1


def test_authenticate_is_a_pure_alias_for_auth_bootstrap():
    """The spec names this command ``scripts/authenticate``; it must not drift
    from ``auth_bootstrap.py``, which keeps its own name and its own tests."""
    import scripts.auth_bootstrap as bootstrap
    import scripts.authenticate as alias

    assert alias.main is bootstrap.main
    assert alias.EXIT_OK == bootstrap.EXIT_OK
    assert alias.EXIT_FAILED == bootstrap.EXIT_FAILED
    assert alias.EXIT_NO_CREDENTIALS == bootstrap.EXIT_NO_CREDENTIALS
    assert alias.EXIT_COOLDOWN == bootstrap.EXIT_COOLDOWN


# =============================================================== control tier
def _broker_imports(script: Path) -> set[str]:
    tree = ast.parse(script.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    return {name for name in imported if "broker" in name.lower()}


def _string_literals(script: Path) -> list[str]:
    tree = ast.parse(script.read_text(encoding="utf-8"))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


@pytest.mark.parametrize("script", CONTROL_FILES, ids=lambda p: p.name)
def test_no_control_script_imports_a_broker(script: Path):
    """A control script that can reach a broker could place a live order under
    the cover of "just stopping something" — checked independently of the
    read-only tier's own version of this, because the two tiers are proven
    for different reasons (network safety there, no second trading writer
    here) and must not be allowed to silently share one one-sided check."""
    assert _broker_imports(script) == set(), f"{script.name} imports a broker"


@pytest.mark.parametrize("script", CONTROL_FILES, ids=lambda p: p.name)
def test_no_control_script_writes_a_trading_table(script: Path):
    """No control script contains a write against a trading table.

    A textual check, deliberately: none of these scripts hand-roll SQL at
    all (every write goes through ``ExecutionRepository.record_audit_event``,
    already exercised by ``tests/unit/test_execution_repository.py``), so the
    absence of ``INSERT``/``UPDATE``/``DELETE`` against any forbidden table
    name is a direct statement of that fact, not a proxy for it. Docstrings
    are stripped first, the same way the read-only tier's own order-endpoint
    check is, so prose explaining this very constraint cannot trip it.
    """
    source = script.read_text(encoding="utf-8")
    code_only = "\n".join(
        line.split("#", 1)[0] for line in source.splitlines() if not line.strip().startswith("#")
    )
    docstring_text = "\n".join(_string_literals(script))

    for table in FORBIDDEN_TRADING_TABLES:
        pattern = re.compile(
            rf"\b(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+{re.escape(table)}\b", re.IGNORECASE
        )
        match = pattern.search(code_only)
        in_code = bool(match) and match.group(0) not in docstring_text
        assert not in_code, f"{script.name} writes directly to {table!r}"


def test_stop_runtime_and_stop_strategy_never_touch_positions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """The consequence-level proof, at the level the plan's decision was made
    at: no verified owner means nothing is signalled and nothing beyond the
    audit trail is written — never a fallback write to ``positions``."""
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    import scripts.stop_runtime as stop_runtime_script
    import scripts.stop_strategy as stop_strategy_script

    for script_module, extra_argv in (
        (stop_runtime_script, []),
        (stop_strategy_script, ["--strategy-id", "does_not_exist"]),
    ):
        rc = script_module.main(["--runtime-id", "intraday_options", *extra_argv])
        assert rc == script_module.EXIT_NOT_RUNNING


def test_square_off_refuses_without_confirm_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    from scripts.square_off import EXIT_NOT_CONFIRMED, main

    assert main(["--strategy-id", "some_strategy"]) == EXIT_NOT_CONFIRMED
    # Nothing written at all — not the request file, not the database.
    assert not (tmp_path / "data").exists()


# =============================================================== launcher tier
@pytest.mark.parametrize("script", LAUNCHER_FILES, ids=lambda p: p.name)
def test_no_launcher_script_imports_a_broker(script: Path):
    assert _broker_imports(script) == set(), f"{script.name} imports a broker"


@pytest.mark.parametrize("script", LAUNCHER_FILES, ids=lambda p: p.name)
def test_no_launcher_script_writes_a_trading_table(script: Path):
    """Same forbidden-table sweep as the control tier. `supervised_launch.py`
    does write — to `errors` — which is deliberately not in this list."""
    source = script.read_text(encoding="utf-8")
    code_only = "\n".join(
        line.split("#", 1)[0] for line in source.splitlines() if not line.strip().startswith("#")
    )
    docstring_text = "\n".join(_string_literals(script))

    for table in FORBIDDEN_TRADING_TABLES:
        pattern = re.compile(
            rf"\b(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+{re.escape(table)}\b", re.IGNORECASE
        )
        match = pattern.search(code_only)
        in_code = bool(match) and match.group(0) not in docstring_text
        assert not in_code, f"{script.name} writes directly to {table!r}"
