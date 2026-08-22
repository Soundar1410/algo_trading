"""``rolling_strangle_otm1`` — the OTM-1 NIFTY rolling strangle, ported from
the legacy ``Trading_Automation`` ``points_rolling_strangle`` implementation
onto the generic :class:`~common.engine.multi_leg_engine.MultiLegEngine` and
its repeated-roll-capable durable claim machinery (migration ``0013``, Phase
1/Phase 2 of this branch).

See ``ROLLING_STRANGLE_OTM1_ALGO_TRADING_SPEC.md`` in this directory for the
authoritative rule set this class implements; section references in the
comments below point at that document.

State ownership (spec section 7/9.5)
-------------------------------------
Unlike ``straddle_920`` (which mutates a handful of simple ``Basket`` scalar
fields directly, relying on the engine's post-hoc critical persist), this
class **never mutates** ``basket`` or ``basket.roll_state`` at all — both are
read-only from here, exactly as :class:`~common.engine.multi_leg_strategy.
BaseMultiLegStrategy`'s own docstring already requires. Every fact a decision
needs is already durable and hydrated onto the ``Basket``/``BasketRollState``
the engine hands to every call:

* whether the day's one primary attempt is consumed (``basket.entries_consumed``);
* the shared reference spot and the candle it was set at
  (``basket.roll_state.reference_price``/``.anchor_candle_ts``);
* each role's independent roll count (``basket.roll_state.roll_count(role)``);
* the role's current in-flight claim, if any, and its lifecycle state
  (``basket.roll_state.active_claim(role)``);
* which concrete leg is currently open per role (``basket.leg_by_role(role)``);
* cumulative realised/unrealised gross P&L, including adjusted-out legs
  (``basket.total_gross_pnl()`` — see :meth:`Basket.realised_gross_pnl`'s own
  docstring: "including adjusted-out legs", spec section 10.1).

Every durable-state transition this strategy needs — consuming the primary
attempt, setting/re-anchoring the reference spot, claiming a roll, expiring a
stale pending replacement — is instead requested through the typed command
surface :class:`~common.engine.multi_leg_models.BasketStateCommit` /
:class:`~common.engine.multi_leg_models.AdjustmentRequest` /
:class:`~common.engine.multi_leg_models.AnchorUpdate`, carried on the
:class:`~common.engine.multi_leg_models.BasketSignal` this class returns. The
engine is the only thing that ever writes any of it, atomically, before any
order effect the same signal authorises (see ``MultiLegEngine._apply_
state_commit``/``_close_adjusted_legs_with_ledger``) — a crash between "the
strategy decided" and "an order was placed" can never leave the day's roll
budget, reference spot, or primary-attempt consumption ambiguous.

This class never constructs a Dhan SDK client, never touches SQLite
directly, and never resolves an option contract itself (contract resolution
is engine-owned — see :meth:`~common.engine.multi_leg_engine.MultiLegEngine.
_enter_legs`) — matching every other strategy in this repository.
"""

from __future__ import annotations

from datetime import time as time_
from typing import Any

from common.engine.models import Moneyness, OptionSelection
from common.engine.multi_leg_models import (
    AdjustmentLifecycle,
    AdjustmentRequest,
    AdjustmentTarget,
    AnchorUpdate,
    Basket,
    BasketAction,
    BasketRollState,
    BasketSignal,
    BasketStateCommit,
    LegInstance,
    LegIntent,
    LegRole,
    LegState,
)
from common.engine.multi_leg_strategy import BaseMultiLegStrategy, register_multi_leg_strategy
from common.indicators.base import OHLC
from common.models import ExitReason, OrderSide, Tick
from common.utils.timeutils import parse_hhmm

#: The two roles this strategy ever opens or rolls. Iterated in a fixed order
#: everywhere a "for each role" loop needs one, so behaviour never depends on
#: dict/set iteration order.
_ROLES: tuple[LegRole, ...] = (LegRole.CE, LegRole.PE)


def _pick(cfg: Any, kwargs: dict[str, Any], key: str, default: Any) -> Any:
    """Read ``key`` from explicit kwargs first, then ``cfg.parameters``, else default.

    Mirrors ``Straddle920Strategy._pick``/``EmaCross9x21BuyStrategy._pick``
    exactly, so this strategy supports both the direct-kwargs construction
    path (``runtimes.intraday_options.multi_leg_engine_worker``'s
    ``strategy_kwargs``) and a future registry-style ``cfg``-only
    construction, without duplicating the read logic per key.
    """
    if key in kwargs:
        return kwargs[key]
    params = dict(getattr(cfg, "parameters", {}) or {})
    return params.get(key, default)


@register_multi_leg_strategy("rolling_strangle_otm1")
class RollingStrangleOtm1Strategy(BaseMultiLegStrategy):
    """Short one-strike-OTM NIFTY CE + PE at 09:45, repeated per-role
    rolling on a 60-point trigger (up to 2 rolls per side), legacy risk
    formula (spec sections 8-10)."""

    def __init__(self, cfg: Any = None, **kwargs: Any) -> None:
        super().__init__(cfg)
        self._lots_per_leg = int(_pick(cfg, kwargs, "lots_per_leg", 10))
        self._entry_time: time_ = parse_hhmm(str(_pick(cfg, kwargs, "entry_time", "09:45")))
        self._new_entry_cutoff: time_ = parse_hhmm(
            str(_pick(cfg, kwargs, "stop_new_entries_after", "15:10"))
        )
        # Informational only (status()) — hard square-off itself is entirely
        # engine/session-owned (spec section 10.2, architecture mapping
        # section 12); this class never evaluates or acts on it directly.
        self._square_off_time: time_ = parse_hhmm(
            str(_pick(cfg, kwargs, "square_off_time", "15:15"))
        )
        self._strike_step = int(_pick(cfg, kwargs, "strike_step", 50))
        self._otm_distance_points = float(_pick(cfg, kwargs, "otm_distance_points", 50))
        self._roll_trigger_points = float(_pick(cfg, kwargs, "roll_trigger_points", 60))
        self._max_rolls: dict[LegRole, int] = {
            LegRole.CE: int(_pick(cfg, kwargs, "max_rolls_ce", 2)),
            LegRole.PE: int(_pick(cfg, kwargs, "max_rolls_pe", 2)),
        }
        self._single_leg_roll = bool(_pick(cfg, kwargs, "single_leg_roll", True))
        self._combined_stop_per_lot = float(_pick(cfg, kwargs, "combined_stop_per_lot", 2000.0))
        # SL_total (spec section 4) — computed once, config-driven; scales
        # exactly by combined_stop_per_lot per lot (spec section 17.5).
        self._sl_total = self._lots_per_leg * self._combined_stop_per_lot
        self._blackout_dates: frozenset[str] = frozenset(
            str(d) for d in _pick(cfg, kwargs, "blackout_dates", ())
        )
        # steps = max(1, round(otm_distance / strike_step)) (spec section
        # 6.4 step 3) — constant for this strategy's lifetime given fixed
        # config; recomputing it per selection would be pure waste since
        # neither input ever changes after construction.
        self._otm_steps = max(1, round(self._otm_distance_points / self._strike_step))

    # ------------------------------------------------------------- lifecycle
    def reset(self) -> None:
        """No-op: every fact this strategy needs — primary-attempt
        consumption, each role's independent roll count, the shared
        reference spot, pending replacement lifecycle — is already durable
        on ``Basket``/``BasketRollState``, which the engine rebuilds fresh
        at the start of each trading day (see module docstring)."""
        return None

    def status(self) -> str:
        mode = "single" if self._single_leg_roll else "both"
        return (
            f"lots={self._lots_per_leg} otm={self._otm_distance_points:g}pt "
            f"trigger={self._roll_trigger_points:g}pt "
            f"rolls<=ce:{self._max_rolls[LegRole.CE]},pe:{self._max_rolls[LegRole.PE]} "
            f"mode={mode} sl={self._sl_total:.0f}"
        )

    @property
    def quantity_lots(self) -> int:
        return self._lots_per_leg

    def _option_selection(self) -> OptionSelection:
        return OptionSelection(moneyness=Moneyness.OTM, steps=self._otm_steps)

    # ------------------------------------------------------------- candle
    def on_candle(
        self,
        candle: OHLC,
        timestamp: Any,
        *,
        basket: Basket,
        vix: float | None,
    ) -> BasketSignal | None:
        t = timestamp.time()

        # The combined stop (on_leg_tick) already durably blocked the day —
        # no new roll, replacement, or entry may follow it (spec section
        # 10.1's last bullet). Cheap short-circuit; _enter_legs/the roll
        # claim path would each independently refuse anyway once legs are
        # gone, but there is nothing left to evaluate once this is set.
        if basket.day_blocked_reason is not None:
            return None

        roll_state = basket.roll_state
        reference_spot = roll_state.reference_price if roll_state is not None else None

        # --- Roll trigger (spec section 9.1) -------------------------------
        # Only evaluated once a reference spot exists (i.e. only after
        # primary entry) and strictly before the new-entry cutoff (section
        # 9.4). An "exit" action (closing a threatened leg) — priority order
        # (section 10.3) places this ahead of a merely pending replacement:
        # exits beat entries at the same logical boundary.
        if reference_spot is not None and t < self._new_entry_cutoff:
            move = candle.close - reference_spot
            roll_signal = self._maybe_roll(move, candle, timestamp, basket)
            if roll_signal is not None:
                return roll_signal

        # --- Pending next-candle replacement / cutoff expiry (section 9.2
        # steps 9-10, section 9.4) ------------------------------------------
        # Evaluated unconditionally on the cutoff (not gated on
        # t < cutoff): a role stuck AWAITING_NEXT_CANDLE once the cutoff has
        # passed must still be durably expired here, or it would never be
        # detected as expired at all.
        if roll_state is not None:
            replacement_signal = self._replacement_or_expiry_signal(roll_state, timestamp, t)
            if replacement_signal is not None:
                return replacement_signal

        # --- Primary entry (spec section 8) ---------------------------------
        if basket.entries_consumed:
            return None
        if t < self._entry_time:
            return None
        return self._primary_entry_signal(candle, timestamp, basket)

    def _maybe_roll(
        self, move: float, candle: OHLC, timestamp: Any, basket: Basket
    ) -> BasketSignal | None:
        if self._single_leg_roll:
            role = self._threatened_role(move)
            if role is None:
                return None
            return self._single_leg_roll_signal(role, candle, timestamp, basket)
        if abs(move) < self._roll_trigger_points:
            return None
        return self._both_leg_roll_signal(candle, timestamp, basket)

    def _threatened_role(self, move: float) -> LegRole | None:
        """Spec section 9.1: exactly +/-60 triggers; the comparison is
        inclusive both ways. Up move threatens CE (it is now deep ITM-
        risking), down move threatens PE."""
        if move >= self._roll_trigger_points:
            return LegRole.CE
        if move <= -self._roll_trigger_points:
            return LegRole.PE
        return None

    def _single_leg_roll_signal(
        self, role: LegRole, candle: OHLC, timestamp: Any, basket: Basket
    ) -> BasketSignal | None:
        leg = basket.leg_by_role(role)
        if leg is None or leg.state is not LegState.OPEN:
            # No open leg for the threatened role (already rolled/closed
            # some other way) — nothing to roll. The opposite role's own
            # budget is untouched (spec section 9.2: "the opposite role's
            # roll counter does not gate this roll").
            return None
        roll_state = basket.roll_state
        count = roll_state.roll_count(role) if roll_state is not None else 0
        if count >= self._max_rolls[role]:
            return None
        request = AdjustmentRequest(
            targets=(AdjustmentTarget(leg_id=leg.leg_id, role=role),),
            anchor=AnchorUpdate(price=candle.close, candle_ts=timestamp),
        )
        return BasketSignal(
            action=BasketAction.ADJUST_LEGS,
            timestamp=timestamp,
            adjustment=request,
            reason=(
                f"{role.value} roll #{count + 1}/{self._max_rolls[role]}: "
                f"spot {candle.close:.2f}"
            ),
        )

    def _both_leg_roll_signal(
        self, candle: OHLC, timestamp: Any, basket: Basket
    ) -> BasketSignal | None:
        """Spec section 9.3 (``single_leg_roll: false``): both roles must
        have an open leg *and* available budget, or nothing rolls — a
        partial both-leg roll is never attempted."""
        legs: dict[LegRole, LegInstance] = {}
        for role in _ROLES:
            leg = basket.leg_by_role(role)
            if leg is None or leg.state is not LegState.OPEN:
                return None
            legs[role] = leg
        roll_state = basket.roll_state
        for role in _ROLES:
            count = roll_state.roll_count(role) if roll_state is not None else 0
            if count >= self._max_rolls[role]:
                return None
        request = AdjustmentRequest(
            targets=tuple(
                AdjustmentTarget(leg_id=legs[role].leg_id, role=role) for role in _ROLES
            ),
            anchor=AnchorUpdate(price=candle.close, candle_ts=timestamp),
        )
        return BasketSignal(
            action=BasketAction.ADJUST_LEGS,
            timestamp=timestamp,
            adjustment=request,
            reason=f"both-leg recentre: spot {candle.close:.2f}",
        )

    def _replacement_or_expiry_signal(
        self, roll_state: BasketRollState, timestamp: Any, t: time_
    ) -> BasketSignal | None:
        awaiting = AdjustmentLifecycle.AWAITING_NEXT_CANDLE.value
        pending: list[tuple[LegRole, str]] = []
        for role in _ROLES:
            claim = roll_state.active_claim(role)
            if claim is not None and claim.lifecycle_state == awaiting:
                pending.append((role, claim.target_leg_id))
        if not pending:
            return None

        if t >= self._new_entry_cutoff:
            # Spec section 9.4: the cutoff was reached before a replacement
            # could be attempted. Durably expire rather than reopen the
            # closed adjusted leg using stale data; the other, unaffected
            # role (if any) keeps trading normally.
            return BasketSignal(
                action=BasketAction.NONE,
                timestamp=timestamp,
                state_commit=BasketStateCommit(
                    expire_replacement_for=tuple(role for role, _ in pending)
                ),
                reason="replacement cutoff expired",
            )

        # The one-shot attempt, on this exact candle (spec section 9.2 step
        # 10): a fresh OTM option of the same role using the current spot.
        # Every role that independently reached AWAITING_NEXT_CANDLE by this
        # candle is replaced together in one signal — the engine's own
        # per-role/claim-group-aware gate (_consume_replacement_claims)
        # consumes each independently, all-or-nothing as a group only when
        # they in fact share one claim_group_id (the both-leg case).
        legs = tuple(
            LegIntent(
                role=role,
                side=OrderSide.SELL,
                option_selection=self._option_selection(),
                is_replacement=True,
                replaces_leg_id=target_leg_id,
            )
            for role, target_leg_id in pending
        )
        return BasketSignal(
            action=BasketAction.ENTER_LEG,
            timestamp=timestamp,
            legs=legs,
            reason=f"replacement for {', '.join(role.value for role, _ in pending)}",
        )

    def _primary_entry_signal(
        self, candle: OHLC, timestamp: Any, basket: Basket
    ) -> BasketSignal:
        # Durably consume the one primary attempt for the day *before*
        # selection or order effects (spec section 8 step 1) — carried on
        # state_commit so the engine commits it atomically, whichever branch
        # below is taken. Never retried later that day (spec section 8: "no
        # second primary-entry attempt").
        if basket.trading_date in self._blackout_dates:
            return BasketSignal(
                action=BasketAction.NONE,
                timestamp=timestamp,
                state_commit=BasketStateCommit(
                    consume_entry_attempt=True,
                    block_day_reason="blackout date",
                ),
                reason="blackout date: no entry",
            )

        legs = tuple(
            LegIntent(role=role, side=OrderSide.SELL, option_selection=self._option_selection())
            for role in _ROLES
        )
        return BasketSignal(
            action=BasketAction.ENTER_BASKET,
            timestamp=timestamp,
            legs=legs,
            state_commit=BasketStateCommit(
                consume_entry_attempt=True,
                anchor=AnchorUpdate(price=candle.close, candle_ts=timestamp),
            ),
            reason=(
                f"primary entry: sell {self._otm_distance_points:g}pt OTM CE and PE"
            ),
        )

    # ------------------------------------------------------------- leg tick
    def on_leg_tick(self, leg: LegInstance, tick: Tick, basket: Basket) -> BasketSignal | None:
        # Hard square-off (highest priority) is entirely engine-owned,
        # evaluated before this is ever reached (spec section 10.2/10.3).
        # There is no leg-level adjustment trigger, profit target, VIX
        # filter, Greeks filter, or trailing stop in this strategy (spec
        # section 10.3) — the combined loss stop is the only tick-driven
        # risk rule.
        if basket.day_blocked_reason is not None:
            return None

        # Spec section 10.1: legacy gross basket P&L — realised from every
        # closed leg (including rolled-out ones) plus unrealised from
        # currently open legs, inclusive threshold.
        total = basket.total_gross_pnl()
        if total <= -self._sl_total:
            return BasketSignal(
                action=BasketAction.EXIT_ALL,
                timestamp=tick.exchange_time,
                exit_reason=ExitReason.DAILY_LOSS_LIMIT,
                reason=f"combined stop: T={total:.2f} <= -{self._sl_total:.2f}",
            )
        return None
