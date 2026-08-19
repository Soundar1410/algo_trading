"""Bounded staged-entry timeout (Phase 5A gap-closing correction) — driven
through the real ``runtimes.positional_options.worker.build_engine`` wiring
against a real temp SQLite database, exactly like
``test_weekly_delta_neutral_entry.py``/``_restart.py``. See
``tests/integration/_weekly_delta_neutral_fixtures.py`` for the shared
fixture helpers.

Cross-evaluation resume alone (``PositionalMultiLegEngine._resume_pending_
entry``) can retry a staged entry forever if a fresh quote never arrives at
all — this suite proves the durable, persisted deadline
(``_advance_entry_stage``/``_expire_entry_stage``) that bounds it: no quote
ever arriving, a quote arriving just before the deadline, a quote arriving
just after it, and the deadline itself surviving a restart unchanged rather
than being recomputed from the restart's own clock.

No live order API is constructed or called anywhere in this module.
"""

from __future__ import annotations

import contextlib
import shutil
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from _weekly_delta_neutral_fixtures import (
    HEDGE_PUT_STRIKE,
    build_scrip_master,
    build_worker_config,
    chain_fetcher_for,
    cycle_id_for,
    fake_margin_fetcher,
    initial_chain_payload,
    leg_tick,
    open_repository,
    underlying_tick,
)

from common.config.models import ExecutionMode
from common.engine.feed import SimulatedFeed
from common.engine.positional.positional_models import CycleState, LegRole
from common.execution import ExecutionRepository
from common.margin import MarginEstimator
from common.models import Tick
from common.persistence import Database
from runtimes.positional_options.worker import build_engine

IST = ZoneInfo("Asia/Kolkata")
ENTRY_DATE = "2026-08-19"  # a Wednesday
CYCLE_ID = cycle_id_for()
ENTRY_TIMEOUT_SECONDS = 5.0

_SCRIP_MASTER = build_scrip_master()


def _ts(hour: int, minute: int, second: int = 0, *, day: int = 19) -> datetime:
    return datetime(2026, 8, day, hour, minute, second, tzinfo=IST)


def _run(
    tmp_path: Any,
    *,
    db_name: str,
    ticks: list[Tick],
    session_pid: int = 1234,
) -> tuple[Database, ExecutionRepository]:
    """One engine session over ``ticks``, against ``tmp_path/db_name`` —
    mirrors ``test_weekly_delta_neutral_entry.py``'s own single-session
    shape."""
    database, repository = open_repository(tmp_path, db_name)
    session = repository.open_session(
        runtime_id="positional_options", strategy_id="weekly_delta_neutral",
        execution_mode=ExecutionMode.PAPER, process_role="worker", pid=session_pid,
    )
    payload = initial_chain_payload()
    config = build_worker_config(ENTRY_DATE, entry_leg_timeout_seconds=ENTRY_TIMEOUT_SECONDS)
    feed = SimulatedFeed(ticks)
    built = build_engine(
        config, repository=repository, session_id=session.id, feed=feed,
        chain_fetcher=chain_fetcher_for(payload), scrip_master=_SCRIP_MASTER,
        margin_estimator=MarginEstimator(margin_fetcher=fake_margin_fetcher),
        clock=lambda: ticks[0].exchange_time,
    )
    built.engine.run()
    return database, repository


def _entry_stage_row(repository: ExecutionRepository) -> Any:
    return repository.load_cycle_entry_stage(cycle_id=CYCLE_ID)


# ===================================================== no quote ever arrives
def test_no_quote_ever_arriving_fails_the_stage_and_unwinds(tmp_path: Any) -> None:
    entry_ts = _ts(9, 26)
    deadline = entry_ts + timedelta(seconds=ENTRY_TIMEOUT_SECONDS)
    ticks = [
        underlying_tick(entry_ts),  # arms the HEDGE_PUT stage; no leg quote ever delivered
        underlying_tick(deadline + timedelta(seconds=1)),  # past the deadline
    ]
    database, repository = _run(tmp_path, db_name="no_quote.db", ticks=ticks)
    try:
        cycle = repository.load_cycle(cycle_id=CYCLE_ID)
        assert cycle is not None
        assert cycle["state"] == CycleState.FAILED.value, "nothing ever opened — a clean unwind"

        legs = {leg["leg_role"]: leg for leg in repository.load_cycle_legs(cycle_id=CYCLE_ID)}
        assert legs["HEDGE_PUT"]["state"] == "FAILED"
        assert legs["HEDGE_CALL"]["state"] == "EXPIRED"
        assert legs["SHORT_PUT"]["state"] == "EXPIRED"
        assert legs["SHORT_CALL"]["state"] == "EXPIRED"

        stage = _entry_stage_row(repository)
        assert stage is not None
        assert stage["entry_stage_role"] is None
        assert stage["entry_stage_deadline_at"] is None

        # No order was ever placed for the timed-out leg.
        history = repository.cycle_order_history(cycle_id=CYCLE_ID)
        assert not [r for r in history if r["leg_id"] == legs["HEDGE_PUT"]["leg_id"]]
    finally:
        database.close()


# ============================================== quote arrives just before it
def test_quote_arriving_just_before_the_deadline_fills_and_keeps_progressing(
    tmp_path: Any,
) -> None:
    entry_ts = _ts(9, 26)
    deadline = entry_ts + timedelta(seconds=ENTRY_TIMEOUT_SECONDS)
    just_before = deadline - timedelta(seconds=1)
    ticks = [
        underlying_tick(entry_ts),
        leg_tick(HEDGE_PUT_STRIKE, "PE", 18.0, 20.0, just_before),
        underlying_tick(just_before),
    ]
    database, repository = _run(tmp_path, db_name="just_before.db", ticks=ticks)
    try:
        cycle = repository.load_cycle(cycle_id=CYCLE_ID)
        assert cycle is not None
        assert cycle["state"] == CycleState.ENTERING.value, "only one of four legs is open yet"

        legs = {leg["leg_role"]: leg for leg in repository.load_cycle_legs(cycle_id=CYCLE_ID)}
        assert legs["HEDGE_PUT"]["state"] == "OPEN", "filled — the quote beat the deadline"

        # Entry keeps progressing: the *next* stage (HEDGE_CALL) is now the
        # one armed, with its own fresh deadline — never stuck on HEDGE_PUT.
        stage = _entry_stage_row(repository)
        assert stage is not None
        assert stage["entry_stage_role"] == LegRole.HEDGE_CALL.value
        assert stage["entry_stage_deadline_at"] is not None
    finally:
        database.close()


# =============================================== quote arrives just after it
def test_quote_arriving_after_the_deadline_never_reopens_the_stage(tmp_path: Any) -> None:
    entry_ts = _ts(9, 26)
    deadline = entry_ts + timedelta(seconds=ENTRY_TIMEOUT_SECONDS)
    just_after = deadline + timedelta(seconds=1)
    ticks = [
        underlying_tick(entry_ts),
        # A technically "fresh" quote relative to just_after — delivered
        # only *after* the stage has already timed out.
        leg_tick(HEDGE_PUT_STRIKE, "PE", 18.0, 20.0, just_after),
        underlying_tick(just_after),
    ]
    database, repository = _run(tmp_path, db_name="just_after.db", ticks=ticks)
    try:
        cycle = repository.load_cycle(cycle_id=CYCLE_ID)
        assert cycle is not None
        assert cycle["state"] == CycleState.FAILED.value, (
            "the late quote must never reopen or continue the entry"
        )
        legs = {leg["leg_role"]: leg for leg in repository.load_cycle_legs(cycle_id=CYCLE_ID)}
        assert legs["HEDGE_PUT"]["state"] == "FAILED"
        assert legs["HEDGE_PUT"]["entry_price"] is None, "never opened, despite the later quote"

        history = repository.cycle_order_history(cycle_id=CYCLE_ID)
        assert not [r for r in history if r["leg_id"] == legs["HEDGE_PUT"]["leg_id"]]
    finally:
        database.close()


# ======================================= restart before/after the deadline
def test_restart_before_the_persisted_deadline_still_waits(tmp_path: Any) -> None:
    database, _repository = _crash_with_armed_deadline(tmp_path, "restart_before.db")
    database.close()

    entry_ts = _ts(9, 26)
    deadline = entry_ts + timedelta(seconds=ENTRY_TIMEOUT_SECONDS)
    shutil.copyfile(tmp_path / "restart_before.db", tmp_path / "restart_before_copy.db")

    just_before = deadline - timedelta(seconds=1)
    database2, repository2 = _run(
        tmp_path, db_name="restart_before_copy.db", ticks=[underlying_tick(just_before)],
        session_pid=2222,
    )
    try:
        cycle2 = repository2.load_cycle(cycle_id=CYCLE_ID)
        assert cycle2 is not None
        assert cycle2["state"] == CycleState.ENTERING.value, "no premature expiry"
        stage2 = _entry_stage_row(repository2)
        assert stage2 is not None
        assert stage2["entry_stage_role"] == LegRole.HEDGE_PUT.value
        assert stage2["entry_stage_deadline_at"] == deadline.isoformat(), (
            "the *original* deadline, never recomputed from the restart's own clock"
        )
        legs2 = {leg["leg_role"]: leg for leg in repository2.load_cycle_legs(cycle_id=CYCLE_ID)}
        assert legs2["HEDGE_PUT"]["state"] == "PENDING_ORDER"
    finally:
        database2.close()


def test_restart_after_the_persisted_deadline_expires_using_the_original_deadline(
    tmp_path: Any,
) -> None:
    database, _repository = _crash_with_armed_deadline(tmp_path, "restart_after.db")
    database.close()

    entry_ts = _ts(9, 26)
    deadline = entry_ts + timedelta(seconds=ENTRY_TIMEOUT_SECONDS)

    just_after = deadline + timedelta(seconds=1)
    database2, repository2 = _run(
        tmp_path, db_name="restart_after.db", ticks=[underlying_tick(just_after)],
        session_pid=3333,
    )
    try:
        cycle2 = repository2.load_cycle(cycle_id=CYCLE_ID)
        assert cycle2 is not None
        assert cycle2["state"] == CycleState.FAILED.value, (
            "expired immediately on the very first post-restart evaluation, using the "
            "original deadline (a restart-relative countdown would still be waiting)"
        )
        legs2 = {leg["leg_role"]: leg for leg in repository2.load_cycle_legs(cycle_id=CYCLE_ID)}
        assert legs2["HEDGE_PUT"]["state"] == "FAILED"
    finally:
        database2.close()


def _crash_with_armed_deadline(tmp_path: Any, db_name: str) -> tuple[Database, ExecutionRepository]:
    """First session: arm HEDGE_PUT's stage deadline durably, then a
    simulated crash before anything else happens (mirrors
    ``test_weekly_delta_neutral_adjustment.py``'s own ``_SimulatedCrash``
    injection pattern)."""
    entry_ts = _ts(9, 26)
    database, repository = open_repository(tmp_path, db_name)
    session = repository.open_session(
        runtime_id="positional_options", strategy_id="weekly_delta_neutral",
        execution_mode=ExecutionMode.PAPER, process_role="worker", pid=1111,
    )
    payload = initial_chain_payload()
    config = build_worker_config(ENTRY_DATE, entry_leg_timeout_seconds=ENTRY_TIMEOUT_SECONDS)
    feed = SimulatedFeed([underlying_tick(entry_ts)])
    built = build_engine(
        config, repository=repository, session_id=session.id, feed=feed,
        chain_fetcher=chain_fetcher_for(payload), scrip_master=_SCRIP_MASTER,
        margin_estimator=MarginEstimator(margin_fetcher=fake_margin_fetcher),
        clock=lambda: entry_ts,
    )
    _crash_after_deadline_armed(built.engine, LegRole.HEDGE_PUT)
    with contextlib.suppress(BaseException):
        built.engine.run()
    return database, repository


class _SimulatedCrash(BaseException):
    """Stand-in for a hard process kill, injected from inside a persistence
    callback — deliberately *not* an ``Exception`` subclass, so it passes
    straight through every internal ``except Exception`` resilience layer
    the engine already has, exactly like
    ``test_weekly_delta_neutral_adjustment.py``'s own helper of the same
    name (duplicated, not imported: these are sibling acceptance modules,
    not a shared library)."""


def _crash_after_deadline_armed(engine: Any, role: LegRole) -> None:
    """Raises :class:`_SimulatedCrash` the first time the cycle is durably
    persisted with ``entry_stage_role is role`` and a deadline already set —
    i.e. right after ``_advance_entry_stage``'s own arming persist lands,
    before the dynamic subscription it guards is ever requested."""
    real_cb = engine._persist_cycle_cb
    state = {"raised": False}

    def wrapper(cycle: Any) -> None:
        real_cb(cycle)
        if (
            not state["raised"]
            and cycle.entry_stage_role is role
            and cycle.entry_stage_deadline_at is not None
        ):
            state["raised"] = True
            raise _SimulatedCrash()

    engine._persist_cycle_cb = wrapper
