"""A minimal, deliberately inert :class:`BasePositionalMultiLegStrategy` —
see this package's own ``__init__.py`` for why it exists.

**Never enters a cycle.** Completing a real four-leg entry needs a
genuinely fresh quote for each selected contract already sitting in the
engine's own ``QuoteBook`` at the instant ``_drive_entry`` runs — true for
every existing single-process test (which pre-delivers every leg's tick
before the triggering underlying one) but not achievable for a *newly*
subscribed contract driven through the real multi-process hub: the
subscription itself must round-trip child -> control queue -> supervisor
-> hub -> adapter before a tick for it can ever arrive, and nothing
resumes a first ``_drive_entry`` attempt that found no fresh quote yet
(a genuine, pre-existing engine gap outside this session's own scope —
recorded, not silently worked around). This strategy therefore proves
exactly what ``test_positional_runtime_multi_strategy.py`` needs of it —
that a second, independent positional worker starts, holds its own lock,
subscribes to its own underlying under the shared hub, and genuinely
receives real-time ticks for it — without depending on completing an
entry at all.

**The one durable, strategy-specific signal it leaves.** On the first
evaluation where the underlying's spot is fresh, it calls
``context.record_pre_entry_incident`` exactly once — the only
repository-reaching hook a strategy has before ever returning
``ENTER_CYCLE`` — so the test can prove, from a real ``errors`` row keyed
by this worker's own ``strategy_id``, that its engine genuinely evaluated
a real tick for its own subscription, not just that the process started.
"""

from __future__ import annotations

from typing import Any

from common.engine.positional.positional_models import Cycle, CycleSignal
from common.engine.positional.positional_strategy import (
    BasePositionalMultiLegStrategy,
    PositionalContext,
    register_positional_strategy,
)


@register_positional_strategy("_fixture_second_strategy")
class FixtureSecondStrategy(BasePositionalMultiLegStrategy):
    def __init__(self, cfg: Any = None, **kwargs: Any) -> None:
        super().__init__(cfg)
        self._marked = False

    def reset_daily(self) -> None:
        self._marked = False

    @property
    def lots(self) -> int:
        return 1

    def status(self) -> str:
        return "fixture_second_strategy: inert (never enters)"

    def evaluate(self, *, cycle: Cycle | None, context: PositionalContext) -> CycleSignal | None:
        if not self._marked and context.spot_is_fresh:
            self._marked = True
            context.record_pre_entry_incident(
                "Phase 5 fixture strategy tick-observation marker (not a real incident): "
                f"underlying_security_id={context.underlying_security_id} spot={context.spot}"
            )
        return None
