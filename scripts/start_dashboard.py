#!/usr/bin/env python3
"""Launch the read-only Streamlit dashboard — a thin wrapper, like start_runtime.py.

    .venv/bin/python -m scripts.start_dashboard [--port 8501] [--force]

``streamlit run`` is the platform's one real entry point for the dashboard
(``dashboards/Home.py``'s own module docstring), but it is a shell command, not
a Python one — nothing to unit-test by importing it. This wrapper exists so
the LaunchAgent generator (``orchestration/launchd/generate_plists.py``) and
the ``algo-dashboard`` console script have exactly one place that knows the
``streamlit`` binary lives in this project's own ``.venv``, not whatever
``streamlit`` a bare ``$PATH`` lookup would find.

**The dashboard has exactly one owner: its own ``RunAtLoad`` LaunchAgent.**
``orchestration.auto_start`` deliberately does not start it. Two owners would
mean two Streamlit servers racing for one port, and would also tie the
dashboard's availability to trading — whereas the useful behaviour is the
opposite one: the dashboard is read-only, so it should be up at weekends and
on holidays, when there is no trading to attach it to.

Two gates, both idempotent:

* ``auto_start.dashboard_auto_start: false`` makes this a successful no-op, so
  an operator can turn the dashboard off without unloading its LaunchAgent.
* an already-listening dashboard port makes this a successful no-op too. A
  duplicate ``RunAtLoad`` (login, then wake, then a manual run) must not
  produce a second server; the second one would fail to bind and exit
  non-zero, which reads like a fault rather than the non-event it is.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
from pathlib import Path

from common.config import load_auto_start_config
from common.config.paths import resolve_project_root

#: Streamlit's own default. Overridable, because the check and the launch must
#: agree on which port "already running" refers to.
DEFAULT_DASHBOARD_PORT = 8501

EXIT_OK = 0
EXIT_FAILED = 1


def port_is_serving(port: int, *, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    """Whether something already accepts connections on ``port``.

    A connect probe rather than a PID file: the thing that must not be
    duplicated is the *listening socket*, and after ``os.execv`` replaces this
    process with Streamlit there is no PID of ours left to record anyway.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_DASHBOARD_PORT)
    parser.add_argument("--config-root", type=Path, default=Path("config"))
    parser.add_argument(
        "--force",
        action="store_true",
        help="Start even if dashboard_auto_start is false (never bypasses the port check).",
    )
    args, passthrough = parser.parse_known_args(list(argv) if argv is not None else [])

    if not args.force:
        try:
            cfg = load_auto_start_config(args.config_root)
        except Exception as exc:
            print(f"could not read auto_start config ({exc}); starting the dashboard anyway")
        else:
            if not cfg.dashboard_auto_start:
                print("auto_start.dashboard_auto_start is false; not starting the dashboard")
                return EXIT_OK

    if port_is_serving(args.port):
        print(f"a dashboard is already serving on port {args.port}; nothing to do")
        return EXIT_OK

    root = resolve_project_root()
    streamlit_bin = root / ".venv" / "bin" / "streamlit"
    app_path = root / "dashboards" / "Home.py"

    if not streamlit_bin.is_file():
        print(f"streamlit not found at {streamlit_bin} — is the .venv set up?")
        return EXIT_FAILED

    command = [
        str(streamlit_bin),
        "run",
        str(app_path),
        "--server.port",
        str(args.port),
        # Headless, because the owner is a RunAtLoad LaunchAgent: Streamlit's
        # macOS default (``headless = false``) opens a browser tab itself, so a
        # non-headless launch pops one open at every login, whether or not the
        # operator wants to look at the dashboard. The server still listens on
        # ``--server.port``; only the auto-open is suppressed. Placed before
        # ``passthrough`` so an explicit ``--server.headless false`` still wins
        # — Streamlit's CLI takes the last occurrence of a repeated option.
        "--server.headless",
        "true",
        *passthrough,
    ]
    # NoReturn on success: os.execv replaces this process image entirely, so
    # there is no line after this one to fall through to on the happy path.
    os.execv(str(streamlit_bin), command)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
