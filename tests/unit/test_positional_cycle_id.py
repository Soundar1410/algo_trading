"""``cycle_id_for`` — the one, generic, durable cycle identity every
positional binding uses (Phase 6A correction). Pure-function coverage; the
real-engine round trip and restart proofs live in
``tests/integration/test_weekly_delta_neutral_restart.py``.
"""

from __future__ import annotations

from common.config.models import ExecutionMode
from common.engine.positional.positional_models import cycle_id_for


def _id(**overrides: object) -> str:
    kwargs: dict[str, object] = {
        "runtime_id": "positional_options",
        "strategy_id": "weekly_delta_neutral",
        "execution_mode": ExecutionMode.PAPER,
        "underlying": "NIFTY",
        "resolved_expiry_date": "2026-08-26",
    }
    kwargs.update(overrides)
    return cycle_id_for(**kwargs)  # type: ignore[arg-type]


def test_deterministic_for_the_same_five_inputs() -> None:
    assert _id() == _id()


def test_paper_and_live_never_collide() -> None:
    assert _id(execution_mode=ExecutionMode.PAPER) != _id(execution_mode=ExecutionMode.LIVE)


def test_different_runtimes_never_collide() -> None:
    assert _id(runtime_id="positional_options") != _id(runtime_id="another_runtime_group")


def test_different_underlyings_never_collide() -> None:
    assert _id(underlying="NIFTY") != _id(underlying="BANKNIFTY")


def test_different_strategies_never_collide() -> None:
    assert _id(strategy_id="weekly_delta_neutral") != _id(strategy_id="another_strategy")


def test_different_expiries_never_collide() -> None:
    assert _id(resolved_expiry_date="2026-08-26") != _id(resolved_expiry_date="2026-09-02")


def test_an_embedded_colon_never_collides_with_a_differently_divided_split() -> None:
    """The percent-encoding proof: without it, ``underlying="A:B", expiry="C"``
    and ``underlying="A", expiry="B:C"`` would join to the identical raw
    string. With it, the separator is only ever the joiner, never data."""
    one = _id(underlying="A:B", resolved_expiry_date="C")
    two = _id(underlying="A", resolved_expiry_date="B:C")
    assert one != two


def test_a_colon_inside_a_component_is_percent_encoded_not_stripped() -> None:
    encoded = _id(underlying="NIFTY:WEEKLY")
    assert "NIFTY:WEEKLY" not in encoded
    assert "NIFTY%3AWEEKLY" in encoded


def test_returns_a_plain_string() -> None:
    assert isinstance(_id(), str)
