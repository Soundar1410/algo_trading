"""Worker health states and heartbeat writing.

The spec's recommended state list is implemented in full even though Phase 1
only reaches a few of them: the dashboard and Telegram both key off these
names, and adding states later would mean changing a persisted vocabulary that
operators have already learned to read.

Heartbeats are rate-limited. The spec is explicit that one row per tick is
wrong; every 5 to 15 seconds is enough, and the interval is configurable. A
heartbeat table that grows at tick rate turns a day of trading into a database
that is slow to query exactly when an incident needs it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum

from common.config.models import ExecutionMode
from common.execution.repository import ExecutionRepository

DEFAULT_INTERVAL_SECONDS = 10.0


class HealthState(StrEnum):
    """Worker lifecycle states (spec section 7 of the process model)."""

    DISABLED = "DISABLED"
    STARTING = "STARTING"
    WARMING_UP = "WARMING_UP"
    RUNNING_PAPER = "RUNNING_PAPER"
    RUNNING_LIVE = "RUNNING_LIVE"
    DEGRADED = "DEGRADED"
    BLOCK_NEW_ENTRIES = "BLOCK_NEW_ENTRIES"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    QUARANTINED = "QUARANTINED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"

    @classmethod
    def running_for(cls, mode: ExecutionMode) -> HealthState:
        return cls.RUNNING_LIVE if mode is ExecutionMode.LIVE else cls.RUNNING_PAPER


class HeartbeatWriter:
    """Writes rate-limited heartbeats for one process."""

    def __init__(
        self,
        repository: ExecutionRepository,
        *,
        session_id: int,
        runtime_id: str,
        strategy_id: str | None,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repo = repository
        self._session_id = session_id
        self._runtime_id = runtime_id
        self._strategy_id = strategy_id
        self._interval = interval_seconds
        #: Injectable so the rate-limit gate can be tested without sleeping —
        #: the same pattern :class:`~common.feed.reconnect.ReconnectingFeed`
        #: uses for its own ``clock``/``sleep``/``rng``.
        self._clock = clock
        self._last_beat: datetime | None = None

    def beat(
        self,
        state: HealthState,
        *,
        last_tick_at: datetime | None = None,
        queue_depth: int | None = None,
        dropped_events: int = 0,
        force: bool = False,
    ) -> bool:
        """Write a heartbeat if the interval has elapsed. Returns True if written.

        ``force`` bypasses the interval for state transitions — a move to
        FAILED or STOPPING must be recorded immediately, not up to ten seconds
        later when the process may no longer exist.
        """
        now = self._clock()
        within_interval = (
            self._last_beat is not None and (now - self._last_beat).total_seconds() < self._interval
        )
        if not force and within_interval:
            return False

        self._repo.record_heartbeat(
            session_id=self._session_id,
            runtime_id=self._runtime_id,
            strategy_id=self._strategy_id,
            health_state=state.value,
            last_tick_at=last_tick_at,
            queue_depth=queue_depth,
            dropped_events=dropped_events,
        )
        self._last_beat = now
        return True
