"""Read-model interface for Intraday Stocks — not implemented yet.

No scanner, supervisor, worker or database exists for this runtime group;
``config/runtimes/intraday_stocks.yaml`` does not exist. The dataclasses
below mirror the reference stock dashboard's information architecture
(scanner status → ranked candidates → accept/reject decisions → paper
trades), written now so a future scanner's persistence has a read-model
shape to satisfy. No function in this module opens a database; there is
none to open.
"""

from __future__ import annotations

from dataclasses import dataclass

NOT_CONFIGURED = (
    "Intraday stocks is not implemented yet. There is no scanner, "
    "supervisor, worker or database for this runtime group."
)


@dataclass(frozen=True)
class ScannerStatusRow:
    market_state: str
    last_snapshot_at: str | None
    symbols_evaluated: int
    eligible_candidates: int
    feed_status: str | None
    queue_depth: int | None
    processing_lag_ms: int | None


@dataclass(frozen=True)
class CandidateRow:
    rank: int
    symbol: str
    side: str
    score: float
    ltp: float | None
    day_change_pct: float | None
    relative_volume: float | None
    feature_quality: str | None
    updated_at: str


@dataclass(frozen=True)
class RejectionRow:
    reason: str
    count: int


@dataclass(frozen=True)
class DecisionRow:
    event_time: str
    symbol: str
    state: str
    accepted: bool
    reason: str | None
