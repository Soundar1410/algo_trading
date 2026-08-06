"""``RiskManager``'s Phase 6 Part 2 surface: ``snapshot``/``restore``,
``stop_price``/``target_price``, and the widened ``new_position(entry_price=...)``.

No concrete risk manager ships in this repository yet (Phase 9 owns the first
one), so every property here is proven against local, test-only doubles rather
than against production code — mirroring the plan's own reasoning: "mechanism
proven by a stateful test double, ready for Phase 9's real risk manager."
"""

from __future__ import annotations

from typing import Any

from common.engine.risk import RiskManager
from common.models import ExitReason


class _MinimalRiskManager(RiskManager):
    """The bare abstract contract, nothing more — proves every default."""

    name = "minimal"

    def reset(self) -> None:
        pass

    def new_position(self, lots: int = 1, *, entry_price: float | None = None) -> None:
        pass

    def on_pnl(self, pnl: float) -> ExitReason | None:
        return None

    @property
    def state(self) -> Any:
        return {}


class _PriceLevelRiskManager(RiskManager):
    """A stateful double that *can* report absolute levels, unlike anything in
    this repository today — the producer the negative-control test in
    ``test_engine_worker_restart.py`` proves does not yet exist in production."""

    name = "price_level"

    def __init__(self, cfg: Any = None, *, stop_offset: float = 10.0) -> None:
        super().__init__(cfg)
        self._stop_offset = stop_offset
        self._entry_price: float | None = None
        self._armed_lots = 0

    def reset(self) -> None:
        self._entry_price = None
        self._armed_lots = 0

    def new_position(self, lots: int = 1, *, entry_price: float | None = None) -> None:
        self._armed_lots = lots
        self._entry_price = entry_price

    def on_pnl(self, pnl: float) -> ExitReason | None:
        return None

    @property
    def state(self) -> Any:
        return {"entry_price": self._entry_price, "armed_lots": self._armed_lots}

    @property
    def stop_price(self) -> float | None:
        return None if self._entry_price is None else self._entry_price - self._stop_offset

    def snapshot(self) -> dict[str, Any]:
        if self._entry_price is None:
            return {}
        return {"entry_price": self._entry_price, "armed_lots": self._armed_lots}

    def restore(self, data: dict[str, Any]) -> None:
        entry_price = data.get("entry_price")
        if isinstance(entry_price, int | float):
            self._entry_price = float(entry_price)
        lots = data.get("armed_lots")
        if isinstance(lots, int):
            self._armed_lots = lots


# ------------------------------------------------------------------- defaults
def test_default_stop_and_target_are_none():
    rm = _MinimalRiskManager(cfg=None)
    assert rm.stop_price is None
    assert rm.target_price is None


def test_default_snapshot_and_restore_are_no_ops():
    rm = _MinimalRiskManager(cfg=None)
    assert rm.snapshot() == {}
    rm.restore({"whatever": "this must not raise"})  # no-op, no exception


def test_new_position_accepts_optional_entry_price():
    """The widened signature: existing callers that omit entry_price still work,
    and a caller that supplies it does not raise."""
    rm = _MinimalRiskManager(cfg=None)
    rm.new_position(2)  # old call shape, unchanged
    rm.new_position(2, entry_price=105.5)  # Phase 6 Part 2 shape


# -------------------------------------------------- a stateful double, proven
def test_price_level_manager_reports_none_before_a_position_is_armed():
    rm = _PriceLevelRiskManager()
    assert rm.stop_price is None


def test_price_level_manager_reports_stop_price_once_armed():
    rm = _PriceLevelRiskManager(stop_offset=10.0)
    rm.new_position(1, entry_price=100.0)
    assert rm.stop_price == 90.0


def test_price_level_manager_snapshot_and_restore_round_trip():
    rm = _PriceLevelRiskManager(stop_offset=10.0)
    rm.new_position(3, entry_price=100.0)
    snapshot = rm.snapshot()
    assert snapshot == {"entry_price": 100.0, "armed_lots": 3}

    restarted = _PriceLevelRiskManager(stop_offset=10.0)
    restarted.reset()
    restarted.restore(snapshot)

    assert restarted.stop_price == 90.0
    assert restarted.state == {"entry_price": 100.0, "armed_lots": 3}


def test_snapshot_is_empty_before_arming():
    rm = _PriceLevelRiskManager()
    assert rm.snapshot() == {}


def test_restore_with_foreign_data_does_not_raise():
    rm = _PriceLevelRiskManager()
    rm.restore({})
    rm.restore({"entry_price": "not-a-number", "unexpected": True})
    assert rm.stop_price is None  # untouched — the bad value was ignored
