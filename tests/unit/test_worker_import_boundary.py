"""The worker's import graph, enforced rather than remembered.

Phase 3 Part 2b-ii-B-2. ``runtimes/intraday_options/worker.py`` is re-imported from
scratch by **every spawned child**, so what it drags in is paid once per worker
process, at startup, before the lock is even contended.

That is not a tidiness concern. In Part 2b-i, re-exporting ``EngineFixtureStrategy``
from ``strategies/intraday_options/__init__.py`` pulled ``common.engine`` — and
through it the exit registry and the indicators — into that graph, cost +0.22 s per
child, and pushed a spawned worker past the 0.5 s window in
``test_duplicate_worker_startup_is_refused``, where the lock holder exits after 0.5 s
of an idle queue. The gate went red for a change that looked like an export tidy-up.

Part 2b-ii-B-2 is the part that finally *does* put the engine in a worker, so the
measurement B-1 ran by hand is promoted to a test here. Three of them, because each
catches something the others cannot:

1. :func:`test_the_worker_module_imports_no_engine_package_at_module_level` reads the
   file. It fails on the **edit**, in the diff that causes it, rather than later on a
   number that moved.
2. :func:`test_a_clean_interpreter_loads_no_engine_module_for_a_worker` runs a real
   interpreter. It is the only one that catches a *transitive* drag — some other
   module growing an engine import three levels down, which no amount of reading
   ``worker.py`` would reveal.
3. :func:`test_the_engine_branch_really_does_load_the_engine` is the positive half.
   Without it, both of the above are satisfied perfectly by an engine path that
   silently never loads, which is the failure mode a boundary test invites.

Wall-clock time is deliberately **not** asserted. A ``< 0.5 s`` assertion here would
be flaky on a loaded machine and would fail for reasons unrelated to the property;
the module count is the *cause*, and
``test_duplicate_worker_startup_is_refused`` remains the timing gate.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKER_PATH = REPO_ROOT / "runtimes" / "intraday_options" / "worker.py"
WORKER_PACKAGE = "runtimes.intraday_options"

#: Packages a worker on the fixture path must never load. ``common.engine`` is the
#: one that matters; the others are listed because they are what it drags with it,
#: so a regression that arrives through them is named rather than mysterious.
FORBIDDEN_PREFIXES = (
    "common.engine",
    "common.exit",
    "common.indicators",
    "common.warmup",
    "strategies.intraday_options.engine_fixture_strategy",
)

#: The single deferred seam. Importing this at module level would defeat the whole
#: arrangement, because it imports every one of the above.
ENGINE_SEAM = f"{WORKER_PACKAGE}.engine_worker"


def _resolve(node: ast.Import | ast.ImportFrom, alias: ast.alias) -> str:
    """The dotted module name an import statement names, relative imports included."""
    if isinstance(node, ast.Import):
        return alias.name
    if node.level:  # `from . import x` / `from .mod import x`
        base = WORKER_PACKAGE.rsplit(".", node.level - 1)[0] if node.level > 1 else WORKER_PACKAGE
        return f"{base}.{node.module}" if node.module else f"{base}.{alias.name}"
    return node.module or ""


def _imports(*, module_level: bool) -> set[str]:
    """Every module named by an import in ``worker.py``, inside or outside a function."""
    tree = ast.parse(WORKER_PATH.read_text())
    # Anything nested inside a function or class body is a deferred import; anything
    # in ``Module.body`` runs the moment the module is imported.
    top_level_nodes = set(map(id, ast.walk(tree)))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for nested in ast.walk(node):
                top_level_nodes.discard(id(nested))

    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Import | ast.ImportFrom):
            continue
        if (id(node) in top_level_nodes) is not module_level:
            continue
        for alias in node.names:
            found.add(_resolve(node, alias))
    return found


def _modules_after(statement: str) -> set[str]:
    """``common.engine`` modules loaded by a fresh interpreter running ``statement``."""
    code = (
        "import sys\n"
        f"{statement}\n"
        "print('\\n'.join(sorted(m for m in sys.modules if m.startswith('common.engine'))))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    return {line for line in completed.stdout.split() if line}


# ------------------------------------------------------------------- static
def test_the_worker_module_imports_no_engine_package_at_module_level():
    offenders = {
        name for name in _imports(module_level=True) if name.startswith(FORBIDDEN_PREFIXES)
    }
    assert offenders == set(), (
        f"{WORKER_PATH.name} imports {sorted(offenders)} at module level. Every "
        "spawned child pays that on startup — it is what cost Part 2b-i the "
        "duplicate-worker window. Move it inside the engine branch."
    )


def test_the_engine_seam_is_imported_only_inside_a_function():
    assert ENGINE_SEAM not in _imports(module_level=True), (
        "engine_worker imports the whole engine package; hoisting this import to "
        "module level defeats the entire boundary."
    )
    assert ENGINE_SEAM in _imports(module_level=False), (
        "the deferred import of engine_worker has disappeared from worker.py — the "
        "engine branch cannot be reachable."
    )


def test_the_engine_worker_module_is_not_re_exported_by_the_package():
    """``runtimes/intraday_options/__init__.py`` imports ``worker``; if it also
    imported ``engine_worker`` the boundary would be bypassed one level up."""
    init_path = REPO_ROOT / "runtimes" / "intraday_options" / "__init__.py"
    assert "engine_worker" not in init_path.read_text(), (
        "the package __init__ pulls in engine_worker, so importing the package at "
        "all loads common.engine."
    )


# -------------------------------------------------------- the real interpreter
def test_a_clean_interpreter_loads_no_engine_module_for_a_worker():
    loaded = _modules_after(f"import {WORKER_PACKAGE}.worker")
    assert loaded == set(), (
        f"importing the worker loaded {len(loaded)} common.engine module(s): "
        f"{sorted(loaded)}. The fixture path must not pay for the engine."
    )


def test_importing_the_worker_does_not_pull_in_the_engine_seam():
    code = (
        f"import sys; import {WORKER_PACKAGE}.worker; "
        f"print('LOADED' if '{ENGINE_SEAM}' in sys.modules else 'ABSENT')"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    assert completed.stdout.strip() == "ABSENT"


# --------------------------------------------------------------- the positive
def test_the_engine_branch_really_does_load_the_engine():
    """The half that stops the boundary from being satisfied by a dead branch."""
    loaded = _modules_after(f"import {ENGINE_SEAM}")
    assert "common.engine.engine" in loaded, (
        "importing engine_worker did not load the engine, so the branch guarded by "
        "the deferred import cannot be doing anything."
    )
    # The whole point of deferring: this is a substantial graph, not one module.
    assert len(loaded) >= 10, sorted(loaded)


def test_the_engine_config_carries_no_engine_owned_type():
    """``EngineWorkerConfig`` is unpickled in the child *before* any engine import.

    A field defaulting to an engine-owned type would drag the package in through the
    dataclass definition itself, which neither the static nor the interpreter check
    above would attribute to the right cause.
    """
    from runtimes.intraday_options.worker import EngineWorkerConfig

    for spec in EngineWorkerConfig.__dataclass_fields__.values():
        annotation = str(spec.type)
        assert not annotation.startswith(FORBIDDEN_PREFIXES), (
            f"EngineWorkerConfig.{spec.name} is annotated {annotation!r}, which is "
            "an engine-owned type; keep every field primitive."
        )
