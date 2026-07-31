"""How the ported exit registry is *wired* — the parts a tidy-up would break.

``test_exit_engines.py`` is the reference repository's own suite, ported with only
its import paths changed, and it covers what each engine decides. This file covers
what that suite cannot: the two documented deviations (D2 and D3) and the
no-runtime-dependency rule, all of which are properties of *this* repository's
port rather than of the original code.

The failure mode being guarded against is a quiet one. Nothing here breaks loudly
if `momentum_low_or_highest_close` is dropped from the registry or folded into the
composite map — strategies that never used it keep passing, and the one that needs
it simply stops exiting the way it was designed to.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.exit import available_exit_engines, build_exit_engine, get_exit_engine
from common.exit.base import BaseExit
from common.exit.composite import _KEY_TO_ENGINE, CompositeExit
from common.models import OrderSide, Side

REPO_ROOT = Path(__file__).resolve().parents[2]
PORTED_PACKAGES = ("common/exit", "common/indicators", "common/warmup", "common/utils")

#: The tenth policy (deviation D2), and the one the composite map deliberately
#: omits (deviation D3).
COMBINED = "momentum_low_or_highest_close"


# ------------------------------------------------------------------ D2: ten
def test_all_ten_policies_are_registered_not_the_nine_the_spec_lists():
    """Deviation D2: the spec names nine; the reference registers ten."""
    assert set(available_exit_engines()) == {
        "consecutive_reversal",
        "fixed_target",
        "highest_close",
        "momentum_close",
        "momentum_low",
        COMBINED,
        "stoploss",
        "supertrend",
        "time_exit",
        "trailing",
    }
    assert len(available_exit_engines()) == 10


# ----------------------------------------------------- D3: both wiring paths
def test_the_composite_map_deliberately_excludes_the_combined_exit():
    """Deviation D3, stated as an assertion rather than a comment.

    ``momentum_low_or_highest_close`` evaluates on the traded option's *own*
    premium candle stream, not the underlying's, so the reference wires it by
    direct instantiation instead of through config. Porting the composite map
    "completely" — the obvious tidy-up — would silently make it selectable by a
    config key it was never meant to answer to.
    """
    mapped_engines = {engine_name for engine_name, _priority in _KEY_TO_ENGINE.values()}
    assert COMBINED not in mapped_engines
    assert set(_KEY_TO_ENGINE) == {
        "stoploss",
        "target",
        "momentum_close",
        "momentum_low",
        "highest_close",
        "consecutive",
        "supertrend",
        "trailing",
        "square_off",
    }


def test_the_combined_exit_is_reachable_by_direct_instantiation():
    """The second wiring path: unreachable by config, reachable by name."""
    engine = get_exit_engine(COMBINED)
    assert isinstance(engine, BaseExit)
    assert engine.label == COMBINED.upper()


def test_no_config_block_can_select_the_combined_exit():
    """The other half of D3 — if this ever passes a COMBINED engine, the map moved."""

    cfg = SimpleNamespace(
        enabled=True,
        mode=COMBINED.upper(),  # even naming it as the mode must not select it
        settings={COMBINED: {"enabled": True}},
    )

    composite = build_exit_engine(cfg)
    assert isinstance(composite, CompositeExit)
    assert all(engine.label != COMBINED.upper() for engine in composite.engines)


def test_every_registered_engine_can_actually_be_built():
    """A name in the registry that cannot be instantiated is worse than absent."""
    for name in available_exit_engines():
        assert isinstance(get_exit_engine(name), BaseExit), name


# ------------------------------------------------- no runtime dependency
@pytest.mark.parametrize("package", PORTED_PACKAGES)
def test_ported_code_has_no_runtime_dependency_on_the_reference_repo(package: str):
    """CLAUDE.md's rule, enforced structurally rather than by review.

    A port that still imports ``framework.*`` is not a port — it is a symlink with
    extra steps, and it would break the moment the reference repository moves.
    """
    for path in sorted((REPO_ROOT / package).rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            for module in modules:
                root = module.split(".")[0]
                assert root not in {"framework", "strategies_v2", "Trading_Automation"}, (
                    f"{path.relative_to(REPO_ROOT)} imports {module!r} from the reference repo"
                )


# ------------------------------------------------------------ the side alias
def test_order_side_is_this_repository_s_side_not_a_second_enum():
    """The ported engines compare with ``is``; two enums would break silently.

    ``position.side is OrderSide.BUY`` is False for a `Side.BUY` if `OrderSide` is
    a distinct type — no error, just an exit rule that never fires.
    """
    assert OrderSide is Side
    assert OrderSide.BUY is Side.BUY
    assert [member.value for member in OrderSide] == ["BUY", "SELL"]
