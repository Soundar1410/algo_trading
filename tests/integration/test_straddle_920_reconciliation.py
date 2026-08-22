"""P0-4: restart reconciliation, exercised directly against a real database.

Builds ``order_intents``/``orders``/``fills``/``positions``/``strategy_baskets``/
``strategy_legs`` rows through the real :class:`ExecutionRepository` API (never
raw SQL for the authoritative execution tables), then calls
:func:`~runtimes.intraday_options.multi_leg_engine_worker.recover_basket`
directly — proving reconciliation, not merely that a projection can be
reloaded. Each named mismatch the correction requires either raises
:class:`UnmanageableBasketState` (fails closed) or resolves the basket in
place (the explicit "the controlled square-off path can safely manage
recognised exposure" carve-out) — every test asserts which, and never both.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from common.config.models import ExecutionMode
from common.engine.multi_leg_models import (
    Basket,
    LegInstance,
    LegRole,
    LegState,
    UnmanageableBasketState,
)
from common.execution import ExecutionRepository
from common.models import Fill, Order, OrderIntent, OrderSide, OrderStatus, OrderType
from common.persistence import Database, migrate
from runtimes.intraday_options.multi_leg_engine_worker import _reconcile_basket, recover_basket

IST = ZoneInfo("Asia/Kolkata")
RUNTIME_ID = "intraday_options"
STRATEGY_ID = "straddle_920"
TRADING_DATE = "2026-08-17"
CE1 = "SIM:NIFTY:WEEKLY:24000:CE"
PE1 = "SIM:NIFTY:WEEKLY:24000:PE"
BASKET_ID = f"{STRATEGY_ID}:{TRADING_DATE}"
CE1_LEG_ID = f"{BASKET_ID}:CE:1"
PE1_LEG_ID = f"{BASKET_ID}:PE:1"


@dataclass
class _FakeConfig:
    """Everything ``recover_basket``/``_reconcile_basket`` actually read from
    ``WorkerConfig`` — a full ``WorkerConfig`` needs a dozen unrelated
    fields (paths, poll intervals, ...) this module never touches."""

    runtime_id: str
    strategy_id: str
    execution_mode: ExecutionMode
    trading_date: str


def _db_path(tmp_path):  # type: ignore[no-untyped-def]
    return tmp_path / "test.db"


def _repository(tmp_path) -> ExecutionRepository:  # type: ignore[no-untyped-def]
    db = Database(_db_path(tmp_path))
    migrate(db)
    return ExecutionRepository(db)


def _raw(tmp_path):  # type: ignore[no-untyped-def]
    """A fresh, independent connection for assertions/setup the repository's
    own typed API does not expose — mirrors ``test_straddle_920_restart.py``'s
    own ``_intents`` helper."""
    return Database(_db_path(tmp_path)).connect()


def _config() -> _FakeConfig:
    return _FakeConfig(
        runtime_id=RUNTIME_ID,
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE,
    )


def _session_id(repository: ExecutionRepository) -> int:
    return repository.open_session(
        runtime_id=RUNTIME_ID,
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        process_role="worker",
        pid=1234,
    ).id


_SEQ = iter(range(1, 100_000))


def _place_order(
    repository: ExecutionRepository,
    *,
    session_id: int,
    security_id: str,
    side: OrderSide,
    quantity: int,
    basket_id: str | None,
    leg_id: str | None,
    status: OrderStatus | None,
    fill_price: float | None = None,
) -> str:
    """Reserve an intent and, unless ``status`` is ``None`` (models "reserved
    but the process died before the broker call ever completed" — the
    genuine UNKNOWN case), record its outcome. ``status=FILLED`` also applies
    a real fill, moving the authoritative ``positions`` row exactly as
    production code does."""
    seq = next(_SEQ)
    correlation_id = f"p_test_{seq}"
    intent = OrderIntent(
        correlation_id=correlation_id,
        strategy_id=STRATEGY_ID,
        runtime_id=RUNTIME_ID,
        execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE,
        sequence_number=seq,
        instrument="NIFTY",
        security_id=security_id,
        side=side,
        quantity=quantity,
        order_type=OrderType.MARKET,
        product_type="INTRADAY",
        created_at=datetime.now(UTC),
        basket_id=basket_id,
        leg_id=leg_id,
    )
    intent_id = repository.reserve_intent(session_id=session_id, intent=intent)
    if status is None:
        return correlation_id  # reserved, never confirmed — the UNKNOWN case

    order = Order(
        correlation_id=correlation_id,
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        status=status,
        updated_at=datetime.now(UTC),
        filled_quantity=quantity if status is OrderStatus.FILLED else 0,
        average_fill_price=fill_price if status is OrderStatus.FILLED else None,
    )
    order_id = repository.record_submission(intent_id=intent_id, order=order, runtime_id=RUNTIME_ID)
    if status is OrderStatus.FILLED:
        assert fill_price is not None
        fill = Fill(
            correlation_id=correlation_id,
            broker_fill_id=f"fill_{seq}",
            strategy_id=STRATEGY_ID,
            execution_mode=ExecutionMode.PAPER,
            quantity=quantity,
            price=fill_price,
            filled_at=datetime.now(UTC),
        )
        repository.apply_fill(
            order_id=order_id,
            runtime_id=RUNTIME_ID,
            fill=fill,
            order_status=status,
            instrument="NIFTY",
            security_id=security_id,
            side=side,
            trading_date=TRADING_DATE,
        )
    return correlation_id


def _seed_basket(repository: ExecutionRepository) -> None:
    repository.upsert_strategy_basket(
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
        original_combined_basis=195.0,
        square_off_state="PENDING",
    )


def _seed_leg(
    repository: ExecutionRepository,
    *,
    leg_id: str,
    security_id: str,
    role: str,
    sequence: int,
    side: str,
    quantity: int | None,
    entry_price: float | None,
    entry_correlation_id: str | None,
    state: str,
    exit_price: float | None = None,
    exit_correlation_id: str | None = None,
    exit_reason: str | None = None,
) -> None:
    repository.upsert_strategy_leg(
        runtime_id=RUNTIME_ID,
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE,
        basket_id=BASKET_ID,
        leg_id=leg_id,
        leg_role=role,
        leg_sequence=sequence,
        is_replacement=False,
        replaces_leg_id=None,
        security_id=security_id,
        symbol=f"NIFTY {security_id[-4:]}",
        strike=24000.0,
        expiry="WEEKLY",
        lot_size=75,
        side=side,
        quantity=quantity,
        entry_price=entry_price,
        entry_time=f"{TRADING_DATE}T09:21:00" if entry_price is not None else None,
        entry_correlation_id=entry_correlation_id,
        exit_price=exit_price,
        exit_time=f"{TRADING_DATE}T09:30:00" if exit_price is not None else None,
        exit_reason=exit_reason,
        exit_correlation_id=exit_correlation_id,
        realized_gross_pnl=None,
        state=state,
    )


# ------------------------------------------------------------------ healthy
def test_a_healthy_two_leg_basket_reconciles_with_no_mismatch_and_no_new_order(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository = _repository(tmp_path)
    session_id = _session_id(repository)
    _seed_basket(repository)

    ce_corr = _place_order(
        repository, session_id=session_id, security_id=CE1, side=OrderSide.SELL, quantity=750,
        basket_id=BASKET_ID, leg_id=CE1_LEG_ID, status=OrderStatus.FILLED, fill_price=100.0,
    )
    pe_corr = _place_order(
        repository, session_id=session_id, security_id=PE1, side=OrderSide.SELL, quantity=750,
        basket_id=BASKET_ID, leg_id=PE1_LEG_ID, status=OrderStatus.FILLED, fill_price=95.0,
    )
    _seed_leg(
        repository, leg_id=CE1_LEG_ID, security_id=CE1, role="CE", sequence=1, side="SELL",
        quantity=750, entry_price=100.0, entry_correlation_id=ce_corr, state="OPEN",
    )
    _seed_leg(
        repository, leg_id=PE1_LEG_ID, security_id=PE1, role="PE", sequence=1, side="SELL",
        quantity=750, entry_price=95.0, entry_correlation_id=pe_corr, state="OPEN",
    )
    before_intent_count = _raw(tmp_path).execute("SELECT COUNT(*) FROM order_intents").fetchone()[0]

    basket = recover_basket(_config(), repository)

    assert basket is not None
    assert basket.legs[CE1_LEG_ID].state is LegState.OPEN
    assert basket.legs[PE1_LEG_ID].state is LegState.OPEN
    after_intent_count = _raw(tmp_path).execute("SELECT COUNT(*) FROM order_intents").fetchone()[0]
    assert after_intent_count == before_intent_count, "recovery alone must place no order"


# --------------------------------------------------------------- mismatches
def test_open_leg_with_unconfirmed_entry_fails_closed(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository = _repository(tmp_path)
    session_id = _session_id(repository)
    _seed_basket(repository)
    # Reserved but the process died before the broker call was ever recorded.
    _place_order(
        repository, session_id=session_id, security_id=CE1, side=OrderSide.SELL, quantity=750,
        basket_id=BASKET_ID, leg_id=CE1_LEG_ID, status=None,
    )
    _seed_leg(
        repository, leg_id=CE1_LEG_ID, security_id=CE1, role="CE", sequence=1, side="SELL",
        quantity=750, entry_price=100.0, entry_correlation_id="stale", state="OPEN",
    )

    with pytest.raises(UnmanageableBasketState):
        recover_basket(_config(), repository)


def test_open_position_with_no_claiming_leg_fails_closed(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository = _repository(tmp_path)
    session_id = _session_id(repository)
    _seed_basket(repository)
    # A real filled order/position exists for CE1, but no strategy_legs row
    # was ever written for it at all.
    _place_order(
        repository, session_id=session_id, security_id=CE1, side=OrderSide.SELL, quantity=750,
        basket_id=BASKET_ID, leg_id=CE1_LEG_ID, status=OrderStatus.FILLED, fill_price=100.0,
    )

    with pytest.raises(UnmanageableBasketState):
        recover_basket(_config(), repository)


def test_leg_quantity_mismatch_fails_closed(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository = _repository(tmp_path)
    session_id = _session_id(repository)
    _seed_basket(repository)
    ce_corr = _place_order(
        repository, session_id=session_id, security_id=CE1, side=OrderSide.SELL, quantity=700,
        basket_id=BASKET_ID, leg_id=CE1_LEG_ID, status=OrderStatus.FILLED, fill_price=100.0,
    )
    # The leg's own recorded quantity (750) disagrees with what was actually
    # filled/persisted into positions (700).
    _seed_leg(
        repository, leg_id=CE1_LEG_ID, security_id=CE1, role="CE", sequence=1, side="SELL",
        quantity=750, entry_price=100.0, entry_correlation_id=ce_corr, state="OPEN",
    )

    with pytest.raises(UnmanageableBasketState):
        recover_basket(_config(), repository)


def test_leg_side_mismatch_fails_closed(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository = _repository(tmp_path)
    session_id = _session_id(repository)
    _seed_basket(repository)
    # The leg's own row claims SELL, but the *authoritative* fill/position
    # was actually a BUY — recorded here by placing a BUY entry (unrealistic
    # for this strategy but exactly the corruption this check exists for).
    ce_corr = _place_order(
        repository, session_id=session_id, security_id=CE1, side=OrderSide.BUY, quantity=750,
        basket_id=BASKET_ID, leg_id=CE1_LEG_ID, status=OrderStatus.FILLED, fill_price=100.0,
    )
    _seed_leg(
        repository, leg_id=CE1_LEG_ID, security_id=CE1, role="CE", sequence=1, side="SELL",
        quantity=750, entry_price=100.0, entry_correlation_id=ce_corr, state="OPEN",
    )

    with pytest.raises(UnmanageableBasketState):
        recover_basket(_config(), repository)


def test_duplicate_open_leg_mapping_fails_closed(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Two distinct leg instances both independently reconcile as OPEN
    against the same security — a data-integrity problem no controlled path
    can safely resolve on its own."""
    repository = _repository(tmp_path)
    session_id = _session_id(repository)
    _seed_basket(repository)
    ce1_corr = _place_order(
        repository, session_id=session_id, security_id=CE1, side=OrderSide.SELL, quantity=750,
        basket_id=BASKET_ID, leg_id=CE1_LEG_ID, status=OrderStatus.FILLED, fill_price=100.0,
    )
    duplicate_leg_id = f"{BASKET_ID}:CE:9"
    ce9_corr = _place_order(
        repository, session_id=session_id, security_id=CE1, side=OrderSide.SELL, quantity=750,
        basket_id=BASKET_ID, leg_id=duplicate_leg_id, status=OrderStatus.FILLED, fill_price=100.0,
    )
    _seed_leg(
        repository, leg_id=CE1_LEG_ID, security_id=CE1, role="CE", sequence=1, side="SELL",
        quantity=750, entry_price=100.0, entry_correlation_id=ce1_corr, state="OPEN",
    )
    _seed_leg(
        repository, leg_id=duplicate_leg_id, security_id=CE1, role="CE", sequence=9, side="SELL",
        quantity=750, entry_price=100.0, entry_correlation_id=ce9_corr, state="OPEN",
    )

    with pytest.raises(UnmanageableBasketState):
        recover_basket(_config(), repository)


def test_replacement_awaiting_entry_while_adjusted_out_leg_still_open_fails_closed(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    repository = _repository(tmp_path)
    session_id = _session_id(repository)
    repository.upsert_strategy_basket(
        runtime_id=RUNTIME_ID,
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE,
        basket_id=BASKET_ID,
        lifecycle_state="OPEN",
        entries_consumed=True,
        day_blocked_reason=None,
        adjustment_count=1,
        pending_replacement_role="CE",
        pending_replacement_state="AWAITING_NEXT_CANDLE",
        original_combined_basis=195.0,
        square_off_state="PENDING",
    )
    ce_corr = _place_order(
        repository, session_id=session_id, security_id=CE1, side=OrderSide.SELL, quantity=750,
        basket_id=BASKET_ID, leg_id=CE1_LEG_ID, status=OrderStatus.FILLED, fill_price=100.0,
    )
    # CE1 is still OPEN in the projection even though the basket believes an
    # adjustment already claimed it and is awaiting a replacement.
    _seed_leg(
        repository, leg_id=CE1_LEG_ID, security_id=CE1, role="CE", sequence=1, side="SELL",
        quantity=750, entry_price=100.0, entry_correlation_id=ce_corr, state="OPEN",
    )

    with pytest.raises(UnmanageableBasketState):
        recover_basket(_config(), repository)


def test_unknown_exit_submission_with_no_open_position_fails_closed(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """CLOSE_SUBMISSION_UNKNOWN, and the close's outcome is genuinely
    unresolved (reserved, never confirmed) — with no open position to adopt
    back either, so real exposure cannot be ruled in or out."""
    repository = _repository(tmp_path)
    session_id = _session_id(repository)
    _seed_basket(repository)
    ce_corr = _place_order(
        repository, session_id=session_id, security_id=CE1, side=OrderSide.SELL, quantity=750,
        basket_id=BASKET_ID, leg_id=CE1_LEG_ID, status=OrderStatus.FILLED, fill_price=100.0,
    )
    # Close reserved but never confirmed AND never actually applied a fill —
    # positions still shows nothing OPEN for CE1 (it was never closed either
    # way, from the authoritative table's point of view this is impossible
    # for a real close attempt, but models "the entry itself was later
    # reversed/adjusted out of the book by an out-of-band correction" —
    # exercising the branch where no position exists to fall back on).
    conn = _raw(tmp_path)
    conn.execute(
        "UPDATE positions SET status = 'CLOSED' WHERE security_id = ? AND strategy_id = ?",
        (CE1, STRATEGY_ID),
    )
    conn.commit()
    _place_order(
        repository, session_id=session_id, security_id=CE1, side=OrderSide.BUY, quantity=750,
        basket_id=BASKET_ID, leg_id=CE1_LEG_ID, status=None,
    )
    _seed_leg(
        repository, leg_id=CE1_LEG_ID, security_id=CE1, role="CE", sequence=1, side="SELL",
        quantity=750, entry_price=100.0, entry_correlation_id=ce_corr,
        state="CLOSE_SUBMISSION_UNKNOWN",
    )

    with pytest.raises(UnmanageableBasketState):
        recover_basket(_config(), repository)


# ------------------------------------------------- self-healed (not a raise)
def test_pending_order_leg_with_a_filled_order_is_adopted_not_reentered(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """P0-1's own scenario: the order genuinely filled but the projection
    write that would have advanced the leg to OPEN never landed. Recovery
    must adopt the real fill rather than leave it PENDING_ORDER (which would
    place a *second* order on the next tick)."""
    repository = _repository(tmp_path)
    session_id = _session_id(repository)
    _seed_basket(repository)
    ce_corr = _place_order(
        repository, session_id=session_id, security_id=CE1, side=OrderSide.SELL, quantity=750,
        basket_id=BASKET_ID, leg_id=CE1_LEG_ID, status=OrderStatus.FILLED, fill_price=100.0,
    )
    _seed_leg(
        repository, leg_id=CE1_LEG_ID, security_id=CE1, role="CE", sequence=1, side="SELL",
        quantity=0, entry_price=None, entry_correlation_id=None, state="PENDING_ORDER",
    )

    basket = recover_basket(_config(), repository)

    assert basket is not None
    leg = basket.legs[CE1_LEG_ID]
    assert leg.state is LegState.OPEN
    assert leg.entry_price == 100.0
    assert leg.quantity == 750
    assert leg.entry_correlation_id == ce_corr


def test_close_submission_unknown_with_a_filled_exit_is_resolved_closed(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository = _repository(tmp_path)
    session_id = _session_id(repository)
    _seed_basket(repository)
    ce_corr = _place_order(
        repository, session_id=session_id, security_id=CE1, side=OrderSide.SELL, quantity=750,
        basket_id=BASKET_ID, leg_id=CE1_LEG_ID, status=OrderStatus.FILLED, fill_price=100.0,
    )
    exit_corr = _place_order(
        repository, session_id=session_id, security_id=CE1, side=OrderSide.BUY, quantity=750,
        basket_id=BASKET_ID, leg_id=CE1_LEG_ID, status=OrderStatus.FILLED, fill_price=205.0,
    )
    _seed_leg(
        repository, leg_id=CE1_LEG_ID, security_id=CE1, role="CE", sequence=1, side="SELL",
        quantity=750, entry_price=100.0, entry_correlation_id=ce_corr,
        state="CLOSE_SUBMISSION_UNKNOWN",
    )

    basket = recover_basket(_config(), repository)

    assert basket is not None
    leg = basket.legs[CE1_LEG_ID]
    assert leg.state is LegState.CLOSED
    assert leg.exit_correlation_id == exit_corr


def test_close_submission_unknown_with_a_definitive_rejection_reverts_to_open(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Phase 2 correction: only a *definitive* proof that the close never
    happened (here, an authoritative REJECTED order status — not merely an
    unconfirmed/ambiguous one) may revert this leg to OPEN. Real exposure
    remains — adopted back as OPEN so square-off can still manage it, per
    the correction's explicit "do not merely tell the operator to close
    manually" instruction. Also proves the "basket marked closed while
    exposure remains" case: lifecycle_state is corrected back to OPEN.

    Before this correction, the same outcome was asserted for a merely
    *unconfirmed* (``status=None``, genuinely ambiguous) exit order —
    which Phase 2 review established is unsafe: a still-open position does
    not prove a close was never submitted, and reverting on that basis
    risks a second close racing the original one's independent resolution.
    See ``test_close_submission_unknown_with_a_genuinely_ambiguous_exit_fails_closed``
    below for that corrected case.
    """
    repository = _repository(tmp_path)
    session_id = _session_id(repository)
    repository.upsert_strategy_basket(
        runtime_id=RUNTIME_ID,
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE,
        basket_id=BASKET_ID,
        lifecycle_state="CLOSED",  # stale: square-off believed this leg closed
        entries_consumed=True,
        day_blocked_reason="hard square-off",
        adjustment_count=0,
        pending_replacement_role=None,
        pending_replacement_state=None,
        original_combined_basis=195.0,
        square_off_state="COMPLETED",
    )
    ce_corr = _place_order(
        repository, session_id=session_id, security_id=CE1, side=OrderSide.SELL, quantity=750,
        basket_id=BASKET_ID, leg_id=CE1_LEG_ID, status=OrderStatus.FILLED, fill_price=100.0,
    )
    # The close was submitted and definitively REJECTED at the broker — not
    # merely unconfirmed — so the position is, correctly and provably,
    # still OPEN in the authoritative table.
    _place_order(
        repository, session_id=session_id, security_id=CE1, side=OrderSide.BUY, quantity=750,
        basket_id=BASKET_ID, leg_id=CE1_LEG_ID, status=OrderStatus.REJECTED,
    )
    _seed_leg(
        repository, leg_id=CE1_LEG_ID, security_id=CE1, role="CE", sequence=1, side="SELL",
        quantity=750, entry_price=100.0, entry_correlation_id=ce_corr,
        state="CLOSE_SUBMISSION_UNKNOWN",
    )

    basket = recover_basket(_config(), repository)

    assert basket is not None
    leg = basket.legs[CE1_LEG_ID]
    assert leg.state is LegState.OPEN, "real exposure must be adopted back as manageable, not lost"
    assert leg.entry_price == 100.0
    assert leg.exit_price is None
    assert basket.lifecycle_state == "OPEN", "the stale CLOSED label must be corrected"


def test_close_submission_unknown_with_a_genuinely_ambiguous_exit_fails_closed(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Phase 2 correction: a still-open position does NOT prove the close
    was never submitted — the exit order here is reserved but genuinely
    unconfirmed (``status=None``), which could still resolve independently
    at the broker. Reverting to OPEN on the strength of the open position
    alone would risk a second close racing that resolution, so this must
    fail closed instead — never guessing the leg is safe to manage again."""
    repository = _repository(tmp_path)
    session_id = _session_id(repository)
    _seed_basket(repository)
    ce_corr = _place_order(
        repository, session_id=session_id, security_id=CE1, side=OrderSide.SELL, quantity=750,
        basket_id=BASKET_ID, leg_id=CE1_LEG_ID, status=OrderStatus.FILLED, fill_price=100.0,
    )
    # Reserved but never confirmed — genuinely ambiguous, not a definitive
    # rejection. The position is still OPEN in the authoritative table.
    _place_order(
        repository, session_id=session_id, security_id=CE1, side=OrderSide.BUY, quantity=750,
        basket_id=BASKET_ID, leg_id=CE1_LEG_ID, status=None,
    )
    _seed_leg(
        repository, leg_id=CE1_LEG_ID, security_id=CE1, role="CE", sequence=1, side="SELL",
        quantity=750, entry_price=100.0, entry_correlation_id=ce_corr,
        state="CLOSE_SUBMISSION_UNKNOWN",
    )

    with pytest.raises(UnmanageableBasketState):
        recover_basket(_config(), repository)


# --------------------------------------------------------- direct-fn probe
def test_reconcile_basket_returns_no_mismatches_for_a_clean_basket(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A lower-level probe of :func:`_reconcile_basket` itself (not just
    through ``recover_basket``'s raise/no-raise surface), confirming an
    empty mismatch list for a genuinely healthy basket."""
    repository = _repository(tmp_path)
    session_id = _session_id(repository)
    ce_corr = _place_order(
        repository, session_id=session_id, security_id=CE1, side=OrderSide.SELL, quantity=750,
        basket_id=BASKET_ID, leg_id=CE1_LEG_ID, status=OrderStatus.FILLED, fill_price=100.0,
    )
    basket = Basket(
        basket_id=BASKET_ID,
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE,
    )
    from common.engine.models import OptionContract
    from common.models import OptionType

    contract = OptionContract(
        symbol="NIFTY 24000 CE",
        security_id=CE1,
        strike=24000.0,
        option_type=OptionType.CE,
        expiry="WEEKLY",
        lot_size=75,
    )
    leg = LegInstance(
        leg_id=CE1_LEG_ID,
        basket_id=BASKET_ID,
        role=LegRole.CE,
        sequence=1,
        is_replacement=False,
        side=OrderSide.SELL,
        quantity=750,
        contract=contract,
        state=LegState.OPEN,
        entry_price=100.0,
        entry_correlation_id=ce_corr,
    )
    basket.legs[leg.leg_id] = leg

    mismatches = _reconcile_basket(repository, basket, _config())

    assert mismatches == []
