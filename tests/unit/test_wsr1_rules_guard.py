"""Spec 13 and 15 (Phase 3): the rules core is pure, enforced here by AST.

``rules.py`` and ``indicators.py`` — and the two modules they import from the
package, ``models.py`` and ``iso_weeks.py`` — must import nothing from
``common.engine``, ``runtimes``, the broker, persistence or market data, no
network or database library, and must never read the clock. That is what lets
a future live adapter reuse the rules unchanged, and what keeps the Monday
decision run offline.

Two checks, because each catches what the other misses:

* an **allow-list** of imports per module — a new dependency has to be added
  here deliberately, with a reviewer looking at it;
* a **deny-list of calls** — ``datetime.now()``, ``date.today()``,
  ``time.time()`` and friends — since ``datetime`` itself is allowed (for the
  ``date`` type) and a clock read would otherwise pass the import check.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = (
    Path(__file__).resolve().parents[2]
    / "strategies"
    / "positional_stocks"
    / "wsr1_weekly_stochrsi"
)

ALLOWED_IMPORTS: dict[str, set[str]] = {
    "rules.py": {
        "__future__",
        "collections",
        "collections.abc",
        "dataclasses",
        "datetime",
        "decimal",
        ".indicators",
        ".iso_weeks",
        ".models",
    },
    "indicators.py": {
        "__future__",
        "collections.abc",
        "dataclasses",
        "itertools",
        ".iso_weeks",
        ".models",
    },
    "iso_weeks.py": {"__future__", "datetime"},
    "models.py": {"__future__", "dataclasses", "datetime", "decimal", "enum", ".iso_weeks"},
}

#: Imports that must never appear, whatever the allow-list says.
FORBIDDEN_PREFIXES = (
    "common",
    "runtimes",
    "orchestration",
    "framework",
    "sqlite3",
    "httpx",
    "requests",
    "socket",
    "time",
    "os",
    "pathlib",
    "subprocess",
    "pandas",
    "numpy",
    "dhanhq",
    "logging",
)

#: Attribute calls that read the clock.
CLOCK_CALLS = {"now", "today", "utcnow", "time", "monotonic", "perf_counter", "time_ns"}


def _imports(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add("." * node.level + (node.module or ""))
    return names


def _tree(module: str) -> ast.AST:
    path = PACKAGE / module
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


@pytest.mark.parametrize("module", sorted(ALLOWED_IMPORTS))
def test_imports_are_exactly_the_allow_list(module: str) -> None:
    assert _imports(_tree(module)) == ALLOWED_IMPORTS[module]


@pytest.mark.parametrize("module", sorted(ALLOWED_IMPORTS))
def test_no_forbidden_import(module: str) -> None:
    for name in _imports(_tree(module)):
        root = name.lstrip(".").split(".")[0]
        if name.startswith("."):
            continue
        assert root not in FORBIDDEN_PREFIXES, f"{module} imports {name}"


@pytest.mark.parametrize("module", sorted(ALLOWED_IMPORTS))
def test_never_reads_the_clock(module: str) -> None:
    for node in ast.walk(_tree(module)):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            assert name not in CLOCK_CALLS, f"{module} calls {name}() at line {node.lineno}"


@pytest.mark.parametrize("module", sorted(ALLOWED_IMPORTS))
def test_no_io(module: str) -> None:
    for node in ast.walk(_tree(module)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"open", "print", "input", "exec", "eval", "__import__"}


def test_the_guard_would_catch_a_clock_read() -> None:
    """The deny-list is not vacuous: a planted ``datetime.now()`` is found."""
    planted = ast.parse("from datetime import datetime\nx = datetime.now()\n")
    calls = [
        n.func.attr
        for n in ast.walk(planted)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    ]
    assert "now" in calls and "now" in CLOCK_CALLS
