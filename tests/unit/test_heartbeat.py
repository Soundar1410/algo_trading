"""``HeartbeatWriter``'s rate-limit gate and ``force`` bypass.

Before Phase 7 Part 1 this had no direct unit test: it was exercised only
incidentally through supervisor/worker integration tests, which cannot isolate
the gate itself from everything else those tests assert. The spec is explicit
(``common/health/heartbeat.py`` module docstring) that a heartbeat every tick
is wrong and every 5-15 seconds is enough — this is the property that enforces
that, so it earns a test of its own.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from common.health.heartbeat import DEFAULT_INTERVAL_SECONDS, HealthState, HeartbeatWriter


class _RecordingRepository:
    """A stand-in for :class:`ExecutionRepository` that only records calls.

    ``HeartbeatWriter`` calls exactly one repository method
    (``record_heartbeat``); a real ``ExecutionRepository`` bound to a real
    database would work too, but would make this test about the database
    rather than about the gate.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def record_heartbeat(self, **kwargs: object) -> None:
        self.calls.append(kwargs)


def _writer(
    repo: _RecordingRepository, *, clock, interval_seconds: float = 10.0
) -> HeartbeatWriter:
    return HeartbeatWriter(
        repo,  # type: ignore[arg-type]
        session_id=1,
        runtime_id="intraday_options",
        strategy_id="st01",
        interval_seconds=interval_seconds,
        clock=clock,
    )


def _ticking_clock(*moments: datetime):
    remaining = iter(moments)

    def clock() -> datetime:
        return next(remaining)

    return clock


def test_the_first_beat_always_writes():
    repo = _RecordingRepository()
    base = datetime(2026, 8, 7, 9, 15, tzinfo=UTC)
    writer = _writer(repo, clock=_ticking_clock(base))

    assert writer.beat(HealthState.RUNNING_PAPER) is True
    assert len(repo.calls) == 1


def test_a_beat_inside_the_interval_is_suppressed():
    repo = _RecordingRepository()
    base = datetime(2026, 8, 7, 9, 15, tzinfo=UTC)
    writer = _writer(
        repo, clock=_ticking_clock(base, base + timedelta(seconds=5)), interval_seconds=10.0
    )

    assert writer.beat(HealthState.RUNNING_PAPER) is True
    assert writer.beat(HealthState.RUNNING_PAPER) is False
    assert len(repo.calls) == 1


def test_a_beat_at_or_past_the_interval_writes():
    repo = _RecordingRepository()
    base = datetime(2026, 8, 7, 9, 15, tzinfo=UTC)
    writer = _writer(
        repo, clock=_ticking_clock(base, base + timedelta(seconds=10)), interval_seconds=10.0
    )

    assert writer.beat(HealthState.RUNNING_PAPER) is True
    assert writer.beat(HealthState.RUNNING_PAPER) is True
    assert len(repo.calls) == 2


def test_force_bypasses_the_interval_even_immediately_after_a_beat():
    """A move to FAILED or STOPPING must be recorded immediately — the process
    may not exist by the time the ordinary interval would next allow a write."""
    repo = _RecordingRepository()
    base = datetime(2026, 8, 7, 9, 15, tzinfo=UTC)
    writer = _writer(repo, clock=_ticking_clock(base, base), interval_seconds=10.0)

    assert writer.beat(HealthState.RUNNING_PAPER) is True
    assert writer.beat(HealthState.FAILED, force=True) is True
    assert len(repo.calls) == 2
    assert repo.calls[-1]["health_state"] == "FAILED"


def test_force_still_resets_the_interval_clock():
    """A forced beat is not free of consequence: the next ordinary beat is
    rate-limited from *its* timestamp, not the last ordinary one's."""
    repo = _RecordingRepository()
    base = datetime(2026, 8, 7, 9, 15, tzinfo=UTC)
    writer = _writer(
        repo,
        clock=_ticking_clock(base, base, base + timedelta(seconds=5)),
        interval_seconds=10.0,
    )

    writer.beat(HealthState.RUNNING_PAPER)  # t=0, writes
    writer.beat(HealthState.DEGRADED, force=True)  # t=0, writes (forced)
    # t=5, still within 10s of the forced beat above:
    assert writer.beat(HealthState.RUNNING_PAPER) is False
    assert len(repo.calls) == 2


def test_optional_fields_travel_through_to_the_repository():
    repo = _RecordingRepository()
    base = datetime(2026, 8, 7, 9, 15, tzinfo=UTC)
    writer = _writer(repo, clock=_ticking_clock(base))
    tick_at = base

    writer.beat(HealthState.RUNNING_PAPER, last_tick_at=tick_at, queue_depth=3, dropped_events=2)

    call = repo.calls[0]
    assert call["last_tick_at"] == tick_at
    assert call["queue_depth"] == 3
    assert call["dropped_events"] == 2


def test_the_default_interval_matches_the_documented_default():
    """common.config.models.HealthConfig's default must not silently drift
    from this module's own default — see that model's docstring."""
    assert DEFAULT_INTERVAL_SECONDS == 10.0


def test_default_clock_is_real_utc_now_when_unspecified():
    """No test above exercises the real default; this proves it exists and
    returns a timezone-aware UTC moment, without asserting an exact time."""
    repo = _RecordingRepository()
    writer = HeartbeatWriter(
        repo,  # type: ignore[arg-type]
        session_id=1,
        runtime_id="intraday_options",
        strategy_id=None,
    )
    before = datetime.now(UTC)
    writer.beat(HealthState.STARTING, force=True)
    after = datetime.now(UTC)

    last_beat = writer._last_beat  # the one direct check available for the real clock
    assert before <= last_beat <= after
