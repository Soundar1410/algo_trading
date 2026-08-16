"""Negative space: ``weekly_delta_neutral`` must never appear as a code
branch in generic infrastructure — the positional counterpart of
``test_no_straddle_920_branches.py``.

Spec section 14.1 / this repo's CLAUDE.md: the strategy package and its own
configuration may name ``weekly_delta_neutral``; generic infrastructure
(``common/engine/positional/``, ``runtimes/positional_options/``,
``dashboards/data/positional.py``) must route by ``CycleAction``, generic
leg role, capability flag, or ``strategy_id`` as data — never as a literal
naming this one strategy. Enforced by reading source text, the same
technique the straddle_920 sibling test uses.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Generic positional infrastructure this strategy's identity must never
#: leak into as a literal comparison/branch.
GENERIC_TARGETS: tuple[Path, ...] = (
    REPO_ROOT / "common" / "engine" / "positional" / "positional_models.py",
    REPO_ROOT / "common" / "engine" / "positional" / "positional_engine.py",
    REPO_ROOT / "common" / "engine" / "positional" / "positional_state.py",
    REPO_ROOT / "common" / "engine" / "positional" / "positional_strategy.py",
    REPO_ROOT / "common" / "engine" / "positional" / "lifecycle.py",
    REPO_ROOT / "common" / "engine" / "adapter_feed.py",
    REPO_ROOT / "runtimes" / "positional_options" / "supervisor.py",
    REPO_ROOT / "runtimes" / "positional_options" / "worker.py",
    REPO_ROOT / "runtimes" / "positional_options" / "__main__.py",
    REPO_ROOT / "runtimes" / "positional_options" / "config_adapter.py",
    REPO_ROOT / "runtimes" / "positional_options" / "positional_multi_leg_engine_worker.py",
    REPO_ROOT / "common" / "execution" / "repository.py",
    REPO_ROOT / "common" / "execution" / "lifecycle.py",
    REPO_ROOT / "common" / "greeks",
    REPO_ROOT / "common" / "market_data" / "chain_view.py",
    REPO_ROOT / "common" / "market_data" / "option_chain.py",
    REPO_ROOT / "common" / "market_data" / "dhan_option_chain.py",
    REPO_ROOT / "common" / "market_data" / "dhan_margin.py",
    REPO_ROOT / "common" / "margin",
    REPO_ROOT / "dashboards" / "data" / "positional.py",
    REPO_ROOT / "dashboards" / "positional_options.py",
)

#: A literal naming this strategy — deliberately narrow (the exact
#: strategy_id string), matching test_no_straddle_920_branches.py's own
#: reasoning for why "weekly_delta_neutral" alone would be too broad.
_LITERAL_RE = re.compile(r"""["']weekly_delta_neutral["']""")


def _python_files(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    if target.is_dir():
        return sorted(target.glob("*.py"))
    return []


def test_no_generic_module_contains_the_strategy_id_as_a_literal() -> None:
    offenders: dict[str, list[int]] = {}
    for target in GENERIC_TARGETS:
        for path in _python_files(target):
            lines = path.read_text(encoding="utf-8").splitlines()
            hits = [i + 1 for i, line in enumerate(lines) if _LITERAL_RE.search(line)]
            if hits:
                offenders[str(path.relative_to(REPO_ROOT))] = hits

    assert offenders == {}, (
        f"generic infrastructure names 'weekly_delta_neutral' as a literal at: "
        f"{offenders}. Route by CycleAction, a generic leg role, a capability "
        "flag, or strategy_id as data instead — never as a branch naming this "
        "one strategy."
    )


def _has_strategy_id_equality_branch(source: str) -> list[int]:
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
    return suspicious


def test_positional_engine_has_no_if_strategy_id_branch() -> None:
    path = REPO_ROOT / "common" / "engine" / "positional" / "positional_engine.py"
    suspicious = _has_strategy_id_equality_branch(path.read_text(encoding="utf-8"))
    assert suspicious == [], (
        f"{path.relative_to(REPO_ROOT)} appears to branch on a strategy id at "
        f"line(s) {suspicious}; PositionalMultiLegEngine must stay generic, "
        "hosting any positional strategy, not just weekly_delta_neutral "
        "(spec section 14.1)."
    )


def test_the_strategy_package_is_the_only_place_the_id_naturally_appears() -> None:
    """A control, not a violation check: confirms the literal genuinely
    exists somewhere sensible, so the negative check above is proven capable
    of finding it rather than passing vacuously."""
    strategy_file = (
        REPO_ROOT
        / "strategies"
        / "positional_options"
        / "weekly_delta_neutral"
        / "strategy.py"
    )
    config_file = REPO_ROOT / "config" / "strategies" / "weekly_delta_neutral.yaml"
    assert _LITERAL_RE.search(strategy_file.read_text(encoding="utf-8"))
    assert '"weekly_delta_neutral"' not in config_file.read_text(encoding="utf-8")
    assert "strategy_id: weekly_delta_neutral" in config_file.read_text(encoding="utf-8")
