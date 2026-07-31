"""Who decides that it is square-off time.

Phase 3 Part 2b-ii-B-1. Until this part, :class:`~common.engine.engine.
TradingEngine` asked ``MarketSession.is_past_square_off(tick.exchange_time)`` while
:mod:`runtimes.intraday_options.worker` asked
``SquareOffPolicy.trigger_at(..., state=...)``. Two deciders against one position
is how a position gets closed twice — and worse, the two disagree in the one case
that matters:

===================  ==============================  =========================
                     ``SquareOffPolicy`` (worker)    ``MarketSession`` (engine)
===================  ==============================  =========================
Inputs               clock **+ persisted state**     clock only
Survives a restart   **yes**                         no
===================  ==============================  =========================

An engine restarted at 15:25 has ``_squared_off = False`` and a session clock that
says "past square-off", so it re-runs square-off against a position the previous
process already closed. Execution §10 states the rule directly — "a process
restart must not reset the square-off state" — and architecture §13 places
square-off at **runtime** level while leaving the entry cutoff and the calendar at
**strategy** level. So the policy owns the decision and the engine is *told*.

The seam is :class:`SquareOffAuthority`: two methods, injected at construction.

* :class:`SessionSquareOffAuthority` is the default and is deliberately identical
  in behaviour to the method it replaces, so every ported engine test and every
  offline run is unaffected by this change.
* :class:`PersistedSquareOffAuthority` is the real one. The worker builds it from
  a :class:`~common.risk.squareoff.SquareOffPolicy` and the persisted
  ``strategy_state.square_off_state`` row, so a restart reads ``COMPLETED`` and
  does not re-close.

The times themselves are reconciled in :mod:`common.engine.config`:
``SessionConfig`` is *derived from* the policy, so two independently configured
square-off times cannot drift apart.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from common.config.models import ExecutionMode
from common.logging import get_logger
from common.risk import SquareOffPolicy, SquareOffState, SquareOffTrigger

if TYPE_CHECKING:  # imported for typing only, so the engine package stays light
    from common.execution.repository import ExecutionRepository

    from .session import MarketSession

log = get_logger(__name__)


@runtime_checkable
class SquareOffAuthority(Protocol):
    """The engine's single source of truth for "is it square-off time?"."""

    def due(self, ts: datetime) -> bool:
        """Whether the engine should force-close everything now.

        Called on **every** tick, so it must be cheap and must not write on every
        call. It may be called again after a square-off has already run: the
        engine's own idempotence latch handles that.
        """
        ...

    def completed(self, ts: datetime) -> None:
        """Record that square-off finished. Idempotent.

        Called once the engine has closed every position, and *only* then — an
        implementation that persists this is what makes a restart safe, so it must
        not be called optimistically.
        """
        ...


class SessionSquareOffAuthority:
    """Clock-only, from the session calendar. The engine's default.

    This is ``MarketSession.is_past_square_off``'s body, moved rather than
    rewritten, including its choice to read ``ts.time()`` without converting to the
    session timezone. Keeping it byte-identical is the point: it is what lets the
    seam land without changing a single ported engine test.

    It persists nothing, so it cannot survive a restart — which is exactly why it
    is not the implementation a worker uses.
    """

    def __init__(self, session: MarketSession) -> None:
        self._session = session

    def due(self, ts: datetime) -> bool:
        return ts.time() >= self._session.square_off

    def completed(self, ts: datetime) -> None:
        """No-op: there is nowhere to record it, and pretending otherwise would
        make an offline run look restart-safe when it is not."""


class PersistedSquareOffAuthority:
    """Policy clock plus the persisted ``strategy_state.square_off_state`` row.

    Reads the stored state **once**, at construction, because that is the moment a
    restart is distinguishable from a continuing run. Everything after that is this
    process's own progress.

    Writes exactly twice per day: ``IN_PROGRESS`` from the first :meth:`due` that
    answers ``True`` — before the engine acts, so a crash mid-close leaves evidence
    that an attempt was started — and ``COMPLETED`` from :meth:`completed`. That is
    the same two-write shape ``worker.py`` already uses for the fixture path, so
    the persisted record does not change form depending on which strategy shape
    produced it.
    """

    def __init__(
        self,
        policy: SquareOffPolicy,
        repository: ExecutionRepository,
        *,
        runtime_id: str,
        strategy_id: str,
        execution_mode: ExecutionMode,
        trading_date: str,
    ) -> None:
        self._policy = policy
        self._repo = repository
        self._runtime_id = runtime_id
        self._strategy_id = strategy_id
        self._mode = execution_mode
        self._trading_date = trading_date
        #: Observable so a test can assert this does not write on every tick.
        self.writes = 0
        self._attempt_recorded = False
        self._state = self._load_state()

    # ---------------------------------------------------------------- loading
    def _load_state(self) -> SquareOffState:
        row = self._repo.load_strategy_state(
            strategy_id=self._strategy_id,
            execution_mode=self._mode,
            trading_date=self._trading_date,
        )
        if row is None:
            return SquareOffState.PENDING
        try:
            stored = SquareOffState(row["square_off_state"])
        except (ValueError, KeyError, IndexError):
            # Unreadable degrades to PENDING, matching the worker's own loader.
            # An extra square-off attempt against an already-flat book costs
            # nothing; a skipped one is an open position overnight.
            log.warning(
                "unreadable square_off_state for %s on %s; treating it as PENDING",
                self._strategy_id,
                self._trading_date,
            )
            return SquareOffState.PENDING

        if stored is SquareOffState.IN_PROGRESS:
            # An IN_PROGRESS read at *startup* was written by a process that is no
            # longer running: whoever owned that attempt is dead, so it is stalled,
            # not in flight. SquareOffPolicy.trigger_at returns NONE for it — which
            # is right for the process mid-attempt and wrong for the one that
            # inherits it — so the normalisation happens here rather than in the
            # policy, which stays untouched. Spec execution §10 provides for
            # exactly this with its retry window.
            log.warning(
                "square-off for %s on %s was left IN_PROGRESS by a previous process; "
                "treating it as an unfinished attempt and retrying",
                self._strategy_id,
                self._trading_date,
            )
            return SquareOffState.PENDING
        return stored

    # ---------------------------------------------------------------- deciding
    def due(self, ts: datetime) -> bool:
        if self._policy.trigger_at(ts, state=self._state) is not SquareOffTrigger.SQUARE_OFF:
            return False
        if not self._attempt_recorded:
            self._attempt_recorded = True
            self._save(SquareOffState.IN_PROGRESS)
        # Deliberately does **not** move ``_state`` to IN_PROGRESS. If the close
        # raises before ``completed()``, the next tick must ask again and get
        # ``True``; latching here would silently abandon the retry.
        return True

    def completed(self, ts: datetime) -> None:
        if self._state is SquareOffState.COMPLETED:
            return
        self._state = SquareOffState.COMPLETED
        self._save(SquareOffState.COMPLETED, last_candle_end_at=ts.isoformat())

    def _save(self, state: SquareOffState, *, last_candle_end_at: str | None = None) -> None:
        self._repo.save_strategy_state(
            runtime_id=self._runtime_id,
            strategy_id=self._strategy_id,
            execution_mode=self._mode,
            trading_date=self._trading_date,
            square_off_state=state.value,
            entries_blocked=True,
            last_candle_end_at=last_candle_end_at,
        )
        self.writes += 1
