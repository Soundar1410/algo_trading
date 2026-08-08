#!/usr/bin/env python3
"""Launch the read-only Streamlit dashboard — a thin wrapper, like start_runtime.py.

    .venv/bin/python -m scripts.start_dashboard

``streamlit run`` is the platform's one real entry point for the dashboard
(``dashboards/app.py``'s own module docstring), but it is a shell command, not
a Python one — nothing to unit-test by importing it. This wrapper exists so
the LaunchAgent generator (``orchestration/launchd/generate_plists.py``) and
the ``algo-dashboard`` console script have exactly one place that knows the
``streamlit`` binary lives in this project's own ``.venv``, not whatever
``streamlit`` a bare ``$PATH`` lookup would find.
"""

from __future__ import annotations

import os
import sys

from common.config.paths import resolve_project_root


def main(argv: list[str] | None = None) -> int:
    argv = list(argv) if argv is not None else []
    root = resolve_project_root()
    streamlit_bin = root / ".venv" / "bin" / "streamlit"
    app_path = root / "dashboards" / "app.py"

    if not streamlit_bin.is_file():
        print(f"streamlit not found at {streamlit_bin} — is the .venv set up?")
        return 1

    # NoReturn on success: os.execv replaces this process image entirely, so
    # there is no line after this one to fall through to on the happy path.
    os.execv(str(streamlit_bin), [str(streamlit_bin), "run", str(app_path), *argv])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
