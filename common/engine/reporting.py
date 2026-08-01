"""End-of-day summary and the engine's liveness/report seams.

Ported selectively from the reference repository's
``framework/reporting/report_generator.py`` and ``framework/dashboard/publisher.py``
(Phase 3 Part 2b-i) — deviation D20.

What came across: :class:`DailySummary` and :func:`summarise`, which are pure
functions of a trade list and the shape the notifier's EOD message is built from.

What deliberately did not: the reference's file-writing ``ReportGenerator`` (CSV /
JSON / HTML into ``report.output_dir``) and the whole ``dashboard/publisher.py``
stack, including its 459-line SQLite ``PortfolioDatabase``. This repository
already persists every order, fill and position through
:class:`~common.execution.repository.ExecutionRepository`, and publishes liveness
through :class:`~common.health.heartbeat.HeartbeatWriter`, behind the migration
machinery and the ``execution_mode`` column that make a paper run auditable.
Porting a second, parallel reporting database beside it is precisely the
"parallel universe" the audit's adaptation note warns against.

So the engine holds two narrow protocols instead, with null implementations here.
Part 2b-ii-B-2 bound them, in :mod:`common.engine.reporting_bindings` — a separate
module rather than this one, because the heartbeat binding needs ``HealthState`` at
runtime and that would pull ``common.execution`` into every ``common.engine`` import.
This module stays what it is: protocols and a pure summariser.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .models import OpenPosition, Trade


@dataclass(frozen=True)
class DailySummary:
    """What one trading day produced. Ported field-for-field."""

    trade_count: int
    wins: int
    losses: int
    gross_pnl: float
    total_charges: float
    net_pnl: float

    @property
    def win_rate(self) -> float:
        return (self.wins / self.trade_count * 100.0) if self.trade_count else 0.0


def summarise(trades: list[Trade]) -> DailySummary:
    """Aggregate a day's closed trades. Pure; no I/O."""
    gross = sum(t.gross_pnl for t in trades)
    charges = sum(t.charges for t in trades)
    net = sum(t.net_pnl for t in trades)
    wins = sum(1 for t in trades if t.net_pnl > 0)
    losses = sum(1 for t in trades if t.net_pnl <= 0)
    return DailySummary(
        trade_count=len(trades),
        wins=wins,
        losses=losses,
        gross_pnl=round(gross, 2),
        total_charges=round(charges, 2),
        net_pnl=round(net, 2),
    )


@runtime_checkable
class EngineReporter(Protocol):
    """Where the engine publishes liveness and closed trades as it runs.

    The reference's ``EngineReporter`` wrapped a dashboard publisher and
    rate-limited its own heartbeats. Here that rate limiting belongs to
    :class:`~common.health.heartbeat.HeartbeatWriter`, which already does it, so
    this is only the three call sites the engine has.
    """

    def start(self) -> None:
        """The run has begun."""
        ...

    def beat(self, positions: list[OpenPosition], trades: list[Trade]) -> None:
        """One liveness beat, carrying the current picture. Called per tick."""
        ...

    def stopped(self, positions: list[OpenPosition], trades: list[Trade]) -> None:
        """The run has ended, cleanly or otherwise."""
        ...


@runtime_checkable
class ReportWriter(Protocol):
    """Where the day's closed trades go at ``_end_day``."""

    def generate(self, trades: list[Trade]) -> None: ...


class NullReporter:
    """Publishes nothing. The default, so an offline run touches no store."""

    def start(self) -> None:
        return None

    def beat(self, positions: list[OpenPosition], trades: list[Trade]) -> None:
        return None

    def stopped(self, positions: list[OpenPosition], trades: list[Trade]) -> None:
        return None


class NullReportWriter:
    """Writes nothing. Keeps ``_end_day`` unconditional, as the reference had it."""

    def generate(self, trades: list[Trade]) -> None:
        return None


class RecordingReporter:
    """Test double: records what the engine published, in order."""

    def __init__(self) -> None:
        self.started = 0
        self.beats: list[tuple[int, int]] = []
        self.stops: list[tuple[int, int]] = []

    def start(self) -> None:
        self.started += 1

    def beat(self, positions: list[OpenPosition], trades: list[Trade]) -> None:
        self.beats.append((len(positions), len(trades)))

    def stopped(self, positions: list[OpenPosition], trades: list[Trade]) -> None:
        self.stops.append((len(positions), len(trades)))
