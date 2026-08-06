"""Composite exit engine + config-driven factory.

A strategy holds a single exit engine but usually wants several rules active at
once (e.g. a directional candle exit *plus* a stop-loss backstop *plus* a
square-off). :class:`CompositeExit` ORs a fixed-priority list of child engines:
``should_exit`` returns ``True`` as soon as any child fires, and records which
child fired (:attr:`last_fired`) so the caller can log/label the exit reason.

:func:`build_exit_engine` turns the ``exit:`` config block into that composite —
changing behaviour is a config edit, no code change:

    exit:
      enabled: true
      mode: MOMENTUM_CLOSE          # the primary directional engine (implies enabled)
      momentum_close: {enabled: true}
      stoploss: {enabled: true, points: 300}
      square_off: {enabled: true, time: "15:15"}

Each sub-block is a registered engine's parameters; an engine participates when
its ``enabled: true`` or when ``mode`` names it. Children are always evaluated in
a fixed priority (protective stops first, then directional, then trailing/time)
so the reported reason is deterministic regardless of config order.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from common.logging import get_logger

from .base import BaseExit, get_exit_engine

log = get_logger(__name__)

# config sub-block key -> (registered engine name, evaluation priority).
# Lower priority number is evaluated first, so its reason wins on a tie.
_KEY_TO_ENGINE: dict[str, tuple[str, int]] = {
    "stoploss": ("stoploss", 10),
    "target": ("fixed_target", 20),
    "momentum_close": ("momentum_close", 30),
    "momentum_low": ("momentum_low", 40),
    "highest_close": ("highest_close", 50),
    "consecutive": ("consecutive_reversal", 60),
    "supertrend": ("supertrend", 70),
    "trailing": ("trailing", 80),
    "square_off": ("time_exit", 90),
}

# `exit.mode` value -> the config sub-block key it selects (mode implies enabled).
_MODE_TO_KEY: dict[str, str] = {
    "MOMENTUM_CLOSE": "momentum_close",
    "MOMENTUM_LOW": "momentum_low",
    "HIGHEST_CLOSE": "highest_close",
    "CONSECUTIVE_REVERSAL": "consecutive",
    "CONSECUTIVE": "consecutive",
    "SUPERTREND": "supertrend",
    "FIXED_TARGET": "target",
    "TARGET": "target",
    "TRAILING": "trailing",
    "STOPLOSS": "stoploss",
    "TIME_EXIT": "square_off",
}


class CompositeExit(BaseExit):
    """Evaluates its child engines in priority order; first True wins."""

    label = "COMPOSITE"

    def __init__(self, engines: list[BaseExit]) -> None:
        super().__init__({})
        self._engines = list(engines)
        self.last_fired: BaseExit | None = None

    @property
    def engines(self) -> list[BaseExit]:
        return list(self._engines)

    def reset(self) -> None:
        self.last_fired = None
        for e in self._engines:
            e.reset()

    def snapshot(self) -> dict[str, Any]:
        """Each child's own snapshot, keyed by its ``label``.

        ``build_exit_engine`` selects at most one instance per registered engine
        name (``_KEY_TO_ENGINE``), so ``label`` is a stable, collision-free key —
        no index or synthetic id needed. A child with nothing to snapshot
        (``{}``) is omitted rather than stored empty, keeping "nothing to
        restore" unambiguous at every level.
        """
        return {e.label: snap for e in self._engines if (snap := e.snapshot())}

    def restore(self, data: dict[str, Any]) -> None:
        """Reapply each child's snapshot by label. Unknown labels are skipped.

        A label present in ``data`` but absent from today's engines means the
        config changed since the snapshot was written (an engine was removed or
        renamed) — logged, not raised, per :meth:`BaseExit.restore`'s contract.
        """
        by_label = {e.label: e for e in self._engines}
        for label, child_data in data.items():
            engine = by_label.get(label)
            if engine is None:
                log.warning(
                    "exit-state snapshot names %r, which is not among today's "
                    "configured engines (%s); skipping it",
                    label,
                    sorted(by_label),
                )
                continue
            if isinstance(child_data, dict):
                engine.restore(child_data)

    def should_exit(
        self,
        position: Any,
        candle: Any,
        candle_history: list[Any],
        indicators: dict[str, Any],
        strategy_config: Any,
        *,
        timestamp: datetime | None = None,
    ) -> bool:
        # Advance every engine's per-candle state (so a stateful engine that
        # isn't the first to fire still tracks the candle), then decide.
        fired: BaseExit | None = None
        for engine in self._engines:
            if (
                engine.should_exit(
                    position,
                    candle,
                    candle_history,
                    indicators,
                    strategy_config,
                    timestamp=timestamp,
                )
                and fired is None
            ):
                fired = engine
        self.last_fired = fired
        return fired is not None


def build_exit_engine(exit_cfg: Any) -> CompositeExit | None:
    """Build a :class:`CompositeExit` from an ``ExitConfig``.

    Returns ``None`` when exits are disabled (``exit.enabled: false`` or no
    ``exit:`` block), so a strategy can cheaply skip the whole subsystem and fall
    back to its per-tick risk manager alone.
    """
    if exit_cfg is None or not getattr(exit_cfg, "enabled", False):
        return None

    settings: dict[str, Any] = dict(getattr(exit_cfg, "settings", {}) or {})
    mode_key = _MODE_TO_KEY.get(str(getattr(exit_cfg, "mode", "")).upper())

    selected: list[tuple[int, BaseExit]] = []
    for key, (engine_name, priority) in _KEY_TO_ENGINE.items():
        block = settings.get(key) or {}
        enabled = bool(block.get("enabled", False)) or key == mode_key
        if enabled:
            params = {k: v for k, v in block.items() if k != "enabled"}
            selected.append((priority, get_exit_engine(engine_name, params)))

    selected.sort(key=lambda pe: pe[0])
    return CompositeExit([engine for _, engine in selected])
