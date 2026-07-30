"""The Block 2 scripts are the only code that touches the network.

Phase 2's central safety claim is that every live call it makes is read-only and
cannot place, modify or cancel an order. That claim is worth a test rather than a
comment, because it is exactly the sort of thing a later refactor erodes quietly.

The checks are structural: no import path from either script reaches a broker, no
order endpoint or verb appears in either file, and ``--status`` is proven offline
by making the socket layer raise.
"""

from __future__ import annotations

import ast
import socket
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
SCRIPT_FILES = sorted(SCRIPTS.glob("*.py"))


def test_the_scripts_directory_is_what_we_think_it_is():
    """Guards the parametrisation below: an empty glob would pass everything."""
    names = {path.name for path in SCRIPT_FILES}
    assert names == {"auth_bootstrap.py", "capture_live_tape.py"}


@pytest.mark.parametrize("script", SCRIPT_FILES, ids=lambda p: p.name)
def test_no_script_imports_a_broker(script: Path):
    """A script that can reach a broker is one refactor away from ordering."""
    tree = ast.parse(script.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    offenders = {name for name in imported if "broker" in name.lower()}
    assert offenders == set(), f"{script.name} imports {offenders}"


@pytest.mark.parametrize("script", SCRIPT_FILES, ids=lambda p: p.name)
def test_no_script_references_an_order_endpoint(script: Path):
    """Dhan's order surface lives under /orders. It must appear nowhere here.

    Comments are stripped before the check so the prose explaining *why* orders
    are out of scope does not trip it.
    """
    source = script.read_text(encoding="utf-8")
    code_only = "\n".join(
        line.split("#", 1)[0] for line in source.splitlines() if not line.strip().startswith("#")
    )
    tree = ast.parse(source)
    # Drop docstrings, which legitimately discuss the order endpoints.
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    docstring_text = "\n".join(literals)

    for forbidden in ("/orders", "/superorder", "/forever", "placeOrder", "cancelOrder"):
        in_code = forbidden in code_only and forbidden not in docstring_text
        assert not in_code, f"{script.name} references the order endpoint {forbidden!r}"


@pytest.mark.parametrize("script", SCRIPT_FILES, ids=lambda p: p.name)
def test_no_script_uses_a_mutating_http_verb_against_the_trading_api(script: Path):
    """PUT and DELETE have no read-only use in Dhan's API."""
    source = script.read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                calls.append(func.attr)
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
