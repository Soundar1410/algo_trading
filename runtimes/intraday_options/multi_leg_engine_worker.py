"""The generic multi-leg engine, running inside one worker process.

Sibling to :mod:`runtimes.intraday_options.engine_worker`, for the identical
reason :class:`~common.engine.multi_leg_engine.MultiLegEngine` is a sibling of
:class:`~common.engine.engine.TradingEngine`: driving N concurrent legs is a
genuinely different tick-routing/restart shape, not a variant of the single-
leg one. Everything that IS leg-count-agnostic in ``engine_worker.py`` — the
broker/lifecycle/gateway construction, ``HubTickFeed``, the square-off
authority, the reporting bindings — is built the same way here, by the same
reasoning; only the pieces that assume one leg are new.

**Deferred import, same discipline as ``engine_worker.py``.** Reached from
exactly one deferred ``import`` inside ``worker.py``'s multi-leg branch.
Nothing may import this module at another module's top level.

**No live mode, deliberately, for now.** ``straddle_920``'s exact legacy
partial-execution behaviour can leave one open short option leg — a live
proposal for that requires a separate written risk review and approval (spec
section 18) that has not happened. Rather than build and then leave untested a
parallel live-wiring path (Dhan live broker, account reservations, live
preflight) this worker never exercises, ``_build`` refuses ``ExecutionMode.
LIVE`` outright. Every committed multi-leg strategy config ships
``mode: paper`` regardless (enforced by ``scripts/assert_no_live_config_committed.py``),
so this is not a live gate weakened — it is one no committed config can reach,
matching the same fail-closed posture ``common.broker.factory.build_broker``
already has for paper vs. live.

**No warm-up wiring, deliberately.** Every multi-leg strategy built against
:class:`~common.engine.multi_leg_strategy.BaseMultiLegStrategy` returns
``None`` from ``warmup_spec()`` unless it opts in — ``straddle_920`` is
time/event-based and has no indicator history to warm (spec section 9.1). A
future multi-leg strategy that needs real warm-up would need this worker
extended the same way ``engine_worker.py`` was; documented as a known
limitation, not silently unsupported (``EngineConfig.warmup_from_history``
still exists and is honoured by whichever strategy reads it).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import replace as dataclass_replace
from datetime import datetime
from importlib import import_module
from pathlib import Path
from typing import Any

from common.broker import build_broker
from common.broker.quotes import QuoteBook
from common.config.models import ExecutionMode
from common.engine.config import EngineConfig, SessionConfig
from common.engine.gateway import LifecycleGateway
from common.engine.hub_feed import HubTickFeed
from common.engine.multi_leg_engine import MultiLegEngine
from common.engine.multi_leg_models import (
    AdjustmentLifecycle,
    Basket,
    BasketRollState,
    LegInstance,
    LegState,
    UnmanageableBasketState,
)
from common.engine.multi_leg_state import BasketRowInconsistent, RollLedger
from common.engine.multi_leg_state import load_basket as _load_basket
from common.engine.multi_leg_state import persist_basket as _persist_basket_row
from common.engine.multi_leg_state import persist_leg as _persist_leg_row
from common.engine.multi_leg_strategy import BaseMultiLegStrategy
from common.engine.positions import PositionManager
from common.engine.reporting_bindings import HeartbeatEngineReporter, RepositoryReportWriter
from common.engine.selection import (
    DhanOptionChainResolver,
    OptionSelector,
    SimulatedOptionChainResolver,
)
from common.engine.square_off import PersistedSquareOffAuthority, SquareOffAuthority
from common.execution import ExecutionRepository, OrderLifecycle
from common.execution.repository import resolve_intent_outcome
from common.health import HealthState, HeartbeatWriter
from common.logging import get_logger
from common.models import OrderSide
from common.notifications import NotificationEvent, SafeNotifier
from common.process import (
    clear_square_off_request,
    read_square_off_request,
    shutdown_signals,
    square_off_request_path,
)
from common.utils.timeutils import now_ist

# Reused verbatim: neither function has any single-leg-engine-owned type in
# its signature — both operate on plain callables/``WorkerConfig`` — so they
# are genuinely generic infrastructure, not something worth a second copy.
from .engine_worker import _combined_poll, _drain_candle_queue, _square_off_completed
from .worker import (
    MultiLegEngineWorkerConfig,
    WorkerConfig,
    WorkerOutcome,
    close_previous_session,
    resolved_config_from_worker,
)

log = get_logger(__name__)

_DRAIN_JOIN_SECONDS = 2.0

#: Phase 2 (strategy-rolling-strangle-otm1): every lifecycle_state value
#: this reconciliation pass recognises — derived from AdjustmentLifecycle
#: itself, never hand-duplicated, so it cannot silently drift from the
#: real vocabulary. See _reconcile_basket_rolls for why an unrecognised
#: value must fail closed rather than be silently skipped.
_RECOGNIZED_ROLL_LIFECYCLE_STATES = frozenset(state.value for state in AdjustmentLifecycle)


# --------------------------------------------------------------- strategy loading
def load_multi_leg_strategy(strategy_ref: str, kwargs: dict[str, Any]) -> BaseMultiLegStrategy:
    """Resolve a ``"package.module:ClassName"`` reference and construct it.

    Mirrors ``engine_worker.load_strategy`` exactly, against the multi-leg
    registry's base class instead.
    """
    module_name, separator, class_name = strategy_ref.partition(":")
    if not separator or not module_name or not class_name:
        raise ValueError(f"strategy_ref must be 'package.module:ClassName', got {strategy_ref!r}")
    module = import_module(module_name)
    try:
        factory = getattr(module, class_name)
    except AttributeError as exc:
        raise ValueError(f"{module_name!r} has no attribute {class_name!r}") from exc
    strategy = factory(**kwargs)
    if not isinstance(strategy, BaseMultiLegStrategy):
        raise TypeError(
            f"{strategy_ref!r} resolved to {type(strategy).__name__}, which is not a "
            "BaseMultiLegStrategy; the multi-leg engine cannot drive it."
        )
    return strategy


# ------------------------------------------------------------- restart recovery
def recover_basket(config: WorkerConfig, repository: ExecutionRepository) -> Basket | None:
    """Rebuild the basket a previous process left open, or ``None`` — and
    *reconcile* it (P0-2/P0-4 correction) rather than merely reconstruct it.

    ``load_basket`` alone only replays the mutable ``strategy_baskets``/
    ``strategy_legs`` projection — a *reconstruction*, not a reconciliation:
    it trusts the projection was written completely and correctly, which is
    exactly the assumption P0-1's failure-injection tests prove can be
    false (a critical persist can still fail after the order it was
    guarding has already happened, e.g. a best-effort post-fill write). This
    cross-checks that projection against the authoritative execution
    history (``order_intents``/``orders``) and the authoritative current
    book (``positions``) for every leg — see :func:`_reconcile_basket` — and
    only returns a basket once every leg's true state has either been
    confirmed to match the projection or *corrected in place* from that
    authoritative source, so already-open exposure a previous process
    genuinely opened is never left unmanaged and never duplicated.

    Mirrors ``engine_worker.recover_position``'s conservative posture:
    :func:`~common.engine.multi_leg_state.load_basket` refuses
    (``BasketRowInconsistent``) to guess at any row it cannot safely
    interpret, and :func:`_reconcile_basket` refuses (returns unresolved
    mismatches) to guess at any leg/position disagreement it cannot safely
    resolve from the authoritative tables. Both reach
    :class:`~common.engine.multi_leg_models.UnmanageableBasketState`, which
    ``MultiLegEngine._adopt_recovered_basket`` propagates — aborting the
    worker rather than trading alongside exposure it cannot prove — while an
    ordinary (non-corruption, non-mismatch) failure blocks new entries only
    and leaves any genuinely open leg visibly OPEN in the database for
    manual handling.
    """
    try:
        basket = _load_basket(
            repository,
            strategy_id=config.strategy_id,
            execution_mode=config.execution_mode,
            trading_date=config.trading_date,
        )
    except BasketRowInconsistent as exc:
        _record_recovery_failure(config, repository, str(exc))
        raise UnmanageableBasketState(str(exc)) from exc
    if basket is None:
        return None

    mismatches = _reconcile_basket(repository, basket, config)
    if mismatches:
        detail = "; ".join(mismatches)
        _record_recovery_failure(config, repository, detail)
        raise UnmanageableBasketState(detail)
    return basket


# --------------------------------------------------------- P0-4 reconciliation
def _reconcile_basket(
    repository: ExecutionRepository, basket: Basket, config: WorkerConfig
) -> list[str]:
    """Cross-check ``basket``'s projected leg states against the
    authoritative ``order_intents``/``orders``/``positions`` tables.

    Mutates ``basket``/its legs *in place* wherever the true state can be
    established from those authoritative tables and differs from the
    projection (e.g. a leg the projection still shows PENDING_ORDER that
    actually filled, or a CLOSE_SUBMISSION_UNKNOWN leg whose close never
    actually took) — this is the "do not merely tell the operator to close
    manually if the existing controlled reconciliation/square-off path can
    safely manage recognized exposure" requirement: recognised exposure is
    adopted back into a manageable state, not just reported.

    Returns every discrepancy that could **not** be safely resolved this
    way — a non-empty list means the caller must fail closed
    (``UnmanageableBasketState``), because genuine ambiguity (an order whose
    outcome cannot be established, contradictory rows) is present and no
    controlled path can manage it automatically.
    """
    open_positions = {
        p.security_id: p
        for p in repository.open_positions(
            strategy_id=config.strategy_id,
            execution_mode=config.execution_mode,
            trading_date=config.trading_date,
        )
    }
    claimed_security_ids: dict[str, str] = {}  # security_id -> leg_id, for duplicate detection
    mismatches: list[str] = []

    for leg in basket.legs.values():
        result = _reconcile_leg(repository, leg, open_positions)
        if result is not None:
            mismatches.append(result)
            continue
        if leg.state is LegState.OPEN and leg.contract is not None:
            sid = leg.contract.security_id
            other = claimed_security_ids.get(sid)
            if other is not None:
                mismatches.append(
                    f"legs {other!r} and {leg.leg_id!r} are both OPEN against the same "
                    f"position {sid} — duplicate open leg mapping"
                )
            else:
                claimed_security_ids[sid] = leg.leg_id

    # The reverse direction: an OPEN position with no leg claiming it at all.
    for security_id in open_positions:
        if security_id not in claimed_security_ids:
            mismatches.append(
                f"position {security_id} is OPEN but no leg in basket "
                f"{basket.basket_id!r} claims it"
            )

    # Phase 2 (strategy-rolling-strangle-otm1): replacement awaiting entry
    # while the adjusted-out leg is still open — checked precisely against
    # the roll ledger's own concrete target_leg_id per claim, not a
    # role-wide "not is_replacement" heuristic. That heuristic breaks the
    # moment a *second* roll closes a leg that is itself a replacement
    # (is_replacement=True) — exactly the repeated-roll case this ledger
    # exists for — since it would then silently treat that leg's own
    # staleness as invisible. basket.roll_state is never None (see
    # Basket.__post_init__/load_basket) — an empty claims tuple (every
    # straddle_920 basket that has never rolled) makes this a no-op.
    if basket.roll_state is not None:
        for claim in basket.roll_state.claims:
            if claim.lifecycle_state != "AWAITING_NEXT_CANDLE":
                continue
            target_leg = basket.legs.get(claim.target_leg_id)
            if target_leg is not None and target_leg.state is LegState.OPEN:
                mismatches.append(
                    f"basket {basket.basket_id!r} has a replacement pending for "
                    f"{claim.leg_role.value} (roll #{claim.roll_sequence}) while its "
                    f"adjusted-out leg {claim.target_leg_id!r} is still OPEN"
                )

    # Correction requirement (pre-Phase-2 legacy data, kept as a
    # defence-in-depth fallback): replacement awaiting entry while *some*
    # non-replacement leg of that role is still open. AWAITING_NEXT_CANDLE/
    # REPLACEMENT_PENDING both mean "a replacement may be entered/was
    # entered for this role" — neither may coexist with an OPEN or
    # unresolved leg of that same role other than the just-adopted
    # replacement itself. Covers a basket whose pending_replacement_role
    # was written before migration 0013 existed and so has no
    # strategy_basket_rolls row for the check above to find.
    #
    # Phase 5 fix (found while proving rolling_strangle_otm1's own restart
    # matrix; generic, shared with straddle_920 since both drive the same
    # _close_adjusted_legs_with_ledger machinery): this must fire only for
    # the two states the comment above actually names.
    # pending_replacement_role/_state are a speculative "compatibility
    # projection" written at *claim* time (_close_adjusted_legs_with_ledger
    # sets pending_replacement_state = EXIT_SUBMISSION_PENDING before the
    # close is even attempted) and are never reset on a terminal FAILED/
    # EXIT_UNKNOWN outcome — only the roll ledger's own precise per-claim
    # row is. Firing unconditionally on "role is set" therefore produced a
    # false-positive UnmanageableBasketState for the spec-required,
    # already-correctly-resolved case of a definitively rejected roll close
    # (leg correctly reconstructed OPEN, roll_sequence not refunded, no
    # replacement) — exactly the state the precise, roll_state-based check
    # above already proves is *not* a contradiction. Restricting the scalar
    # fallback to the two states that actually mean "a replacement is
    # pending" leaves its original legacy (pre-0013) coverage unchanged and
    # only removes this false positive.
    _replacement_pending_states = {
        AdjustmentLifecycle.AWAITING_NEXT_CANDLE.value,
        AdjustmentLifecycle.REPLACEMENT_PENDING.value,
    }
    if (
        basket.pending_replacement_role is not None
        and basket.pending_replacement_state in _replacement_pending_states
    ):
        role = basket.pending_replacement_role
        stale_open = [
            leg
            for leg in basket.legs.values()
            if leg.role is role and leg.state is LegState.OPEN and not leg.is_replacement
        ]
        if stale_open:
            mismatches.append(
                f"basket {basket.basket_id!r} has a replacement pending for {role.value} "
                f"while leg(s) {[leg.leg_id for leg in stale_open]} of that role are still OPEN"
            )

    mismatches.extend(_reconcile_basket_rolls(repository, basket))

    if not mismatches and basket.lifecycle_state == "CLOSED" and open_positions:
        # A leg reverted back to OPEN above (a square-off close that never
        # actually took) makes this label stale rather than wrong — correct
        # it so the engine's own bookkeeping matches what it is about to
        # manage, instead of leaving "CLOSED" attached to a basket with a
        # currently-open leg.
        basket.lifecycle_state = "OPEN"
        basket.square_off_state = "PENDING"

    return mismatches


def _reconcile_basket_rolls(repository: ExecutionRepository, basket: Basket) -> list[str]:
    """Phase 2 (strategy-rolling-strangle-otm1): reconcile every
    non-terminal ``strategy_basket_rolls`` claim against its **own**
    reserved ``close_intent_id`` — never by scanning a leg's full order
    history (:func:`_reconcile_leg`'s own multi-attempt handling is a
    separate, independent concern: a later, unrelated square-off attempt
    on the same leg must not leak into a roll claim's own outcome; see
    migration ``0013``'s header). Mutates ``basket.roll_state`` in place
    with the resolved outcomes, mirroring ``_reconcile_basket``'s own
    "correct in place from authoritative data" contract for legs.

    A ``CLAIMED`` target (``close_intent_id`` still ``NULL``) is left
    exactly as it is — nothing was ever dispatched under any identity, so
    it is unconditionally safe to resume with a fresh submission; no
    authoritative lookup is needed or possible.

    An ``EXIT_SUBMISSION_PENDING`` target's ``close_intent_id`` is, by
    construction, always populated (the atomic reserve-and-associate
    write) — resolved via the same :func:`~common.execution.repository.
    resolve_intent_outcome` classification restart recovery already uses
    for legs, so a live exception and a post-crash restart resolve
    identically:

    * ``FILLED`` -> ``EXIT_CONFIRMED``;
    * ``TERMINAL_NO_FILL`` -> ``FAILED`` (the leg's own OPEN reconstruction
      is :func:`_reconcile_leg`'s job, via that leg's own order history);
    * anything else (``UNKNOWN``, or the structurally-unreachable
      ``NEVER_PLACED``) -> ``EXIT_UNKNOWN`` — never retried merely because
      a position is still open.

    Once every member of one ``claim_group_id`` reaches ``EXIT_CONFIRMED``,
    the whole group advances to ``AWAITING_NEXT_CANDLE`` together — mirrors
    ``MultiLegEngine._maybe_advance_claim_group`` for the case where that
    confirmation only became knowable through reconciliation (e.g. the
    second target's own best-effort outcome write was lost).

    Returns every discrepancy that could not be safely resolved — for now,
    only a structurally-impossible ``EXIT_SUBMISSION_PENDING`` row with no
    ``close_intent_id`` (a code defect, not a real-world race) — mirroring
    ``_reconcile_basket``'s own fail-closed contract.
    """
    if basket.roll_state is None or not basket.roll_state.claims:
        return []

    mismatches: list[str] = []
    resolved: dict[tuple[str, int], str] = {}
    for claim in basket.roll_state.claims:
        if claim.lifecycle_state not in _RECOGNIZED_ROLL_LIFECYCLE_STATES:
            # An unrecognised durable lifecycle_state must never be
            # silently treated as terminal, eligible, replaceable or safe
            # to continue — RollClaim.lifecycle_state is deliberately kept
            # as a permissive str on load (see that class's own docstring)
            # precisely so a future vocabulary extension is representable
            # rather than raising there; the fail-closed obligation lives
            # here instead, at the point a decision would otherwise be made
            # from it. Fails the whole basket closed via the normal
            # mismatches path — UnmanageableBasketState, entries blocked, a
            # critical incident recorded, exposure preserved untouched.
            mismatches.append(
                f"basket {basket.basket_id!r} roll claim ({claim.leg_role.value} "
                f"#{claim.roll_sequence}) has an unrecognised lifecycle_state "
                f"{claim.lifecycle_state!r} — refusing to reconcile or manage it "
                "automatically"
            )
            continue
        if claim.lifecycle_state != "EXIT_SUBMISSION_PENDING":
            continue
        if claim.close_intent_id is None:
            mismatches.append(
                f"basket {basket.basket_id!r} roll claim ({claim.leg_role.value} "
                f"#{claim.roll_sequence}) is EXIT_SUBMISSION_PENDING with no "
                "close_intent_id on record — structurally impossible under migration "
                "0013's atomic reserve-and-associate write"
            )
            continue
        row = repository.order_intent_by_id(claim.close_intent_id)
        outcome = resolve_intent_outcome(row)
        if outcome == "FILLED":
            new_state = "EXIT_CONFIRMED"
        elif outcome == "TERMINAL_NO_FILL":
            new_state = "FAILED"
        else:  # UNKNOWN, or the structurally-unreachable NEVER_PLACED
            new_state = "EXIT_UNKNOWN"
        repository.update_basket_roll_outcome(
            basket_id=basket.basket_id,
            leg_role=claim.leg_role.value,
            roll_sequence=claim.roll_sequence,
            lifecycle_state=new_state,
        )
        resolved[(claim.leg_role.value, claim.roll_sequence)] = new_state

    if resolved:
        basket.roll_state = BasketRollState(
            reference_price=basket.roll_state.reference_price,
            anchor_candle_ts=basket.roll_state.anchor_candle_ts,
            claims=tuple(
                dataclass_replace(claim, lifecycle_state=resolved[key])
                if (key := (claim.leg_role.value, claim.roll_sequence)) in resolved
                else claim
                for claim in basket.roll_state.claims
            ),
        )

    confirmed = "EXIT_CONFIRMED"
    advancing: list[str] = []
    for group_id in {claim.claim_group_id for claim in basket.roll_state.claims}:
        members = basket.roll_state.claims_for_group(group_id)
        if members and all(member.lifecycle_state == confirmed for member in members):
            advancing.append(group_id)
    if advancing:
        for group_id in advancing:
            for member in basket.roll_state.claims_for_group(group_id):
                repository.update_basket_roll_outcome(
                    basket_id=basket.basket_id,
                    leg_role=member.leg_role.value,
                    roll_sequence=member.roll_sequence,
                    lifecycle_state="AWAITING_NEXT_CANDLE",
                )
        advancing_set = set(advancing)
        basket.roll_state = BasketRollState(
            reference_price=basket.roll_state.reference_price,
            anchor_candle_ts=basket.roll_state.anchor_candle_ts,
            claims=tuple(
                dataclass_replace(claim, lifecycle_state="AWAITING_NEXT_CANDLE")
                if claim.claim_group_id in advancing_set
                else claim
                for claim in basket.roll_state.claims
            ),
        )

    return mismatches


def _classify_exit_attempts(exit_rows: list[Any]) -> tuple[str, Any]:
    """Phase 2 (strategy-rolling-strangle-otm1): classify potentially
    *multiple* exit-side ``order_intents`` rows for one leg into a single
    authoritative outcome. A leg may legitimately carry more than one exit
    attempt — a definitively rejected adjustment/roll close followed,
    later, by an unrelated hard square-off closing the same leg — which
    the previous unconditional ``len(exit_rows) > 1`` refusal could not
    tolerate. Never treats this as corruption by count alone; still fails
    closed on genuine ambiguity or a genuine over-close.

    Returns ``(outcome, definitive_row)``:

    * exactly one row ``FILLED`` -> ``("FILLED", that row)`` — authoritative
      regardless of how many ``TERMINAL_NO_FILL`` rows exist alongside it;
    * two or more rows ``FILLED`` -> ``("OVER_CLOSE", None)`` — a genuine
      contradiction (real exposure closed twice), never resolved
      automatically;
    * no row ``FILLED`` but at least one ``UNKNOWN`` ->
      ``("UNKNOWN", None)`` — the leg's true status cannot be established
      with confidence from any attempt;
    * no rows at all, or every row ``TERMINAL_NO_FILL`` ->
      ``("TERMINAL_NO_FILL", None)`` — safe: nothing has ever closed this
      leg through any attempt on record.
    """
    if not exit_rows:
        return "TERMINAL_NO_FILL", None
    classified = [(row, _resolve_intent_outcome(row)) for row in exit_rows]
    filled = [row for row, outcome in classified if outcome == "FILLED"]
    if len(filled) > 1:
        return "OVER_CLOSE", None
    if len(filled) == 1:
        return "FILLED", filled[0]
    if any(outcome == "UNKNOWN" for _row, outcome in classified):
        return "UNKNOWN", None
    return "TERMINAL_NO_FILL", None


def _reconcile_leg(
    repository: ExecutionRepository,
    leg: LegInstance,
    open_positions: dict[str, Any],
) -> str | None:
    """Reconcile one leg. Returns ``None`` if resolved (``leg`` mutated in
    place where the authoritative tables disagreed with the projection), or
    a description of an unresolved discrepancy."""
    history = repository.leg_order_history(leg_id=leg.leg_id)
    entry_side = leg.side.value
    entry_rows = [r for r in history if r["side"] == entry_side]
    exit_rows = [r for r in history if r["side"] != entry_side]
    if len(entry_rows) > 1:
        return f"leg {leg.leg_id!r} has {len(entry_rows)} entry order_intents rows (expected <= 1)"
    entry_row = entry_rows[0] if entry_rows else None
    entry_outcome = _resolve_intent_outcome(entry_row)
    exit_outcome, definitive_exit_row = _classify_exit_attempts(exit_rows)
    if exit_outcome == "OVER_CLOSE":
        return (
            f"leg {leg.leg_id!r} has more than one CONFIRMED closing fill across its "
            f"{len(exit_rows)} exit attempt(s) — an over-close contradiction that cannot "
            "be resolved automatically"
        )
    position = open_positions.get(leg.contract.security_id) if leg.contract is not None else None

    if leg.state is LegState.OPEN:
        if entry_outcome != "FILLED":
            return (
                f"leg {leg.leg_id!r} is OPEN in the projection but its entry order "
                f"resolves to {entry_outcome}, not a confirmed fill"
            )
        if position is None:
            return f"leg {leg.leg_id!r} is OPEN but no matching OPEN position exists"
        # Side: the persisted Position.quantity's sign is the authoritative
        # record of which direction is actually held (negative = short/SELL,
        # positive = long/BUY — see common.execution.repository.apply_fill).
        # It must agree with the leg's own recorded side.
        implied_side = OrderSide.SELL if position.quantity < 0 else OrderSide.BUY
        if implied_side is not leg.side:
            return (
                f"leg {leg.leg_id!r} recorded side {leg.side.value} does not match position "
                f"{leg.contract.security_id if leg.contract else '?'}'s implied side "
                f"{implied_side.value} (quantity={position.quantity})"
            )
        # LegInstance.quantity (mirrors OpenPosition.quantity) is always a
        # positive magnitude; the persisted Position.quantity is signed
        # (negative for a short/SELL leg) — compare magnitudes.
        if abs(position.quantity) != leg.quantity:
            return (
                f"leg {leg.leg_id!r} quantity {leg.quantity} does not match position "
                f"{leg.contract.security_id if leg.contract else '?'} quantity {position.quantity}"
            )
        return None

    if leg.state is LegState.PENDING_CONTRACT or leg.state is LegState.PENDING_SUBSCRIPTION:
        # No entry order is even possible yet at these states — leg.contract
        # may be None, so there is nothing further to cross-check.
        if entry_outcome == "FILLED":
            return (
                f"leg {leg.leg_id!r} is {leg.state.value} (no contract resolved yet) but an "
                "entry order for it is FILLED — this cannot happen without a code defect"
            )
        return None

    if leg.state is LegState.PENDING_ORDER:
        if entry_outcome == "FILLED":
            _upgrade_pending_leg_to_open(leg, entry_row, position)
            return None
        if entry_outcome == "UNKNOWN":
            return (
                f"leg {leg.leg_id!r} is PENDING_ORDER and its entry order's outcome cannot be "
                "established (reserved but never confirmed submitted/rejected)"
            )
        # NEVER_PLACED or TERMINAL_NO_FILL: nothing happened at the broker
        # for this leg yet — safe to leave PENDING_ORDER; the engine places
        # a fresh, legitimate order on the next tick.
        return None

    if leg.state is LegState.CLOSE_SUBMISSION_UNKNOWN:
        if exit_outcome == "FILLED":
            _resolve_unknown_close(leg, definitive_exit_row)
            _backfill_realized_gross_pnl(repository, leg)
            return None
        if exit_outcome == "TERMINAL_NO_FILL":
            # Phase 2 correction: only a *definitive* proof that every exit
            # attempt on record failed (rejected/cancelled/expired, or
            # risk-blocked) may revert this leg to OPEN. A merely
            # ambiguous/pending attempt does NOT prove the close was never
            # submitted — see the UNKNOWN branch below — so this branch, not
            # "position is still open" alone, is what makes a revert safe.
            if position is not None:
                _revert_unresolved_close_to_open(leg, position)
                return None
            return (
                f"leg {leg.leg_id!r}'s close attempt(s) all definitively failed but no "
                "open position exists to adopt back — cannot establish whether real "
                "exposure remains"
            )
        # UNKNOWN: at least one exit attempt is still genuinely pending or
        # ambiguous. Never reverted to OPEN-and-eligible merely because a
        # position row still shows open — that close may yet resolve
        # independently, and reverting risks a second, duplicate close
        # attempt racing it. Fail closed instead; only an operator or a
        # later authoritative resolution may clear this.
        return (
            f"leg {leg.leg_id!r}'s close outcome is unresolved ({exit_outcome}) — at least "
            "one exit attempt is still pending or ambiguous; cannot safely conclude "
            "whether real exposure remains without guessing"
        )

    # Terminal states (CLOSED/FAILED/EXPIRED): the one contradiction that
    # matters is real exposure the projection believes is gone.
    if leg.state is LegState.CLOSED:
        if position is not None:
            return (
                f"leg {leg.leg_id!r} is CLOSED in the projection but position "
                f"{leg.contract.security_id if leg.contract else '?'} is still OPEN"
            )
        _backfill_realized_gross_pnl(repository, leg)
    return None


def _resolve_intent_outcome(row: Any) -> str:
    """Classify one ``leg_order_history`` row (or ``None``) into the outcome
    :func:`_reconcile_leg` needs to decide what, if anything, to do.

    Phase 2 (strategy-rolling-strangle-otm1): this is now a thin alias for
    :func:`~common.execution.repository.resolve_intent_outcome` — the single
    shared classifier restart reconciliation and the engine's same-process
    close-outcome resolution both use, so the two can never drift apart.
    Kept as a module-level name here (rather than updating every call site
    below to the qualified import) to keep this diff minimal; behaviour is
    byte-identical to the classifier this used to define locally.
    """
    return resolve_intent_outcome(row)


def _upgrade_pending_leg_to_open(leg: LegInstance, entry_row: Any, position: Any) -> None:
    """P0-1's own exact scenario, resolved: the entry order genuinely
    filled but the best-effort projection write that would have advanced
    the leg to OPEN never landed. Uses the authoritative ``positions`` row
    when one exists (it carries the real entry price/quantity/correlation
    ID), falling back to the order row's own fill figures otherwise."""
    if position is not None:
        leg.entry_price = position.average_price
        leg.quantity = abs(position.quantity)
        leg.entry_correlation_id = position.entry_correlation_id
        leg.last_price = position.average_price
    else:
        leg.entry_price = entry_row["order_average_fill_price"]
        leg.quantity = entry_row["order_filled_quantity"] or leg.quantity
        leg.entry_correlation_id = entry_row["correlation_id"]
        leg.last_price = leg.entry_price
    leg.state = LegState.OPEN


def _resolve_unknown_close(leg: LegInstance, exit_row: Any) -> None:
    """A CLOSE_SUBMISSION_UNKNOWN leg whose exit order is, in fact, FILLED —
    the close happened, only the projection's confirmation write failed."""
    leg.state = LegState.CLOSED
    leg.exit_price = exit_row["order_average_fill_price"]
    leg.exit_correlation_id = exit_row["correlation_id"]


def _backfill_realized_gross_pnl(repository: ExecutionRepository, leg: LegInstance) -> None:
    """Phase 2 (strategy-rolling-strangle-otm1): reconstruct
    ``strategy_legs.realized_gross_pnl`` from the authoritative
    ``trade_ledger`` when the best-effort post-close projection write was
    lost. Required because ``rolling_strangle_otm1``'s combined stop sums
    realised P&L across rolled-out legs (spec section 10.1) — a silently
    missing value would under-count it after a restart and could fail to
    trigger. Only fills a genuinely missing value; never overwrites an
    existing one (the projection's own figure, when present, is already
    the exact number the strategy's own risk formula used at the time)."""
    if leg.realized_gross_pnl is not None or leg.exit_correlation_id is None:
        return
    value = repository.trade_ledger_gross_pnl_for_exit(leg.exit_correlation_id)
    if value is not None:
        leg.realized_gross_pnl = value


def _revert_unresolved_close_to_open(leg: LegInstance, position: Any) -> None:
    """A CLOSE_SUBMISSION_UNKNOWN leg whose close did not, in fact, resolve
    to a fill — real exposure remains. Adopted back as OPEN from the
    authoritative position row so normal management (including square-off)
    resumes rather than leaving it invisible."""
    leg.state = LegState.OPEN
    leg.entry_price = position.average_price
    leg.quantity = abs(position.quantity)
    leg.entry_correlation_id = position.entry_correlation_id
    leg.last_price = position.average_price
    leg.exit_price = None
    leg.exit_time = None
    leg.exit_reason = None
    leg.exit_correlation_id = None


def _record_recovery_failure(
    config: WorkerConfig, repository: ExecutionRepository, detail: str
) -> None:
    message = (
        f"cannot adopt the open basket for {config.strategy_id}: {detail}. New entries "
        "are blocked for the day; any open leg remains OPEN in the database and is NOT "
        "being managed by this process — close it manually."
    )
    log.error("%s", message)
    repository.record_error(
        runtime_id=config.runtime_id,
        strategy_id=config.strategy_id,
        execution_mode=config.execution_mode,
        severity="CRITICAL",
        component="multi_leg_engine.recovery",
        message=message,
    )


# ------------------------------------------------------------------- the run
def run_multi_leg_engine(
    config: WorkerConfig,
    engine_config: MultiLegEngineWorkerConfig,
    *,
    repository: ExecutionRepository,
    session_id: int,
    heartbeat: HeartbeatWriter,
    notifier: SafeNotifier,
    outcome: WorkerOutcome,
    candle_queue: Any,
    tick_queue: Any,
    control_queue: Any,
) -> WorkerOutcome:
    """Drive the multi-leg engine to completion. Called only from ``worker.py``."""
    if tick_queue is None:
        outcome.exit_code = 1
        outcome.error = (
            "this worker is configured for the multi-leg engine but was given no tick "
            "queue; the supervisor must register it with tick_channel=True"
        )
        log.error("refusing to start: %s", outcome.error)
        repository.record_error(
            runtime_id=config.runtime_id,
            strategy_id=config.strategy_id,
            execution_mode=config.execution_mode,
            severity="CRITICAL",
            component="multi_leg_engine",
            message=outcome.error,
        )
        heartbeat.beat(HealthState.FAILED, force=True)
        return outcome

    close_previous_session(config, repository, session_id)
    square_off_request_file = square_off_request_path(
        config.pid_dir.parent, config.runtime_id, config.strategy_id
    )

    try:
        engine, gateway, feed, positions = _build(
            config,
            engine_config,
            repository=repository,
            session_id=session_id,
            heartbeat=heartbeat,
            notifier=notifier,
            tick_queue=tick_queue,
            control_queue=control_queue,
            square_off_request_file=square_off_request_file,
        )
        notifier.send(
            NotificationEvent(
                event_type="worker_started",
                message=(
                    f"{config.strategy_id} started in {config.execution_mode.value} "
                    "mode on the multi-leg engine"
                ),
                runtime_id=config.runtime_id,
                strategy_id=config.strategy_id,
                execution_mode=config.execution_mode,
            )
        )
        requested = _drive(engine, config, candle_queue)
    except Exception as exc:
        outcome.exit_code = 1
        outcome.error = str(exc)
        repository.record_error(
            runtime_id=config.runtime_id,
            strategy_id=config.strategy_id,
            execution_mode=config.execution_mode,
            severity="CRITICAL",
            component="multi_leg_engine",
            message=outcome.error,
        )
        heartbeat.beat(HealthState.FAILED, force=True)
        log.exception("multi-leg engine worker failed strategy_id=%s", config.strategy_id)
        return outcome

    outcome.ticks_processed = feed.ticks_received
    outcome.ticks_dropped_upstream = feed.ticks_dropped_upstream
    outcome.trades_closed = len(positions.trades)
    outcome.orders_placed = gateway.executions
    outcome.stopped_by_request = requested or engine.stopped_by_request
    outcome.square_off_completed = _square_off_completed(config, repository)

    operator_request = read_square_off_request(square_off_request_file)
    if operator_request is not None and outcome.square_off_completed:
        repository.record_audit_event(
            runtime_id=config.runtime_id,
            action="square_off_completed",
            actor=operator_request.requested_by,
            strategy_id=config.strategy_id,
            execution_mode=config.execution_mode,
            detail=f"requested at {operator_request.requested_at}: {operator_request.reason}",
        )
        clear_square_off_request(square_off_request_file)

    still_open = (
        repository.open_positions(
            strategy_id=config.strategy_id,
            execution_mode=config.execution_mode,
            trading_date=config.trading_date,
        )
        if outcome.stopped_by_request
        else []
    )
    outcome.clean_engine_shutdown = not still_open

    if outcome.clean_engine_shutdown:
        notifier.send(
            NotificationEvent(
                event_type="worker_stopped",
                message=f"{config.strategy_id} stopped cleanly",
                runtime_id=config.runtime_id,
                strategy_id=config.strategy_id,
                execution_mode=config.execution_mode,
            )
        )
        repository.close_session(
            session_id, reason="signal" if outcome.stopped_by_request else "clean_shutdown"
        )
        heartbeat.beat(HealthState.STOPPED, force=True)
    else:
        _raise_silent_engine_alarm(config, repository, heartbeat, notifier, still_open)
        repository.close_session(session_id, reason="engine_did_not_stop")
    return outcome


def _build(
    config: WorkerConfig,
    engine_config: MultiLegEngineWorkerConfig,
    *,
    repository: ExecutionRepository,
    session_id: int,
    heartbeat: HeartbeatWriter,
    notifier: SafeNotifier,
    tick_queue: Any,
    control_queue: Any,
    square_off_request_file: Path,
) -> tuple[MultiLegEngine, LifecycleGateway, HubTickFeed, PositionManager]:
    if config.execution_mode is ExecutionMode.LIVE:
        raise RuntimeError(
            "live execution is not wired for the multi-leg engine — see this module's "
            "own docstring; every committed multi-leg strategy config ships mode: paper"
        )

    option_selector, option_segment = _build_option_selector(config, engine_config)
    from common.engine.feed import SubscriptionMode

    option_mode = None if option_segment is None else int(SubscriptionMode.FULL)

    quotes = QuoteBook()
    instrument_rules = getattr(option_selector.resolver, "instrument_rules", None)

    broker = build_broker(
        resolved_config_from_worker(config),
        preflight_passed=config.live_preflight_passed,
        paper_execution=config.paper_execution,
        cost_rates=config.cost_rates,
        quotes=quotes,
        instrument_rules=instrument_rules,
    )
    lifecycle = OrderLifecycle(
        repository=repository,
        broker=broker,
        runtime_id=config.runtime_id,
        strategy_id=config.strategy_id,
        execution_mode=config.execution_mode,
        session_id=session_id,
        config_fingerprint=config.config_fingerprint,
        quotes=quotes,
    )
    gateway = LifecycleGateway(
        lifecycle,
        strategy_id=config.strategy_id,
        execution_mode=config.execution_mode,
        trading_date=config.trading_date,
        repository=repository,
        runtime_id=config.runtime_id,
    )
    positions = PositionManager(gateway, lots=engine_config.lots)

    cfg = EngineConfig(
        timeframe=engine_config.timeframe,
        session=SessionConfig.from_square_off_policy(
            config.square_off_policy,
            start_time=engine_config.session_start_time,
            holidays=tuple(engine_config.holidays),
        ),
        execution_mode=config.execution_mode,
        max_daily_loss_percent=engine_config.max_daily_loss_percent,
        starting_capital=engine_config.starting_capital,
        parameters=dict(engine_config.parameters),
    )

    holder: list[MultiLegEngine] = []

    def _on_square_off(reason: str) -> None:
        holder[0].request_square_off(reason)

    def _on_tick_dropped(_notice: Any) -> None:
        holder[0].block_entries(
            "the hub dropped tick(s) for this worker; candles built here may differ "
            "from the hub's, so trading on them is not safe"
        )

    resolved_expiry = getattr(option_selector.resolver, "expiry", None) or engine_config.expiry
    square_off_authority = PersistedSquareOffAuthority(
        config.square_off_policy,
        repository,
        runtime_id=config.runtime_id,
        strategy_id=config.strategy_id,
        execution_mode=config.execution_mode,
        trading_date=config.trading_date,
        expiry=resolved_expiry,
    )

    vix_id = engine_config.vix_security_id or None
    vix_segment_code: int | None = None
    if vix_id:
        from common.market_data.scrip_master import segment_code

        vix_segment_code = segment_code(engine_config.vix_segment)

    feed = HubTickFeed(
        tick_queue,
        request_subscription=_subscription_sender(
            config,
            control_queue,
            option_segment=option_segment,
            option_mode=option_mode,
            vix_id=vix_id,
            vix_segment=vix_segment_code,
            vix_mode=engine_config.vix_mode,
        ),
        on_square_off=_on_square_off,
        on_tick_dropped=_on_tick_dropped,
        should_stop=lambda: bool(holder) and holder[0].square_off_requested,
        on_poll=_combined_poll(
            _wall_clock_square_off(holder, square_off_authority, trading_date=config.trading_date),
            _operator_requested_square_off(holder, square_off_request_file),
        ),
        poll_seconds=engine_config.feed_poll_seconds,
        idle_timeout_seconds=config.idle_timeout_seconds,
    )
    feed.add_tick_observer(quotes.record)

    def _recover() -> Basket | None:
        return recover_basket(config, repository)

    def _persist_basket_cb(basket: Basket) -> None:
        _persist_basket_row(repository, basket, runtime_id=config.runtime_id)

    def _persist_leg_cb(leg: LegInstance) -> None:
        _persist_leg_row(
            repository,
            leg,
            runtime_id=config.runtime_id,
            strategy_id=config.strategy_id,
            execution_mode=config.execution_mode,
            trading_date=config.trading_date,
        )

    def _record_incident_cb(basket_id: str, message: str) -> None:
        # Independent write path (P0-1): a separate repository call from the
        # basket/leg persistence the incident is often reporting a failure
        # of, so an incident about "persistence is failing" is not itself
        # silently lost to the same failure. record_error has its own
        # transaction; nothing here catches a failure of *this* call — that
        # is deliberately left to MultiLegEngine._record_incident's own
        # try/except, which already logs at CRITICAL regardless.
        repository.record_error(
            runtime_id=config.runtime_id,
            strategy_id=config.strategy_id,
            execution_mode=config.execution_mode,
            severity="CRITICAL",
            component="multi_leg_engine.incident",
            message=f"basket={basket_id}: {message}",
        )

    # Phase 2 (strategy-rolling-strangle-otm1). Wired generically for any
    # multi-leg strategy — including straddle_920, whose own EXIT_LEG +
    # ExitReason.ADJUSTMENT is normalised into the same claim machinery
    # (MultiLegEngine._close_adjusted_legs), gaining the same durable
    # close-attempt identity tracking with no change to its externally
    # observable behaviour. See RollLedgerPort's own docstring.
    roll_ledger = RollLedger(
        repository,
        runtime_id=config.runtime_id,
        strategy_id=config.strategy_id,
        execution_mode=config.execution_mode,
        trading_date=config.trading_date,
    )

    engine = MultiLegEngine(
        cfg,
        feed=feed,
        option_selector=option_selector,
        strategy=load_multi_leg_strategy(engine_config.strategy_ref, engine_config.strategy_kwargs),
        position_manager=positions,
        underlying_security_id=config.security_id,
        underlying_instrument=engine_config.underlying_instrument or config.instrument,
        vix_security_id=vix_id,
        runtime_id=config.runtime_id,
        notifier=notifier,
        reporter=HeartbeatEngineReporter(
            heartbeat,
            execution_mode=config.execution_mode,
            entries_blocked=lambda: holder[0].entries_blocked if holder else None,
        ),
        report=RepositoryReportWriter(
            repository,
            runtime_id=config.runtime_id,
            strategy_id=config.strategy_id,
            execution_mode=config.execution_mode,
            trading_date=config.trading_date,
        ),
        square_off_authority=square_off_authority,
        recover_basket=_recover,
        persist_basket=_persist_basket_cb,
        persist_leg=_persist_leg_cb,
        record_incident=_record_incident_cb,
        roll_ledger=roll_ledger,
        trading_date=config.trading_date,
    )
    holder.append(engine)
    return engine, gateway, feed, positions


def _build_option_selector(
    config: WorkerConfig, engine_config: MultiLegEngineWorkerConfig
) -> tuple[OptionSelector, int | None]:
    """Mirrors ``engine_worker.build_option_selector`` — kept as its own small
    copy rather than shared, since the two configs (``EngineWorkerConfig`` /
    ``MultiLegEngineWorkerConfig``) are structurally similar but distinct
    picklable types, and this function is short enough that sharing it would
    cost more in indirection than it saves in lines."""
    if engine_config.contract_resolver == "simulated":
        return (
            OptionSelector(
                SimulatedOptionChainResolver(config.instrument, lot_size=engine_config.lot_size),
                strike_step=engine_config.strike_step,
                expiry=engine_config.expiry,
            ),
            None,
        )

    if engine_config.contract_resolver != "dhan":
        raise ValueError(
            f"Unknown contract_resolver {engine_config.contract_resolver!r}; "
            "expected 'simulated' or 'dhan'."
        )

    from common.config.paths import load_paths
    from common.market_data.scrip_master import (
        ScripMaster,
        ScripMasterCache,
        resolve_index_meta,
        segment_code,
    )

    meta = resolve_index_meta(
        config.instrument,
        index_security_id=engine_config.index_security_id or None,
        index_segment=engine_config.index_segment or None,
        fno_segment=engine_config.fno_segment or None,
    )
    cache_dir = (
        Path(engine_config.scrip_master_cache_dir)
        if engine_config.scrip_master_cache_dir
        else load_paths().cache_root
    )
    master = ScripMaster(config.instrument, exchange=meta.exchange).load(
        cache=ScripMasterCache(cache_dir)
    )
    resolver = DhanOptionChainResolver(master, expiry=engine_config.expiry)
    log.info(
        "resolving real %s contracts: expiry=%s lot_size=%s segment=%s",
        config.instrument,
        resolver.expiry,
        resolver.lot_size,
        meta.fno_segment,
    )
    return (
        OptionSelector(resolver, strike_step=engine_config.strike_step, expiry=resolver.expiry),
        segment_code(meta.fno_segment),
    )


def _wall_clock_square_off(
    holder: list[MultiLegEngine],
    authority: SquareOffAuthority,
    *,
    trading_date: str,
    clock: Callable[[], datetime] = now_ist,
) -> Callable[[], None]:
    """The multi-leg counterpart of ``engine_worker._wall_clock_square_off`` —
    same reasoning, same "one owner" property (asks the same authority the
    engine asks), typed against :class:`MultiLegEngine` instead."""

    def _check() -> None:
        if not holder:
            return
        engine = holder[0]
        if engine.square_off_requested:
            return
        now = clock()
        if now.date().isoformat() != trading_date:
            return
        if not authority.due(now):
            return
        log.warning(
            "wall-clock square-off: the square-off time has passed with no tick to "
            "carry it, so the close is being requested on the poll timer instead"
        )
        engine.request_square_off("wall clock reached the square-off time")

    return _check


def _operator_requested_square_off(
    holder: list[MultiLegEngine], request_path: Path
) -> Callable[[], None]:
    """Multi-leg counterpart of ``engine_worker._operator_requested_square_off``."""

    def _check() -> None:
        if not holder:
            return
        engine = holder[0]
        if engine.square_off_requested:
            return
        request = read_square_off_request(request_path)
        if request is None:
            return
        log.warning("operator-requested square-off: %s", request.reason)
        engine.request_square_off(f"operator requested: {request.reason}")

    return _check


def _subscription_sender(
    config: WorkerConfig,
    control_queue: Any,
    *,
    option_segment: int | None,
    option_mode: int | None,
    vix_id: str | None,
    vix_segment: int | None,
    vix_mode: int | None,
) -> Callable[[str], None] | None:
    """Forward the engine's runtime subscriptions upstream, never blocking.

    Three instrument classes, each with their own segment/mode (spec section
    6): the underlying keeps the hub's own defaults (set once, at channel
    registration); India VIX carries its own explicit ``IDX_I`` segment/mode
    (never the option segment); everything else — CE/PE/replacement — is an
    option contract and gets ``option_segment``/``option_mode``. Mirrors
    ``engine_worker._subscription_sender``'s reasoning exactly, generalised
    to the extra VIX case a single-leg strategy never has.
    """
    if control_queue is None:
        return None

    def _send(security_id: str) -> None:
        if security_id == config.security_id:
            segment, mode = None, None
        elif vix_id is not None and security_id == vix_id:
            segment, mode = vix_segment, vix_mode
        else:
            segment, mode = option_segment, option_mode
        try:
            control_queue.put_nowait((security_id, segment, mode))
        except Exception:
            log.exception(
                "could not forward a subscription request for %s from %s; ticks for it "
                "will not arrive",
                security_id,
                config.strategy_id,
            )

    return _send


def _drive(engine: MultiLegEngine, config: WorkerConfig, candle_queue: Any) -> bool:
    """Run the engine on this thread, with the candle drain beside it.
    Mirrors ``engine_worker._drive`` exactly, typed against
    :class:`MultiLegEngine`."""
    requested = threading.Event()

    def _request(reason: str) -> None:
        requested.set()
        engine.request_square_off(reason)

    stop_draining = threading.Event()
    drain_thread = threading.Thread(
        target=_drain_candle_queue,
        args=(candle_queue, _request, stop_draining),
        name=f"{config.strategy_id}:candles",
        daemon=True,
    )

    with shutdown_signals(lambda: _request("shutdown signal")):
        drain_thread.start()
        try:
            engine.run()
        finally:
            stop_draining.set()
            drain_thread.join(timeout=_DRAIN_JOIN_SECONDS)
    return requested.is_set()


def _raise_silent_engine_alarm(
    config: WorkerConfig,
    repository: ExecutionRepository,
    heartbeat: HeartbeatWriter,
    notifier: SafeNotifier,
    still_open: list[Any],
) -> None:
    """Mirrors ``engine_worker._raise_silent_engine_alarm`` exactly."""
    instruments = ", ".join(sorted(position.security_id for position in still_open))
    message = (
        "the multi-leg engine was asked to square off and the run has ended, but "
        f"{len(still_open)} position(s) are still OPEN in the database: {instruments}. "
        "No process is managing them now. Close them manually and check the positions "
        "table before the next session."
    )
    log.error("%s", message)
    repository.record_error(
        runtime_id=config.runtime_id,
        strategy_id=config.strategy_id,
        execution_mode=config.execution_mode,
        severity="CRITICAL",
        component="multi_leg_engine",
        message=message,
    )
    notifier.send(
        NotificationEvent(
            event_type="engine_square_off_incomplete",
            message=message,
            runtime_id=config.runtime_id,
            strategy_id=config.strategy_id,
            execution_mode=config.execution_mode,
        )
    )
    heartbeat.beat(HealthState.DEGRADED, force=True)
