"""Permitted automated resolution actions (spec section 10, "Future
controlled-live resolution policies") — deliberately narrow, deliberately a
closed vocabulary enforced here in Python, not just by the
``reconciliation_mismatches.resolution_action`` CHECK constraint (which only
proves the vocabulary is closed, not that a given category may use a given
action).

Never auto-flatten a broker-only position. Never mark an order rejected
except from a positive, broker-confirmed rejection. Broker-evidence recovery
(:mod:`common.reconciliation.recovery`) calls
:func:`resolution_is_permitted` before applying or writing a resolution — a
category/action without positive evidence stays unresolved, alerted, and
blocking, which is the safe default.
"""

from __future__ import annotations

from common.models import OrderStatus

#: category -> the one automated action allowed for it, or None (must stay
#: alerted/blocked, never auto-resolved). Matches spec 2248-2259's examples
#: exactly: adopt a broker order found by correlation ID; update local
#: traded quantity from broker-confirmed fills; mark rejected only when
#: broker status confirms it; mark closed only when broker evidence proves
#: closure.
_PERMITTED_ACTION_BY_CATEGORY: dict[str, str | None] = {
    "MATCHED": None,  # nothing to resolve
    "LOCAL_ONLY": "adopt_broker_order",  # only once a broker record later appears
    "BROKER_ONLY": None,  # never auto-flattened — alert and block only
    "QUANTITY_MISMATCH": "update_traded_quantity",
    "SIDE_MISMATCH": None,  # too dangerous to guess which side is right
    "PRODUCT_MISMATCH": None,  # informational, needs an operator decision
    "PRICE_MISMATCH": None,  # informational only, spec's own tolerance note
    "LOCAL_OPEN_BROKER_CLOSED": "mark_closed",
    "LOCAL_CLOSED_BROKER_OPEN": None,  # never auto-flattened — alert and block only
    "UNKNOWN_ORDER": None,  # only the bounded verification policy may resolve this
    "DUPLICATE_CORRELATION": None,  # always requires operator investigation
}


def permitted_action_for(category: str) -> str | None:
    """The one automated action ``category`` may resolve through, or
    ``None`` if it must stay alerted and blocking."""
    if category not in _PERMITTED_ACTION_BY_CATEGORY:
        raise ValueError(f"unknown mismatch category {category!r}")
    return _PERMITTED_ACTION_BY_CATEGORY[category]


def can_mark_rejected(*, broker_status: OrderStatus | None) -> bool:
    """``mark_rejected`` may only ever be applied from a positive,
    broker-confirmed ``REJECTED`` status — never from absence, never from a
    lookup that merely failed to find the order (architecture report §4/§10:
    absence of evidence is not evidence of absence)."""
    return broker_status is OrderStatus.REJECTED


def can_mark_closed(*, broker_quantity: int | None) -> bool:
    """``mark_closed`` may only be applied when the broker's own position
    report proves closure (zero or absent quantity) — never inferred from
    the local side alone."""
    return broker_quantity is None or broker_quantity == 0


def resolution_is_permitted(
    category: str,
    action: str,
    *,
    broker_status: OrderStatus | None = None,
    broker_quantity: int | None = None,
) -> bool:
    """Validate an action together with the positive broker evidence it needs."""
    if action == "mark_rejected":
        return category == "UNKNOWN_ORDER" and can_mark_rejected(
            broker_status=broker_status
        )
    if action == "mark_closed":
        return (
            permitted_action_for(category) == action
            and can_mark_closed(broker_quantity=broker_quantity)
        )
    return permitted_action_for(category) == action
