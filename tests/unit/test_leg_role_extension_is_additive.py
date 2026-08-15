"""``LegRole`` gained four positional-engine members (``SHORT_CALL``,
``SHORT_PUT``, ``HEDGE_CALL``, ``HEDGE_PUT``) on the weekly-delta-neutral
branch — one shared, persistence-facing enum, extended additively, never a
second parallel one (spec review correction 3). This file pins the half of
that claim that a functional test elsewhere cannot: that ``straddle_920``'s
own engine machinery is *structurally* unaware the new members exist, not
merely untested against them.
"""

from __future__ import annotations

from common.engine import multi_leg_engine, multi_leg_state
from common.engine.multi_leg_models import LegRole
from common.models import OptionType


def test_the_three_original_members_keep_their_exact_values():
    assert LegRole.CE.value == "CE"
    assert LegRole.PE.value == "PE"
    assert LegRole.GENERIC.value == "GENERIC"


def test_the_four_new_members_exist_with_the_documented_values():
    assert LegRole.SHORT_CALL.value == "SHORT_CALL"
    assert LegRole.SHORT_PUT.value == "SHORT_PUT"
    assert LegRole.HEDGE_CALL.value == "HEDGE_CALL"
    assert LegRole.HEDGE_PUT.value == "HEDGE_PUT"


def test_multi_leg_engines_own_role_mapping_is_unchanged_by_the_extension():
    """straddle_920's engine must remain structurally incapable of resolving
    a positional-only role — not merely never asked to."""
    assert multi_leg_engine._ROLE_TO_OPTION_TYPE == {
        LegRole.CE: OptionType.CE,
        LegRole.PE: OptionType.PE,
    }
    for new_role in (
        LegRole.SHORT_CALL,
        LegRole.SHORT_PUT,
        LegRole.HEDGE_CALL,
        LegRole.HEDGE_PUT,
        LegRole.GENERIC,
    ):
        assert new_role not in multi_leg_engine._ROLE_TO_OPTION_TYPE


def test_multi_leg_states_own_role_mapping_is_unchanged_by_the_extension():
    assert multi_leg_state._ROLE_TO_OPTION_TYPE == {
        LegRole.CE: OptionType.CE,
        LegRole.PE: OptionType.PE,
    }
    for new_role in (
        LegRole.SHORT_CALL,
        LegRole.SHORT_PUT,
        LegRole.HEDGE_CALL,
        LegRole.HEDGE_PUT,
        LegRole.GENERIC,
    ):
        assert new_role not in multi_leg_state._ROLE_TO_OPTION_TYPE


def test_strategy_legs_migration_0009_check_constraint_is_untouched():
    """Migration 0009 is already-shipped; it must keep its original, narrower
    vocabulary — the new roles belong only to migration 0010's
    strategy_cycle_legs table."""
    from common.persistence.migrations import VERSIONS_DIR

    text = (VERSIONS_DIR / "0009_multi_leg_baskets.sql").read_text(encoding="utf-8")
    assert "CHECK (leg_role IN ('CE', 'PE', 'GENERIC'))" in text
    assert "SHORT_CALL" not in text
    assert "SHORT_PUT" not in text
    assert "HEDGE_CALL" not in text
    assert "HEDGE_PUT" not in text
