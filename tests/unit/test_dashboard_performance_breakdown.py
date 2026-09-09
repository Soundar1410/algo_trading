"""DB-backed tests for the Intraday Options Performance tab's read model.

Fixtures go through the real write path (``ExecutionRepository`` /
``OrderLifecycle`` / ``PaperBroker``), same discipline as
``tests/unit/test_dashboard_intraday_options_data.py`` — the helpers here are
parameterised by strategy id, trading date and execution mode because this
read model's whole job is to tell several strategies over several days apart,
which that module's single-strategy, single-date fixtures cannot express.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from common.broker import PaperBroker, PaperFillConfig, SlippageConfig
from common.config.models import ExecutionMode
from common.execution import ExecutionRepository, OrderLifecycle
from common.models import Candle, Side, Signal
from common.persistence import Database, MigrationRunner, connect_readonly
from dashboards.data.intraday_options import (
    COMBINED_STRATEGY_ID,
    MIXED_EXECUTION_MODE,
    build_performance_breakdown,
    equity_curves_by_strategy,
    load_closed_trades,
)

IST = ZoneInfo("Asia/Kolkata")
RUNTIME_ID = "intraday_options"
DAY_ONE = "2026-08-10"
DAY_TWO = "2026-08-11"
DAY_THREE = "2026-08-12"
WINDOW = {"start_date": "2026-08-01", "end_date": "2026-08-31"}


@pytest.fixture
def repository(database_path: Path) -> ExecutionRepository:
    database = Database(database_path)
    MigrationRunner(database).run_pending()
    return ExecutionRepository(database)


def _open_session(
    repository: ExecutionRepository,
    strategy_id: str,
    *,
    on: str = DAY_ONE,
    mode: ExecutionMode = ExecutionMode.PAPER,
):
    """A session recorded as having started on ``on``.

    ``open_session`` always stamps "now" as ``started_at``; in production a
    worker's session date and the trading date it processes are the same,
    because it is opened fresh each morning. Backdating here reproduces that
    alignment for fixtures that use fixed historical dates — the same
    technique ``test_dashboard_intraday_options_data.py`` uses for
    ``load_daily_outcomes``.
    """
    session = repository.open_session(
        runtime_id=RUNTIME_ID,
        strategy_id=strategy_id,
        execution_mode=mode,
        process_role="worker",
        pid=4242,
    )
    with repository.database.transaction() as conn:
        conn.execute(
            "UPDATE runtime_sessions SET started_at = ? WHERE id = ?",
            (f"{on}T09:00:00+00:00", session.id),
        )
    return session


def _candle(day: str, minute: int, close: float) -> Candle:
    parsed = date.fromisoformat(day)
    start = datetime(parsed.year, parsed.month, parsed.day, 9, minute, tzinfo=IST)
    return Candle(
        security_id="13",
        instrument="NIFTY",
        open=close,
        high=close + 1.0,
        low=close - 1.0,
        close=close,
        volume=100,
        start_at=start,
        end_at=start + timedelta(minutes=1),
        tick_count=4,
    )


def _signal(
    strategy_id: str, side: Side, *, day: str, minute: int, close: float, mode: ExecutionMode
) -> Signal:
    candle = _candle(day, minute, close)
    return Signal(
        strategy_id=strategy_id,
        execution_mode=mode,
        instrument="NIFTY",
        security_id="13",
        side=side,
        quantity=75,
        candle=candle,
        reference_price=candle.close,
        evaluated_at=candle.end_at,
        reason="ema_cross",
    )


def _round_trip(
    repository: ExecutionRepository,
    session,
    strategy_id: str,
    *,
    day: str,
    entry: float,
    exit: float,
    minute: int = 15,
    mode: ExecutionMode = ExecutionMode.PAPER,
) -> None:
    lifecycle = OrderLifecycle(
        repository=repository,
        broker=PaperBroker(
            config=PaperFillConfig(slippage=SlippageConfig(mode="points", market_order_points=0.0))
        ),
        runtime_id=RUNTIME_ID,
        strategy_id=strategy_id,
        execution_mode=mode,
        session_id=session.id,
    )
    lifecycle.handle_signal(
        _signal(strategy_id, Side.BUY, day=day, minute=minute, close=entry, mode=mode),
        trading_date=day,
    )
    lifecycle.handle_signal(
        _signal(strategy_id, Side.SELL, day=day, minute=minute + 1, close=exit, mode=mode),
        trading_date=day,
    )


def _config_root(tmp_path: Path, *strategy_ids: str) -> Path:
    """A config tree declaring exactly ``strategy_ids`` — anything else the
    database knows about is therefore a retired id."""
    strategies_dir = tmp_path / "strategies"
    strategies_dir.mkdir(parents=True, exist_ok=True)
    for strategy_id in strategy_ids:
        (strategies_dir / f"{strategy_id}.yaml").write_text(
            f"strategy_id: {strategy_id}\n", encoding="utf-8"
        )
    return tmp_path


def _breakdown(database_path: Path, config_root: Path, **kwargs):
    conn = connect_readonly(database_path)
    try:
        return build_performance_breakdown(
            conn, RUNTIME_ID, config_root, **{**WINDOW, **kwargs}
        )
    finally:
        conn.close()


def _by_id(breakdown) -> dict[str, object]:
    return {row.strategy_id: row for row in breakdown.rows}


# ===================================================== discovery of every id
def test_a_strategy_that_ran_without_trading_still_gets_a_row(
    repository: ExecutionRepository, database_path: Path, tmp_path: Path
):
    """The whole reason this tab counts session days separately: a strategy
    that was up all session and never fired is not the same as one that was
    never running, and neither is the same as a losing one."""
    traded = _open_session(repository, "alpha")
    _round_trip(repository, traded, "alpha", day=DAY_ONE, entry=100.0, exit=110.0)
    _open_session(repository, "quiet")

    rows = _by_id(_breakdown(database_path, _config_root(tmp_path, "alpha", "quiet")))

    assert set(rows) == {"alpha", "quiet"}
    assert rows["quiet"].days_ran == 1
    assert rows["quiet"].days_traded == 0
    assert rows["quiet"].metrics.sample_size == 0
    assert rows["quiet"].first_trade_date is None
    assert rows["quiet"].pnl_per_trading_day is None


def test_a_retired_strategy_id_keeps_its_history_and_is_flagged(
    repository: ExecutionRepository, database_path: Path, tmp_path: Path
):
    """A rename leaves the old id in ``trade_ledger`` with real trades. The
    picker stops offering it (``strategy_scope``'s 31 August 2026 decision),
    but its P&L is not thereby fictional and must stay counted."""
    current = _open_session(repository, "c921_ema_cross_buy")
    _round_trip(repository, current, "c921_ema_cross_buy", day=DAY_ONE, entry=100.0, exit=110.0)
    old = _open_session(repository, "ema_cross_9_21_buy")
    _round_trip(repository, old, "ema_cross_9_21_buy", day=DAY_ONE, entry=100.0, exit=120.0)

    breakdown = _breakdown(database_path, _config_root(tmp_path, "c921_ema_cross_buy"))
    rows = _by_id(breakdown)

    assert rows["c921_ema_cross_buy"].configured is True
    assert rows["ema_cross_9_21_buy"].configured is False
    assert rows["ema_cross_9_21_buy"].metrics.sample_size == 1
    assert breakdown.combined.metrics.sample_size == 2


def test_days_ran_and_days_traded_are_counted_independently(
    repository: ExecutionRepository, database_path: Path, tmp_path: Path
):
    session = _open_session(repository, "alpha")
    _open_session(repository, "alpha")  # a restart on the same date
    _round_trip(repository, session, "alpha", day=DAY_ONE, entry=100.0, exit=110.0)

    rows = _by_id(_breakdown(database_path, _config_root(tmp_path, "alpha")))

    # Two sessions, one date — days_ran counts distinct dates, not sessions.
    assert rows["alpha"].days_ran == 1
    assert rows["alpha"].days_traded == 1


def test_pnl_per_trading_day_divides_by_days_traded_not_days_ran(
    repository: ExecutionRepository, database_path: Path, tmp_path: Path
):
    """Dividing by days ran would penalise a selective strategy for being
    selective — it would report a smaller per-day figure purely because it
    sat out a day it was up."""
    session = _open_session(repository, "alpha")
    _round_trip(repository, session, "alpha", day=DAY_ONE, entry=100.0, exit=110.0)
    _open_session(repository, "alpha")  # up on a second date, no trade

    row = _by_id(_breakdown(database_path, _config_root(tmp_path, "alpha")))["alpha"]

    assert row.days_traded == 1
    assert row.pnl_per_trading_day == pytest.approx(row.metrics.net_profit)


# ================================================================== combined
def test_combined_drawdown_comes_from_the_merged_curve_not_the_sum(
    repository: ExecutionRepository, database_path: Path, tmp_path: Path
):
    """Two strategies whose drawdowns fall on different days have a combined
    drawdown strictly shallower than their sum — one is climbing while the
    other falls. A caller that added the per-strategy figures would overstate
    the portfolio's worst moment, which is why the combined row is recomputed
    from every trade instead of being summed.

    Both strategies win first so each has a real running peak before it falls:
    ``drawdown_curve`` seeds its peak from the first point of the curve, so a
    strategy whose very first trade loses reports no drawdown from it. That is
    pre-existing behaviour shared with the Strategy Comparison tab and is not
    what this test is about.
    """
    alpha = _open_session(repository, "alpha")
    for day, exit_price in ((DAY_ONE, 140.0), (DAY_TWO, 60.0), (DAY_THREE, 140.0)):
        _round_trip(repository, alpha, "alpha", day=day, entry=100.0, exit=exit_price, minute=15)

    beta = _open_session(repository, "beta")
    for day, exit_price in ((DAY_ONE, 140.0), (DAY_TWO, 140.0), (DAY_THREE, 60.0)):
        _round_trip(repository, beta, "beta", day=day, entry=100.0, exit=exit_price, minute=25)

    breakdown = _breakdown(database_path, _config_root(tmp_path, "alpha", "beta"))
    rows = _by_id(breakdown)

    summed = sum(row.metrics.max_drawdown for row in rows.values())
    combined = breakdown.combined.metrics.max_drawdown
    assert combined is not None
    assert combined > summed
    # Net P&L, unlike every ratio and the drawdown, genuinely is additive.
    assert breakdown.combined.metrics.net_profit == pytest.approx(
        sum(row.metrics.net_profit for row in rows.values())
    )


def test_combined_win_rate_is_recomputed_not_averaged(
    repository: ExecutionRepository, database_path: Path, tmp_path: Path
):
    """A strategy with one winning trade and one with three losers average to
    50% but are really 25%. The combined row must weight by trade count."""
    alpha = _open_session(repository, "alpha")
    _round_trip(repository, alpha, "alpha", day=DAY_ONE, entry=100.0, exit=200.0)

    beta = _open_session(repository, "beta")
    for minute, day in ((25, DAY_ONE), (35, DAY_ONE), (25, DAY_TWO)):
        _round_trip(repository, beta, "beta", day=day, entry=100.0, exit=50.0, minute=minute)

    breakdown = _breakdown(database_path, _config_root(tmp_path, "alpha", "beta"))

    assert breakdown.combined.metrics.sample_size == 4
    assert breakdown.combined.metrics.win_rate == pytest.approx(25.0)


def test_combined_days_ran_is_a_distinct_date_union_not_a_sum(
    repository: ExecutionRepository, database_path: Path, tmp_path: Path
):
    _open_session(repository, "alpha")
    _open_session(repository, "beta")

    breakdown = _breakdown(database_path, _config_root(tmp_path, "alpha", "beta"))

    assert [row.days_ran for row in breakdown.rows] == [1, 1]
    assert breakdown.combined.days_ran == 1
    assert breakdown.combined.strategy_id == COMBINED_STRATEGY_ID


def test_eligible_days_counts_weekdays_in_the_window(
    repository: ExecutionRepository, database_path: Path, tmp_path: Path
):
    _open_session(repository, "alpha")

    breakdown = _breakdown(
        database_path,
        _config_root(tmp_path, "alpha"),
        start_date="2026-08-10",
        end_date="2026-08-16",
    )

    # 10-16 August 2026 is Monday to Sunday: five weekdays, holidays not
    # modelled (dashboards.data.calendar_stats.TRADING_DAY_CAVEAT).
    assert breakdown.eligible_days == 5


# ====================================================================== modes
def test_paper_and_live_never_blend_into_one_strategy_row(
    repository: ExecutionRepository, database_path: Path, tmp_path: Path
):
    """One strategy that has run in both modes gets two rows, matching
    ``build_strategy_comparison``'s never-blend rule.

    Only the paper row carries trades here, and deliberately so: live order
    placement is fail-closed until Phase 10, and ``OrderLifecycle`` refuses a
    live signal with no account reservation gate wired. Faking a live fill to
    make a prettier fixture would be testing a path that cannot happen. The
    live session is real, so the row it produces is real.
    """
    paper = _open_session(repository, "alpha")
    _round_trip(repository, paper, "alpha", day=DAY_ONE, entry=100.0, exit=110.0)
    _open_session(repository, "alpha", mode=ExecutionMode.LIVE)

    breakdown = _breakdown(database_path, _config_root(tmp_path, "alpha"))

    assert [(r.strategy_id, r.execution_mode) for r in breakdown.rows] == [
        ("alpha", "paper"),
        ("alpha", "live"),
    ]
    assert breakdown.combined.execution_mode == MIXED_EXECUTION_MODE
    assert breakdown.combined.metrics.sample_size == 1


def test_mode_filter_restricts_rows_and_the_combined_row(
    repository: ExecutionRepository, database_path: Path, tmp_path: Path
):
    paper = _open_session(repository, "alpha")
    _round_trip(repository, paper, "alpha", day=DAY_ONE, entry=100.0, exit=110.0)
    _open_session(repository, "alpha", mode=ExecutionMode.LIVE)

    breakdown = _breakdown(
        database_path, _config_root(tmp_path, "alpha"), execution_mode="paper"
    )

    assert [r.execution_mode for r in breakdown.rows] == ["paper"]
    assert breakdown.combined.execution_mode == "paper"
    assert breakdown.combined.metrics.sample_size == 1


# ================================================================== scoping
def test_strategy_ids_scope_restricts_rows_and_the_combined_row(
    repository: ExecutionRepository, database_path: Path, tmp_path: Path
):
    alpha = _open_session(repository, "alpha")
    _round_trip(repository, alpha, "alpha", day=DAY_ONE, entry=100.0, exit=110.0)
    beta = _open_session(repository, "beta")
    _round_trip(repository, beta, "beta", day=DAY_ONE, entry=100.0, exit=130.0, minute=25)

    breakdown = _breakdown(
        database_path, _config_root(tmp_path, "alpha", "beta"), strategy_ids=("alpha",)
    )

    assert [r.strategy_id for r in breakdown.rows] == ["alpha"]
    assert breakdown.combined.metrics.sample_size == 1
    assert breakdown.combined.metrics.net_profit == pytest.approx(
        breakdown.rows[0].metrics.net_profit
    )


def test_rows_are_sorted_by_net_pnl_descending(
    repository: ExecutionRepository, database_path: Path, tmp_path: Path
):
    winner = _open_session(repository, "winner")
    _round_trip(repository, winner, "winner", day=DAY_ONE, entry=100.0, exit=200.0)
    loser = _open_session(repository, "loser")
    _round_trip(repository, loser, "loser", day=DAY_ONE, entry=100.0, exit=50.0, minute=25)

    breakdown = _breakdown(database_path, _config_root(tmp_path, "winner", "loser"))

    assert [r.strategy_id for r in breakdown.rows] == ["winner", "loser"]


def test_empty_window_yields_no_rows_and_an_empty_combined_row(
    repository: ExecutionRepository, database_path: Path, tmp_path: Path
):
    session = _open_session(repository, "alpha")
    _round_trip(repository, session, "alpha", day=DAY_ONE, entry=100.0, exit=110.0)

    breakdown = _breakdown(
        database_path,
        _config_root(tmp_path, "alpha"),
        start_date="2026-07-01",
        end_date="2026-07-31",
    )

    assert breakdown.rows == ()
    assert breakdown.combined.metrics.sample_size == 0
    assert breakdown.combined.days_ran == 0
    assert breakdown.combined.pnl_per_trading_day is None


# ============================================================= equity curves
def test_equity_curves_are_grouped_per_strategy_and_mode(
    repository: ExecutionRepository, database_path: Path, tmp_path: Path
):
    alpha = _open_session(repository, "alpha")
    _round_trip(repository, alpha, "alpha", day=DAY_ONE, entry=100.0, exit=110.0)
    _round_trip(repository, alpha, "alpha", day=DAY_TWO, entry=100.0, exit=120.0)
    beta = _open_session(repository, "beta")
    _round_trip(repository, beta, "beta", day=DAY_ONE, entry=100.0, exit=130.0, minute=25)

    conn = connect_readonly(database_path)
    try:
        trades = load_closed_trades(conn, RUNTIME_ID, **WINDOW)
    finally:
        conn.close()
    curves = equity_curves_by_strategy(trades)

    assert set(curves) == {("alpha", "paper"), ("beta", "paper")}
    assert len(curves[("alpha", "paper")]) == 2
    # Each series is cumulative within its own strategy, so the last point is
    # that strategy's net P&L — not the portfolio's.
    alpha_row = _by_id(_breakdown(database_path, _config_root(tmp_path, "alpha", "beta")))["alpha"]
    assert curves[("alpha", "paper")][-1][1] == pytest.approx(alpha_row.metrics.net_profit)
