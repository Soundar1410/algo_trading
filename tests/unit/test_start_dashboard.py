"""scripts.start_dashboard: one owner, and idempotent.

The dashboard is started by its own RunAtLoad LaunchAgent and by nothing else —
in particular not by the trading controller, which would make two owners racing
for one port. These tests pin both gates that keep a duplicate trigger harmless.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

import scripts.start_dashboard as sd


@pytest.fixture(autouse=True)
def _never_exec(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """os.execv replaces the process; in tests it must only be recorded."""
    calls: list[list[str]] = []

    def _fake_execv(path: str, argv: list[str]) -> None:
        calls.append(list(argv))
        raise SystemExit(0)

    monkeypatch.setattr(sd.os, "execv", _fake_execv)
    return calls


def _config(tmp_path: Path, *, dashboard_auto_start: bool) -> Path:
    config = tmp_path / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "global.yaml").write_text(
        "global:\n  timezone: Asia/Kolkata\n"
        f"auto_start:\n  dashboard_auto_start: {str(dashboard_auto_start).lower()}\n",
        encoding="utf-8",
    )
    return config


def test_a_disabled_dashboard_flag_is_a_clean_no_op(tmp_path: Path, _never_exec):
    config = _config(tmp_path, dashboard_auto_start=False)
    assert sd.main(["--config-root", str(config)]) == sd.EXIT_OK
    assert _never_exec == [], "nothing may be launched when the flag is off"


def test_an_already_serving_port_is_a_clean_no_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _never_exec
):
    """A duplicate RunAtLoad must not try to bind a second server."""
    monkeypatch.setattr(sd, "port_is_serving", lambda port, **kwargs: True)
    config = _config(tmp_path, dashboard_auto_start=True)

    assert sd.main(["--config-root", str(config)]) == sd.EXIT_OK
    assert _never_exec == []


def test_the_port_probe_reports_a_closed_port_as_free():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        free_port = sock.getsockname()[1]
    assert sd.port_is_serving(free_port, timeout=0.1) is False


def test_the_port_probe_detects_a_listening_socket():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        assert sd.port_is_serving(port, timeout=0.5) is True


def test_force_bypasses_the_flag_but_never_the_port_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _never_exec
):
    monkeypatch.setattr(sd, "port_is_serving", lambda port, **kwargs: True)
    config = _config(tmp_path, dashboard_auto_start=False)

    assert sd.main(["--config-root", str(config), "--force"]) == sd.EXIT_OK
    assert _never_exec == [], "--force must not create a duplicate server"


def test_a_missing_streamlit_binary_is_reported_not_crashed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _never_exec
):
    monkeypatch.setattr(sd, "port_is_serving", lambda port, **kwargs: False)
    monkeypatch.setattr(sd, "resolve_project_root", lambda: tmp_path)
    config = _config(tmp_path, dashboard_auto_start=True)

    assert sd.main(["--config-root", str(config)]) == sd.EXIT_FAILED


def test_the_auto_start_controller_never_launches_the_dashboard():
    """One owner. Two would race for the port and tie the dashboard's
    availability to trading, when the useful behaviour is the opposite."""
    from orchestration.auto_start import controller

    source = Path(controller.__file__).read_text(encoding="utf-8")
    assert "start_dashboard" not in source

    main_source = Path(
        Path(controller.__file__).parent / "__main__.py"
    ).read_text(encoding="utf-8")
    assert "start_dashboard" not in main_source
