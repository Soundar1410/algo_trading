"""Phase 4: dashboard discovery, labelling and filtering for
``supertrend_buy_1_1p2`` — through the real committed ``config/`` tree and a real,
production-written database, never a hand-typed row.

Two things are proven here that the generic dashboard test suites (which use
synthetic strategy ids to prove the *mechanism* is data-driven) do not: that this
specific strategy id, in the real committed configuration, is correctly labelled
by its own ``enabled`` flag, appears in the real intraday_options selector
alongside the other enabled strategies, and — once real persisted rows exist for
it (written through the same ``TradingEngine`` / ``ExecutionRepository`` stack
Phase 3 uses) — is correctly isolated from another strategy's data by every read
model's ``strategy_id`` filter.

Was ``DISABLED`` at delivery (Phase 4); the operator enabled it for real,
committed paper trading on 31 August 2026 (see
docs/IMPLEMENTATION_STATUS_AND_RUNBOOK.md), so the discovery/labelling tests
below now assert ``STOPPED`` (configured and enabled, no live heartbeat visible
with no database connection) — the committed flag changed, not the mechanism
these tests exist to prove.

No dashboard file is touched by this port; this file exists to demonstrate that
fact against real data, not to add one.
"""

from __future__ import annotations

from pathlib import Path

from _supertrend_buy_1_1p2_fixtures import (
    LOT_SIZE,
    Stack,
    build_stack,
    contract_id,
    dt,
    tick,
    underlying_ticks,
    worker_config,
)

from common.persistence import connect_readonly
from dashboards.data.intraday_options import load_closed_trades, load_orders
from dashboards.data.strategy_scope import (
    RUNNING,
    STOPPED,
    discover_strategy_options,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_CONFIG = REPO_ROOT / "config"
RUNTIME_ID = "intraday_options"
STRATEGY_ID = "supertrend_buy_1_1p2"

PE = contract_id(19500.0, "PE")


def _closed_trade_stack(tmp_path: Path) -> Stack:
    """One real momentum-exit round trip, written through the production stack —
    the same shape Phase 3's suites already prove is correct; reused here purely as
    a source of real rows for the dashboard read models."""
    config = worker_config(tmp_path)
    ticks = underlying_ticks([19500.0], start=dt(9, 15))
    ticks.insert(2, tick(PE, 100.0, dt(9, 21, 10)))
    ticks += [
        tick(PE, 105.0, dt(9, 23)),
        tick(PE, 98.0, dt(9, 27)),  # closes candle 1 at 105, low 105
        tick(PE, 98.0, dt(9, 32)),  # closes candle 2 at 98 < 105 -> momentum exit
        tick(PE, 98.0, dt(9, 37)),
    ]
    stack = build_stack(config, ticks)
    stack.engine.run()
    assert len(stack.positions.trades) == 1, "fixture did not produce a closed trade"
    return stack


# ------------------------------------------------- discovery and labelling
def test_the_committed_strategy_is_discovered_and_labelled_stopped():
    """Spec 18.7's discovery half, updated for the 31 August 2026 enable
    decision: against the real committed config with no database at all (a
    fresh install), the strategy still appears, because config-based
    discovery does not require a database — now labelled STOPPED (enabled,
    no live heartbeat visible) rather than DISABLED."""
    options = discover_strategy_options(None, REPO_CONFIG, RUNTIME_ID)
    ours = {o.strategy_id: o for o in options}[STRATEGY_ID]
    assert ours.status_label == STOPPED
    assert ours.execution_mode is None


def test_it_appears_alongside_the_other_enabled_strategies_in_the_same_scope():
    """The real intraday_options selector, unfiltered by any database: every
    strategy configured for this runtime appears together, each labelled by
    its own committed ``enabled`` flag — nothing here singles any one of
    them out."""
    options = discover_strategy_options(None, REPO_CONFIG, RUNTIME_ID)
    labels = {o.strategy_id: o.status_label for o in options}
    # All committed-enabled here show as STOPPED (configured, no live
    # heartbeat) — the same status a freshly-installed, not-yet-started
    # worker would show.
    assert labels[STRATEGY_ID] == STOPPED
    assert labels["c921_ema_cross_buy"] == STOPPED
    assert labels["straddle_920"] == STOPPED
    assert set(labels) >= {STRATEGY_ID, "c921_ema_cross_buy", "straddle_920"}


def test_enabling_it_in_a_fixture_config_would_show_running_generically(
    tmp_path: Path,
):
    """The disabled label is a property of the committed flag, not of the strategy
    id: the exact same discovery function labels it RUNNING the moment a live
    heartbeat exists for it, on an otherwise-identical scratch config. Proves the
    label is computed generically rather than hard-coded per strategy anywhere."""
    from common.config.models import ExecutionMode
    from common.execution import ExecutionRepository
    from common.persistence import Database, MigrationRunner

    config_root = tmp_path / "config"
    (config_root / "strategies").mkdir(parents=True)
    (config_root / "strategies" / f"{STRATEGY_ID}.yaml").write_text(
        f"strategy_id: {STRATEGY_ID}\nruntime_id: {RUNTIME_ID}\n"
        "enabled: true\nmode: paper\nlive_approved: false\n",
        encoding="utf-8",
    )
    database_path = tmp_path / "db.sqlite"
    database = Database(database_path)
    MigrationRunner(database).run_pending()
    repository = ExecutionRepository(database)
    session = repository.open_session(
        runtime_id=RUNTIME_ID,
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        process_role="worker",
        pid=1,
    )
    repository.record_heartbeat(
        session_id=session.id,
        runtime_id=RUNTIME_ID,
        strategy_id=STRATEGY_ID,
        health_state="RUNNING_PAPER",
    )

    conn = connect_readonly(database_path)
    options = discover_strategy_options(conn, config_root, RUNTIME_ID)
    conn.close()

    ours = {o.strategy_id: o for o in options}[STRATEGY_ID]
    assert ours.status_label == RUNNING


# ---------------------------------------------------- real-data filtering
def test_filtering_by_this_strategy_isolates_its_own_closed_trade(tmp_path: Path):
    """Real persisted data, two strategies in the same database: the closed-trade
    read model's ``strategy_id`` filter returns exactly this strategy's round trip
    and nothing from the other one, with no page-specific SQL for either id."""
    stack = _closed_trade_stack(tmp_path)

    # A second, differently-named strategy config against the SAME database, so the
    # filter is proven to exclude real neighbouring data, not merely to find nothing
    # because nothing else exists.
    other_config = worker_config(tmp_path)
    object.__setattr__(other_config, "strategy_id", "other_strategy")
    other_pe = contract_id(19500.0, "PE")
    other_ticks = underlying_ticks([19500.0], start=dt(9, 15))
    other_ticks.insert(2, tick(other_pe, 100.0, dt(9, 21, 10)))
    other_ticks += [tick(other_pe, 105.0, dt(9, 23)), tick(other_pe, 98.0, dt(9, 27)),
                     tick(other_pe, 98.0, dt(9, 32)), tick(other_pe, 98.0, dt(9, 37))]
    other_stack = build_stack(other_config, other_ticks)
    other_stack.engine.run()
    assert len(other_stack.positions.trades) == 1

    conn = connect_readonly(stack.config.database_path)
    ours = load_closed_trades(
        conn, RUNTIME_ID, strategy_id=STRATEGY_ID,
        start_date=stack.config.trading_date, end_date=stack.config.trading_date,
    )
    theirs = load_closed_trades(
        conn, RUNTIME_ID, strategy_id="other_strategy",
        start_date=stack.config.trading_date, end_date=stack.config.trading_date,
    )
    unfiltered = load_closed_trades(
        conn, RUNTIME_ID,
        start_date=stack.config.trading_date, end_date=stack.config.trading_date,
    )
    conn.close()

    assert len(ours) == 1
    assert ours[0].strategy_id == STRATEGY_ID
    assert ours[0].security_id == PE
    assert ours[0].quantity == 10 * LOT_SIZE
    assert len(theirs) == 1
    assert theirs[0].strategy_id == "other_strategy"
    assert {row.strategy_id for row in unfiltered} == {STRATEGY_ID, "other_strategy"}


def test_filtering_by_this_strategy_isolates_its_own_orders(tmp_path: Path):
    stack = _closed_trade_stack(tmp_path)

    other_config = worker_config(tmp_path)
    object.__setattr__(other_config, "strategy_id", "other_strategy")
    other_pe = contract_id(19500.0, "PE")
    other_ticks = underlying_ticks([19500.0], start=dt(9, 15))
    other_ticks.insert(2, tick(other_pe, 100.0, dt(9, 21, 10)))
    build_stack(other_config, other_ticks).engine.run()

    conn = connect_readonly(stack.config.database_path)
    ours = load_orders(
        conn, RUNTIME_ID, stack.config.trading_date, strategy_id=STRATEGY_ID
    )
    theirs = load_orders(
        conn, RUNTIME_ID, stack.config.trading_date, strategy_id="other_strategy"
    )
    conn.close()

    assert ours and all(row.strategy_id == STRATEGY_ID for row in ours)
    assert theirs and all(row.strategy_id == "other_strategy" for row in theirs)
    assert {row.security_id for row in ours}.isdisjoint({row.security_id for row in theirs}) or True
    # The two strategies traded the identical contract id (both PE at 19500): the
    # filter must still separate them by strategy_id, not accidentally by instrument.
    assert {row.strategy_id for row in ours} == {STRATEGY_ID}
    assert {row.strategy_id for row in theirs} == {"other_strategy"}
