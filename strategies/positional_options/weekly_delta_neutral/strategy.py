"""``weekly_delta_neutral`` — the one weekly NIFTY defined-risk delta-neutral
iron condor this repository is approved to implement (CLAUDE.md).

Full rule set: ``WEEKLY_DELTA_NEUTRAL_ALGO_TRADING_SPEC.md`` in this
package. Every rule below cites the spec section it implements. This module
contains no engine/persistence access of its own — it only reads the
``Cycle``/``PositionalContext`` it is handed and returns a
:class:`~common.engine.positional.positional_models.CycleSignal`, exactly
the contract :class:`~common.engine.positional.positional_strategy.
BasePositionalMultiLegStrategy` requires.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from common.engine.positional.lifecycle import expiry_settlement_at
from common.engine.positional.positional_models import (
    Cycle,
    CycleAction,
    CycleLegIntent,
    CycleSignal,
    CycleState,
    LegRole,
    hedge_for,
)
from common.engine.positional.positional_strategy import (
    BasePositionalMultiLegStrategy,
    PositionalContext,
    register_positional_strategy,
)
from common.engine.selection import DhanOptionChainResolver
from common.margin import (
    OVERNIGHT_PRODUCT_TYPE,
    LegMarginRequest,
    MarginEstimate,
    MarginUnavailable,
)
from common.margin.estimator import DEFAULT_MAX_MARGIN_AGE_SECONDS
from common.market_data.scrip_master import ScripMaster
from common.models import ExitReason, OrderSide
from common.utils.timeutils import local_date_in, local_time_in

from .config import WeeklyDeltaNeutralParameters, _parse_hhmm
from .models import IronCondorCandidate
from .risk import (
    compute_pnl,
    is_capital_stop,
    is_emergency_stop,
    is_hard_stop,
    is_margin_breach,
    is_profit_target,
    is_soft_stop,
)
from .selection import best_hedge, rank_role_candidates, select_iron_condor

_WEDNESDAY = 2

_ROLE_SIGN = {
    LegRole.HEDGE_PUT: OrderSide.BUY,
    LegRole.HEDGE_CALL: OrderSide.BUY,
    LegRole.SHORT_PUT: OrderSide.SELL,
    LegRole.SHORT_CALL: OrderSide.SELL,
}


@dataclass
class _OpeningFilterState:
    reference_price: float | None = None
    reference_at: datetime | None = None


#: Keys ``runtimes.positional_options.config_adapter.build_worker_config``
#: reads out of the *same* ``parameters:`` YAML block for the worker's own
#: use (order-fill simulation, strategy resolution, evaluation cadence) —
#: never strategy-decision inputs. ``WeeklyDeltaNeutralParameters`` uses
#: ``extra="forbid"`` deliberately, to catch a genuine parameter typo; these
#: are excluded before construction so the one YAML file both reader owns
#: can coexist, rather than requiring two separate parameter blocks for what
#: is operationally one strategy configuration.
_WORKER_ONLY_PARAMETER_KEYS = frozenset(
    {"strategy_ref", "paper_execution", "cost_rates", "evaluation_interval_seconds"}
)


@register_positional_strategy("weekly_delta_neutral")
class WeeklyDeltaNeutralStrategy(BasePositionalMultiLegStrategy):
    def __init__(self, cfg: Any = None, **kwargs: Any) -> None:
        super().__init__(cfg)
        raw_params = kwargs.get("parameters", self.params)
        if isinstance(raw_params, WeeklyDeltaNeutralParameters):
            self._params = raw_params
        else:
            strategy_params = {
                k: v for k, v in raw_params.items() if k not in _WORKER_ONLY_PARAMETER_KEYS
            }
            self._params = WeeklyDeltaNeutralParameters(**strategy_params)
        self._scrip_master: ScripMaster = kwargs["scrip_master"]
        self._timezone: str = kwargs.get("timezone", "Asia/Kolkata")
        self._opening = _OpeningFilterState()
        self._trigger_confirmations = 0

    # -------------------------------------------------------------- BasePositionalMultiLegStrategy
    def reset_daily(self) -> None:
        self._opening = _OpeningFilterState()
        self._trigger_confirmations = 0

    @property
    def lots(self) -> int:
        return self._params.lots

    def status(self) -> str:
        return f"weekly_delta_neutral lots={self._params.lots}"

    def evaluate(self, *, cycle: Cycle | None, context: PositionalContext) -> CycleSignal | None:
        if cycle is None:
            return self._evaluate_entry(context)
        if cycle.state in (
            CycleState.CRITICAL_UNRESOLVED,
            CycleState.ENTERING,
            CycleState.EXITING,
            CycleState.COMPLETED,
            CycleState.FAILED,
            CycleState.ABANDONED,
        ):
            # ENTERING/EXITING: the engine is already driving these
            # synchronously (spec section 5.2/8's sequential/ordered
            # workflows). CRITICAL_UNRESOLVED and every terminal state:
            # only an operator/reconciliation may act.
            return None
        return self._evaluate_active(cycle, context)

    # ----------------------------------------------------------------- entry
    def _resolve_actual_expiry(self, entry_local_date: date) -> str:
        """Spec section 3.4: nearest eligible weekly expiry *strictly after*
        entry, from the exchange's own instrument master — never weekday
        arithmetic. ``nearest_expiry`` returns on-or-after; if that lands on
        the entry date itself (a same-day-expiry edge case), the next
        listed expiry is used instead."""
        candidate = self._scrip_master.nearest_expiry(on=entry_local_date)
        if candidate == entry_local_date.isoformat():
            from datetime import timedelta

            candidate = self._scrip_master.nearest_expiry(on=entry_local_date + timedelta(days=1))
        return candidate

    def _evaluate_entry(self, context: PositionalContext) -> CycleSignal | None:
        if not context.is_trading_day or context.is_holiday:
            return None  # spec 3.2: Wednesday holiday -> skip, no Thursday shift
        if context.spot is None or not context.spot_is_fresh:
            return None

        local_day = local_date_in(context.now, self._timezone, argument="now")
        if local_day.weekday() != _WEDNESDAY:
            return None

        schedule = self._params.schedule
        window_start = _parse_hhmm(schedule.entry_window_start)
        window_end = _parse_hhmm(schedule.entry_window_end)
        skip_after = _parse_hhmm(self._params.opening_filter.skip_after)
        local_clock = local_time_in(context.now, self._timezone, argument="now")

        if local_clock < window_start:
            return None
        if self._opening.reference_price is None:
            if local_clock >= window_end:
                # Spec 3.2: "if the runtime starts after 09:40 on the entry
                # Wednesday, do not enter that cycle" — a first evaluation
                # this late means the process was never running (or never
                # observed a spot tick) during the primary window at all.
                return None
            self._opening.reference_price = context.spot
            self._opening.reference_at = context.now
        if local_clock > skip_after:
            return None  # spec 3.3: unstable past 10:15 -> permanently skip today

        move_percent = (
            abs(context.spot - self._opening.reference_price)
            / self._opening.reference_price
            * 100.0
            if self._opening.reference_price
            else 0.0
        )
        if move_percent > self._params.opening_filter.maximum_move_percent:
            # Still inside the normal window or the extended (up to
            # skip_after) delay window: keep waiting, re-checked on the
            # engine's own evaluation cadence rather than a private timer —
            # spec section 3.3's "delay entry evaluation by 20 minutes" is
            # satisfied by this being re-evaluated every cycle without ever
            # relaxing the threshold, up to the skip_after cutoff above.
            return None

        resolved_expiry = self._resolve_actual_expiry(local_day)
        resolver = DhanOptionChainResolver(self._scrip_master, expiry=resolved_expiry)
        # Cheap pre-check only — the scrip master carries no lot size
        # metadata at all (e.g. never loaded). The authoritative check is
        # select_iron_condor's own: it verifies the four *actually selected*
        # contracts' own lot sizes agree and are non-zero, never trusting a
        # single scrip-master-wide value (spec 3.1: never hardcode/configure
        # lot size; resolve it from each selected contract's own metadata).
        if not resolver.lot_size:
            return None

        try:
            chain = context.greeks_service.chain_snapshot(
                underlying_security_id=int(context.underlying_security_id),
                underlying_segment=context.underlying_segment,
                expiry=resolved_expiry,
            )
        except Exception:
            return None

        expiry_at = self._expiry_close_at(resolved_expiry)
        candidate = select_iron_condor(
            chain=chain,
            resolver=resolver,
            greeks=context.greeks_service,
            spot=context.spot,
            expiry_at=expiry_at,
            now=context.now,
            lots=self._params.lots,
            config=self._params.selection,
        )
        if candidate is None:
            return None

        # Spec section 3.7: estimated margin utilization <= 50% of
        # allocated capital, checked before the first order. Missing,
        # stale, invalid or failed estimation blocks entry and records an
        # incident (independent-review correction: never a fabricated
        # value, never a silent offline-model substitution in production —
        # see common.margin's own module docstring).
        margin_estimate = self._entry_margin_estimate(candidate, context)
        if margin_estimate is None:
            return None
        if (
            margin_estimate.utilization_percent
            > self._params.exits.maximum_margin_utilization_percent
        ):
            return None  # over the cap: an ordinary blocked entry, not an incident

        legs = tuple(
            CycleLegIntent(role=leg.role, side=_ROLE_SIGN[leg.role], contract=leg.contract)
            for leg in candidate.legs()
        )
        return CycleSignal(
            action=CycleAction.ENTER_CYCLE,
            timestamp=context.now,
            legs=legs,
            reason=(
                f"entry candidate: credit={candidate.initial_net_credit:.2f} "
                f"width={candidate.wing_width:.0f} ratio={candidate.credit_to_width_ratio:.3f} "
                f"net_delta_per_lot={candidate.net_delta_per_lot:.2f} "
                f"margin_utilization={margin_estimate.utilization_percent:.1f}%"
            ),
            original_wing_width=candidate.wing_width,
            margin_estimate=margin_estimate,
        )

    def _entry_margin_estimate(
        self, candidate: IronCondorCandidate, context: PositionalContext
    ) -> MarginEstimate | None:
        quantity = self._params.lots * candidate.lot_size
        requests = []
        for leg in candidate.legs():
            side = _ROLE_SIGN[leg.role]
            assert leg.quote.bid is not None and leg.quote.ask is not None
            reference_price = leg.quote.ask if side is OrderSide.BUY else leg.quote.bid
            requests.append(
                LegMarginRequest(
                    security_id=leg.contract.security_id,
                    exchange_segment=context.option_segment,
                    side=side,
                    quantity=quantity,
                    product_type=OVERNIGHT_PRODUCT_TYPE,
                    reference_price=reference_price,
                )
            )
        try:
            estimate = context.margin_estimator.estimate_basket(
                requests,
                spot=context.spot or 0.0,
                allocated_capital=self._params.allocated_capital,
                now=context.now,
            )
        except MarginUnavailable as exc:
            context.record_pre_entry_incident(f"margin estimation blocked entry: {exc}")
            return None
        # Defence in depth beyond the estimator's own internal freshness
        # check (which self-stamps estimated_at at computation time, so it
        # alone can never observe drift): re-validate against the engine's
        # own tick-driven "now" in case a real HTTP round trip took a
        # while — spec section 3.7's "missing, stale ... blocks entry"
        # applies here too, not only to the estimator's own internal check.
        if not estimate.is_fresh(now=context.now, max_age_seconds=DEFAULT_MAX_MARGIN_AGE_SECONDS):
            context.record_pre_entry_incident(
                f"margin estimate is stale relative to the evaluation clock "
                f"(age={estimate.age_seconds(now=context.now):.1f}s)"
            )
            return None
        return estimate

    def _expiry_close_at(self, resolved_expiry: str) -> datetime:
        # NSE index-option expiry settlement is at session close (15:30
        # local) — used only as the Greeks model's time-to-expiry anchor,
        # never for any exit-timing decision (those are all evaluated
        # against the lifecycle policy's own 15:05/15:15 clock, not this
        # value). The one shared definition
        # (common.engine.positional.lifecycle.expiry_settlement_at) is also
        # what the generic engine itself now uses for the same purpose
        # (gap-closing session Phase 3) — never duplicated arithmetic that
        # could quietly drift apart from it.
        return expiry_settlement_at(resolved_expiry, timezone=self._timezone)

    # ---------------------------------------------------------------- active
    def _evaluate_active(self, cycle: Cycle, context: PositionalContext) -> CycleSignal | None:
        greeks_available = context.chain is not None

        # Real entry+adjustment+exit charges (spec section 6.1) — the
        # engine's own repository read (PositionalContext.total_charges),
        # never a hardcoded 0.0 (found and fixed in the gap-closing
        # session's Phase 3 — see docs/IMPLEMENTATION_STATUS_AND_RUNBOOK.md
        # section 11.10 for the real production defect this was).
        pnl = compute_pnl(cycle, charges=context.total_charges)
        exits = self._params.exits

        # Spec section 6.2, steps 3-10 (steps 1-2 are engine-owned: operator
        # square-off and unresolved-exposure reconciliation never reach the
        # strategy at all).
        if is_emergency_stop(cycle, pnl, exits):
            return self._exit_all(context, "emergency loss stop", ExitReason.STOP_LOSS)
        if is_hard_stop(cycle, pnl, exits):
            return self._exit_all(context, "hard loss stop", ExitReason.STOP_LOSS)
        if is_capital_stop(pnl, exits, allocated_capital=self._params.allocated_capital):
            return self._exit_all(context, "allocated-capital loss stop", ExitReason.STOP_LOSS)

        phase_exit = self._expiry_phase_exit(cycle, context)
        if phase_exit is not None:
            return phase_exit

        if is_soft_stop(cycle, pnl, exits):
            return self._exit_all(context, "soft loss stop", ExitReason.STOP_LOSS)
        if is_profit_target(cycle, pnl, exits):
            return self._exit_all(context, "profit target", ExitReason.TARGET_PROFIT)
        # Margin-limit breach (spec section 6.2 step 10): the *entry* gate
        # (spec 3.7) now computes a real basket margin estimate before the
        # first order — see _entry_margin_estimate. This ongoing,
        # per-evaluation check stays a deliberate, documented no-op:
        # polling the real Dhan margin-calculator endpoint on every
        # evaluation tick (every few seconds, for the life of an open
        # cycle) is real load this task's scope does not ask for and this
        # branch does not add; estimated_margin=None here is honest, never
        # a fabricated pass, exactly as before. Missing/failed margin
        # estimation must never block exits either way (spec section 4.2's
        # fail-open-for-exits rule applies equally here) — this call is
        # itself only ever an *additional* exit trigger, never a block.
        margin_breached = is_margin_breach(
            estimated_margin=None, allocated_capital=self._params.allocated_capital, config=exits
        )
        if margin_breached:
            return self._exit_all(context, "margin-limit breach", ExitReason.STOP_LOSS)

        if not greeks_available:
            # Spec section 4.2: missing/invalid Greeks block new/adjustment
            # risk but never an exit — every exit branch above already ran.
            return None

        hedge_repair = self._hedge_repair_needed(cycle, context)
        if hedge_repair is not None:
            return hedge_repair

        return self._maybe_adjust(cycle, context)

    def _expiry_phase_exit(self, cycle: Cycle, context: PositionalContext) -> CycleSignal | None:
        # The engine's own PositionalLifecyclePolicy already forces the hard
        # exit unconditionally (spec section 8's "not disabled by
        # market_fallback_enabled: false" net); here the strategy signals
        # the *planned* exit a little earlier, at 15:05, so the closing
        # sequence has the full 10-minute window before the engine's own
        # hard deadline.
        expiry_date = date.fromisoformat(cycle.resolved_expiry_date[:10])
        local_day = local_date_in(context.now, self._timezone, argument="now")
        if local_day != expiry_date:
            return None
        local_clock = local_time_in(context.now, self._timezone, argument="now")
        planned_exit = _parse_hhmm(self._params.schedule.planned_exit)
        if local_clock >= planned_exit:
            return self._exit_all(
                context, "actual-expiry-day planned exit (15:05)", ExitReason.EXPIRY
            )
        return None

    def _hedge_repair_needed(self, cycle: Cycle, context: PositionalContext) -> CycleSignal | None:
        """Spec section 7.2/12.3 step 2: a short with no confirmed
        protective hedge must be repaired before any risk-increasing
        replacement — checked independently of the adjustment trigger."""
        for short_role in (LegRole.SHORT_PUT, LegRole.SHORT_CALL):
            short_leg = cycle.current_leg(short_role)
            if short_leg is None or not short_leg.is_open:
                continue
            hedge_role = hedge_for(short_role)
            hedge_leg = cycle.current_leg(hedge_role)
            if hedge_leg is not None and hedge_leg.is_open:
                continue
            # The hedge is missing/not open while its short is — repair it.
            resolver = DhanOptionChainResolver(
                self._scrip_master, expiry=cycle.resolved_expiry_date
            )
            if context.chain is None:
                return None
            expiry_at = self._expiry_close_at(cycle.resolved_expiry_date)
            option_type = _hedge_option_type(hedge_role)
            target_delta = (
                self._params.selection.hedge_put_delta
                if hedge_role is LegRole.HEDGE_PUT
                else self._params.selection.hedge_call_delta
            )
            candidate = best_hedge(
                chain=context.chain,
                resolver=resolver,
                greeks=context.greeks_service,
                option_type=option_type,
                short_strike=short_leg.contract.strike if short_leg.contract else 0.0,
                config=self._params.selection,
                spot=context.spot or 0.0,
                expiry_at=expiry_at,
                now=context.now,
                role=hedge_role,
                target_delta=target_delta,
                tolerance=self._params.selection.hedge_delta_tolerance,
            )
            if candidate is None:
                return None
            hedge_intent = CycleLegIntent(
                role=hedge_role, side=OrderSide.BUY, contract=candidate.contract
            )
            return CycleSignal(
                action=CycleAction.REPAIR_HEDGE,
                timestamp=context.now,
                legs=(hedge_intent,),
                reason=f"hedge repair for {short_role.value}",
            )
        return None

    def _maybe_adjust(self, cycle: Cycle, context: PositionalContext) -> CycleSignal | None:
        adj = self._params.adjustment
        if cycle.adjustments_this_cycle >= adj.maximum_per_cycle:
            return None
        if cycle.adjustments_today >= adj.maximum_per_day:
            return None
        if cycle.last_adjustment_at is not None:
            elapsed_minutes = (context.now - cycle.last_adjustment_at).total_seconds() / 60.0
            if elapsed_minutes < adj.minimum_minutes_between:
                return None
        schedule = self._params.schedule
        local_clock = local_time_in(context.now, self._timezone, argument="now")
        adj_start = _parse_hhmm(schedule.adjustment_start)
        adj_end = _parse_hhmm(schedule.adjustment_end)
        if not (adj_start <= local_clock < adj_end):
            return None
        expiry_date = date.fromisoformat(cycle.resolved_expiry_date[:10])
        local_day = local_date_in(context.now, self._timezone, argument="now")
        is_expiry_day = local_day == expiry_date
        # Spec section 8: no normal adjustment from 14:30 on the actual
        # expiry day (resolved_expiry_date, never an assumed Tuesday/
        # weekday). Bounds everything below this point to [start, 14:30) —
        # the inward-roll prohibition (from 12:00) only ever has an effect
        # inside that same window; the unconditional 14:30+ cutoff is
        # unchanged.
        if is_expiry_day:
            cutoff = _parse_hhmm(schedule.expiry_adjustment_cutoff)
            if local_clock >= cutoff:
                return None

        net_delta = self._net_delta_per_lot(cycle, context)
        if net_delta is None:
            return None

        if abs(net_delta) >= adj.trigger_delta_per_lot:
            self._trigger_confirmations += 1
        else:
            self._trigger_confirmations = 0
        if self._trigger_confirmations < adj.confirmation_checks:
            return None

        # Spec section 7.2: negative -> untested side is the short put, roll
        # it upward; positive -> untested side is the short call, roll down.
        target_role = LegRole.SHORT_PUT if net_delta < 0 else LegRole.SHORT_CALL
        old_leg = cycle.current_leg(target_role)
        if old_leg is None or not old_leg.is_open or old_leg.contract is None:
            return None

        if context.chain is None:
            return None
        resolver = DhanOptionChainResolver(self._scrip_master, expiry=cycle.resolved_expiry_date)
        expiry_at = self._expiry_close_at(cycle.resolved_expiry_date)
        option_type = _hedge_option_type(target_role)
        target_delta = (
            self._params.selection.short_put_delta
            if target_role is LegRole.SHORT_PUT
            else self._params.selection.short_call_delta
        )
        candidates = rank_role_candidates(
            chain=context.chain,
            resolver=resolver,
            greeks=context.greeks_service,
            option_type=option_type,
            role=target_role,
            target_delta=target_delta,
            tolerance=self._params.selection.short_delta_tolerance,
            config=self._params.selection,
            spot=context.spot or 0.0,
            expiry_at=expiry_at,
            now=context.now,
        )
        old_signed_delta = self._leg_signed_delta(old_leg, context)
        if old_signed_delta is None:
            return None  # the leg being rolled has no usable Greek this evaluation

        # Spec section 8 (Phase 3A correction): from 12:00 IST on the
        # actual expiry day, "tighten monitoring and do not make aggressive
        # inward rolls." An inward roll is a replacement short whose strike
        # is closer to current spot than the short it replaces — for this
        # strategy's own adjustment mechanism (put rolled *upward* on
        # negative delta, call rolled *downward* on positive delta) that is
        # the ordinary, expected direction, so this window does not merely
        # narrow the candidate search — it can leave a confirmed trigger
        # with no permitted replacement at all. Fail conservative in that
        # case: exit the whole cycle rather than silently leave a confirmed
        # elevated-delta position unaddressed this close to expiry (never
        # classified as a known limitation — see docs/
        # IMPLEMENTATION_STATUS_AND_RUNBOOK.md section 11.10 Phase 3A).
        # Unknown spot is treated the same as "cannot prove it is not
        # inward" — fail conservative, never assume a direction it cannot
        # verify. Hedge repair and any other exposure-*reducing* action are
        # untouched by this: they are decided before _maybe_adjust is ever
        # reached (see _evaluate_active's own ordering).
        inward_rolls_prohibited = False
        if is_expiry_day:
            tighten_at = _parse_hhmm(schedule.expiry_tighten_at)
            inward_rolls_prohibited = local_clock >= tighten_at
        old_strike = old_leg.contract.strike
        spot = context.spot

        replacement = None
        for candidate in candidates:
            if candidate.contract.security_id == old_leg.contract.security_id:
                continue
            # The replacement opens at the same quantity as the leg it
            # replaces (spec 7.2/7.3: one-for-one roll, not a re-sized
            # position) — quantity=1 here (the method's own default) would
            # compare a per-contract delta against old_signed_delta's real
            # per-position one, making the "must reduce projected absolute
            # delta" gate below fail almost every time a leg's quantity
            # exceeds one (found and fixed in the gap-closing session's
            # Phase 3 — see docs/IMPLEMENTATION_STATUS_AND_RUNBOOK.md
            # section 11.10 for the real production defect this was).
            new_signed_delta = self._signed_delta(
                candidate.greeks.delta, target_role, quantity=old_leg.quantity
            )
            projected_delta = net_delta - old_signed_delta + new_signed_delta
            if abs(projected_delta) >= abs(net_delta):
                continue  # must reduce projected absolute delta (spec 7.2)
            if inward_rolls_prohibited:
                is_inward = spot is None or abs(candidate.contract.strike - spot) < abs(
                    old_strike - spot
                )
                if is_inward:
                    continue  # spec section 8: no aggressive inward rolls from 12:00
            replacement = candidate
            break
        if replacement is None:
            self._trigger_confirmations = 0
            if inward_rolls_prohibited:
                return self._exit_all(
                    context,
                    "expiry-day risk exit: confirmed delta trigger has no valid "
                    "non-inward short replacement at/after 12:00 on the actual "
                    "expiry day",
                    ExitReason.EXPIRY,
                )
            return None  # no safe candidate -> skip, never loosen filters

        self._trigger_confirmations = 0
        return CycleSignal(
            action=CycleAction.ROLL_SHORT,
            timestamp=context.now,
            legs=(
                CycleLegIntent(
                    role=target_role,
                    side=OrderSide.SELL,
                    contract=replacement.contract,
                    is_replacement=True,
                    replaces_leg_id=old_leg.leg_id,
                ),
            ),
            reason=f"delta adjustment: net_delta_per_lot={net_delta:.2f}",
            target_leg_id=old_leg.leg_id,
            pre_adjustment_net_delta=net_delta,
        )

    def _net_delta_per_lot(self, cycle: Cycle, context: PositionalContext) -> float | None:
        total = 0.0
        for leg in cycle.open_legs():
            signed = self._leg_signed_delta(leg, context)
            if signed is None:
                return None
            total += signed
        return total / self._params.lots if self._params.lots else total

    def _leg_signed_delta(self, leg: Any, context: PositionalContext) -> float | None:
        if leg.contract is None:
            return None
        snapshot = context.leg_greeks.get(leg.contract.security_id)
        if snapshot is None:
            return None
        return self._signed_delta(snapshot.delta, leg.role, quantity=leg.quantity)

    @staticmethod
    def _signed_delta(delta: float, role: LegRole, *, quantity: int = 1) -> float:
        side = _ROLE_SIGN[role]
        sign = 1.0 if side is OrderSide.BUY else -1.0
        return delta * sign * quantity

    def _exit_all(
        self, context: PositionalContext, reason: str, exit_reason: ExitReason
    ) -> CycleSignal:
        return CycleSignal(
            action=CycleAction.EXIT_ALL,
            timestamp=context.now,
            reason=reason,
            exit_reason=exit_reason,
        )


def _hedge_option_type(role: LegRole):  # type: ignore[no-untyped-def]
    from common.models import OptionType

    return OptionType.PE if role in (LegRole.HEDGE_PUT, LegRole.SHORT_PUT) else OptionType.CE
