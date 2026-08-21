"""Negative space: ``supertrend_buy_1_1p2`` must never appear as a code branch in
generic infrastructure.

The strategy's own package and its own configuration file may name it; generic
infrastructure must route by ``EngineKind``, ``strategy_ref``, capability flags and
``strategy_id`` **as data** (a config lookup, a WHERE-clause parameter) — never as a
literal naming this one strategy. Enforced by reading source text rather than trusting
review, exactly as ``tests/unit/test_no_straddle_920_branches.py`` already does for the
multi-leg port.

The whole point of this port is that it needed **no** shared-code change: the config
adapter resolves ``parameters.strategy_ref`` generically, the supervisor allocates a
tick/control channel from ``WorkerConfig.requires_tick_channel`` alone, the engine
already owns reversal/session/daily-risk, and the dashboard discovers strategies from
``config/strategies/*.yaml``. This file is what keeps that true.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Generic infrastructure this strategy's identity must never leak into as a literal
#: comparison/branch. Each is a file or a directory; every ``.py`` file under a
#: directory entry is checked. Deliberately the same list
#: ``test_no_straddle_920_branches.py`` guards, plus the single-leg path this strategy
#: actually runs on (the option selector, the warm-up manager and the strategy base
#: class), which the multi-leg port had no reason to include.
GENERIC_TARGETS: tuple[Path, ...] = (
    REPO_ROOT / "common" / "engine" / "engine.py",
    REPO_ROOT / "common" / "engine" / "strategy.py",
    REPO_ROOT / "common" / "engine" / "selection.py",
    REPO_ROOT / "common" / "engine" / "daily_guard.py",
    REPO_ROOT / "common" / "engine" / "session.py",
    REPO_ROOT / "common" / "engine" / "square_off.py",
    REPO_ROOT / "common" / "engine" / "state_payload.py",
    REPO_ROOT / "common" / "engine" / "multi_leg_engine.py",
    REPO_ROOT / "common" / "indicators",
    REPO_ROOT / "common" / "exit",
    REPO_ROOT / "common" / "warmup",
    REPO_ROOT / "runtimes" / "intraday_options",
    REPO_ROOT / "common" / "feed" / "hub.py",
    REPO_ROOT / "common" / "broker" / "factory.py",
    REPO_ROOT / "common" / "broker" / "paper.py",
    REPO_ROOT / "common" / "execution" / "repository.py",
    REPO_ROOT / "common" / "execution" / "lifecycle.py",
    REPO_ROOT / "common" / "reconciliation",
    REPO_ROOT / "common" / "risk",
    # Phase 4 widening: the dashboard, script and orchestration checks below no
    # longer sample a handful of files — every .py file under each of these three
    # top-level packages is walked (recursively, including subpackages), so the
    # dashboard's page shims and every operator script are covered without having
    # to name each one.
    REPO_ROOT / "orchestration",
    REPO_ROOT / "dashboards",
    REPO_ROOT / "scripts",
)


def _all_python_files(root: Path) -> list[Path]:
    """Every ``.py`` file under ``root``, recursively — unlike :func:`_python_files`
    below (kept for the two file-or-shallow-directory targets above), this walks
    subpackages such as ``dashboards/pages`` and ``orchestration/auto_start``."""
    if root.is_file():
        return [root]
    return sorted(root.rglob("*.py"))

#: A literal naming this strategy — the identity a generic file must never branch on.
#: Deliberately narrow (the exact quoted strategy_id) rather than "supertrend" alone,
#: which would flag ``common/indicators/supertrend.py`` and ``common/exit/
#: supertrend_exit.py``: those are generic components this strategy *uses*, and their
#: existence is the opposite of a violation.
_LITERAL_RE = re.compile(r"""["']supertrend_buy_1_1p2["']""")


def _python_files(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    if target.is_dir():
        return sorted(target.glob("*.py"))
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
        f"generic infrastructure names 'supertrend_buy_1_1p2' as a literal at: "
        f"{offenders}. Route by EngineKind, parameters.strategy_ref, a capability "
        "flag, or strategy_id as data (a config lookup / WHERE-clause parameter) "
        "instead — never as a branch naming this one strategy."
    )


def test_the_single_leg_engine_still_has_no_if_strategy_id_branch():
    """The invariant this port must not break: ``TradingEngine`` — the engine this
    strategy actually runs on — stays generic. Reads the AST for an equality
    comparison naming *any* strategy at all, not just this one."""
    suspicious = _strategy_id_equality_lines(REPO_ROOT / "common" / "engine" / "engine.py")
    assert suspicious == [], (
        f"common/engine/engine.py appears to branch on a strategy id at line(s) "
        f"{suspicious}; TradingEngine must stay generic."
    )


def test_the_intraday_worker_and_adapter_have_no_if_strategy_id_branch():
    """The other place a per-strategy special case would be tempting: the config
    adapter that chooses an engine and the worker that constructs it."""
    offenders: dict[str, list[int]] = {}
    for name in ("config_adapter.py", "engine_worker.py", "worker.py", "supervisor.py"):
        path = REPO_ROOT / "runtimes" / "intraday_options" / name
        lines = _strategy_id_equality_lines(path)
        if lines:
            offenders[name] = lines
    assert offenders == {}, (
        f"the intraday_options runtime appears to branch on a strategy id at "
        f"{offenders}; routing must stay data-driven (EngineKind + strategy_ref)."
    )


def test_the_shared_supertrend_indicator_is_untouched_by_this_strategy():
    """Spec section 6 / section 15: the strategy reuses the canonical indicator and
    must not reimplement it, nor mention itself inside it.

    Also pins the specific seam this port deliberately did **not** move: the
    indicator's own ``warmup_requirement()`` still declares ``min_bars=self.period``.
    Raising the warm-up floor to 75 completed buckets is the *strategy's* decision, made in
    its own ``warmup_spec()``, so no other consumer of ``SuperTrend`` inherits it.
    """
    source = (REPO_ROOT / "common" / "indicators" / "supertrend.py").read_text(
        encoding="utf-8"
    )
    assert not _LITERAL_RE.search(source)
    assert "WarmupRequirement(min_bars=self.period, continuity_required=True)" in source


def _executable_source(path: Path) -> str:
    """``path``'s source with every comment and string literal removed.

    Prose that *explains* why a piece of indicator arithmetic lives in
    ``common/indicators`` rather than here is legitimate and expected — the check
    below is about copied *code*, so docstrings and comments must not be able to trip
    it (the same reasoning that keeps ``_LITERAL_RE`` narrow).
    """
    import tokenize

    kept: list[str] = []
    with path.open("rb") as handle:
        for token in tokenize.tokenize(handle.readline):
            if token.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            kept.append(token.string)
    return " ".join(kept)


def test_the_strategy_does_not_reimplement_supertrend_or_the_combined_exit():
    """Spec section 15: "Do not duplicate SuperTrend or combined-candle-exit logic
    inside the strategy." Proven mechanically, not by eye."""
    path = (
        REPO_ROOT
        / "strategies"
        / "intraday_options"
        / "supertrend_buy_1_1p2"
        / "strategy.py"
    )
    source = path.read_text(encoding="utf-8")
    assert "from common.indicators.supertrend import" in source
    assert "from common.exit.combined_candle_exit import CombinedCandleExit" in source

    code = _executable_source(path).lower()
    # The giveaways of a copied indicator or exit engine: band arithmetic, Wilder
    # smoothing, and a retracement/extreme comparison living in the strategy itself.
    for forbidden in (
        "hl2",
        "final_upper",
        "final_lower",
        "upper_basic",
        "lower_basic",
        "atr",
        "true_range",
        "retrace",
        "extreme",
        "trail_points",
    ):
        assert forbidden not in code, f"{forbidden!r} suggests copied logic in {path.name}"


def test_the_strategy_package_is_the_only_place_the_id_naturally_appears():
    """A control, not a violation check: confirms the literal genuinely exists
    somewhere sensible, so the negative check above is proven capable of finding it
    rather than passing vacuously because the regex never matches anything."""
    strategy_file = (
        REPO_ROOT
        / "strategies"
        / "intraday_options"
        / "supertrend_buy_1_1p2"
        / "strategy.py"
    )
    assert _LITERAL_RE.search(strategy_file.read_text(encoding="utf-8"))
