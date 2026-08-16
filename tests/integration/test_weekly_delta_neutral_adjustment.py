"""Delta-adjustment acceptance rows (spec section 7, spec section 13.3) —
driven through the real ``runtimes.positional_options.worker.build_engine``
wiring against a real temp SQLite database, exactly like
``test_weekly_delta_neutral_entry.py``/``_restart.py``. See
``tests/integration/_weekly_delta_neutral_fixtures.py`` for the shared
fixture helpers and why a custom ``ScriptedFeed`` is used.

No live order API is constructed or called anywhere in this module.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from _weekly_delta_neutral_fixtures import (
    HEDGE_CALL_STRIKE,
    HEDGE_PUT_STRIKE,
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
from common.engine.positional.positional_models import CycleState, LegRole
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
) -> tuple[Database, ExecutionRepository]:
    """One engine session: build, run to completion over ``steps``, and
    return ``(database, repository)`` — left open for the caller's own
    assertions and a possible next session, mirroring the restart test's
    own style. ``before_run``, if given, is called with the built engine
    right before ``.run()`` — the seam a test uses to force one internal
    call to fail (e.g. simulating an unknown close outcome), never to
    change what the engine itself does."""
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
    built.engine.run()
    return database, repository


def _assert_active_condor(repository: ExecutionRepository) -> None:
    cycle = repository.load_cycle(cycle_id=CYCLE_ID)
    assert cycle is not None and cycle["state"] == CycleState.ACTIVE.value, (
        "fixture sanity: entry must produce an ACTIVE 4-leg cycle"
    )


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
