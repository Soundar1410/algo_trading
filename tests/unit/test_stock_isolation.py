"""Spec 13 v1.2i: the paper runtimes never load ``positional_stocks`` code.

The two paper runtimes run from this working tree. Everything of this feature
lives in modules they must never import, so no change here can reach live
paper trading. This walks every module of ``runtimes.intraday_options``,
``runtimes.positional_options`` and ``orchestration`` in a fresh interpreter
and asserts that none of them pulls in a ``positional_stocks`` module.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

_PROBE = """
import importlib, pkgutil, sys
root = sys.argv[1]
package = importlib.import_module(root)
for info in pkgutil.walk_packages(package.__path__, root + "."):
    try:
        importlib.import_module(info.name)
    except Exception:
        pass
print(len([name for name in sys.modules if name.startswith(root)]))
print("\\n".join(sorted(name for name in sys.modules if "positional_stocks" in name)))
"""


@pytest.mark.parametrize(
    "root", ["runtimes.intraday_options", "runtimes.positional_options", "orchestration"]
)
def test_the_paper_runtimes_load_no_positional_stocks_module(root: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, root],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    loaded, *stock_modules = result.stdout.strip().split("\n")
    # Not vacuous: the walk really imported the package's modules.
    assert int(loaded) >= 5
    assert stock_modules == []
