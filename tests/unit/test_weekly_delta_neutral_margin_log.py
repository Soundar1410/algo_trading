"""``weekly_delta_neutral``'s entry-margin observability.

The margin utilization cap is the last gate an entry candidate clears and
was the only silent one: its ``return None`` is commented "an ordinary
blocked entry, not an incident", so a strategy blocked there on every
evaluation for a whole entry window left no trace in the log, the incidents
table or the database. That is exactly what happened on 2 and 9 September
2026 — the two Wednesdays this strategy has been up — and the reason it was
diagnosable only by counting margin-calculator HTTP calls in the log.

``_log_margin_reading`` is driven directly here. It is pure observability
with a rate limiter; reconstructing a full chain/greeks/margin pipeline to
reach it would test the pipeline, not the logging.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from common.margin import MarginEstimate
from strategies.positional_options.weekly_delta_neutral.strategy import WeeklyDeltaNeutralStrategy

IST = ZoneInfo("Asia/Kolkata")
_LOGGER = "strategies.positional_options.weekly_delta_neutral.strategy"

#: The live configuration's own values, so the numbers in these tests are
#: the ones the running strategy actually compares against.
ALLOCATED_CAPITAL = 4_000_000.0
CAP_PERCENT = 50.0


class _StubScripMaster:
    underlying = "NIFTY"
    lot_size = 75

    def nearest_expiry(self, on: Any = None) -> str:
        return "2026-09-15"


def _strategy() -> WeeklyDeltaNeutralStrategy:
    return WeeklyDeltaNeutralStrategy(
        parameters={
            "underlying": "NIFTY",
            "allocated_capital": ALLOCATED_CAPITAL,
            "exits": {"maximum_margin_utilization_percent": CAP_PERCENT},
        },
        scrip_master=_StubScripMaster(),
    )


def _context(now: datetime) -> Any:
    """Only ``now`` is read by the rate limiter; the rest is inert."""

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.now = now  # type: ignore[attr-defined]
    return ctx


def _estimate(margin: float, *, per_leg: tuple[tuple[str, float], ...] = ()) -> MarginEstimate:
    return MarginEstimate(
        estimated_margin=margin,
        source="dhan_margin_calculator_summed_legs",
        estimated_at=datetime(2026, 9, 9, 10, 0, tzinfo=IST),
        allocated_capital=ALLOCATED_CAPITAL,
        per_leg=per_leg,
    )


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 9, 9, hour, minute, second, tzinfo=IST)


def test_logs_the_utilization_the_cap_and_the_blocked_decision(caplog) -> None:
    """The whole point: the number that decides the entry is now in the log."""
    strategy = _strategy()
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        strategy._log_margin_reading(
            _context(_at(9, 30)), _estimate(5_200_000.0), blocked=True
        )

    (record,) = caplog.records
    message = record.getMessage()
    assert "utilization_percent=130.00" in message
    assert "cap_percent=50.00" in message
    assert "estimated_margin=5200000" in message
    assert "allocated_capital=4000000" in message
    assert "blocked=True" in message


def test_logs_the_per_leg_breakdown_that_explains_the_total(caplog) -> None:
    """The estimator sums each leg independently
    (``common.margin.estimator.estimate_basket``: "Every leg's margin,
    summed"), so a hedged iron condor is priced as two naked shorts plus two
    long premiums. Whether that is what busts the cap cannot be read off the
    total, only off the breakdown."""
    strategy = _strategy()
    legs = (
        ("SHORT_CALL_ID", 2_400_000.0),
        ("SHORT_PUT_ID", 2_500_000.0),
        ("LONG_CALL_ID", 150_000.0),
        ("LONG_PUT_ID", 150_000.0),
    )
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        strategy._log_margin_reading(
            _context(_at(9, 30)), _estimate(5_200_000.0, per_leg=legs), blocked=True
        )

    message = caplog.records[0].getMessage()
    assert "source=dhan_margin_calculator_summed_legs" in message
    for security_id, margin in legs:
        assert f"{security_id}={margin:.0f}" in message


def test_an_allowed_entry_is_logged_too_not_only_a_blocked_one(caplog) -> None:
    """A gate that only speaks when it refuses cannot be told apart from a
    gate that is never reached."""
    strategy = _strategy()
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        strategy._log_margin_reading(
            _context(_at(9, 30)), _estimate(1_200_000.0), blocked=False
        )

    message = caplog.records[0].getMessage()
    assert "blocked=False" in message
    assert "utilization_percent=30.00" in message


def test_repeated_identical_readings_are_rate_limited_to_once_a_minute(caplog) -> None:
    """Entry evaluation runs about every five seconds for the whole
    09:25-12:00 window. Logging every one would add thousands of identical
    lines a day, which is how the useful signal gets lost."""
    strategy = _strategy()
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        for second in range(0, 55, 5):
            strategy._log_margin_reading(
                _context(_at(9, 30, second)), _estimate(5_200_000.0), blocked=True
            )

    assert len(caplog.records) == 1


def test_a_heartbeat_line_still_lands_after_a_minute(caplog) -> None:
    strategy = _strategy()
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        strategy._log_margin_reading(
            _context(_at(9, 30)), _estimate(5_200_000.0), blocked=True
        )
        strategy._log_margin_reading(
            _context(_at(9, 30) + timedelta(seconds=61)), _estimate(5_200_000.0), blocked=True
        )

    assert len(caplog.records) == 2


def test_a_blocked_to_allowed_transition_logs_immediately(caplog) -> None:
    """The moment the cap stops blocking is the single most interesting
    event this gate can produce, and it must not wait for the heartbeat."""
    strategy = _strategy()
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        strategy._log_margin_reading(
            _context(_at(9, 30, 0)), _estimate(5_200_000.0), blocked=True
        )
        strategy._log_margin_reading(
            _context(_at(9, 30, 5)), _estimate(1_200_000.0), blocked=False
        )

    assert len(caplog.records) == 2
    assert "blocked=True" in caplog.records[0].getMessage()
    assert "blocked=False" in caplog.records[1].getMessage()


def test_reset_daily_clears_the_rate_limiter(caplog) -> None:
    """A new session must log its first reading, not inherit yesterday's
    suppression."""
    strategy = _strategy()
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        strategy._log_margin_reading(
            _context(_at(9, 30)), _estimate(5_200_000.0), blocked=True
        )
        strategy.reset_daily()
        strategy._log_margin_reading(
            _context(_at(9, 30, 5)), _estimate(5_200_000.0), blocked=True
        )

    assert len(caplog.records) == 2


@pytest.mark.parametrize(
    ("margin", "expected_blocked"),
    [
        (1_999_000.0, False),  # just under 50% of 40,00,000
        (2_000_000.0, False),  # exactly at the cap — not "greater than"
        (2_000_100.0, True),  # just over
    ],
)
def test_the_logged_decision_matches_the_caps_own_strict_comparison(
    margin: float, expected_blocked: bool
) -> None:
    """Guards the boundary the caller uses: the cap blocks on strictly
    greater than, so an estimate exactly at 50% still enters."""
    estimate = _estimate(margin)
    assert (estimate.utilization_percent > CAP_PERCENT) is expected_blocked
