"""Permitted resolution actions: a narrow, closed vocabulary, and no
automated path that flattens a broker-only position."""

from __future__ import annotations

import pytest

from common.models import OrderStatus
from common.reconciliation import (
    MISMATCH_CATEGORIES,
    can_mark_closed,
    can_mark_rejected,
    permitted_action_for,
    resolution_is_permitted,
)

_PERMITTED_VOCABULARY = frozenset(
    {"adopt_broker_order", "update_traded_quantity", "mark_rejected", "mark_closed"}
)


def test_no_automated_resolution_outside_the_permitted_four():
    for category in MISMATCH_CATEGORIES:
        action = permitted_action_for(category)
        assert action is None or action in _PERMITTED_VOCABULARY


def test_broker_only_position_is_alerted_and_blocked_not_flattened():
    """The single most important assertion in this file."""
    assert permitted_action_for("BROKER_ONLY") is None


def test_local_closed_broker_open_is_never_auto_flattened():
    assert permitted_action_for("LOCAL_CLOSED_BROKER_OPEN") is None


def test_side_mismatch_has_no_automated_resolution():
    """Too dangerous to guess which side is correct."""
    assert permitted_action_for("SIDE_MISMATCH") is None


def test_duplicate_correlation_always_requires_an_operator():
    assert permitted_action_for("DUPLICATE_CORRELATION") is None


def test_unknown_order_is_never_auto_resolved_inline():
    """Only the bounded verification/reconciliation policy may resolve it —
    never inline, never automatically, from this lookup table alone."""
    assert permitted_action_for("UNKNOWN_ORDER") is None


def test_quantity_mismatch_permits_updating_the_traded_quantity():
    assert permitted_action_for("QUANTITY_MISMATCH") == "update_traded_quantity"


def test_local_open_broker_closed_permits_marking_closed():
    assert permitted_action_for("LOCAL_OPEN_BROKER_CLOSED") == "mark_closed"


def test_unrecognised_category_raises():
    with pytest.raises(ValueError, match="unknown mismatch category"):
        permitted_action_for("NOT_A_REAL_CATEGORY")


# ------------------------------------------------------------ mark_rejected
def test_mark_rejected_requires_a_positive_broker_confirmation():
    assert can_mark_rejected(broker_status=OrderStatus.REJECTED)
    assert not can_mark_rejected(broker_status=None)
    assert not can_mark_rejected(broker_status=OrderStatus.UNKNOWN)
    assert not can_mark_rejected(broker_status=OrderStatus.CANCELLED)
    assert resolution_is_permitted(
        "UNKNOWN_ORDER", "mark_rejected", broker_status=OrderStatus.REJECTED
    )
    assert not resolution_is_permitted(
        "UNKNOWN_ORDER", "mark_rejected", broker_status=OrderStatus.UNKNOWN
    )


# -------------------------------------------------------------- mark_closed
def test_mark_closed_requires_broker_confirmed_zero_or_absent_quantity():
    assert can_mark_closed(broker_quantity=0)
    assert can_mark_closed(broker_quantity=None)
    assert not can_mark_closed(broker_quantity=75)
    assert not can_mark_closed(broker_quantity=-75)
