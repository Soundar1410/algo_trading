"""Delta-adjustment acceptance rows (spec section 7, spec section 13.3) —
driven through the real ``runtimes.positional_options.worker.build_engine``
wiring against a real temp SQLite database, exactly like
``test_weekly_delta_neutral_entry.py``/``_restart.py``. See
``tests/integration/_weekly_delta_neutral_fixtures.py`` for the shared
fixture helpers and why a custom ``ScriptedFeed`` is used.

No live order API is constructed or called anywhere in this module.
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from _weekly_delta_neutral_fixtures import (
    EXPIRY_DATE,
    HEDGE_CALL_STRIKE,
    HEDGE_PUT_STRIKE,
    OUTWARD_SHORT_PUT_STRIKE,
    REPAIR_HEDGE_PUT_STRIKE,
    ROLL_DOWN_SHORT_CALL_STRIKE,
    ROLL_UP_SHORT_PUT_STRIKE,
    ROLL_UP_SHORT_PUT_STRIKE_2,
    SHORT_CALL_STRIKE,
    SHORT_PUT_STRIKE,
    ScriptedFeed,
    build_scrip_master,
    build_worker_config,
    chain_fetcher_for,
    cycle_id_for,
    entry_ticks,
    fake_margin_fetcher,
    initial_chain_payload,
    leg_tick,
    open_repository,
    set_leg_delta,
    underlying_tick,
)

from common.config.models import ExecutionMode
from common.engine.positional.positional_models import (
    AdjustmentLifecycle,
    CycleState,
    LegRole,
    LegState,
)
from common.execution import ExecutionRepository
from common.margin import MarginEstimator
from common.models import ExitReason
from common.persistence import Database
from runtimes.positional_options.worker import build_engine

IST = ZoneInfo("Asia/Kolkata")
ENTRY_DATE = "2026-08-19"  # a Wednesday
CYCLE_ID = cycle_id_for()

_SCRIP_MASTER = build_scrip_master()


def _ts(hour: int, minute: int, second: int = 0, day: int = 19) -> datetime:
    return datetime(2026, 8, day, hour, minute, second, tzinfo=IST)


def _run_session(
    tmp_path: Any,
    *,
    db_name: str,
    trading_date: str,
    steps: list[Any],
    payload: dict[str, Any],
    clock_ts: datetime,
    max_adjustments_per_day: int = 1,
    max_adjustments_per_cycle: int = 3,
    min_minutes_between_adjustments: int = 90,
    session_pid: int = 1234,
    before_run: Any = None,
    tolerate_crash: bool = False,
) -> tuple[Database, ExecutionRepository]:
    """One engine session: build, run to completion over ``steps``, and
    return ``(database, repository)`` — left open for the caller's own
    assertions and a possible next session, mirroring the restart test's
    own style. ``before_run``, if given, is called with the built engine
    right before ``.run()`` — the seam a test uses to force one internal
    call to fail (e.g. simulating an unknown close outcome), never to
    change what the engine itself does.

    ``tolerate_crash=True`` is the restart-boundary tests' own seam for
    simulating a genuine process death: ``PositionalMultiLegEngine.run()``
    always re-raises an unhandled exception (spec section 9.2 — never a
    silent square-off), exactly like a real process would simply stop.
    With this flag, this helper is the "supervisor" that observes the
    process die and swallows it, returning whatever was left durably
    committed right up to that point — never anything this helper decided
    on its own, only what the real engine/repository already wrote."""
    database, repository = open_repository(tmp_path, db_name)
    session = repository.open_session(
        runtime_id="positional_options", strategy_id="weekly_delta_neutral",
        execution_mode=ExecutionMode.PAPER, process_role="worker", pid=session_pid,
    )
    config = build_worker_config(
        trading_date,
        max_adjustments_per_day=max_adjustments_per_day,
        max_adjustments_per_cycle=max_adjustments_per_cycle,
        min_minutes_between_adjustments=min_minutes_between_adjustments,
    )
    feed = ScriptedFeed(steps=list(steps), initial_now=clock_ts)
    built = build_engine(
        config, repository=repository, session_id=session.id,
        feed=feed,
        chain_fetcher=chain_fetcher_for(payload),
        scrip_master=_SCRIP_MASTER,
        margin_estimator=MarginEstimator(margin_fetcher=fake_margin_fetcher),
        clock=feed.clock,
    )
    if before_run is not None:
        before_run(built.engine)
    if tolerate_crash:
        # BaseException, not Exception: _SimulatedCrash (below) is
        # deliberately *not* an Exception subclass, so it passes straight
        # through every internal ``except Exception`` layer the engine
        # itself has (persist/leg-persist/incident are all best-effort-
        # wrapped) — only a real process kill should be able to bypass
        # those, and this is this suite's stand-in for one. Swallowed
        # here, exactly as an external process supervisor observing the
        # crash would.
        with contextlib.suppress(BaseException):
            built.engine.run()
    else:
        built.engine.run()
    return database, repository


def _assert_active_condor(repository: ExecutionRepository) -> None:
    cycle = repository.load_cycle(cycle_id=CYCLE_ID)
    assert cycle is not None and cycle["state"] == CycleState.ACTIVE.value, (
        "fixture sanity: entry must produce an ACTIVE 4-leg cycle"
    )


# =============================================== 7.3/9.5 — restart boundaries
#
# Nine durable failure-injection/restart boundaries across the claim/submit/
# reconcile adjustment state machine (spec section 7.3/9.5) — every scenario
# below is produced by driving the *real* engine through production
# repository APIs and an injected persistence/gateway failure, exactly like
# ``test_unknown_close_outcome_blocks_replacement_and_new_entries`` above;
# never by fabricating a ``strategy_cycles``/``strategy_cycle_legs`` row
# directly.
#
# The original short put's own leg id is deterministic (``Cycle.next_leg_id``
# is ``f"{cycle_id}:{role}:{sequence}"``, and the original leg is always
# sequence 1) — used below to target an injected failure at one *specific*
# leg's persistence, never every leg's.
_OLD_SHORT_PUT_LEG_ID = f"{CYCLE_ID}:SHORT_PUT:1"
_REPLACEMENT_SHORT_PUT_LEG_ID = f"{CYCLE_ID}:SHORT_PUT:2"


class _SimulatedCrash(BaseException):
    """Stand-in for a hard process kill (SIGKILL/os._exit) injected from
    inside a persistence callback — deliberately *not* an ``Exception``
    subclass, so it passes straight through every internal
    ``except Exception`` resilience layer the engine already has
    (``_persist_cycle``/``_persist_leg``/``_record_incident`` are all
    best-effort-wrapped) instead of being gracefully handled — only a real
    crash could bypass those, and this is this suite's stand-in for one.
    Always paired with ``_run_session(tolerate_crash=True)``.
    """


def _trigger_steps(
    bump: Any, replacement_tick: Any, *, t1: datetime, t2: datetime, t3: datetime
) -> list[Any]:
    """The standard three-consecutive-confirmation trigger sequence shared
    by every boundary test below (mirrors the non-restart adjustment tests
    above) — a strongly negative net delta, confirmed on three consecutive
    evaluations, with the replacement's own fresh quote delivered ahead of
    the confirming (third) tick."""
    return [bump, underlying_tick(t1), underlying_tick(t2), replacement_tick, underlying_tick(t3)]


def _bump_call_delta_step(payload: dict[str, Any]) -> Any:
    def _bump() -> None:
        set_leg_delta(payload, SHORT_CALL_STRIKE, "CE", 0.37)

    return _bump


def _last_incident(database: Database) -> Any:
    return (
        database.connect()
        .execute(
            "SELECT severity, component, message FROM errors "
            "WHERE component = 'positional_multi_leg_engine.incident' "
            "ORDER BY id DESC LIMIT 1"
        )
        .fetchone()
    )


def _incident_count(database: Database) -> int:
    row = (
        database.connect()
        .execute(
            "SELECT COUNT(*) AS n FROM errors "
            "WHERE component = 'positional_multi_leg_engine.incident'"
        )
        .fetchone()
    )
    return int(row["n"])


def _fail_cycle_persist_once_before_write(engine: Any, state_value: str) -> None:
    """The engine's ``_persist_cycle_cb`` raises once, *without* performing
    the real write, the first time the cycle being persisted has
    ``pending_adjustment_state == state_value`` — simulating an ordinary
    persistence failure the engine's own ``_persist_cycle`` catches and
    (for a critical call) converts to ``MultiLegDurabilityError``, never
    propagating past ``_roll_short``'s own try/except. Every other call
    delegates to the real callback unchanged."""
    real_cb = engine._persist_cycle_cb
    state = {"raised": False}

    def wrapper(cycle: Any) -> None:
        if not state["raised"] and cycle.pending_adjustment_state == state_value:
            state["raised"] = True
            raise RuntimeError("injected cycle-persistence failure (before write)")
        real_cb(cycle)

    engine._persist_cycle_cb = wrapper


def _fail_cycle_persist_once_after_write(engine: Any, state_value: str) -> None:
    """The engine's ``_persist_cycle_cb`` performs the real, durable write
    as normal, then raises once — the first time the cycle just persisted
    had ``pending_adjustment_state == state_value`` — simulating the
    write's own confirmation being lost even though it actually landed
    (e.g. the process is killed, or the connection drops, immediately
    after commit). A critical call's exception still reaches
    ``_roll_short``'s try/except (reverting in-memory state that the DB no
    longer agrees with); every other call delegates unchanged."""
    real_cb = engine._persist_cycle_cb
    state = {"raised": False}

    def wrapper(cycle: Any) -> None:
        real_cb(cycle)
        if not state["raised"] and cycle.pending_adjustment_state == state_value:
            state["raised"] = True
            raise RuntimeError("injected cycle-persistence failure (after write)")

    engine._persist_cycle_cb = wrapper


def _crash_after_cycle_persisted(engine: Any, state_value: str) -> None:
    """Like :func:`_fail_cycle_persist_once_after_write`, but raises
    :class:`_SimulatedCrash` — a genuine, uncaught process-kill simulation
    — instead of an ordinary exception, for the one non-critical persist
    point (``AWAITING_NEXT_CANDLE``) whose own try/except would otherwise
    silently swallow anything short of that and let ``_roll_short``
    continue running as if nothing happened."""
    real_cb = engine._persist_cycle_cb
    state = {"raised": False}

    def wrapper(cycle: Any) -> None:
        real_cb(cycle)
        if not state["raised"] and cycle.pending_adjustment_state == state_value:
            state["raised"] = True
            raise _SimulatedCrash()

    engine._persist_cycle_cb = wrapper


def _crash_after_leg_persisted(engine: Any, leg_id: str, state: LegState) -> None:
    """Like :func:`_crash_after_cycle_persisted`, but for
    ``_persist_cycle_leg_cb``: performs the real, durable write for
    ``leg_id`` in ``state`` as normal, then raises :class:`_SimulatedCrash`
    — simulating a process kill immediately after that one leg's row
    became durable, before anything the caller does next (e.g. submitting
    its order) ever runs."""
    real_cb = engine._persist_cycle_leg_cb
    state_box = {"raised": False}

    def wrapper(leg: Any) -> None:
        real_cb(leg)
        if not state_box["raised"] and leg.leg_id == leg_id and leg.state is state:
            state_box["raised"] = True
            raise _SimulatedCrash()

    engine._persist_cycle_leg_cb = wrapper


def _fail_leg_persist_once(engine: Any, leg_id: str, state: LegState) -> None:
    """The engine's ``_persist_cycle_leg_cb`` performs every other leg's
    persist normally, but raises once — the first time ``leg_id`` is
    persisted in ``state`` — simulating that one leg's own best-effort
    projection write failing right after the real broker-side fill/close it
    describes already succeeded (spec section 9.4)."""
    real_cb = engine._persist_cycle_leg_cb
    state_box = {"raised": False}

    def wrapper(leg: Any) -> None:
        if not state_box["raised"] and leg.leg_id == leg_id and leg.state is state:
            state_box["raised"] = True
            raise RuntimeError(f"injected leg-persistence failure for {leg_id} ({state.value})")
        real_cb(leg)

    engine._persist_cycle_leg_cb = wrapper


def _break_close_for_security(security_id: str) -> Any:
    """Mirrors ``test_unknown_close_outcome_blocks_replacement_and_new_
    entries``'s own ``_break_close`` helper, factored out for reuse: makes
    ``engine.positions.close`` raise for exactly one security id, leaving
    every other close untouched."""

    def _before_run(engine: Any) -> None:
        real_close = engine.positions.close

        def _raising_close(sid: str, *args: Any, **kwargs: Any) -> Any:
            if sid == security_id:
                raise RuntimeError("simulated paper-broker close failure")
            return real_close(sid, *args, **kwargs)

        engine.positions.close = _raising_close

    return _before_run


def _break_open_for_security(security_id: str) -> Any:
    """The open-side counterpart of :func:`_break_close_for_security`:
    makes ``engine.positions.open`` raise for exactly one security id — the
    replacement's own submission — leaving every other leg's open
    untouched. ``_open_leg_now``'s own contract resolves this definitively
    to ``LegState.FAILED`` (this engine's opening path has no ambiguous
    "unknown" outcome the way closing does), never leaves it pending."""

    def _before_run(engine: Any) -> None:
        real_open = engine.positions.open

        def _raising_open(contract: Any, *args: Any, **kwargs: Any) -> Any:
            if contract.security_id == security_id:
                raise RuntimeError("simulated paper-broker open failure")
            return real_open(contract, *args, **kwargs)

        engine.positions.open = _raising_open

    return _before_run


# =========================================================== 13.3 — trigger
def test_three_consecutive_trigger_checks_are_required_and_roll_the_put_upward(
    tmp_path: Any,
) -> None:
    """Negative net delta -> the untested side is the short put; it is
    rolled *upward* — only after three consecutive evaluations at or beyond
    the trigger (spec 7.1/7.2), never on the first or second."""
    entry_ts = _ts(9, 26)
    payload = initial_chain_payload()
    steps: list[Any] = [*entry_ticks(entry_ts)]

    # Bump the short call's delta so the portfolio net delta goes strongly
    # negative (spec 7.2's own reasoning: shorting a call is a bearish
    # signed-delta contribution; deepening it pulls the whole basket
    # negative) — see the module-level comment on the exact arithmetic in
    # docs/IMPLEMENTATION_STATUS_AND_RUNBOOK.md section 11.10 Phase 3.
    def _bump_call_delta() -> None:
        set_leg_delta(payload, SHORT_CALL_STRIKE, "CE", 0.37)

    steps.append(_bump_call_delta)
    check_1 = _ts(9, 31)
    check_2 = _ts(9, 36)
    check_3 = _ts(9, 41)
    steps.append(underlying_tick(check_1))
    steps.append(underlying_tick(check_2))
    # The replacement candidate's own fresh quote must exist before the
    # confirming evaluation that actually rolls — delivered ahead of the
    # third check, exactly as entry's own leg ticks precede its underlying
    # tick.
    steps.append(leg_tick(ROLL_UP_SHORT_PUT_STRIKE, "PE", 95.0, 97.0, check_3))
    steps.append(underlying_tick(check_3))

    database, repository = _run_session(
        tmp_path, db_name="three_checks.db", trading_date=ENTRY_DATE,
        steps=steps, payload=payload, clock_ts=entry_ts,
    )
    try:
        _assert_active_condor(repository)
        cycle = repository.load_cycle(cycle_id=CYCLE_ID)
        assert cycle is not None
        assert cycle["adjustments_this_cycle"] == 1
        assert cycle["adjustments_today"] == 1
        assert cycle["last_adjustment_at"] is not None

        legs = repository.load_cycle_legs(cycle_id=CYCLE_ID)
        short_put_legs = [leg for leg in legs if leg["leg_role"] == LegRole.SHORT_PUT.value]
        assert len(short_put_legs) == 2, "the original short put plus its replacement"
        original = next(leg for leg in short_put_legs if not leg["is_replacement"])
        replacement = next(leg for leg in short_put_legs if leg["is_replacement"])
        assert original["state"] == "CLOSED"
        assert original["strike"] == SHORT_PUT_STRIKE
        assert replacement["state"] == "OPEN"
        assert replacement["strike"] == ROLL_UP_SHORT_PUT_STRIKE  # rolled UPWARD
        assert replacement["strike"] > original["strike"]

        # The other three legs are completely untouched.
        for role, strike in (
            (LegRole.HEDGE_PUT, HEDGE_PUT_STRIKE),
            (LegRole.HEDGE_CALL, HEDGE_CALL_STRIKE),
            (LegRole.SHORT_CALL, SHORT_CALL_STRIKE),
        ):
            matching = [leg for leg in legs if leg["leg_role"] == role.value]
            assert len(matching) == 1
            assert matching[0]["state"] == "OPEN"
            assert matching[0]["strike"] == strike
    finally:
        database.close()


def test_positive_net_delta_rolls_the_call_downward(tmp_path: Any) -> None:
    """The symmetric case (spec 7.2): a strongly positive net delta rolls
    the short *call* downward, never the put."""
    entry_ts = _ts(9, 26)
    payload = initial_chain_payload()
    steps: list[Any] = [*entry_ticks(entry_ts)]

    def _bump_put_delta() -> None:
        set_leg_delta(payload, SHORT_PUT_STRIKE, "PE", -0.37)

    steps.append(_bump_put_delta)
    check_1 = _ts(9, 31)
    check_2 = _ts(9, 36)
    check_3 = _ts(9, 41)
    steps.append(underlying_tick(check_1))
    steps.append(underlying_tick(check_2))
    steps.append(leg_tick(ROLL_DOWN_SHORT_CALL_STRIKE, "CE", 93.0, 95.0, check_3))
    steps.append(underlying_tick(check_3))

    database, repository = _run_session(
        tmp_path, db_name="positive_delta.db", trading_date=ENTRY_DATE,
        steps=steps, payload=payload, clock_ts=entry_ts,
    )
    try:
        legs = repository.load_cycle_legs(cycle_id=CYCLE_ID)
        short_call_legs = [leg for leg in legs if leg["leg_role"] == LegRole.SHORT_CALL.value]
        assert len(short_call_legs) == 2
        original = next(leg for leg in short_call_legs if not leg["is_replacement"])
        replacement = next(leg for leg in short_call_legs if leg["is_replacement"])
        assert original["state"] == "CLOSED"
        assert replacement["state"] == "OPEN"
        assert replacement["strike"] == ROLL_DOWN_SHORT_CALL_STRIKE  # rolled DOWNWARD
        assert replacement["strike"] < original["strike"]

        short_put_legs = [leg for leg in legs if leg["leg_role"] == LegRole.SHORT_PUT.value]
        assert len(short_put_legs) == 1, "the put side must be untouched by a call-side roll"
        assert short_put_legs[0]["state"] == "OPEN"
    finally:
        database.close()


def test_one_or_two_checks_never_roll(tmp_path: Any) -> None:
    """Spec 7.1: three consecutive checks are *required* — one or two must
    never be enough, whatever the delta."""
    entry_ts = _ts(9, 26)
    payload = initial_chain_payload()
    steps: list[Any] = [*entry_ticks(entry_ts)]

    def _bump_call_delta() -> None:
        set_leg_delta(payload, SHORT_CALL_STRIKE, "CE", 0.37)

    steps.append(_bump_call_delta)
    steps.append(underlying_tick(_ts(9, 31)))
    steps.append(underlying_tick(_ts(9, 36)))

    database, repository = _run_session(
        tmp_path, db_name="two_checks.db", trading_date=ENTRY_DATE,
        steps=steps, payload=payload, clock_ts=entry_ts,
    )
    try:
        cycle = repository.load_cycle(cycle_id=CYCLE_ID)
        assert cycle is not None
        assert cycle["adjustments_this_cycle"] == 0
        legs = repository.load_cycle_legs(cycle_id=CYCLE_ID)
        assert all(leg["state"] == "OPEN" for leg in legs)
        assert len(legs) == 4
    finally:
        database.close()


def test_confirmation_counter_resets_inside_the_band(tmp_path: Any) -> None:
    """Spec 7.1: "reset the confirmation counter when delta returns below
    the trigger" — two triggering checks, one back inside the band, then
    two more triggering checks must still not be enough (a reset counter
    starting over, not a running total)."""
    entry_ts = _ts(9, 26)
    payload = initial_chain_payload()
    steps: list[Any] = [*entry_ticks(entry_ts)]

    def _bump() -> None:
        set_leg_delta(payload, SHORT_CALL_STRIKE, "CE", 0.37)

    def _relax() -> None:
        set_leg_delta(payload, SHORT_CALL_STRIKE, "CE", 0.20)  # back inside the band

    steps.append(_bump)
    steps.append(underlying_tick(_ts(9, 31)))  # confirmation 1
    steps.append(underlying_tick(_ts(9, 36)))  # confirmation 2
    steps.append(_relax)
    steps.append(underlying_tick(_ts(9, 41)))  # inside the band -> reset to 0
    steps.append(_bump)
    steps.append(underlying_tick(_ts(9, 46)))  # confirmation 1 again (not 3)
    steps.append(underlying_tick(_ts(9, 51)))  # confirmation 2 again (not 4)

    database, repository = _run_session(
        tmp_path, db_name="reset_band.db", trading_date=ENTRY_DATE,
        steps=steps, payload=payload, clock_ts=entry_ts,
    )
    try:
        cycle = repository.load_cycle(cycle_id=CYCLE_ID)
        assert cycle is not None
        assert cycle["adjustments_this_cycle"] == 0, (
            "the counter reset means only 2 consecutive checks have accrued "
            "since the reset, never enough to roll"
        )
        legs = repository.load_cycle_legs(cycle_id=CYCLE_ID)
        assert all(leg["state"] == "OPEN" for leg in legs)
    finally:
        database.close()



# ===================================================== 7.1/13.3 — limits
def test_max_one_adjustment_per_day_blocks_a_second_same_day_roll(tmp_path: Any) -> None:
    entry_ts = _ts(9, 26)
    payload = initial_chain_payload()
    steps: list[Any] = [*entry_ticks(entry_ts)]

    def _bump() -> None:
        set_leg_delta(payload, SHORT_CALL_STRIKE, "CE", 0.37)

    steps.append(_bump)
    steps.append(underlying_tick(_ts(9, 31)))
    steps.append(underlying_tick(_ts(9, 36)))
    steps.append(leg_tick(ROLL_UP_SHORT_PUT_STRIKE, "PE", 95.0, 97.0, _ts(9, 41)))
    steps.append(underlying_tick(_ts(9, 41)))  # roll 1 completes

    # A fresh, stronger trigger later the same day — day limit (1) must
    # block it even though the cycle limit and cooldown would both permit
    # it (max_adjustments_per_cycle/min_minutes_between_adjustments are set
    # generously below specifically to isolate the day limit).
    def _bump_again() -> None:
        set_leg_delta(payload, SHORT_CALL_STRIKE, "CE", 0.40)

    steps.append(_bump_again)
    steps.append(underlying_tick(_ts(11, 30)))

    database, repository = _run_session(
        tmp_path, db_name="day_limit.db", trading_date=ENTRY_DATE,
        steps=steps, payload=payload, clock_ts=entry_ts,
        max_adjustments_per_day=1, max_adjustments_per_cycle=5,
        min_minutes_between_adjustments=1,
    )
    try:
        cycle = repository.load_cycle(cycle_id=CYCLE_ID)
        assert cycle is not None
        assert cycle["adjustments_today"] == 1
        assert cycle["adjustments_this_cycle"] == 1
        legs = repository.load_cycle_legs(cycle_id=CYCLE_ID)
        short_put_legs = [leg for leg in legs if leg["leg_role"] == LegRole.SHORT_PUT.value]
        assert len(short_put_legs) == 2, "exactly one roll, never a second"
    finally:
        database.close()


def test_max_three_adjustments_per_cycle_blocks_a_fourth(tmp_path: Any) -> None:
    """Isolates the *cycle* counter from the day counter: set
    ``max_adjustments_per_cycle=1`` (a small, easy-to-cross ceiling — the
    mechanism under test is identical whatever the configured number is)
    with a generous day limit and cooldown, and confirm a second trigger
    the same day is still blocked, purely by the cycle counter."""
    entry_ts = _ts(9, 26)
    payload = initial_chain_payload()
    steps: list[Any] = [*entry_ticks(entry_ts)]

    def _bump() -> None:
        set_leg_delta(payload, SHORT_CALL_STRIKE, "CE", 0.37)

    steps.append(_bump)
    steps.append(underlying_tick(_ts(9, 31)))
    steps.append(underlying_tick(_ts(9, 36)))
    steps.append(leg_tick(ROLL_UP_SHORT_PUT_STRIKE, "PE", 95.0, 97.0, _ts(9, 41)))
    steps.append(underlying_tick(_ts(9, 41)))  # roll 1 completes; cycle counter -> 1

    def _bump_again() -> None:
        set_leg_delta(payload, SHORT_CALL_STRIKE, "CE", 0.40)

    steps.append(_bump_again)
    steps.append(underlying_tick(_ts(11, 30)))

    database, repository = _run_session(
        tmp_path, db_name="cycle_limit.db", trading_date=ENTRY_DATE,
        steps=steps, payload=payload, clock_ts=entry_ts,
        max_adjustments_per_day=5, max_adjustments_per_cycle=1,
        min_minutes_between_adjustments=1,
    )
    try:
        cycle = repository.load_cycle(cycle_id=CYCLE_ID)
        assert cycle is not None
        assert cycle["adjustments_this_cycle"] == 1
        legs = repository.load_cycle_legs(cycle_id=CYCLE_ID)
        short_put_legs = [leg for leg in legs if leg["leg_role"] == LegRole.SHORT_PUT.value]
        assert len(short_put_legs) == 2
    finally:
        database.close()


def test_ninety_minute_cooldown_blocks_an_early_second_roll(tmp_path: Any) -> None:
    entry_ts = _ts(9, 26)
    payload = initial_chain_payload()
    steps: list[Any] = [*entry_ticks(entry_ts)]

    def _bump() -> None:
        set_leg_delta(payload, SHORT_CALL_STRIKE, "CE", 0.37)

    steps.append(_bump)
    steps.append(underlying_tick(_ts(9, 31)))
    steps.append(underlying_tick(_ts(9, 36)))
    steps.append(leg_tick(ROLL_UP_SHORT_PUT_STRIKE, "PE", 95.0, 97.0, _ts(9, 41)))
    steps.append(underlying_tick(_ts(9, 41)))  # roll 1 completes at 09:41

    def _bump_again() -> None:
        set_leg_delta(payload, SHORT_CALL_STRIKE, "CE", 0.40)

    steps.append(_bump_again)
    # 89 minutes later — inside the 90-minute cooldown.
    steps.append(underlying_tick(_ts(11, 10)))

    database, repository = _run_session(
        tmp_path, db_name="cooldown.db", trading_date=ENTRY_DATE,
        steps=steps, payload=payload, clock_ts=entry_ts,
        max_adjustments_per_day=5, max_adjustments_per_cycle=5,
        min_minutes_between_adjustments=90,
    )
    try:
        cycle = repository.load_cycle(cycle_id=CYCLE_ID)
        assert cycle is not None
        assert cycle["adjustments_this_cycle"] == 1, (
            "still inside the 90-minute cooldown from the first roll"
        )
    finally:
        database.close()


# ===================================================== 9.2/13.3 — restart
def test_daily_counter_resets_next_trading_day_while_cycle_counter_persists(
    tmp_path: Any,
) -> None:
    """Real restart, real recovery (spec section 9.5): a second roll on the
    *next* trading day is permitted (the daily counter reset at a verified
    new trading date) while the cycle counter keeps accumulating rather
    than resetting alongside it."""
    entry_ts = _ts(9, 26)
    payload = initial_chain_payload()
    db_name = "daily_reset.db"

    # --------------------------------------------------------- day 1: entry + roll 1
    steps_day1: list[Any] = [*entry_ticks(entry_ts)]

    def _bump() -> None:
        set_leg_delta(payload, SHORT_CALL_STRIKE, "CE", 0.37)

    steps_day1.append(_bump)
    steps_day1.append(underlying_tick(_ts(9, 31)))
    steps_day1.append(underlying_tick(_ts(9, 36)))
    steps_day1.append(leg_tick(ROLL_UP_SHORT_PUT_STRIKE, "PE", 95.0, 97.0, _ts(9, 41)))
    steps_day1.append(underlying_tick(_ts(9, 41)))

    database1, repository1 = _run_session(
        tmp_path, db_name=db_name, trading_date=ENTRY_DATE,
        steps=steps_day1, payload=payload, clock_ts=entry_ts,
        max_adjustments_per_day=1, max_adjustments_per_cycle=5,
        min_minutes_between_adjustments=1,
    )
    cycle_after_day1 = repository1.load_cycle(cycle_id=CYCLE_ID)
    assert cycle_after_day1 is not None
    assert cycle_after_day1["adjustments_today"] == 1
    assert cycle_after_day1["adjustments_this_cycle"] == 1
    database1.close()

    # ------------------------------------------------ day 2: restart + roll 2
    restart_ts = _ts(9, 31, day=20)

    def _bump_again() -> None:
        set_leg_delta(payload, SHORT_CALL_STRIKE, "CE", 0.40)
        # Bring the day-2 replacement candidate into its target band —
        # deliberately out of band in the shared baseline payload so it
        # cannot silently become day 1's own candidate too. -0.23 (the
        # tolerance edge) rather than something closer to -0.20: day 1's
        # roll already moved the open short put to -0.22, so only a
        # *larger*-magnitude candidate genuinely reduces projected delta
        # further in the same direction — the original -0.20 strike is
        # still chain-listed and would be tried first (closest to target)
        # but correctly rejected for moving the wrong way.
        set_leg_delta(payload, ROLL_UP_SHORT_PUT_STRIKE_2, "PE", -0.23)

    steps_day2: list[Any] = [underlying_tick(restart_ts), _bump_again]
    steps_day2.append(underlying_tick(_ts(9, 36, day=20)))
    steps_day2.append(underlying_tick(_ts(9, 41, day=20)))
    steps_day2.append(
        leg_tick(ROLL_UP_SHORT_PUT_STRIKE_2, "PE", 96.0, 98.0, _ts(9, 46, day=20))
    )
    steps_day2.append(underlying_tick(_ts(9, 46, day=20)))

    database2, repository2 = _run_session(
        tmp_path, db_name=db_name, trading_date="2026-08-20",
        steps=steps_day2, payload=payload, clock_ts=restart_ts,
        max_adjustments_per_day=1, max_adjustments_per_cycle=5,
        min_minutes_between_adjustments=1, session_pid=2222,
    )
    try:
        cycle_after_day2 = repository2.load_cycle(cycle_id=CYCLE_ID)
        assert cycle_after_day2 is not None
        assert cycle_after_day2["adjustments_today"] == 1, (
            "reset to 0 at the new trading date, then incremented once more today"
        )
        assert cycle_after_day2["adjustments_this_cycle"] == 2, (
            "the cycle counter is never reset by a new trading date — it keeps accruing"
        )
        legs = repository2.load_cycle_legs(cycle_id=CYCLE_ID)
        short_put_legs = [leg for leg in legs if leg["leg_role"] == LegRole.SHORT_PUT.value]
        assert len(short_put_legs) == 3, "original + two real replacements"
        open_put = next(leg for leg in short_put_legs if leg["state"] == "OPEN")
        assert open_put["strike"] == ROLL_UP_SHORT_PUT_STRIKE_2
    finally:
        database2.close()


# ================================================== 7.3/13.3 — unknown close
def test_unknown_close_outcome_blocks_replacement_and_new_entries(tmp_path: Any) -> None:
    """Spec section 7.3: "never open the replacement while the old short's
    close is unknown." Forces the old short's own close attempt to raise
    (a genuine, if rare, paper-broker/position-manager failure) and proves
    no replacement is ever opened and new entries are blocked."""
    entry_ts = _ts(9, 26)
    payload = initial_chain_payload()
    steps: list[Any] = [*entry_ticks(entry_ts)]

    def _bump() -> None:
        set_leg_delta(payload, SHORT_CALL_STRIKE, "CE", 0.37)

    steps.append(_bump)
    steps.append(underlying_tick(_ts(9, 31)))
    steps.append(underlying_tick(_ts(9, 36)))
    steps.append(leg_tick(ROLL_UP_SHORT_PUT_STRIKE, "PE", 95.0, 97.0, _ts(9, 41)))
    steps.append(underlying_tick(_ts(9, 41)))

    def _break_close(engine: Any) -> None:
        real_close = engine.positions.close

        def _raising_close(security_id: str, *args: Any, **kwargs: Any) -> Any:
            if security_id == "90002":  # the original short put — the leg being rolled
                raise RuntimeError("simulated paper-broker close failure")
            return real_close(security_id, *args, **kwargs)

        engine.positions.close = _raising_close

    database, repository = _run_session(
        tmp_path, db_name="unknown_close.db", trading_date=ENTRY_DATE,
        steps=steps, payload=payload, clock_ts=entry_ts, before_run=_break_close,
    )
    try:
        cycle = repository.load_cycle(cycle_id=CYCLE_ID)
        assert cycle is not None
        legs = repository.load_cycle_legs(cycle_id=CYCLE_ID)
        short_put_legs = [leg for leg in legs if leg["leg_role"] == LegRole.SHORT_PUT.value]
        assert len(short_put_legs) == 1, "no replacement was ever created"
        assert short_put_legs[0]["state"] == "CLOSE_SUBMISSION_UNKNOWN"
        assert short_put_legs[0]["strike"] == SHORT_PUT_STRIKE

        # Entries are now blocked (positional_engine.block_entries) — proven
        # through the same durable signal the dashboard/operator reads:
        # cycle.day_blocked_reason is untouched (that's an entry-cycle-level
        # field), but no new cycle/replacement leg exists, and every other
        # leg is exactly as entry left it.
        for role, strike in (
            (LegRole.HEDGE_PUT, HEDGE_PUT_STRIKE),
            (LegRole.HEDGE_CALL, HEDGE_CALL_STRIKE),
            (LegRole.SHORT_CALL, SHORT_CALL_STRIKE),
        ):
            matching = [leg for leg in legs if leg["leg_role"] == role.value]
            assert len(matching) == 1
            assert matching[0]["state"] == "OPEN"
            assert matching[0]["strike"] == strike
    finally:
        database.close()


# ================================================ 7.2/12.3 — hedge repair
def test_hedge_repair_precedes_a_pending_roll(tmp_path: Any) -> None:
    """Spec section 7.2/12.3 step 2 / spec 6.2 priority (hedge repair, step
    11, outranks normal delta adjustment, step 12): a deficient hedge is
    repaired first, even in the same evaluation a delta trigger would
    otherwise roll a short — the roll only proceeds on a later evaluation,
    once the hedge is whole again."""
    entry_ts = _ts(9, 26)
    payload = initial_chain_payload()
    engine_box: list[Any] = []
    steps: list[Any] = [*entry_ticks(entry_ts)]

    def _bump_call_delta() -> None:
        set_leg_delta(payload, SHORT_CALL_STRIKE, "CE", 0.37)

    def _close_hedge_put() -> None:
        engine = engine_box[0]
        hedge_leg = engine._cycle.current_leg(LegRole.HEDGE_PUT)
        assert hedge_leg is not None and hedge_leg.state.value == "OPEN"
        closed = engine._close_leg_safely(
            hedge_leg, entry_ts, ExitReason.STRATEGY_EXIT, reason_text="simulated anomaly"
        )
        assert closed, "fixture sanity: the simulated hedge close must succeed cleanly"
        # The original strike's own delta drifted out of the hedge target
        # band too (the reason this hedge needed repairing in the first
        # place) — otherwise the search would happily re-open the identical
        # strike (a *correct* repair outcome in general, but not what this
        # test is trying to prove: that the search finds a genuinely
        # different valid candidate, not just literally what just closed).
        set_leg_delta(payload, HEDGE_PUT_STRIKE, "PE", -0.35)

    def _repair_hedge_delta_in_band() -> None:
        set_leg_delta(payload, REPAIR_HEDGE_PUT_STRIKE, "PE", -0.06)

    steps.append(_bump_call_delta)
    steps.append(_close_hedge_put)
    steps.append(_repair_hedge_delta_in_band)
    repair_ts = _ts(9, 31)
    steps.append(leg_tick(REPAIR_HEDGE_PUT_STRIKE, "PE", 19.0, 21.0, repair_ts))
    steps.append(underlying_tick(repair_ts))  # evaluation 1: repair, not adjust

    database, repository = _run_session(
        tmp_path, db_name="hedge_repair.db", trading_date=ENTRY_DATE,
        steps=steps, payload=payload, clock_ts=entry_ts,
        before_run=lambda engine: engine_box.append(engine),
    )
    try:
        cycle = repository.load_cycle(cycle_id=CYCLE_ID)
        assert cycle is not None
        assert cycle["adjustments_this_cycle"] == 0, (
            "the hedge repair evaluation must never also count as an adjustment"
        )
        legs = repository.load_cycle_legs(cycle_id=CYCLE_ID)
        hedge_put_legs = [leg for leg in legs if leg["leg_role"] == LegRole.HEDGE_PUT.value]
        assert len(hedge_put_legs) == 2, "the original hedge plus its repair"
        original = next(leg for leg in hedge_put_legs if not leg["is_replacement"])
        repaired = next(leg for leg in hedge_put_legs if leg["is_replacement"])
        assert original["state"] == "CLOSED"
        assert repaired["state"] == "OPEN"
        assert repaired["strike"] == REPAIR_HEDGE_PUT_STRIKE

        # The short put — the leg a delta trigger would otherwise roll —
        # is completely untouched by this evaluation.
        short_put_legs = [leg for leg in legs if leg["leg_role"] == LegRole.SHORT_PUT.value]
        assert len(short_put_legs) == 1
        assert short_put_legs[0]["state"] == "OPEN"
        assert short_put_legs[0]["strike"] == SHORT_PUT_STRIKE
    finally:
        database.close()


# ============================================================ 8/13.3 — cutoff
def test_no_normal_adjustment_after_the_expiry_day_cutoff(tmp_path: Any) -> None:
    """Spec section 8: no normal delta adjustment from 14:30 on the actual
    expiry day — real restart onto the actual persisted expiry date, then a
    genuinely triggering delta that must still never roll anything."""
    entry_ts = _ts(9, 26)
    payload = initial_chain_payload()
    db_name = "expiry_cutoff.db"

    database1, repository1 = _run_session(
        tmp_path, db_name=db_name, trading_date=ENTRY_DATE,
        steps=entry_ticks(entry_ts), payload=payload, clock_ts=entry_ts,
    )
    _assert_active_condor(repository1)
    database1.close()

    expiry_restart_ts = datetime(2026, 8, 26, 14, 26, tzinfo=IST)

    def _bump() -> None:
        set_leg_delta(payload, SHORT_CALL_STRIKE, "CE", 0.37)

    steps: list[Any] = [underlying_tick(expiry_restart_ts), _bump]
    steps.append(underlying_tick(datetime(2026, 8, 26, 14, 31, tzinfo=IST)))
    steps.append(underlying_tick(datetime(2026, 8, 26, 14, 33, tzinfo=IST)))
    steps.append(underlying_tick(datetime(2026, 8, 26, 14, 35, tzinfo=IST)))

    database2, repository2 = _run_session(
        tmp_path, db_name=db_name, trading_date="2026-08-26",
        steps=steps, payload=payload, clock_ts=expiry_restart_ts, session_pid=2222,
    )
    try:
        cycle = repository2.load_cycle(cycle_id=CYCLE_ID)
        assert cycle is not None
        assert cycle["adjustments_this_cycle"] == 0
        legs = repository2.load_cycle_legs(cycle_id=CYCLE_ID)
        short_put_legs = [leg for leg in legs if leg["leg_role"] == LegRole.SHORT_PUT.value]
        assert len(short_put_legs) == 1, "no roll was ever attempted past the cutoff"
        assert short_put_legs[0]["state"] == "OPEN"
    finally:
        database2.close()


# ============================================ 8/Phase 3A — 12:00 inward-roll rule
def _enter_for_expiry_day_test(tmp_path: Any, db_name: str) -> dict[str, Any]:
    entry_ts = _ts(9, 26)
    payload = initial_chain_payload()
    database, repository = _run_session(
        tmp_path, db_name=db_name, trading_date=ENTRY_DATE,
        steps=entry_ticks(entry_ts), payload=payload, clock_ts=entry_ts,
    )
    _assert_active_condor(repository)
    database.close()
    return payload


def test_inward_short_put_roll_prohibited_at_1200_on_expiry_day_exits(tmp_path: Any) -> None:
    """Spec section 8 (Phase 3A correction): from 12:00 IST on the actual
    expiry day, an inward short-put replacement (closer to spot than the
    short it replaces — here, the only qualifying candidate,
    ROLL_UP_SHORT_PUT_STRIKE at 23600, closer to spot 24000 than the
    original 23500) is prohibited; with no non-inward candidate available,
    the strategy exits the whole cycle rather than silently doing
    nothing."""
    payload = _enter_for_expiry_day_test(tmp_path, "inward_put_1200.db")
    restart_ts = _ts(11, 58, day=26)

    def _bump() -> None:
        set_leg_delta(payload, SHORT_CALL_STRIKE, "CE", 0.37)

    steps: list[Any] = [
        _bump,
        underlying_tick(_ts(11, 58, day=26)),
        underlying_tick(_ts(11, 59, day=26)),
        underlying_tick(_ts(12, 0, day=26)),  # exactly 12:00 -> the 3rd confirmation
    ]

    database, repository = _run_session(
        tmp_path, db_name="inward_put_1200.db", trading_date=EXPIRY_DATE,
        steps=steps, payload=payload, clock_ts=restart_ts, session_pid=2222,
    )
    try:
        cycle = repository.load_cycle(cycle_id=CYCLE_ID)
        assert cycle is not None
        assert cycle["state"] == CycleState.COMPLETED.value, (
            "no non-inward candidate exists -> a full exit, never a silent skip"
        )
        assert cycle["adjustments_this_cycle"] == 0, "never an adjustment, an exit"
        assert cycle["day_blocked_reason"] is not None
        assert "expiry-day risk exit" in cycle["day_blocked_reason"]
        legs = repository.load_cycle_legs(cycle_id=CYCLE_ID)
        assert all(leg["state"] == "CLOSED" for leg in legs)
        short_put_legs = [leg for leg in legs if leg["leg_role"] == LegRole.SHORT_PUT.value]
        assert len(short_put_legs) == 1, "the original short put — never rolled"
    finally:
        database.close()


def test_inward_short_call_roll_prohibited_at_1200_on_expiry_day_exits(tmp_path: Any) -> None:
    """The symmetric case: positive net delta targets the short call;
    ROLL_DOWN_SHORT_CALL_STRIKE (24400) is closer to spot (24000) than the
    original 24500 — inward, and the only qualifying candidate — so the
    strategy exits rather than rolling."""
    payload = _enter_for_expiry_day_test(tmp_path, "inward_call_1200.db")
    restart_ts = _ts(11, 58, day=26)

    def _bump() -> None:
        set_leg_delta(payload, SHORT_PUT_STRIKE, "PE", -0.37)

    steps: list[Any] = [
        _bump,
        underlying_tick(_ts(11, 58, day=26)),
        underlying_tick(_ts(11, 59, day=26)),
        underlying_tick(_ts(12, 0, day=26)),
    ]

    database, repository = _run_session(
        tmp_path, db_name="inward_call_1200.db", trading_date=EXPIRY_DATE,
        steps=steps, payload=payload, clock_ts=restart_ts, session_pid=2222,
    )
    try:
        cycle = repository.load_cycle(cycle_id=CYCLE_ID)
        assert cycle is not None
        assert cycle["state"] == CycleState.COMPLETED.value
        assert cycle["adjustments_this_cycle"] == 0
        assert cycle["day_blocked_reason"] is not None
        assert "expiry-day risk exit" in cycle["day_blocked_reason"]
        legs = repository.load_cycle_legs(cycle_id=CYCLE_ID)
        assert all(leg["state"] == "CLOSED" for leg in legs)
        short_call_legs = [leg for leg in legs if leg["leg_role"] == LegRole.SHORT_CALL.value]
        assert len(short_call_legs) == 1, "the original short call — never rolled"
    finally:
        database.close()


def test_ordinary_inward_roll_still_permitted_just_before_1200_on_expiry_day(
    tmp_path: Any,
) -> None:
    """The same inward candidate (ROLL_UP_SHORT_PUT_STRIKE) that Phase 3A
    prohibits at/after 12:00 must still roll normally when the confirming
    trigger completes strictly *before* 12:00, even on the actual expiry
    day itself."""
    payload = _enter_for_expiry_day_test(tmp_path, "before_1200.db")
    restart_ts = _ts(11, 55, day=26)

    def _bump() -> None:
        set_leg_delta(payload, SHORT_CALL_STRIKE, "CE", 0.37)

    steps: list[Any] = [
        _bump,
        underlying_tick(_ts(11, 55, day=26)),
        underlying_tick(_ts(11, 56, day=26)),
        leg_tick(ROLL_UP_SHORT_PUT_STRIKE, "PE", 95.0, 97.0, _ts(11, 57, day=26)),
        underlying_tick(_ts(11, 57, day=26)),  # the 3rd confirmation, at 11:57 < 12:00
    ]

    database, repository = _run_session(
        tmp_path, db_name="before_1200.db", trading_date=EXPIRY_DATE,
        steps=steps, payload=payload, clock_ts=restart_ts, session_pid=2222,
    )
    try:
        cycle = repository.load_cycle(cycle_id=CYCLE_ID)
        assert cycle is not None
        assert cycle["state"] == CycleState.ACTIVE.value, "a roll, never an exit, before 12:00"
        assert cycle["adjustments_this_cycle"] == 1
        legs = repository.load_cycle_legs(cycle_id=CYCLE_ID)
        short_put_legs = [leg for leg in legs if leg["leg_role"] == LegRole.SHORT_PUT.value]
        assert len(short_put_legs) == 2
        replacement = next(leg for leg in short_put_legs if leg["is_replacement"])
        assert replacement["state"] == "OPEN"
        assert replacement["strike"] == ROLL_UP_SHORT_PUT_STRIKE
    finally:
        database.close()


def test_ordinary_adjustment_unaffected_on_a_non_expiry_day(tmp_path: Any) -> None:
    """Phase 3A control: the exact same inward candidate rolls normally,
    with no exit and no inward-roll check ever applied, on an ordinary
    (non-expiry) trading day — proven independently of, and identically
    to, test_three_consecutive_trigger_checks_are_required_and_roll_the_
    put_upward above, so the Phase 3A correction's own test module records
    an explicit non-expiry-day control alongside its new expiry-day
    boundary tests."""
    entry_ts = _ts(9, 26)
    payload = initial_chain_payload()
    steps: list[Any] = [*entry_ticks(entry_ts)]

    def _bump() -> None:
        set_leg_delta(payload, SHORT_CALL_STRIKE, "CE", 0.37)

    steps.append(_bump)
    steps.append(underlying_tick(_ts(9, 31)))
    steps.append(underlying_tick(_ts(9, 36)))
    steps.append(leg_tick(ROLL_UP_SHORT_PUT_STRIKE, "PE", 95.0, 97.0, _ts(9, 41)))
    steps.append(underlying_tick(_ts(9, 41)))

    database, repository = _run_session(
        tmp_path, db_name="non_expiry_control.db", trading_date=ENTRY_DATE,
        steps=steps, payload=payload, clock_ts=entry_ts,
    )
    try:
        cycle = repository.load_cycle(cycle_id=CYCLE_ID)
        assert cycle is not None
        assert cycle["state"] == CycleState.ACTIVE.value
        assert cycle["adjustments_this_cycle"] == 1
        legs = repository.load_cycle_legs(cycle_id=CYCLE_ID)
        short_put_legs = [leg for leg in legs if leg["leg_role"] == LegRole.SHORT_PUT.value]
        replacement = next(leg for leg in short_put_legs if leg["is_replacement"])
        assert replacement["state"] == "OPEN"
        assert replacement["strike"] == ROLL_UP_SHORT_PUT_STRIKE
    finally:
        database.close()


def test_outward_replacement_still_permitted_at_1200_on_expiry_day(tmp_path: Any) -> None:
    """"Exposure-reducing actions remain allowed": OUTWARD_SHORT_PUT_STRIKE
    (23400) is *farther* from spot (24000) than the original short
    (23500) — not prohibited — and still reduces projected delta, so it is
    used even though the evaluation happens at exactly 12:00. The nearer,
    inward candidate (ROLL_UP_SHORT_PUT_STRIKE, 23600) would otherwise
    rank first and is skipped for being inward, never for being invalid."""
    payload = _enter_for_expiry_day_test(tmp_path, "outward_1200.db")
    restart_ts = _ts(11, 58, day=26)

    def _bump() -> None:
        set_leg_delta(payload, SHORT_CALL_STRIKE, "CE", 0.37)
        # A larger-magnitude delta than the original -0.20 (still within
        # the -0.20 +/- 0.03 target band) — genuinely reduces projected
        # delta despite being the farther-from-spot strike.
        set_leg_delta(payload, OUTWARD_SHORT_PUT_STRIKE, "PE", -0.23)

    steps: list[Any] = [
        _bump,
        underlying_tick(_ts(11, 58, day=26)),
        underlying_tick(_ts(11, 59, day=26)),
        leg_tick(OUTWARD_SHORT_PUT_STRIKE, "PE", 60.0, 62.0, _ts(12, 0, day=26)),
        underlying_tick(_ts(12, 0, day=26)),
    ]

    database, repository = _run_session(
        tmp_path, db_name="outward_1200.db", trading_date=EXPIRY_DATE,
        steps=steps, payload=payload, clock_ts=restart_ts, session_pid=2222,
    )
    try:
        cycle = repository.load_cycle(cycle_id=CYCLE_ID)
        assert cycle is not None
        assert cycle["state"] == CycleState.ACTIVE.value, (
            "an outward, delta-reducing replacement is not prohibited -> a roll, not an exit"
        )
        assert cycle["adjustments_this_cycle"] == 1
        legs = repository.load_cycle_legs(cycle_id=CYCLE_ID)
        short_put_legs = [leg for leg in legs if leg["leg_role"] == LegRole.SHORT_PUT.value]
        assert len(short_put_legs) == 2
        replacement = next(leg for leg in short_put_legs if leg["is_replacement"])
        assert replacement["state"] == "OPEN"
        assert replacement["strike"] == OUTWARD_SHORT_PUT_STRIKE
    finally:
        database.close()


# ------------------------------------------------------------ boundary 1
def test_restart_boundary_1_claim_never_persisted_reverts_cleanly(tmp_path: Any) -> None:
    """Boundary 1 (spec 7.3/9.5): a persistence failure on the very first,
    critical claim write — before ``pending_adjustment_role``/``_state``
    ever become durable — must leave the cycle exactly as if the trigger
    had never fired: no counter incremented, no leg touched. A restart
    afterwards must be able to roll normally, proving nothing was left
    corrupted in memory or on disk by the failed claim."""
    entry_ts = _ts(9, 26)
    payload = initial_chain_payload()
    db_name = "restart_boundary_1.db"
    steps: list[Any] = [
        *entry_ticks(entry_ts),
        *_trigger_steps(
            _bump_call_delta_step(payload),
            leg_tick(ROLL_UP_SHORT_PUT_STRIKE, "PE", 95.0, 97.0, _ts(9, 41)),
            t1=_ts(9, 31), t2=_ts(9, 36), t3=_ts(9, 41),
        ),
    ]

    def _before_run(engine: Any) -> None:
        _fail_cycle_persist_once_before_write(
            engine, AdjustmentLifecycle.EXIT_SUBMISSION_PENDING.value
        )

    database, repository = _run_session(
        tmp_path, db_name=db_name, trading_date=ENTRY_DATE,
        steps=steps, payload=payload, clock_ts=entry_ts, before_run=_before_run,
    )
    try:
        cycle = repository.load_cycle(cycle_id=CYCLE_ID)
        assert cycle is not None
        assert cycle["adjustments_this_cycle"] == 0
        assert cycle["adjustments_today"] == 0
        assert cycle["pending_adjustment_role"] is None
        assert cycle["pending_adjustment_state"] is None
        legs = repository.load_cycle_legs(cycle_id=CYCLE_ID)
        assert len(legs) == 4, "no replacement leg was ever created"
        assert all(leg["state"] == "OPEN" for leg in legs)
    finally:
        database.close()

    # A fresh trigger after the aborted claim must roll completely
    # normally — proving nothing was left stuck.
    restart_ts = _ts(9, 46)
    steps2 = _trigger_steps(
        _bump_call_delta_step(payload),
        leg_tick(ROLL_UP_SHORT_PUT_STRIKE, "PE", 95.0, 97.0, _ts(9, 56)),
        t1=_ts(9, 46), t2=_ts(9, 51), t3=_ts(9, 56),
    )
    database2, repository2 = _run_session(
        tmp_path, db_name=db_name, trading_date=ENTRY_DATE,
        steps=steps2, payload=payload, clock_ts=restart_ts, session_pid=2222,
    )
    try:
        cycle2 = repository2.load_cycle(cycle_id=CYCLE_ID)
        assert cycle2 is not None
        assert cycle2["adjustments_this_cycle"] == 1
        assert cycle2["adjustments_today"] == 1
        legs2 = repository2.load_cycle_legs(cycle_id=CYCLE_ID)
        short_put_legs = [leg for leg in legs2 if leg["leg_role"] == LegRole.SHORT_PUT.value]
        assert len(short_put_legs) == 2
        replacement = next(leg for leg in short_put_legs if leg["is_replacement"])
        assert replacement["state"] == "OPEN"
        assert replacement["strike"] == ROLL_UP_SHORT_PUT_STRIKE
    finally:
        database2.close()


# ------------------------------------------------------------ boundary 2
def test_restart_boundary_2_close_never_attempted_resumes_then_fails_closed(
    tmp_path: Any,
) -> None:
    """Boundary 2: the claim write's own confirmation is lost even though
    it actually landed (a real crash immediately after commit) — the old
    short's close was never even attempted. On restart, the old short is
    still genuinely OPEN, so resuming safely re-attempts exactly the close
    ``_roll_short`` itself would have attempted next; with no original
    replacement candidate ever persisted, the cycle then fails closed on
    the very next evaluation rather than guessing at one."""
    entry_ts = _ts(9, 26)
    payload = initial_chain_payload()
    db_name = "restart_boundary_2.db"
    steps: list[Any] = [
        *entry_ticks(entry_ts),
        *_trigger_steps(
            _bump_call_delta_step(payload),
            leg_tick(ROLL_UP_SHORT_PUT_STRIKE, "PE", 95.0, 97.0, _ts(9, 41)),
            t1=_ts(9, 31), t2=_ts(9, 36), t3=_ts(9, 41),
        ),
    ]

    def _before_run(engine: Any) -> None:
        _fail_cycle_persist_once_after_write(
            engine, AdjustmentLifecycle.EXIT_SUBMISSION_PENDING.value
        )

    database, repository = _run_session(
        tmp_path, db_name=db_name, trading_date=ENTRY_DATE,
        steps=steps, payload=payload, clock_ts=entry_ts, before_run=_before_run,
    )
    # The DB durably shows the claim (real write succeeded) even though the
    # in-memory engine reverted after catching MultiLegDurabilityError —
    # exactly the "ghost claim" this boundary is about.
    cycle = repository.load_cycle(cycle_id=CYCLE_ID)
    assert cycle is not None
    assert cycle["adjustments_this_cycle"] == 1
    assert cycle["adjustments_today"] == 1
    assert cycle["pending_adjustment_role"] == LegRole.SHORT_PUT.value
    assert cycle["pending_adjustment_state"] == AdjustmentLifecycle.EXIT_SUBMISSION_PENDING.value
    legs = repository.load_cycle_legs(cycle_id=CYCLE_ID)
    short_put_legs = [leg for leg in legs if leg["leg_role"] == LegRole.SHORT_PUT.value]
    assert len(short_put_legs) == 1
    assert short_put_legs[0]["state"] == "OPEN", "the close was never actually attempted"
    database.close()

    restart_ts = _ts(9, 46)
    steps2 = [underlying_tick(restart_ts), underlying_tick(_ts(9, 51))]
    database2, repository2 = _run_session(
        tmp_path, db_name=db_name, trading_date=ENTRY_DATE,
        steps=steps2, payload=payload, clock_ts=restart_ts, session_pid=2222,
    )
    try:
        cycle2 = repository2.load_cycle(cycle_id=CYCLE_ID)
        assert cycle2 is not None
        assert cycle2["state"] == CycleState.CRITICAL_UNRESOLVED.value
        # Never re-claimed, never double-counted.
        assert cycle2["adjustments_this_cycle"] == 1
        assert cycle2["adjustments_today"] == 1
        legs2 = repository2.load_cycle_legs(cycle_id=CYCLE_ID)
        short_put_legs2 = [leg for leg in legs2 if leg["leg_role"] == LegRole.SHORT_PUT.value]
        assert len(short_put_legs2) == 1, "resumed and closed exactly once — never duplicated"
        assert short_put_legs2[0]["state"] == "CLOSED"
        incident = _last_incident(database2)
        assert incident is not None
        assert "AWAITING_NEXT_CANDLE" in incident["message"]
    finally:
        database2.close()


# ------------------------------------------------------------ boundary 3
def test_restart_boundary_3_close_submission_unknown_reconciles_then_fails_closed(
    tmp_path: Any,
) -> None:
    """Boundary 3: the close attempt itself raised (paper-broker failure),
    leaving the leg ``CLOSE_SUBMISSION_UNKNOWN`` durably. On restart,
    reconciliation proves — from authoritative order/position state — that
    the close never actually took effect (no fill, position still open),
    restoring the leg to OPEN; resume then safely re-attempts the close
    exactly once, and fails closed on the next evaluation for the same
    "no replacement candidate ever persisted" reason as boundary 2."""
    entry_ts = _ts(9, 26)
    payload = initial_chain_payload()
    db_name = "restart_boundary_3.db"
    steps: list[Any] = [
        *entry_ticks(entry_ts),
        *_trigger_steps(
            _bump_call_delta_step(payload),
            leg_tick(ROLL_UP_SHORT_PUT_STRIKE, "PE", 95.0, 97.0, _ts(9, 41)),
            t1=_ts(9, 31), t2=_ts(9, 36), t3=_ts(9, 41),
        ),
    ]

    database, repository = _run_session(
        tmp_path, db_name=db_name, trading_date=ENTRY_DATE,
        steps=steps, payload=payload, clock_ts=entry_ts,
        before_run=_break_close_for_security("90002"),
    )
    cycle = repository.load_cycle(cycle_id=CYCLE_ID)
    assert cycle is not None
    assert cycle["pending_adjustment_state"] == AdjustmentLifecycle.EXIT_UNKNOWN.value
    legs = repository.load_cycle_legs(cycle_id=CYCLE_ID)
    short_put_legs = [leg for leg in legs if leg["leg_role"] == LegRole.SHORT_PUT.value]
    assert len(short_put_legs) == 1
    assert short_put_legs[0]["state"] == "CLOSE_SUBMISSION_UNKNOWN"
    database.close()

    restart_ts = _ts(9, 46)
    steps2 = [underlying_tick(restart_ts), underlying_tick(_ts(9, 51))]
    database2, repository2 = _run_session(
        tmp_path, db_name=db_name, trading_date=ENTRY_DATE,
        steps=steps2, payload=payload, clock_ts=restart_ts, session_pid=2222,
    )
    try:
        cycle2 = repository2.load_cycle(cycle_id=CYCLE_ID)
        assert cycle2 is not None
        assert cycle2["state"] == CycleState.CRITICAL_UNRESOLVED.value
        assert cycle2["adjustments_this_cycle"] == 1
        assert cycle2["adjustments_today"] == 1
        legs2 = repository2.load_cycle_legs(cycle_id=CYCLE_ID)
        short_put_legs2 = [leg for leg in legs2 if leg["leg_role"] == LegRole.SHORT_PUT.value]
        assert len(short_put_legs2) == 1, "reconciled, then resumed and closed exactly once"
        assert short_put_legs2[0]["state"] == "CLOSED"
        incident = _last_incident(database2)
        assert incident is not None
        assert "AWAITING_NEXT_CANDLE" in incident["message"]
    finally:
        database2.close()


# ------------------------------------------------------------ boundary 4
def test_restart_boundary_4_close_confirmed_leg_projection_missing_reconciles(
    tmp_path: Any,
) -> None:
    """Boundary 4: the old short's close (and the replacement's own open)
    both genuinely succeed at the broker/position layer, but the old
    short's own leg-projection persist fails (best-effort, swallowed) —
    leaving its row stale (still ``OPEN``) even though it is truly closed.
    Restart reconciliation (``_reconcile_leg``'s own authoritative-fill
    fallback) must reconstruct it as ``CLOSED`` with a real exit price and
    P&L, purely from order/position state — no incident, no operator
    needed, and the replacement (persisted normally) is left untouched."""
    entry_ts = _ts(9, 26)
    payload = initial_chain_payload()
    db_name = "restart_boundary_4.db"
    steps: list[Any] = [
        *entry_ticks(entry_ts),
        *_trigger_steps(
            _bump_call_delta_step(payload),
            leg_tick(ROLL_UP_SHORT_PUT_STRIKE, "PE", 95.0, 97.0, _ts(9, 41)),
            t1=_ts(9, 31), t2=_ts(9, 36), t3=_ts(9, 41),
        ),
    ]

    def _before_run(engine: Any) -> None:
        _fail_leg_persist_once(engine, _OLD_SHORT_PUT_LEG_ID, LegState.CLOSED)

    database, repository = _run_session(
        tmp_path, db_name=db_name, trading_date=ENTRY_DATE,
        steps=steps, payload=payload, clock_ts=entry_ts, before_run=_before_run,
    )
    # The roll otherwise completed fully (in memory and at the cycle
    # level) — only the old leg's own row is stale.
    cycle = repository.load_cycle(cycle_id=CYCLE_ID)
    assert cycle is not None
    assert cycle["pending_adjustment_state"] == AdjustmentLifecycle.REPLACEMENT_FILLED.value
    legs = repository.load_cycle_legs(cycle_id=CYCLE_ID)
    short_put_legs = {leg["leg_id"]: leg for leg in legs if leg["leg_role"] == "SHORT_PUT"}
    assert short_put_legs[_OLD_SHORT_PUT_LEG_ID]["state"] == "OPEN", (
        "stale — the real close never landed here"
    )
    assert short_put_legs[_REPLACEMENT_SHORT_PUT_LEG_ID]["state"] == "OPEN"
    database.close()

    restart_ts = _ts(9, 46)
    database2, repository2 = _run_session(
        tmp_path, db_name=db_name, trading_date=ENTRY_DATE,
        steps=[underlying_tick(restart_ts)], payload=payload, clock_ts=restart_ts,
        session_pid=2222,
    )
    try:
        cycle2 = repository2.load_cycle(cycle_id=CYCLE_ID)
        assert cycle2 is not None
        assert cycle2["state"] == CycleState.ACTIVE.value, "fully healed by reconciliation alone"
        legs2 = repository2.load_cycle_legs(cycle_id=CYCLE_ID)
        by_id = {leg["leg_id"]: leg for leg in legs2}
        old_leg = by_id[_OLD_SHORT_PUT_LEG_ID]
        assert old_leg["state"] == "CLOSED", "reconstructed from the authoritative exit fill"
        assert old_leg["exit_price"] is not None
        replacement = by_id[_REPLACEMENT_SHORT_PUT_LEG_ID]
        assert replacement["state"] == "OPEN", "untouched by the old leg's own reconciliation"
        assert replacement["strike"] == ROLL_UP_SHORT_PUT_STRIKE
    finally:
        database2.close()


# ------------------------------------------------------------ boundary 5
def test_restart_boundary_5_awaiting_next_candle_persisted_crash_fails_closed(
    tmp_path: Any,
) -> None:
    """Boundary 5: the old short's close is fully confirmed and durable
    (``AWAITING_NEXT_CANDLE`` genuinely committed), then the process is
    killed before any replacement is even evaluated. Restart must never
    guess a replacement candidate out of thin air — it fails closed with a
    named incident, leaving the cycle ``CRITICAL_UNRESOLVED`` with no
    naked/duplicate exposure and the adjustment counters exactly where the
    durable claim left them."""
    entry_ts = _ts(9, 26)
    payload = initial_chain_payload()
    db_name = "restart_boundary_5.db"
    steps: list[Any] = [
        *entry_ticks(entry_ts),
        *_trigger_steps(
            _bump_call_delta_step(payload),
            leg_tick(ROLL_UP_SHORT_PUT_STRIKE, "PE", 95.0, 97.0, _ts(9, 41)),
            t1=_ts(9, 31), t2=_ts(9, 36), t3=_ts(9, 41),
        ),
    ]

    def _before_run(engine: Any) -> None:
        _crash_after_cycle_persisted(engine, AdjustmentLifecycle.AWAITING_NEXT_CANDLE.value)

    database, repository = _run_session(
        tmp_path, db_name=db_name, trading_date=ENTRY_DATE,
        steps=steps, payload=payload, clock_ts=entry_ts, before_run=_before_run,
        tolerate_crash=True,
    )
    cycle = repository.load_cycle(cycle_id=CYCLE_ID)
    assert cycle is not None
    assert cycle["pending_adjustment_state"] == AdjustmentLifecycle.AWAITING_NEXT_CANDLE.value
    assert cycle["pending_adjustment_role"] == LegRole.SHORT_PUT.value
    assert cycle["adjustments_this_cycle"] == 1
    assert cycle["adjustments_today"] == 1
    legs = repository.load_cycle_legs(cycle_id=CYCLE_ID)
    short_put_legs = [leg for leg in legs if leg["leg_role"] == LegRole.SHORT_PUT.value]
    assert len(short_put_legs) == 1, "no replacement was ever attempted before the crash"
    assert short_put_legs[0]["state"] == "CLOSED"
    database.close()

    restart_ts = _ts(9, 46)
    database2, repository2 = _run_session(
        tmp_path, db_name=db_name, trading_date=ENTRY_DATE,
        steps=[underlying_tick(restart_ts)], payload=payload, clock_ts=restart_ts,
        session_pid=2222,
    )
    try:
        cycle2 = repository2.load_cycle(cycle_id=CYCLE_ID)
        assert cycle2 is not None
        assert cycle2["state"] == CycleState.CRITICAL_UNRESOLVED.value
        assert cycle2["adjustments_this_cycle"] == 1, "never re-attempted"
        assert cycle2["adjustments_today"] == 1
        legs2 = repository2.load_cycle_legs(cycle_id=CYCLE_ID)
        short_put_legs2 = [leg for leg in legs2 if leg["leg_role"] == LegRole.SHORT_PUT.value]
        assert len(short_put_legs2) == 1, "no naked/duplicate replacement was ever created"
        assert short_put_legs2[0]["state"] == "CLOSED"
        incident = _last_incident(database2)
        assert incident is not None
        assert "AWAITING_NEXT_CANDLE" in incident["message"]
    finally:
        database2.close()


# ------------------------------------------------------------ boundary 6
def test_restart_boundary_6_replacement_claimed_before_submission_resumes(
    tmp_path: Any,
) -> None:
    """Boundary 6: the replacement leg is fully, durably claimed
    (``REPLACEMENT_PENDING``, the leg's own row persisted ``PENDING_
    ORDER``) but the process is killed before its own order is ever
    submitted. Restart must resume by submitting exactly that one already-
    claimed replacement — never re-claim, never re-run candidate search,
    never touch the old (already-closed) leg again."""
    entry_ts = _ts(9, 26)
    payload = initial_chain_payload()
    db_name = "restart_boundary_6.db"
    steps: list[Any] = [
        *entry_ticks(entry_ts),
        *_trigger_steps(
            _bump_call_delta_step(payload),
            leg_tick(ROLL_UP_SHORT_PUT_STRIKE, "PE", 95.0, 97.0, _ts(9, 41)),
            t1=_ts(9, 31), t2=_ts(9, 36), t3=_ts(9, 41),
        ),
    ]

    def _before_run(engine: Any) -> None:
        _crash_after_leg_persisted(engine, _REPLACEMENT_SHORT_PUT_LEG_ID, LegState.PENDING_ORDER)

    database, repository = _run_session(
        tmp_path, db_name=db_name, trading_date=ENTRY_DATE,
        steps=steps, payload=payload, clock_ts=entry_ts, before_run=_before_run,
        tolerate_crash=True,
    )
    cycle = repository.load_cycle(cycle_id=CYCLE_ID)
    assert cycle is not None
    assert cycle["pending_adjustment_state"] == AdjustmentLifecycle.REPLACEMENT_PENDING.value
    legs = repository.load_cycle_legs(cycle_id=CYCLE_ID)
    by_id = {leg["leg_id"]: leg for leg in legs if leg["leg_role"] == "SHORT_PUT"}
    assert by_id[_OLD_SHORT_PUT_LEG_ID]["state"] == "CLOSED"
    assert by_id[_REPLACEMENT_SHORT_PUT_LEG_ID]["state"] == "PENDING_ORDER", (
        "claimed and persisted, but never submitted before the crash"
    )
    database.close()

    restart_ts = _ts(9, 46)
    steps2 = [
        leg_tick(ROLL_UP_SHORT_PUT_STRIKE, "PE", 95.0, 97.0, restart_ts),
        underlying_tick(restart_ts),
    ]
    database2, repository2 = _run_session(
        tmp_path, db_name=db_name, trading_date=ENTRY_DATE,
        steps=steps2, payload=payload, clock_ts=restart_ts, session_pid=2222,
    )
    try:
        cycle2 = repository2.load_cycle(cycle_id=CYCLE_ID)
        assert cycle2 is not None
        assert cycle2["state"] == CycleState.ACTIVE.value
        assert cycle2["pending_adjustment_role"] is None
        assert cycle2["adjustments_this_cycle"] == 1, "never re-claimed"
        legs2 = repository2.load_cycle_legs(cycle_id=CYCLE_ID)
        by_id2 = {leg["leg_id"]: leg for leg in legs2 if leg["leg_role"] == "SHORT_PUT"}
        assert by_id2[_OLD_SHORT_PUT_LEG_ID]["state"] == "CLOSED", "never touched again"
        assert by_id2[_REPLACEMENT_SHORT_PUT_LEG_ID]["state"] == "OPEN"
        assert by_id2[_REPLACEMENT_SHORT_PUT_LEG_ID]["strike"] == ROLL_UP_SHORT_PUT_STRIKE
    finally:
        database2.close()


# ------------------------------------------------------------ boundary 7
def test_restart_boundary_7_replacement_open_outcome_durable_but_cycle_stale_fails_closed(
    tmp_path: Any,
) -> None:
    """Boundary 7: the replacement's own open attempt is definitively
    resolved (this engine's opening path has no ambiguous "unknown"
    outcome the way closing does — a raise always resolves to ``FAILED``,
    durably persisted), but the *cycle-level* bookkeeping that would have
    cleared ``pending_adjustment_role``/``_state`` is lost before it
    becomes durable. Restart must not mistake the stale
    ``REPLACEMENT_PENDING`` label for something still resumable — the
    replacement leg is terminal (``FAILED``), so it fails closed instead of
    re-attempting anything."""
    entry_ts = _ts(9, 26)
    payload = initial_chain_payload()
    db_name = "restart_boundary_7.db"
    steps: list[Any] = [
        *entry_ticks(entry_ts),
        *_trigger_steps(
            _bump_call_delta_step(payload),
            leg_tick(ROLL_UP_SHORT_PUT_STRIKE, "PE", 95.0, 97.0, _ts(9, 41)),
            t1=_ts(9, 31), t2=_ts(9, 36), t3=_ts(9, 41),
        ),
    ]

    def _before_run(engine: Any) -> None:
        _break_open_for_security("90005")(engine)  # ROLL_UP_SHORT_PUT_STRIKE's security id
        _fail_cycle_persist_once_before_write(
            engine, AdjustmentLifecycle.REPLACEMENT_FAILED.value
        )

    database, repository = _run_session(
        tmp_path, db_name=db_name, trading_date=ENTRY_DATE,
        steps=steps, payload=payload, clock_ts=entry_ts, before_run=_before_run,
    )
    cycle = repository.load_cycle(cycle_id=CYCLE_ID)
    assert cycle is not None
    # Stale: the cycle-level clearing of the claim never made it to disk,
    # so it still shows the earlier, now-outdated REPLACEMENT_PENDING.
    assert cycle["pending_adjustment_state"] == AdjustmentLifecycle.REPLACEMENT_PENDING.value
    legs = repository.load_cycle_legs(cycle_id=CYCLE_ID)
    by_id = {leg["leg_id"]: leg for leg in legs if leg["leg_role"] == "SHORT_PUT"}
    assert by_id[_REPLACEMENT_SHORT_PUT_LEG_ID]["state"] == "FAILED", (
        "the leg's own outcome is durable even though the cycle's bookkeeping is not"
    )
    database.close()

    restart_ts = _ts(9, 46)
    database2, repository2 = _run_session(
        tmp_path, db_name=db_name, trading_date=ENTRY_DATE,
        steps=[underlying_tick(restart_ts)], payload=payload, clock_ts=restart_ts,
        session_pid=2222,
    )
    try:
        cycle2 = repository2.load_cycle(cycle_id=CYCLE_ID)
        assert cycle2 is not None
        assert cycle2["state"] == CycleState.CRITICAL_UNRESOLVED.value, (
            "the stale REPLACEMENT_PENDING label must never be treated as still-resumable"
        )
        legs2 = repository2.load_cycle_legs(cycle_id=CYCLE_ID)
        by_id2 = {leg["leg_id"]: leg for leg in legs2 if leg["leg_role"] == "SHORT_PUT"}
        assert by_id2[_REPLACEMENT_SHORT_PUT_LEG_ID]["state"] == "FAILED", "never re-attempted"
        incident = _last_incident(database2)
        assert incident is not None
        assert "no matching leg" in incident["message"]
    finally:
        database2.close()


# ------------------------------------------------------------ boundary 8
def test_restart_boundary_8_replacement_filled_leg_projection_missing_reconciles(
    tmp_path: Any,
) -> None:
    """Boundary 8: the replacement fills for real (broker/position layer
    fully confirms it), but its own leg-projection persist fails
    (best-effort, swallowed) — leaving its row stale (``PENDING_ORDER``)
    even though it is truly open. Restart reconciliation's existing
    pending-state fallback (``_reconcile_leg``'s own ``PENDING_ORDER ->
    OPEN`` promotion from an authoritative fill) must resolve it silently —
    no incident, no operator needed."""
    entry_ts = _ts(9, 26)
    payload = initial_chain_payload()
    db_name = "restart_boundary_8.db"
    steps: list[Any] = [
        *entry_ticks(entry_ts),
        *_trigger_steps(
            _bump_call_delta_step(payload),
            leg_tick(ROLL_UP_SHORT_PUT_STRIKE, "PE", 95.0, 97.0, _ts(9, 41)),
            t1=_ts(9, 31), t2=_ts(9, 36), t3=_ts(9, 41),
        ),
    ]

    def _before_run(engine: Any) -> None:
        _fail_leg_persist_once(engine, _REPLACEMENT_SHORT_PUT_LEG_ID, LegState.OPEN)

    database, repository = _run_session(
        tmp_path, db_name=db_name, trading_date=ENTRY_DATE,
        steps=steps, payload=payload, clock_ts=entry_ts, before_run=_before_run,
    )
    cycle = repository.load_cycle(cycle_id=CYCLE_ID)
    assert cycle is not None
    assert cycle["pending_adjustment_state"] == AdjustmentLifecycle.REPLACEMENT_FILLED.value
    assert cycle["pending_adjustment_role"] is None
    legs = repository.load_cycle_legs(cycle_id=CYCLE_ID)
    by_id = {leg["leg_id"]: leg for leg in legs if leg["leg_role"] == "SHORT_PUT"}
    assert by_id[_REPLACEMENT_SHORT_PUT_LEG_ID]["state"] == "PENDING_ORDER", (
        "stale — the real fill never landed here"
    )
    database.close()

    restart_ts = _ts(9, 46)
    database2, repository2 = _run_session(
        tmp_path, db_name=db_name, trading_date=ENTRY_DATE,
        steps=[underlying_tick(restart_ts)], payload=payload, clock_ts=restart_ts,
        session_pid=2222,
    )
    try:
        cycle2 = repository2.load_cycle(cycle_id=CYCLE_ID)
        assert cycle2 is not None
        assert cycle2["state"] == CycleState.ACTIVE.value, "fully healed by reconciliation alone"
        assert cycle2["adjustments_this_cycle"] == 1
        legs2 = repository2.load_cycle_legs(cycle_id=CYCLE_ID)
        by_id2 = {leg["leg_id"]: leg for leg in legs2 if leg["leg_role"] == "SHORT_PUT"}
        replacement = by_id2[_REPLACEMENT_SHORT_PUT_LEG_ID]
        assert replacement["state"] == "OPEN", "reconstructed from the authoritative fill"
        assert replacement["strike"] == ROLL_UP_SHORT_PUT_STRIKE
        assert by_id2[_OLD_SHORT_PUT_LEG_ID]["state"] == "CLOSED"
    finally:
        database2.close()


# ------------------------------------------------------------ boundary 9
def test_restart_boundary_9_completed_replacement_restart_is_a_pure_no_op(
    tmp_path: Any,
) -> None:
    """Boundary 9 (the control): a fully completed, cleanly persisted
    adjustment — no failure injected anywhere — followed by an ordinary
    restart with no new trigger. ``_resume_pending_adjustment`` must be a
    pure no-op (``pending_adjustment_role`` is already ``None``): nothing
    is re-touched, no new leg appears, and the counters stay exactly where
    the original roll left them."""
    entry_ts = _ts(9, 26)
    payload = initial_chain_payload()
    db_name = "restart_boundary_9.db"
    steps: list[Any] = [
        *entry_ticks(entry_ts),
        *_trigger_steps(
            _bump_call_delta_step(payload),
            leg_tick(ROLL_UP_SHORT_PUT_STRIKE, "PE", 95.0, 97.0, _ts(9, 41)),
            t1=_ts(9, 31), t2=_ts(9, 36), t3=_ts(9, 41),
        ),
    ]

    database, repository = _run_session(
        tmp_path, db_name=db_name, trading_date=ENTRY_DATE,
        steps=steps, payload=payload, clock_ts=entry_ts,
    )
    cycle = repository.load_cycle(cycle_id=CYCLE_ID)
    assert cycle is not None
    assert cycle["pending_adjustment_role"] is None
    assert cycle["pending_adjustment_state"] == AdjustmentLifecycle.REPLACEMENT_FILLED.value
    assert cycle["adjustments_this_cycle"] == 1
    incidents_before = _incident_count(database)
    database.close()

    restart_ts = _ts(9, 46)
    database2, repository2 = _run_session(
        tmp_path, db_name=db_name, trading_date=ENTRY_DATE,
        steps=[underlying_tick(restart_ts), underlying_tick(_ts(9, 51))],
        payload=payload, clock_ts=restart_ts, session_pid=2222,
    )
    try:
        cycle2 = repository2.load_cycle(cycle_id=CYCLE_ID)
        assert cycle2 is not None
        assert cycle2["state"] == CycleState.ACTIVE.value
        assert cycle2["pending_adjustment_role"] is None
        assert cycle2["adjustments_this_cycle"] == 1, "untouched by the restart"
        assert cycle2["adjustments_today"] == 1
        legs2 = repository2.load_cycle_legs(cycle_id=CYCLE_ID)
        short_put_legs2 = [leg for leg in legs2 if leg["leg_role"] == LegRole.SHORT_PUT.value]
        assert len(short_put_legs2) == 2, "no new leg was created by the restart"
        assert _incident_count(database2) == incidents_before, "no incident from a healthy restart"
    finally:
        database2.close()
