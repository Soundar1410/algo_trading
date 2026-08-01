"""`pandas-ta-classic` must never reach the live incremental path.

Phase 4 Part 2. The architecture document asks for a "pandas-ta-classic adapter",
and :mod:`common.indicators.vectorised` is it — but the values the engine
*trades on* are computed by the ported reference maths, not by the library.
Routing live values through it would change numbers the ported regression tests
were written against, which the project rules forbid.

**This is enforced here rather than promised in a docstring.** Phase 3 Part
2b-ii-B-1 found a claim in ``hub.py``'s module docstring asserting an
entry-block as fact while no code performed it; it survived review because it
was written in the same change as the counting that *was* real. A rule that only
exists in prose is a rule that quietly stops being true.

The same AST walk that backs ``test_exit_registry_wiring.py``'s
no-reference-import rule already covers ``common/indicators``; this file adds the
inverse direction — not "does the port import the reference" but "does the live
path import the oracle".
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The only non-test module permitted to import the oracle library.
ORACLE_MODULE = REPO_ROOT / "common" / "indicators" / "vectorised.py"

#: Everything shipped. Tests are excluded — they are the oracle's other caller.
SHIPPED_PACKAGES = ("common", "runtimes", "strategies", "scripts", "dashboards")

FORBIDDEN = ("pandas_ta_classic", "pandas_ta")


def _imported_modules(path: Path) -> set[str]:
    """Every module named by an import in ``path``, nested imports included."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module)
    return names


def _shipped_files() -> list[Path]:
    files: list[Path] = []
    for package in SHIPPED_PACKAGES:
        root = REPO_ROOT / package
        if root.is_dir():
            files.extend(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)
    return sorted(files)


def test_only_the_adapter_imports_the_oracle_library():
    """The rule, stated as a test. If this fails, the deviation recorded in the
    runbook — 'never on the live incremental path' — has stopped being true."""
    offenders: list[str] = []
    for path in _shipped_files():
        if path == ORACLE_MODULE:
            continue
        for module in _imported_modules(path):
            root = module.split(".")[0]
            if root in FORBIDDEN:
                offenders.append(f"{path.relative_to(REPO_ROOT)} imports {module!r}")
    assert offenders == [], (
        "pandas-ta-classic reached the live path:\n  "
        + "\n  ".join(offenders)
        + "\nThe oracle computes cross-check and warm-up-replay values only. "
        "Live incremental values come from the ported reference maths."
    )


def test_the_adapter_really_does_import_it():
    """The positive half. Without this the test above passes trivially if the
    adapter is deleted or renamed — a dead rule that looks like a live one."""
    assert ORACLE_MODULE.is_file(), f"{ORACLE_MODULE} is missing"
    roots = {module.split(".")[0] for module in _imported_modules(ORACLE_MODULE)}
    assert "pandas_ta_classic" in roots, (
        "the oracle adapter no longer imports pandas-ta-classic, so the "
        "boundary test above is guarding nothing"
    )


@pytest.mark.parametrize(
    "module_name",
    [
        "common.indicators.ema",
        "common.indicators.rsi",
        "common.indicators.vwap",
        "common.indicators.atr",
        "common.indicators.adx",
        "common.indicators.supertrend",
    ],
)
def test_importing_an_indicator_does_not_load_the_oracle(module_name: str):
    """Import-time proof, not just a source scan: a transitive import three
    levels down would satisfy the AST walk above and still load the library."""
    import subprocess
    import sys

    code = (
        "import sys\n"
        f"import {module_name}\n"
        "print('LOADED' if 'pandas_ta_classic' in sys.modules else 'ABSENT')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ABSENT", f"importing {module_name} pulled in pandas_ta_classic"


def test_the_engine_package_does_not_load_the_oracle():
    """The classifier ported in this part reaches ADX and ATR, so the engine now
    has an indicator import chain. It must still not reach the library."""
    import subprocess
    import sys

    code = (
        "import sys\n"
        "import common.engine.regime\n"
        "print('LOADED' if 'pandas_ta_classic' in sys.modules else 'ABSENT')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=REPO_ROOT
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ABSENT"
