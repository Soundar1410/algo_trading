"""Streamlit page-discovery shim. Logic lives in ``dashboards/intraday_stocks.py``.

See ``1_Intraday_Options.py`` for why the ``sys.path`` fix-up below is here.
"""

from __future__ import annotations

import sys
from pathlib import Path

for _parent in Path(__file__).resolve().parents:
    if (_parent / "pyproject.toml").is_file():
        if str(_parent) not in sys.path:
            sys.path.insert(0, str(_parent))
        break

from dashboards.intraday_stocks import main  # noqa: E402

main()
