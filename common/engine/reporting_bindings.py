"""The D20 reporting protocols, bound to this repository's existing stores.

Phase 3 Part 2b-ii-B-2. :mod:`common.engine.reporting` shipped
:class:`~common.engine.reporting.EngineReporter` and
:class:`~common.engine.reporting.ReportWriter` as protocols with null
implementations, and said "Part 2b-ii binds them to the existing repository and
heartbeat writer". This is that binding.

Why this is a separate module, and not re-exported
--------------------------------------------------
It is **deliberately absent from** ``common/engine/__init__.py``, for the same class
of reason ``strategies/intraday_options/__init__.py`` gives for not re-exporting
``EngineFixtureStrategy``: re-exporting drags a dependency into everyone's import
graph to serve one consumer.

Specifically, :class:`HeartbeatEngineReporter` needs ``HealthState`` values at
*runtime*, and ``common.health`` imports
:class:`~common.execution.repository.ExecutionRepository`. Putting that in
``reporting.py`` — which ``engine.py`` imports — would pull ``common.execution``
into every ``common.engine`` import, undoing the property ``gateway.py`` and
``square_off.py`` each maintain on purpose with their ``TYPE_CHECKING``-only
imports ("keeps common.execution out of the import graph"). ``reporting.py`` stays
what it is: protocols and a pure summariser.

So the worker imports this module directly, and nothing else does.

What is *not* rebuilt here
--------------------------
The trades. Every leg already went through
:class:`~common.engine.gateway.LifecycleGateway` into ``signals`` → ``order_intents``
→ ``orders`` → ``fills`` → ``positions``, each with a correlation ID and an
``execution_mode``. Writing them a second time is precisely the "parallel universe"
:mod:`common.engine.reporting` rejects the reference's ``PortfolioDatabase`` for.
What :class:`RepositoryReportWriter` adds is only what those tables cannot hold:
MFE/MAE, the regime tags, the engine's exit reason, and the day's net.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from common.config.models import ExecutionMode
from common.health import HealthState
from common.logging import get_logger

from .models import OpenPosition, Trade
from .reporting import summarise
from .state_payload import DAY_SUMMARY_KEY, merge_payload

if TYPE_CHECKING:  # typing only: both are injected, neither is constructed here
    from common.execution.repository import ExecutionRepository
    from common.health.heartbeat import HeartbeatWriter

log = get_logger(__name__)


class HeartbeatEngineReporter:
    """Publishes the engine's liveness through the existing heartbeat writer.

    The reference's ``EngineReporter`` wrapped a dashboard publisher and rate-limited
    its own beats; :class:`~common.health.heartbeat.HeartbeatWriter` already does the
    rate limiting, so this is a thin adapter over the three call sites the engine has.

    **Deliberately does not write the terminal states.** ``STARTING``, ``STOPPED`` and
    ``FAILED`` belong to the worker, which owns the process lifecycle and knows
    whether the run ended well. Two writers competing for the last word is how the
    ordering rule in ``supervisor._publish_final_health`` gets broken — a tidy
    ``STOPPED`` overwriting an alarm nobody then sees.
    """

    def __init__(
        self,
        heartbeat: HeartbeatWriter,
        *,
        execution_mode: ExecutionMode,
        entries_blocked: Callable[[], str | None] | None = None,
    ) -> None:
        self._heartbeat = heartbeat
        self._mode = execution_mode
        #: Zero-arg callable returning the engine's block reason, or ``None``. Passed
        #: as a callable rather than the engine itself so this class holds no
        #: reference to what it reports on.
        self._entries_blocked = entries_blocked

    def _state(self) -> HealthState:
        if self._entries_blocked is not None and self._entries_blocked() is not None:
            # The same state the fixture path publishes when the square-off policy
            # closes the entry window, so the dashboard reads one vocabulary.
            return HealthState.BLOCK_NEW_ENTRIES
        return HealthState.running_for(self._mode)

    def start(self) -> None:
        self._heartbeat.beat(HealthState.running_for(self._mode), force=True)

    def beat(self, positions: list[OpenPosition], trades: list[Trade]) -> None:
        # Called per tick; the writer's own interval is what keeps this off the
        # database. Nothing here may raise: it is on the tick path.
        try:
            self._heartbeat.beat(self._state())
        except Exception:  # liveness reporting must not stop trading
            log.exception("could not write a heartbeat; the run continues")

    def stopped(self, positions: list[OpenPosition], trades: list[Trade]) -> None:
        try:
            self._heartbeat.beat(HealthState.STOPPING, force=True)
        except Exception:  # the run is ending either way
            log.exception("could not write the stopping heartbeat")


class RepositoryReportWriter:
    """Writes the day's summary into ``strategy_state.payload``.

    One row per strategy-day, merged rather than replaced, so it coexists with the
    ``open_position`` record :class:`~common.engine.gateway.LifecycleGateway` keeps in
    the same column — see :mod:`common.engine.state_payload` for why a bare write
    would clobber it.
    """

    def __init__(
        self,
        repository: ExecutionRepository,
        *,
        runtime_id: str,
        strategy_id: str,
        execution_mode: ExecutionMode,
        trading_date: str,
    ) -> None:
        self._repo = repository
        self._runtime_id = runtime_id
        self._strategy_id = strategy_id
        self._mode = execution_mode
        self._trading_date = trading_date

    def generate(self, trades: list[Trade]) -> None:
        summary = summarise(trades)
        record: dict[str, Any] = {
            "trade_count": summary.trade_count,
            "wins": summary.wins,
            "losses": summary.losses,
            "gross_pnl": summary.gross_pnl,
            "total_charges": summary.total_charges,
            "net_pnl": summary.net_pnl,
            "win_rate": round(summary.win_rate, 2),
            # Only the per-trade fields the execution tables do not carry. Prices,
            # quantities and charges are already in fills/positions and are not
            # duplicated here.
            "trades": [
                {
                    "security_id": trade.contract.security_id,
                    "exit_reason": trade.exit_reason.value,
                    "net_pnl": round(trade.net_pnl, 2),
                    "mfe": round(trade.mfe, 2),
                    "mae": round(trade.mae, 2),
                    "entry_regime": trade.entry_regime,
                    "exit_regime": trade.exit_regime,
                    "session_tags": trade.session_tags,
                }
                for trade in trades
            ],
        }
        try:
            merge_payload(
                self._repo,
                {DAY_SUMMARY_KEY: record},
                runtime_id=self._runtime_id,
                strategy_id=self._strategy_id,
                execution_mode=self._mode,
                trading_date=self._trading_date,
            )
        except Exception:  # _end_day must still complete
            log.exception("could not write the day summary for %s", self._strategy_id)
