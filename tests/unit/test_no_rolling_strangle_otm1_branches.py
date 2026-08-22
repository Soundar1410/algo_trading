"""Negative space: ``rolling_strangle_otm1`` must never appear as a code
branch in generic infrastructure.

The strategy's own package and its own configuration file may name it;
generic infrastructure must route by ``EngineKind``, ``parameters.
strategy_ref``, capability flags, basket actions and ``strategy_id`` **as
data** (a config lookup, a WHERE-clause parameter) — never as a literal
naming this one strategy. Enforced by reading source text rather than
trusting review, exactly as ``tests/unit/test_no_straddle_920_branches.py``
and ``tests/unit/test_no_supertrend_buy_1_1p2_branches.py`` already do.

This file follows the more thorough (later) of the two existing patterns —
``test_no_supertrend_buy_1_1p2_branches.py``'s recursive ``dashboards``/
``orchestration``/``scripts`` walk — combined with straddle_920's own
``multi_leg_*`` module list, since this strategy (like straddle_920) runs on
the generic multi-leg engine rather than the single-leg one.

The whole point of Phase 4 is that it needed **no** shared-code change
beyond the three generic completions already made, reviewed and regression-
tested in Phase 3 (``MultiLegEngine._apply_state_commit``, the ``ENTER_
BASKET`` lot-size cross-check, and ``_maybe_record_replacement_filled`` —
none of which reference this or any other strategy by name): the config
adapter resolves ``parameters.strategy_ref`` generically, the supervisor
allocates a tick/control channel from ``WorkerConfig.requires_tick_channel``
alone, and the engine already owns claim/reservation/reconciliation
machinery shared with ``straddle_920``. This file is what keeps that true.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Generic infrastructure this strategy's identity must never leak into as a
#: literal comparison/branch. Straddle_920's own multi_leg_* module list,
#: since this strategy runs on the same generic multi-leg engine, plus
#: supertrend_buy_1_1p2's later, broader recursive dashboards/orchestration/
#: scripts coverage.
GENERIC_TARGETS: tuple[Path, ...] = (
    REPO_ROOT / "common" / "engine" / "engine.py",
    REPO_ROOT / "common" / "engine" / "multi_leg_engine.py",
    REPO_ROOT / "common" / "engine" / "multi_leg_models.py",
    REPO_ROOT / "common" / "engine" / "multi_leg_strategy.py",
    REPO_ROOT / "common" / "engine" / "multi_leg_state.py",
    REPO_ROOT / "common" / "engine" / "selection.py",
    REPO_ROOT / "common" / "engine" / "daily_guard.py",
    REPO_ROOT / "common" / "engine" / "session.py",
    REPO_ROOT / "common" / "engine" / "square_off.py",
    REPO_ROOT / "runtimes" / "intraday_options",
    REPO_ROOT / "common" / "feed" / "hub.py",
    REPO_ROOT / "common" / "broker" / "factory.py",
    REPO_ROOT / "common" / "broker" / "paper.py",
    REPO_ROOT / "common" / "execution" / "repository.py",
    REPO_ROOT / "common" / "execution" / "lifecycle.py",
    REPO_ROOT / "common" / "reconciliation",
    REPO_ROOT / "common" / "risk",
    REPO_ROOT / "orchestration",
    REPO_ROOT / "dashboards",
    REPO_ROOT / "scripts",
)

#: A literal naming this strategy — the identity a generic file must never
#: branch on. Deliberately the exact quoted strategy_id, not "rolling" or
#: "strangle" alone, which would flag legitimate, unrelated prose.
_LITERAL_RE = re.compile(r"""["']rolling_strangle_otm1["']""")


def _all_python_files(root: Path) -> list[Path]:
    """Every ``.py`` file under ``root``, recursively for a directory target
    (covering e.g. ``dashboards/pages``, ``orchestration/auto_start``) or
    just the one file for a file target."""
    if root.is_file():
        return [root]
    if root.is_dir():
        return sorted(root.rglob("*.py"))
    return []  # a target that doesn't exist yet is not this test's problem


def _strategy_id_equality_lines(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    suspicious: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            if not isinstance(op, ast.Eq):
                continue
            if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
                left_src = ast.dump(node.left).lower()
                if "strategy" in left_src and "id" in left_src:
                    suspicious.append(node.lineno)
    return suspicious


def test_no_generic_module_contains_the_strategy_id_as_a_literal():
    offenders: dict[str, list[int]] = {}
    for target in GENERIC_TARGETS:
        for path in _all_python_files(target):
            lines = path.read_text(encoding="utf-8").splitlines()
            hits = [i + 1 for i, line in enumerate(lines) if _LITERAL_RE.search(line)]
            if hits:
                offenders[str(path.relative_to(REPO_ROOT))] = hits

    assert offenders == {}, (
        f"generic infrastructure names 'rolling_strangle_otm1' as a literal at: "
        f"{offenders}. Route by EngineKind, parameters.strategy_ref, a capability "
        "flag, a basket action, or strategy_id as data (a config lookup / "
        "WHERE-clause parameter) instead — never as a branch naming this one "
        "strategy."
    )


def test_the_multi_leg_engine_still_has_no_if_strategy_id_branch():
    """The invariant this port must not break: ``MultiLegEngine`` — the
    engine this strategy actually runs on, shared with ``straddle_920`` —
    stays generic. Reads the AST for an equality comparison naming *any*
    strategy at all, not just this one."""
    path = REPO_ROOT / "common" / "engine" / "multi_leg_engine.py"
    suspicious = _strategy_id_equality_lines(path)
    assert suspicious == [], (
        f"common/engine/multi_leg_engine.py appears to branch on a strategy id at "
        f"line(s) {suspicious}; MultiLegEngine must stay generic, hosting any "
        "multi-leg strategy, not just rolling_strangle_otm1."
    )


def test_the_single_leg_engine_is_also_untouched_and_branch_free():
    """Architecture mapping (spec section 12): "The implementation must not
    modify single-leg TradingEngine behaviour." Same AST check as the
    multi-leg one, on the sibling this strategy never runs on."""
    suspicious = _strategy_id_equality_lines(REPO_ROOT / "common" / "engine" / "engine.py")
    assert suspicious == [], (
        f"common/engine/engine.py appears to branch on a strategy id at line(s) "
        f"{suspicious}; TradingEngine must stay generic and untouched."
    )


def test_the_intraday_worker_and_adapter_have_no_if_strategy_id_branch():
    """The other place a per-strategy special case would be tempting: the
    config adapter that chooses an engine and the worker that constructs
    it."""
    offenders: dict[str, list[int]] = {}
    for name in (
        "config_adapter.py",
        "engine_worker.py",
        "multi_leg_engine_worker.py",
        "worker.py",
        "supervisor.py",
    ):
        path = REPO_ROOT / "runtimes" / "intraday_options" / name
        lines = _strategy_id_equality_lines(path)
        if lines:
            offenders[name] = lines
    assert offenders == {}, (
        f"the intraday_options runtime appears to branch on a strategy id at "
        f"{offenders}; routing must stay data-driven (EngineKind + strategy_ref)."
    )


def test_the_strategy_package_is_the_only_place_the_id_naturally_appears():
    """A control, not a violation check: confirms the literal genuinely
    exists somewhere sensible (the strategy's own package), so the negative
    check above is proven capable of finding it rather than passing
    vacuously because the regex never matches anything in this repository."""
    strategy_file = (
        REPO_ROOT / "strategies" / "intraday_options" / "rolling_strangle_otm1" / "strategy.py"
    )
    config_file = REPO_ROOT / "config" / "strategies" / "rolling_strangle_otm1.yaml"
    assert _LITERAL_RE.search(strategy_file.read_text(encoding="utf-8"))
    assert '"rolling_strangle_otm1"' not in config_file.read_text(encoding="utf-8")
    assert "strategy_id: rolling_strangle_otm1" in config_file.read_text(encoding="utf-8")


def test_the_new_engine_completions_reference_no_strategy_by_name():
    """Phase 3's three generic engine completions
    (``_apply_state_commit``/``_expire_replacements``, the ``ENTER_BASKET``
    lot-size cross-check, ``_maybe_record_replacement_filled``) must each
    read as ordinary generic engine code, not as this strategy's own
    special case, however local their motivating bug report was."""
    source = (REPO_ROOT / "common" / "engine" / "multi_leg_engine.py").read_text(
        encoding="utf-8"
    )
    for method in (
        "_apply_state_commit",
        "_expire_replacements",
        "_maybe_record_replacement_filled",
    ):
        assert f"def {method}" in source
    assert not _LITERAL_RE.search(source)
