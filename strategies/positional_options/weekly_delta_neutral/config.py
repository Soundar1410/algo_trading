"""Typed, strict configuration for ``weekly_delta_neutral`` (spec section 10).

Unknown, contradictory, unsafe or incorrectly-ordered parameters fail at
load time — the same ``extra="forbid"`` discipline
:class:`common.config.models._StrictModel` uses, reproduced here (this
package must not import ``common.config.models`` directly to avoid a
strategy -> runtime-config-layer dependency the architecture forbids;
``strict = True`` on ``ConfigDict`` gives the identical guarantee).

There is deliberately no ``lot_size`` field anywhere in this model — spec
section 3.1/10: lot size is resolved from the selected Dhan contract's own
metadata at runtime, never configured.
"""

from __future__ import annotations

from datetime import time

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ScheduleConfig(_Strict):
    entry_day: str = "WEDNESDAY"
    entry_window_start: str = "09:25"
    # Widened from "09:40": entry may now fire any time up to 12:00 once
    # the volatility gate (see VolatilityGateConfig) confirms normal —
    # decision already made, no earliest-entry preference. Kept in step
    # with opening_filter.skip_after below by
    # WeeklyDeltaNeutralParameters._skip_after_not_before_entry_window_end.
    entry_window_end: str = "12:00"
    adjustment_start: str = "09:25"
    adjustment_end: str = "14:45"
    expiry_tighten_at: str = "12:00"
    expiry_adjustment_cutoff: str = "14:30"
    planned_exit: str = "15:05"
    hard_exit: str = "15:15"

    def _times(self) -> dict[str, time]:
        return {
            name: _parse_hhmm(getattr(self, name))
            for name in (
                "entry_window_start",
                "entry_window_end",
                "adjustment_start",
                "adjustment_end",
                "expiry_tighten_at",
                "expiry_adjustment_cutoff",
                "planned_exit",
                "hard_exit",
            )
        }

    @model_validator(mode="after")
    def _ordered(self) -> ScheduleConfig:
        if self.entry_day.upper() != "WEDNESDAY":
            raise ValueError(
                f"entry_day must be WEDNESDAY (spec section 3.2: no Thursday shift); "
                f"got {self.entry_day!r}"
            )
        t = self._times()
        if not (t["entry_window_start"] < t["entry_window_end"]):
            raise ValueError("entry_window_start must be before entry_window_end")
        if not (t["adjustment_start"] < t["adjustment_end"]):
            raise ValueError("adjustment_start must be before adjustment_end")
        if not (
            t["expiry_tighten_at"]
            < t["expiry_adjustment_cutoff"]
            < t["planned_exit"]
            < t["hard_exit"]
        ):
            raise ValueError(
                "expiry-day timing must satisfy expiry_tighten_at < "
                "expiry_adjustment_cutoff < planned_exit < hard_exit"
            )
        if t["adjustment_end"] > t["hard_exit"]:
            raise ValueError("adjustment_end must not be scheduled after hard_exit")
        if t["entry_window_end"] > t["hard_exit"]:
            raise ValueError("entry_window_end must not be scheduled after hard_exit")
        return self


def _parse_hhmm(value: str) -> time:
    try:
        hour, minute = (int(part) for part in value.split(":", 1))
        return time(hour, minute)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"expected 'HH:MM', got {value!r}") from exc


class SelectionConfig(_Strict):
    short_call_delta: float = 0.20
    short_put_delta: float = -0.20
    short_delta_tolerance: float = Field(default=0.03, gt=0)
    hedge_call_delta: float = 0.06
    hedge_put_delta: float = -0.06
    hedge_delta_tolerance: float = Field(default=0.03, gt=0)
    minimum_hedge_width_points: float = Field(default=250, gt=0)
    maximum_hedge_width_points: float = Field(default=500, gt=0)
    maximum_entry_delta_per_lot: float = Field(default=3.0, gt=0)
    maximum_combined_spread_points: float = Field(default=8.0, gt=0)
    minimum_volume: int = Field(default=1, gt=0)
    minimum_open_interest: int = Field(default=1, gt=0)
    quote_max_age_seconds: float = Field(default=5.0, gt=0)
    minimum_credit_to_width_ratio: float = Field(default=0.18, gt=0)

    @model_validator(mode="after")
    def _hedge_is_farther_otm_than_short(self) -> SelectionConfig:
        # A call hedge's delta must be smaller (closer to 0, farther OTM)
        # than the short call's; a put hedge's delta must be larger (closer
        # to 0, farther OTM) than the short put's (put deltas are negative).
        if not (self.hedge_call_delta < self.short_call_delta):
            raise ValueError("hedge_call_delta must be farther OTM (smaller) than short_call_delta")
        if not (self.hedge_put_delta > self.short_put_delta):
            raise ValueError("hedge_put_delta must be farther OTM (larger) than short_put_delta")
        return self

    @model_validator(mode="after")
    def _width_ordered(self) -> SelectionConfig:
        if self.minimum_hedge_width_points > self.maximum_hedge_width_points:
            raise ValueError(
                "minimum_hedge_width_points must not exceed maximum_hedge_width_points"
            )
        return self


class OpeningFilterConfig(_Strict):
    #: Only read by ``volatility_gate.method == "displacement"`` (the
    #: legacy path — spec section 3.3, pre-rewrite) and by the optional gap
    #: veto (``volatility_gate.gap_veto_enabled``); the new default
    #: ``method: realized`` never reads it.
    maximum_move_percent: float = Field(default=0.80, gt=0)
    #: Retained but unused — never read by any code (verified before this
    #: change: only ever declared here and set in the shipped YAML). Not
    #: removed because that would be an unrequested config-contract change;
    #: left for a future cleanup.
    delay_minutes: int = Field(default=20, gt=0)
    # Widened from "10:15" alongside schedule.entry_window_end above: the
    # give-up cutoff for "volatility never confirmed normal this week" is
    # now 12:00, not the old entry window's own end.
    skip_after: str = "12:00"


class VolatilityGateConfig(_Strict):
    """Spec section 3.3 (rewritten): what the entry gate treats as "the
    underlying is calm enough to sell a range around it". ``method``
    selects the computation:

    - ``"realized"`` (new default): tick-based realized volatility — the
      1-sigma percentage move implied by the stdev of consecutive spot
      returns over a rolling ``lookback``-sample window, confirmed
      ``confirmations_required`` times in a row before entry fires.
    - ``"displacement"``: the original opening-stability filter (spot's
      absolute % move from the session's first in-window reference price),
      preserved unchanged and selectable so a paper session can A/B it
      against ``"realized"`` without a code change (requirement 3). Reads
      ``OpeningFilterConfig.maximum_move_percent``/``skip_after`` above,
      never this class's own ``threshold_percent``/``lookback``/
      ``confirmations_required``.

    ``method: "atr"`` is deliberately rejected, not silently ignored: this
    strategy receives spot TICKS only — the positional runtime opts out of
    the candle channel (``runtimes/positional_options/supervisor.py``'s
    ``receive_candles=False``, pinned by
    ``tests/integration/test_positional_candle_channel_composition.py``) —
    so no completed-candle-based ATR is possible without new engine-level
    plumbing this change does not add.

    ``threshold_percent`` and ``gap_veto_percent`` below are UNPROVEN
    STARTING POINTS (requirement 5), not tuned/optimal values — pick a real
    threshold from ``logs/<runtime>.log``'s own
    ``realized_vol_percent=...`` readings (see strategy.py's
    ``_log_volatility_reading``) across real paper sessions before trusting
    either default.
    """

    method: str = "realized"
    #: UNPROVEN starting point — the 1-sigma percentage move of NIFTY spot
    #: over ``lookback`` samples that counts as "calm" enough to sell a
    #: range around. Only read when ``method == "realized"``.
    threshold_percent: float = Field(default=0.15, gt=0)
    #: Samples in the rolling realized-volatility window — roughly
    #: ``lookback * evaluation_interval_seconds`` seconds of history (~5
    #: minutes at the shipped 5s cadence). Coupled to
    #: ``threshold_percent``: changing one without reconsidering the other
    #: changes what "calm" means. Only read when ``method == "realized"``.
    #: Minimum 3, not 2: a window of ``lookback`` samples yields
    #: ``lookback - 1`` consecutive returns, and a standard deviation needs
    #: at least 2 data points to be defined — ``lookback: 2`` would yield
    #: exactly 1 return and could never confirm "normal", ever.
    lookback: int = Field(default=60, ge=3)
    #: Consecutive "normal" readings required before entry fires — guards
    #: against a brief lull inside an otherwise choppy morning. Mirrors
    #: ``AdjustmentConfig.confirmation_checks``'s own pattern below,
    #: including resetting to 0 on any not-normal reading. Only read when
    #: ``method == "realized"``.
    confirmations_required: int = Field(default=3, gt=0)
    #: Optional AND-ed veto (requirement 4), OFF by default: "volatility is
    #: normal now" does not mean "the market didn't gap violently at open
    #: and then go quiet" — entering right after a large gap means the
    #: strikes are centered on a level that just repriced. Defaulted off
    #: because this change's whole purpose is to stop rejecting exactly
    #: that "gapped then went calm" morning; a veto on by default at
    #: anything near the old 0.80% threshold would reproduce the rejection
    #: this change removes. One flag away for A/B in paper.
    gap_veto_enabled: bool = False
    #: UNPROVEN starting point, deliberately looser than
    #: ``OpeningFilterConfig.maximum_move_percent`` (0.80) — this veto is a
    #: "violent reprice" backstop, not a restatement of the filter this
    #: change replaces. Only read when ``gap_veto_enabled`` is True.
    gap_veto_percent: float = Field(default=1.50, gt=0)

    @model_validator(mode="after")
    def _method_is_supported(self) -> VolatilityGateConfig:
        if self.method == "atr":
            raise ValueError(
                "volatility_gate.method: 'atr' is not implemented — weekly_delta_neutral "
                "receives spot TICKS only, never completed candles (the positional runtime "
                "opts out of the candle channel: runtimes/positional_options/supervisor.py "
                "sets receive_candles=False, pinned by "
                "tests/integration/test_positional_candle_channel_composition.py). Use "
                "'realized' (tick-based realized volatility, the default) or 'displacement' "
                "(the legacy opening-stability filter)."
            )
        if self.method not in ("displacement", "realized"):
            raise ValueError(
                f"volatility_gate.method must be 'displacement' or 'realized'; got "
                f"{self.method!r}"
            )
        return self


class AdjustmentConfig(_Strict):
    warning_delta_per_lot: float = Field(default=8.0, gt=0)
    trigger_delta_per_lot: float = Field(default=12.0, gt=0)
    confirmation_checks: int = Field(default=3, gt=0)
    target_delta_per_lot: float = Field(default=3.0, gt=0)
    minimum_minutes_between: int = Field(default=90, gt=0)
    maximum_per_day: int = Field(default=1, gt=0)
    maximum_per_cycle: int = Field(default=3, gt=0)

    @model_validator(mode="after")
    def _target_below_trigger(self) -> AdjustmentConfig:
        ordered = (
            self.target_delta_per_lot < self.warning_delta_per_lot < self.trigger_delta_per_lot
        )
        if not ordered:
            raise ValueError(
                "adjustment thresholds must satisfy target_delta_per_lot < "
                "warning_delta_per_lot < trigger_delta_per_lot"
            )
        return self


class ExitsConfig(_Strict):
    profit_credit_capture_percent: float = Field(default=55.0, gt=0)
    soft_loss_credit_multiple: float = Field(default=1.25, gt=0)
    hard_loss_credit_multiple: float = Field(default=1.50, gt=0)
    emergency_loss_credit_multiple: float = Field(default=1.75, gt=0)
    maximum_cycle_loss_capital_percent: float = Field(default=1.0, gt=0)
    maximum_margin_utilization_percent: float = Field(default=50.0, gt=0)

    @model_validator(mode="after")
    def _strictly_increasing(self) -> ExitsConfig:
        if not (
            self.soft_loss_credit_multiple
            < self.hard_loss_credit_multiple
            < self.emergency_loss_credit_multiple
        ):
            raise ValueError(
                "loss multiples must satisfy soft_loss_credit_multiple < "
                "hard_loss_credit_multiple < emergency_loss_credit_multiple (strictly increasing)"
            )
        return self


class ExecutionConfig(_Strict):
    order_type: str = "LIMIT"
    market_fallback_enabled: bool = False
    persist_before_submit: bool = True
    cancel_before_retry: bool = True

    @model_validator(mode="after")
    def _limit_only(self) -> ExecutionConfig:
        if self.order_type != "LIMIT":
            raise ValueError("order_type must be LIMIT — spec section 9/10")
        return self


class WeeklyDeltaNeutralParameters(_Strict):
    underlying: str = "NIFTY"
    lots: int = Field(default=1, gt=0)
    allocated_capital: float = Field(default=500_000.0, gt=0)
    risk_free_rate: float = Field(default=0.065, ge=0)
    dividend_yield: float = Field(default=0.0, ge=0)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    selection: SelectionConfig = Field(default_factory=SelectionConfig)
    opening_filter: OpeningFilterConfig = Field(default_factory=OpeningFilterConfig)
    volatility_gate: VolatilityGateConfig = Field(default_factory=VolatilityGateConfig)
    adjustment: AdjustmentConfig = Field(default_factory=AdjustmentConfig)
    exits: ExitsConfig = Field(default_factory=ExitsConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    # Market-data resolution (mirrors straddle_920's own config vocabulary —
    # deliberately the same shape, not a competing convention).
    index_security_id: str = ""
    index_segment: str = ""
    fno_segment: str = ""
    scrip_master_cache_dir: str = ""

    @model_validator(mode="after")
    def _skip_after_not_before_entry_window_end(self) -> WeeklyDeltaNeutralParameters:
        # The give-up cutoff can never precede the window it is meant to
        # extend — otherwise entry_window_end..skip_after would be an empty
        # (or inverted) evaluation range and the gate could never confirm.
        if _parse_hhmm(self.opening_filter.skip_after) < _parse_hhmm(
            self.schedule.entry_window_end
        ):
            raise ValueError(
                "opening_filter.skip_after must not be before schedule.entry_window_end "
                f"(got skip_after={self.opening_filter.skip_after!r}, "
                f"entry_window_end={self.schedule.entry_window_end!r})"
            )
        return self
