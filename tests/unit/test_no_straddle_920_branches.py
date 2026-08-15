"""Negative space: ``straddle_920`` must never appear as a code branch in
generic infrastructure.

Spec section 14.1 / this repo's CLAUDE.md: the strategy package and its own
configuration may name ``straddle_920``; generic infrastructure must route by
engine kind, capability flags, basket actions and ``strategy_id`` **as data**
(a config lookup, a WHERE-clause filter) — never as a literal naming this one
strategy. Enforced by reading source text rather than trusting review, the
same technique ``test_the_engine_module_installs_no_signal_handler`` and
``common/engine/engine.py``'s own "no framework.* import" tests already use
in this tree.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Generic infrastructure this strategy's identity must never leak into as a
#: literal comparison/branch. Each is a file or a directory; every ``.py``
#: file under a directory entry is checked.
GENERIC_TARGETS: tuple[Path, ...] = (
    REPO_ROOT / "common" / "engine" / "engine.py",
    REPO_ROOT / "common" / "engine" / "multi_leg_engine.py",
    REPO_ROOT / "common" / "engine" / "multi_leg_models.py",
    REPO_ROOT / "common" / "engine" / "multi_leg_strategy.py",
    REPO_ROOT / "common" / "engine" / "multi_leg_state.py",
    REPO_ROOT / "runtimes" / "intraday_options" / "supervisor.py",
    REPO_ROOT / "runtimes" / "intraday_options" / "worker.py",
    REPO_ROOT / "runtimes" / "intraday_options" / "engine_worker.py",
    REPO_ROOT / "runtimes" / "intraday_options" / "multi_leg_engine_worker.py",
    REPO_ROOT / "common" / "feed" / "hub.py",
    REPO_ROOT / "common" / "broker" / "factory.py",
    REPO_ROOT / "common" / "broker" / "paper.py",
    REPO_ROOT / "common" / "execution" / "repository.py",
    REPO_ROOT / "common" / "execution" / "lifecycle.py",
    REPO_ROOT / "common" / "reconciliation",
    REPO_ROOT / "common" / "risk" / "account_risk.py",
    REPO_ROOT / "common" / "risk" / "account_reservations.py",
    REPO_ROOT / "dashboards" / "data",
)

#: A literal naming this strategy — the identity a generic file must never
#: branch on. Deliberately narrow (the exact strategy_id string) rather than
#: "straddle" alone, which would also flag legitimate prose like a docstring
#: explaining *why* a piece of generic code exists (this file's own docstring
#: included).
_LITERAL_RE = re.compile(r"""["']straddle_920["']""")


def _python_files(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    if target.is_dir():
        return sorted(target.glob("*.py"))
    return []  # a target that doesn't exist yet is not this test's problem


def test_no_generic_module_contains_the_strategy_id_as_a_literal():
    offenders: dict[str, list[int]] = {}
    for target in GENERIC_TARGETS:
        for path in _python_files(target):
            lines = path.read_text(encoding="utf-8").splitlines()
            hits = [i + 1 for i, line in enumerate(lines) if _LITERAL_RE.search(line)]
            if hits:
                offenders[str(path.relative_to(REPO_ROOT))] = hits

    assert offenders == {}, (
        f"generic infrastructure names 'straddle_920' as a literal at: {offenders}. "
        "Route by EngineKind, a capability flag, a basket action, or strategy_id "
        "as data (a config lookup / WHERE-clause parameter) instead — never as a "
        "branch naming this one strategy."
    )


def test_trading_engine_still_has_no_if_strategy_id_branch():
    """The specific, original invariant this port must not break: the
    single-leg engine stays generic. Reads the AST for an equality comparison
    naming any strategy at all — the strongest form of "no per-strategy
    branch", not just this one strategy's id."""
    import ast

    source = (REPO_ROOT / "common" / "engine" / "engine.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    suspicious: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            if not isinstance(op, ast.Eq):
                continue
            if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
                left_src = ast.dump(node.left)
                if "strategy" in left_src.lower() and "id" in left_src.lower():
                    suspicious.append(node.lineno)

    assert suspicious == [], (
        f"common/engine/engine.py appears to branch on a strategy id at line(s) "
        f"{suspicious}; TradingEngine must stay generic (spec section 14.1)."
    )


def test_multi_leg_engine_also_has_no_if_strategy_id_branch():
    import ast

    source = (REPO_ROOT / "common" / "engine" / "multi_leg_engine.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)

    suspicious: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            if not isinstance(op, ast.Eq):
                continue
            if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
                left_src = ast.dump(node.left)
                if "strategy" in left_src.lower() and "id" in left_src.lower():
                    suspicious.append(node.lineno)

    assert suspicious == [], (
        f"common/engine/multi_leg_engine.py appears to branch on a strategy id at "
        f"line(s) {suspicious}; MultiLegEngine must stay generic, hosting any "
        "multi-leg strategy, not just straddle_920 (spec section 14.1)."
    )


def test_the_strategy_package_is_the_only_place_the_id_naturally_appears():
    """A control, not a violation check: confirms the literal genuinely exists
    somewhere sensible (the strategy's own package and config), so the
    negative check above is proven capable of finding it rather than passing
    vacuously because the regex never matches anything in this repository."""
    strategy_file = (
        REPO_ROOT
        / "strategies"
        / "intraday_options"
        / "straddle_920"
        / "strategy.py"
    )
    config_file = REPO_ROOT / "config" / "strategies" / "straddle_920.yaml"
    assert _LITERAL_RE.search(strategy_file.read_text(encoding="utf-8"))
    assert '"straddle_920"' not in config_file.read_text(encoding="utf-8")
    assert "strategy_id: straddle_920" in config_file.read_text(encoding="utf-8")
