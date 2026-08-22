"""Phase 1 (strategy-rolling-strangle-otm1): the generic durable roll data
model, repository API and migration `0013` — proven against a real SQLite
repository, never a hand-built projection.

Covers, per the Phase 0 report's Phase 1 test list:
    * repository round-trip (anchor + claims, via commit_basket_state and the
      individual upsert methods);
    * single-transaction atomicity under injected failure;
    * fail-closed unknown-role load;
    * all seven LegRole members round-trip;
    * reserve_roll_close_intent's atomic reserve-and-associate write, and its
      refusal to associate with a row that is not CLAIMED;
    * order_intent_by_id's scoped-to-one-identity lookup.

No engine, no strategy, no worker — this module proves the data layer alone,
matching Phase 1's scope (generic roll data model, repository API,
migration only).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from common.config.models import ExecutionMode
from common.engine.multi_leg_models import BasketRollState, LegRole, RollClaim
from common.engine.multi_leg_state import (
    BasketRowInconsistent,
    load_basket_roll_state,
)
from common.execution.repository import BasketRollClaimSeed, ExecutionRepository
from common.models import OrderIntent, OrderType, RiskDecision, Side
from common.persistence import Database, migrate

RUNTIME_ID = "intraday_options"
STRATEGY_ID = "rolling_strangle_otm1"
TRADING_DATE = "2026-08-17"
BASKET_ID = f"{STRATEGY_ID}:{TRADING_DATE}"


def _repository(tmp_path) -> ExecutionRepository:  # type: ignore[no-untyped-def]
    db = Database(tmp_path / "test.db")
    migrate(db)
    return ExecutionRepository(db)


def _basket_kwargs(**overrides):  # type: ignore[no-untyped-def]
    base = dict(
        runtime_id=RUNTIME_ID,
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE,
        basket_id=BASKET_ID,
        lifecycle_state="OPEN",
        entries_consumed=True,
        day_blocked_reason=None,
        adjustment_count=0,
        pending_replacement_role=None,
        pending_replacement_state=None,
        original_combined_basis=None,
        square_off_state="PENDING",
    )
    base.update(overrides)
    return base


def _claim_seed(
    *, role: str, sequence: int, target_leg_id: str, group: str | None = None
) -> BasketRollClaimSeed:
    return BasketRollClaimSeed(
        claim_group_id=group or f"grp-{role}-{sequence}",
        leg_role=role,
        roll_sequence=sequence,
        target_leg_id=target_leg_id,
        reference_price_at_claim=20000.0 + sequence,
        claim_candle_ts="2026-08-17T09:50:00+05:30",
        claimed_at="2026-08-17T09:50:01+05:30",
    )


# --------------------------------------------------------------- round-trip
def test_commit_basket_state_writes_basket_anchor_and_claims_together(tmp_path):  # type: ignore[no-untyped-def]
    repo = _repository(tmp_path)

    repo.commit_basket_state(
        **_basket_kwargs(),
        anchor=(20000.0, "2026-08-17T09:45:00+05:30"),
        new_claims=(
            _claim_seed(role="CE", sequence=1, target_leg_id=f"{BASKET_ID}:CE:1"),
            _claim_seed(role="PE", sequence=1, target_leg_id=f"{BASKET_ID}:PE:1", group="grp-both"),
        ),
    )

    anchor = repo.load_basket_roll_anchor(
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE,
        basket_id=BASKET_ID,
    )
    assert anchor is not None
    assert anchor["reference_price"] == 20000.0
    assert anchor["anchor_candle_ts"] == "2026-08-17T09:45:00+05:30"

    rolls = repo.load_basket_rolls(basket_id=BASKET_ID)
    assert len(rolls) == 2
    by_role = {row["leg_role"]: row for row in rolls}
    assert by_role["CE"]["lifecycle_state"] == "CLAIMED"
    assert by_role["CE"]["roll_sequence"] == 1
    assert by_role["CE"]["close_correlation_id"] is None
    assert by_role["PE"]["target_leg_id"] == f"{BASKET_ID}:PE:1"

    basket_row = repo.load_strategy_basket(
        strategy_id=STRATEGY_ID, execution_mode=ExecutionMode.PAPER, trading_date=TRADING_DATE
    )
    assert basket_row is not None
    assert basket_row["basket_id"] == BASKET_ID


def test_load_basket_roll_state_hydrates_the_typed_read_model(tmp_path):  # type: ignore[no-untyped-def]
    repo = _repository(tmp_path)
    repo.commit_basket_state(
        **_basket_kwargs(),
        anchor=(20000.0, "2026-08-17T09:45:00+05:30"),
        new_claims=(_claim_seed(role="CE", sequence=1, target_leg_id=f"{BASKET_ID}:CE:1"),),
    )

    state = load_basket_roll_state(
        repo,
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE,
        basket_id=BASKET_ID,
    )
    assert isinstance(state, BasketRollState)
    assert state.reference_price == 20000.0
    assert state.roll_count(LegRole.CE) == 1
    assert state.roll_count(LegRole.PE) == 0
    claim = state.active_claim(LegRole.CE)
    assert claim is not None
    assert claim.lifecycle_state == "CLAIMED"
    assert claim.is_terminal is False


def test_load_basket_roll_state_on_a_basket_with_no_roll_history_is_empty_not_none(tmp_path):  # type: ignore[no-untyped-def]
    """Every straddle_920 basket, and a fresh rolling_strangle_otm1 basket
    before its first roll, must get a real (empty) BasketRollState — never
    None — so a strategy never has to distinguish "never loaded" from
    "loaded, nothing yet"."""
    repo = _repository(tmp_path)
    repo.upsert_strategy_basket(**_basket_kwargs())

    state = load_basket_roll_state(
        repo,
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE,
        basket_id=BASKET_ID,
    )
    assert state.reference_price is None
    assert state.anchor_candle_ts is None
    assert state.claims == ()
    assert state.roll_count(LegRole.CE) == 0
    assert state.active_claim(LegRole.CE) is None


# ------------------------------------------------------------------ atomicity
def test_commit_basket_state_is_atomic_under_injected_failure(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    """A failure writing the second of two claims must roll back the whole
    transaction — the basket row and the anchor row (both written earlier in
    the same transaction) must not land either."""
    repo = _repository(tmp_path)

    calls = {"n": 0}
    original = ExecutionRepository._write_basket_roll_row

    def _flaky(self, conn, **kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("injected failure writing the second claim")
        return original(self, conn, **kwargs)

    monkeypatch.setattr(ExecutionRepository, "_write_basket_roll_row", _flaky)

    with pytest.raises(RuntimeError, match="injected failure"):
        repo.commit_basket_state(
            **_basket_kwargs(),
            anchor=(20000.0, "2026-08-17T09:45:00+05:30"),
            new_claims=(
                _claim_seed(role="CE", sequence=1, target_leg_id=f"{BASKET_ID}:CE:1"),
                _claim_seed(
                    role="PE", sequence=1, target_leg_id=f"{BASKET_ID}:PE:1", group="grp-both"
                ),
            ),
        )

    assert (
        repo.load_strategy_basket(
            strategy_id=STRATEGY_ID, execution_mode=ExecutionMode.PAPER, trading_date=TRADING_DATE
        )
        is None
    ), "the basket row must not survive a failure later in the same transaction"
    assert (
        repo.load_basket_roll_anchor(
            strategy_id=STRATEGY_ID,
            execution_mode=ExecutionMode.PAPER,
            trading_date=TRADING_DATE,
            basket_id=BASKET_ID,
        )
        is None
    ), "the anchor row must not survive a failure later in the same transaction"
    assert repo.load_basket_rolls(basket_id=BASKET_ID) == [], (
        "the first claim must not survive a failure writing the second, in the same "
        "transaction"
    )


def test_a_post_crash_retry_of_the_same_claim_is_idempotent_not_a_double_claim(tmp_path):  # type: ignore[no-untyped-def]
    """UNIQUE (basket_id, leg_role, roll_sequence): a retried commit for the
    exact same claim upserts in place rather than creating a second row."""
    repo = _repository(tmp_path)
    seed = _claim_seed(role="CE", sequence=1, target_leg_id=f"{BASKET_ID}:CE:1")
    repo.commit_basket_state(**_basket_kwargs(), new_claims=(seed,))
    repo.commit_basket_state(**_basket_kwargs(), new_claims=(seed,))

    rolls = repo.load_basket_rolls(basket_id=BASKET_ID)
    assert len(rolls) == 1
    assert rolls[0]["roll_sequence"] == 1


# ------------------------------------------------------- fail-closed loading
def test_load_basket_roll_state_fails_closed_on_an_unrecognised_leg_role(tmp_path):  # type: ignore[no-untyped-def]
    repo = _repository(tmp_path)
    repo.upsert_strategy_basket(**_basket_kwargs())
    repo.upsert_basket_roll(
        runtime_id=RUNTIME_ID,
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE,
        basket_id=BASKET_ID,
        claim_group_id="grp-1",
        leg_role="NOT_A_REAL_ROLE",
        roll_sequence=1,
        lifecycle_state="CLAIMED",
        target_leg_id=f"{BASKET_ID}:X:1",
        close_correlation_id=None,
        close_intent_id=None,
        replacement_leg_id=None,
        reference_price_at_claim=20000.0,
        claim_candle_ts="2026-08-17T09:50:00+05:30",
        claimed_at="2026-08-17T09:50:01+05:30",
    )

    with pytest.raises(BasketRowInconsistent, match="NOT_A_REAL_ROLE"):
        load_basket_roll_state(
            repo,
            strategy_id=STRATEGY_ID,
            execution_mode=ExecutionMode.PAPER,
            trading_date=TRADING_DATE,
            basket_id=BASKET_ID,
        )


def test_an_unrecognised_lifecycle_state_does_not_raise_and_is_conservatively_non_terminal(
    tmp_path,  # type: ignore[no-untyped-def]
):
    """Unlike leg_role, lifecycle_state is deliberately not validated against
    a closed set on load — RollClaim.is_terminal treats anything it does not
    recognise as terminal as False (still needs managing), never guessing
    the other way."""
    repo = _repository(tmp_path)
    repo.upsert_strategy_basket(**_basket_kwargs())
    repo.upsert_basket_roll(
        runtime_id=RUNTIME_ID,
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE,
        basket_id=BASKET_ID,
        claim_group_id="grp-1",
        leg_role="CE",
        roll_sequence=1,
        lifecycle_state="SOME_FUTURE_STATE_THIS_CODE_DOES_NOT_KNOW",
        target_leg_id=f"{BASKET_ID}:CE:1",
        close_correlation_id=None,
        close_intent_id=None,
        replacement_leg_id=None,
        reference_price_at_claim=20000.0,
        claim_candle_ts="2026-08-17T09:50:00+05:30",
        claimed_at="2026-08-17T09:50:01+05:30",
    )

    state = load_basket_roll_state(
        repo,
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE,
        basket_id=BASKET_ID,
    )
    claim = state.active_claim(LegRole.CE)
    assert claim is not None
    assert claim.lifecycle_state == "SOME_FUTURE_STATE_THIS_CODE_DOES_NOT_KNOW"
    assert claim.is_terminal is False


# ----------------------------------------------------- every LegRole member
@pytest.mark.parametrize(
    "role",
    [
        LegRole.CE,
        LegRole.PE,
        LegRole.GENERIC,
        LegRole.SHORT_CALL,
        LegRole.SHORT_PUT,
        LegRole.HEDGE_CALL,
        LegRole.HEDGE_PUT,
    ],
)
def test_every_leg_role_round_trips_through_the_roll_ledger(tmp_path, role):  # type: ignore[no-untyped-def]
    repo = _repository(tmp_path)
    repo.upsert_strategy_basket(**_basket_kwargs())
    repo.upsert_basket_roll(
        runtime_id=RUNTIME_ID,
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE,
        basket_id=BASKET_ID,
        claim_group_id="grp-1",
        leg_role=role.value,
        roll_sequence=1,
        lifecycle_state="CLAIMED",
        target_leg_id=f"{BASKET_ID}:{role.value}:1",
        close_correlation_id=None,
        close_intent_id=None,
        replacement_leg_id=None,
        reference_price_at_claim=20000.0,
        claim_candle_ts="2026-08-17T09:50:00+05:30",
        claimed_at="2026-08-17T09:50:01+05:30",
    )

    state = load_basket_roll_state(
        repo,
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE,
        basket_id=BASKET_ID,
    )
    claim = state.active_claim(role)
    assert claim is not None
    assert claim.leg_role is role
    assert isinstance(claim, RollClaim)


# -------------------------------------- reserve_roll_close_intent / order_intent_by_id
def _intent(*, basket_id: str, leg_id: str, security_id: str, sequence: int) -> OrderIntent:
    return OrderIntent(
        correlation_id=f"PAPER-{RUNTIME_ID}-{STRATEGY_ID}-{TRADING_DATE}-{sequence}",
        strategy_id=STRATEGY_ID,
        runtime_id=RUNTIME_ID,
        execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE,
        sequence_number=sequence,
        instrument=security_id,
        security_id=security_id,
        side=Side.BUY,
        quantity=650,
        order_type=OrderType.MARKET,
        product_type="INTRADAY",
        created_at=datetime.now(UTC),
        basket_id=basket_id,
        leg_id=leg_id,
        risk_decision=RiskDecision.ALLOWED,
    )


def _open_session(repo: ExecutionRepository) -> int:
    session = repo.open_session(
        runtime_id=RUNTIME_ID,
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        process_role="worker",
        pid=1,
    )
    return session.id


def test_reserve_roll_close_intent_atomically_associates_the_claim(tmp_path):  # type: ignore[no-untyped-def]
    repo = _repository(tmp_path)
    session_id = _open_session(repo)
    repo.commit_basket_state(
        **_basket_kwargs(),
        new_claims=(_claim_seed(role="CE", sequence=1, target_leg_id=f"{BASKET_ID}:CE:1"),),
    )

    intent_id = repo.reserve_roll_close_intent(
        session_id=session_id,
        intent=_intent(
            basket_id=BASKET_ID, leg_id=f"{BASKET_ID}:CE:1", security_id="CE1", sequence=1
        ),
        leg_role="CE",
        roll_sequence=1,
    )
    assert intent_id > 0

    rolls = repo.load_basket_rolls(basket_id=BASKET_ID)
    assert len(rolls) == 1
    assert rolls[0]["lifecycle_state"] == "EXIT_SUBMISSION_PENDING"
    assert rolls[0]["close_intent_id"] == intent_id
    assert rolls[0]["close_correlation_id"] is not None

    row = repo.order_intent_by_id(intent_id)
    assert row is not None
    assert row["intent_id"] == intent_id
    assert row["order_status"] is None  # reserved, not yet submitted/recorded


def test_reserve_roll_close_intent_refuses_when_there_is_no_claimed_row_to_associate(tmp_path):  # type: ignore[no-untyped-def]
    """Never silently create or overwrite a claim it was not asked to
    associate with — e.g. a wrong (leg_role, roll_sequence), or a claim
    that already moved past CLAIMED."""
    repo = _repository(tmp_path)
    session_id = _open_session(repo)
    repo.commit_basket_state(**_basket_kwargs())  # no claims at all

    with pytest.raises(ValueError, match="no CLAIMED"):
        repo.reserve_roll_close_intent(
            session_id=session_id,
            intent=_intent(
                basket_id=BASKET_ID, leg_id=f"{BASKET_ID}:CE:1", security_id="CE1", sequence=1
            ),
            leg_role="CE",
            roll_sequence=1,
        )


def test_reserve_roll_close_intent_refuses_a_second_time_for_an_already_pending_claim(tmp_path):  # type: ignore[no-untyped-def]
    repo = _repository(tmp_path)
    session_id = _open_session(repo)
    repo.commit_basket_state(
        **_basket_kwargs(),
        new_claims=(_claim_seed(role="CE", sequence=1, target_leg_id=f"{BASKET_ID}:CE:1"),),
    )
    repo.reserve_roll_close_intent(
        session_id=session_id,
        intent=_intent(
            basket_id=BASKET_ID, leg_id=f"{BASKET_ID}:CE:1", security_id="CE1", sequence=1
        ),
        leg_role="CE",
        roll_sequence=1,
    )

    with pytest.raises(ValueError, match="no CLAIMED"):
        repo.reserve_roll_close_intent(
            session_id=session_id,
            intent=_intent(
                basket_id=BASKET_ID, leg_id=f"{BASKET_ID}:CE:1", security_id="CE1", sequence=2
            ),
            leg_role="CE",
            roll_sequence=1,
        )


def test_order_intent_by_id_returns_none_for_an_unknown_id(tmp_path):  # type: ignore[no-untyped-def]
    repo = _repository(tmp_path)
    assert repo.order_intent_by_id(999999) is None
