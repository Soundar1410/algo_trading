"""Streamlit page-discovery shim. Logic lives in ``dashboards/intraday_options.py``.

Streamlit auto-discovers this file from its filename and position in
``dashboards/pages/`` — it does not import it as part of any package, so
this file cannot assume the project root is already on ``sys.path`` the way
a normal ``python -m`` invocation would guarantee. Fixed up explicitly,
walking up to ``pyproject.toml`` the same way
``common.config.paths._discover_root_from_source`` does, before importing
anything from ``dashboards``.
"""

from __future__ import annotations

import sys
from pathlib import Path

for _parent in Path(__file__).resolve().parents:
    if (_parent / "pyproject.toml").is_file():
        if str(_parent) not in sys.path:
            sys.path.insert(0, str(_parent))
        break

from dashboards.intraday_options import main  # noqa: E402

main()
