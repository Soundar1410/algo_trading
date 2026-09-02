"""``weekly_delta_neutral``'s entry-timing/volatility gate — the widened
09:25-12:00 window and the new ``volatility_gate`` (spec section 3.3,
rewritten). Drives ``WeeklyDeltaNeutralStrategy._evaluate_entry`` directly
with a fake, minimal ``PositionalContext``, never a real chain/margin/
candidate pipeline (that is already covered by
``test_weekly_delta_neutral_candidate_search.py`` and the
``tests/integration/test_weekly_delta_neutral_entry.py`` family).

"Did the gate open?" is observed through a spy ``scrip_master`` whose
``lot_size`` is deliberately ``0`` (falsy): once the gate passes,
``_evaluate_entry`` always calls ``scrip_master.nearest_expiry(...)`` next
(to resolve the candidate expiry) and then returns ``None`` immediately
after, at ``if not resolver.lot_size: return None`` — before ever touching
``context.chain``/``context.greeks_service``/``context.margin_estimator``.
So "``nearest_expiry`` was called" is a precise, minimal proxy for "the
timing/volatility gate let evaluation through", with no candidate-selection
fixture required.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from strategies.positional_options.weekly_delta_neutral.strategy import WeeklyDeltaNeutralStrategy

IST = ZoneInfo("Asia/Kolkata")

# 2026-09-02 is a Wednesday; 2026-09-01/2026-09-03 are the adjacent
# Tuesday/Thursday, used by the Wednesday-only test.
_WEDNESDAY = date(2026, 9, 2)
_TUESDAY = date(2026, 9, 1)
_THURSDAY = date(2026, 9, 3)


class _SpyScripMaster:
    """Duck-typed ``ScripMaster`` stand-in. ``lot_size`` is deliberately
    falsy so a strategy that gets *past* the timing/volatility gate stops
    at the very next check, right after resolving the candidate expiry —
    see module docstring."""

    underlying = "NIFTY"
    lot_size = 0

    def __init__(self) -> None:
        self.nearest_expiry_calls: list[date] = []

    def nearest_expiry(self, on: date | None = None) -> str:
        self.nearest_expiry_calls.append(on)  # type: ignore[arg-type]
        return "2026-09-08"


def _strategy(
    scrip_master: _SpyScripMaster, parameters_overrides: dict[str, Any] | None = None
) -> WeeklyDeltaNeutralStrategy:
    parameters: dict[str, Any] = {"underlying": "NIFTY"}
    if parameters_overrides:
        for key, value in parameters_overrides.items():
            if key in parameters and isinstance(parameters[key], dict) and isinstance(value, dict):
                parameters[key] = {**parameters[key], **value}
            else:
                parameters[key] = value
    return WeeklyDeltaNeutralStrategy(parameters=parameters, scrip_master=scrip_master)


def _at(day: date, hour: int, minute: int) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST)


def _context(
    now: datetime,
    spot: float,
    incidents: list[str],
    *,
    spot_is_fresh: bool = True,
    is_trading_day: bool = True,
    is_holiday: bool = False,
) -> Any:
    from common.engine.positional.positional_strategy import PositionalContext

    return PositionalContext(
        now=now,
        trading_date=now.astimezone(IST).date().isoformat(),
        spot=spot,
        spot_is_fresh=spot_is_fresh,
        chain=None,
        leg_greeks={},
        can_enter=True,
        is_holiday=is_holiday,
        is_trading_day=is_trading_day,
        greeks_service=None,  # type: ignore[arg-type]  # never read: gate blocks first
        underlying_security_id="13",
        underlying_segment="IDX_I",
        option_segment="NSE_FNO",
        margin_estimator=None,  # type: ignore[arg-type]  # never read: gate blocks first
        record_pre_entry_incident=incidents.append,
        total_charges=0.0,
    )


# ------------------------------------------------------- method: realized (default)
def test_late_entry_fires_once_realized_volatility_confirms_normal() -> None:
    """Change 1 + Change 2 together: entry may fire well outside the old
    09:25-09:40 window (here, ~09:32) the instant volatility confirms
    normal — lookback=3, confirmations_required=2 for a fast, deterministic
    test. A flat spot (stdev 0) is unambiguously "normal" for any positive
    threshold, so this isolates the timing/counting mechanics from the
    realized-vol arithmetic (covered by the mixed-window test below)."""
    scrip_master = _SpyScripMaster()
    strategy = _strategy(
        scrip_master,
        {"volatility_gate": {"method": "realized", "lookback": 3, "confirmations_required": 2}},
    )
    incidents: list[str] = []

    # Calls 1-2: window not yet full -> not-normal (fail closed), no entry.
    for minute in (25, 26):
        signal = strategy.evaluate(
            cycle=None, context=_context(_at(_WEDNESDAY, 9, minute), 20000.0, incidents)
        )
        assert signal is None
    assert scrip_master.nearest_expiry_calls == []

    # Call 3 (still flat spot): window fills, first real reading is
    # normal -> confirmations=1, still short of confirmations_required=2.
    strategy.evaluate(cycle=None, context=_context(_at(_WEDNESDAY, 9, 32), 20000.0, incidents))
    assert scrip_master.nearest_expiry_calls == []

    # Call 4, well past the old 09:40 cutoff: second consecutive normal
    # reading -> confirmations=2 -> gate opens.
    strategy.evaluate(cycle=None, context=_context(_at(_WEDNESDAY, 9, 45), 20000.0, incidents))
    assert scrip_master.nearest_expiry_calls == [date(2026, 9, 2)]
    assert incidents == []  # a fired entry is not a skipped week


def test_late_entry_at_eleven_am_once_confirmed() -> None:
    """Requirement: "enters at a late time (e.g. 11:xx) once volatility
    confirms normal" — same mechanics as above, timed in the 11 o'clock
    hour, entirely outside the pre-change 09:25-09:40 window."""
    scrip_master = _SpyScripMaster()
    strategy = _strategy(
        scrip_master,
        {"volatility_gate": {"method": "realized", "lookback": 3, "confirmations_required": 2}},
    )
    incidents: list[str] = []
    for minute in (0, 1, 2, 3):
        strategy.evaluate(
            cycle=None, context=_context(_at(_WEDNESDAY, 11, minute), 20000.0, incidents)
        )
    assert scrip_master.nearest_expiry_calls == [date(2026, 9, 2)]


def test_choppy_morning_never_normalizes_and_the_week_is_skipped_once() -> None:
    """No entry all morning, and past the (now 12:00) give-up cutoff the
    week is permanently skipped — recording exactly one pre-entry incident
    even though evaluation keeps firing after the cutoff."""
    scrip_master = _SpyScripMaster()
    strategy = _strategy(
        scrip_master,
        {"volatility_gate": {"method": "realized", "lookback": 3, "confirmations_required": 2}},
    )
    incidents: list[str] = []
    # Alternating spot -> every full window reads far above any sane
    # threshold_percent (a multi-percent swing every step).
    choppy = [20000.0, 20500.0, 20000.0, 20600.0, 20000.0, 20700.0]
    for minute, spot in zip(range(25, 25 + len(choppy)), choppy, strict=True):
        signal = strategy.evaluate(
            cycle=None, context=_context(_at(_WEDNESDAY, 9, minute), spot, incidents)
        )
        assert signal is None
    assert scrip_master.nearest_expiry_calls == []
    assert incidents == []  # not yet past skip_after -> not a skip yet, just still waiting

    # Past the widened 12:00 give-up cutoff: permanently skipped.
    signal = strategy.evaluate(
        cycle=None, context=_context(_at(_WEDNESDAY, 12, 1), 20000.0, incidents)
    )
    assert signal is None
    assert scrip_master.nearest_expiry_calls == []
    assert len(incidents) == 1
    assert "skipped" in incidents[0] and "realized" in incidents[0]

    # A later evaluation the same day must not record a second incident.
    strategy.evaluate(cycle=None, context=_context(_at(_WEDNESDAY, 14, 0), 20000.0, incidents))
    assert len(incidents) == 1


def test_wednesday_only() -> None:
    """The same calm data that fires an entry on Wednesday must never fire
    on the adjacent Tuesday or Thursday — the weekday check short-circuits
    before the gate is even reached."""
    for day in (_TUESDAY, _THURSDAY):
        scrip_master = _SpyScripMaster()
        strategy = _strategy(
            scrip_master,
            {
                "volatility_gate": {
                    "method": "realized", "lookback": 3, "confirmations_required": 2,
                }
            },
        )
        incidents: list[str] = []
        for minute in (0, 1, 2, 3):
            context = _context(_at(day, 11, minute), 20000.0, incidents)
            strategy.evaluate(cycle=None, context=context)
        assert scrip_master.nearest_expiry_calls == [], f"unexpected entry attempt on {day}"


def test_confirmation_counter_resets_on_a_not_normal_reading_rather_than_accumulating() -> None:
    """normal, normal, *not-normal*, normal, normal, normal (3rd required)
    -> entry only fires on the final call, proving the not-normal reading
    at step 3 reset the counter rather than merely pausing it (a bug here
    would let steps 1+2+4 accumulate to 3 without ever needing step 5/6)."""
    scrip_master = _SpyScripMaster()
    strategy = _strategy(
        scrip_master,
        {"volatility_gate": {"method": "realized", "lookback": 3, "confirmations_required": 3}},
    )
    incidents: list[str] = []

    def _evaluate(minute: int, spot: float) -> None:
        strategy.evaluate(cycle=None, context=_context(_at(_WEDNESDAY, 9, minute), spot, incidents))

    # Calls 1-2: fill the window (not-normal by construction: too short).
    _evaluate(25, 20000.0)
    _evaluate(26, 20000.0)

    # Call 3 (window=[20000,20000,20000]): flat -> normal. confirmations=1.
    _evaluate(27, 20000.0)
    assert scrip_master.nearest_expiry_calls == []

    # Call 4 (window=[20000,20000,20000]): still flat -> normal. confirmations=2.
    _evaluate(28, 20000.0)
    assert scrip_master.nearest_expiry_calls == []

    # Call 5: a real jump -> not-normal. confirmations resets to 0. This is
    # the behaviour under test: without this call, 2 (from steps 3-4) + 1
    # (from a hypothetical step 6) would wrongly reach 3.
    _evaluate(29, 20500.0)
    assert scrip_master.nearest_expiry_calls == []

    # Calls 6-8: a steady, constant compounding growth rate after the jump
    # -> every subsequent window's two consecutive returns are equal, so
    # its stdev is exactly 0 -> normal every time. Three consecutive
    # normal readings are needed (confirmations_required=3); the third is
    # call 8.
    p = 20500.0
    for minute in (30, 31, 32):
        p *= 1.025
        _evaluate(minute, p)
        if minute < 32:
            assert scrip_master.nearest_expiry_calls == [], f"fired too early at minute={minute}"
    assert scrip_master.nearest_expiry_calls == [date(2026, 9, 2)]


def test_stale_spot_never_contributes_a_sample_or_confirms() -> None:
    scrip_master = _SpyScripMaster()
    strategy = _strategy(
        scrip_master,
        {"volatility_gate": {"method": "realized", "lookback": 3, "confirmations_required": 1}},
    )
    incidents: list[str] = []
    for minute in range(25, 30):
        signal = strategy.evaluate(
            cycle=None,
            context=_context(_at(_WEDNESDAY, 9, minute), 20000.0, incidents, spot_is_fresh=False),
        )
        assert signal is None
    assert scrip_master.nearest_expiry_calls == []
    assert len(strategy._vol_gate.samples) == 0


# ------------------------------------------------------------ method: displacement
def test_displacement_method_still_works_selected_by_config() -> None:
    """Requirement 3: the pre-2026-09 behaviour, unchanged and reachable by
    config alone. No confirmation counter, and — this is the legacy
    behaviour, not new — the very first in-window evaluation always
    "passes" its own move-from-reference check (the reference *is* that
    same spot, so the move is trivially 0%), so it already reaches
    ``nearest_expiry`` on call 1. A second, later evaluation with a small
    (0.25%) move away from that reference must *also* still pass, proving
    the filter does not wrongly start refusing on a later normal tick."""
    scrip_master = _SpyScripMaster()
    strategy = _strategy(
        scrip_master,
        {
            "volatility_gate": {"method": "displacement"},
            "opening_filter": {"maximum_move_percent": 0.80},
        },
    )
    incidents: list[str] = []
    strategy.evaluate(cycle=None, context=_context(_at(_WEDNESDAY, 9, 25), 20000.0, incidents))
    assert scrip_master.nearest_expiry_calls == [date(2026, 9, 2)]

    # 0.25% move from the captured reference -- inside the 0.80% tolerance.
    strategy.evaluate(cycle=None, context=_context(_at(_WEDNESDAY, 9, 26), 20050.0, incidents))
    assert scrip_master.nearest_expiry_calls == [date(2026, 9, 2)] * 2


def test_displacement_method_refuses_past_the_maximum_move_and_then_skips_the_week() -> None:
    scrip_master = _SpyScripMaster()
    strategy = _strategy(
        scrip_master,
        {
            "volatility_gate": {"method": "displacement"},
            "opening_filter": {"maximum_move_percent": 0.80},
        },
    )
    incidents: list[str] = []
    # Call 1 captures the reference and, as above, trivially passes its own
    # 0%-move check.
    strategy.evaluate(cycle=None, context=_context(_at(_WEDNESDAY, 9, 25), 20000.0, incidents))
    assert scrip_master.nearest_expiry_calls == [date(2026, 9, 2)]

    # 1% move from that same reference -- outside the 0.80% tolerance ->
    # no *further* entry attempt (the call count does not grow).
    signal = strategy.evaluate(
        cycle=None, context=_context(_at(_WEDNESDAY, 9, 26), 20200.0, incidents)
    )
    assert signal is None
    assert scrip_master.nearest_expiry_calls == [date(2026, 9, 2)]
    assert incidents == []

    # Still displaced past the (widened) 12:00 skip_after -> the week is
    # permanently skipped, exactly one incident recorded.
    signal = strategy.evaluate(
        cycle=None, context=_context(_at(_WEDNESDAY, 12, 1), 20200.0, incidents)
    )
    assert signal is None
    assert len(incidents) == 1
    assert "displacement" in incidents[0]


# ---------------------------------------------------------------------- gap veto
#: Reference captured at 20000.0 (call 1), then a jump to 21000.0 that
#: repeats until it fills the whole rolling window (calls 2-4, lookback=3
#: -> the window at call 4 is [21000, 21000, 21000]) -- a morning that
#: gapped 5% and then went completely calm. The realized-vol gate alone
#: reads this as "normal" (flat window, stdev 0); only the gap veto, which
#: compares the *current* spot against the original reference rather than
#: the rolling window, can see the 5% gap at all.
_GAPPED_THEN_CALM = [20000.0, 21000.0, 21000.0, 21000.0]


def test_gap_veto_blocks_an_otherwise_normal_reading_when_enabled() -> None:
    scrip_master = _SpyScripMaster()
    strategy = _strategy(
        scrip_master,
        {
            "volatility_gate": {
                "method": "realized", "lookback": 3, "confirmations_required": 1,
                "gap_veto_enabled": True, "gap_veto_percent": 1.5,
            }
        },
    )
    incidents: list[str] = []
    for minute, spot in zip(range(25, 29), _GAPPED_THEN_CALM, strict=True):
        signal = strategy.evaluate(
            cycle=None, context=_context(_at(_WEDNESDAY, 9, minute), spot, incidents)
        )
        assert signal is None
    assert scrip_master.nearest_expiry_calls == []


def test_gap_veto_off_by_default_lets_the_same_data_enter() -> None:
    scrip_master = _SpyScripMaster()
    strategy = _strategy(
        scrip_master,
        {
            "volatility_gate": {
                "method": "realized", "lookback": 3, "confirmations_required": 1,
            }
        },
    )
    incidents: list[str] = []
    for minute, spot in zip(range(25, 29), _GAPPED_THEN_CALM, strict=True):
        strategy.evaluate(cycle=None, context=_context(_at(_WEDNESDAY, 9, minute), spot, incidents))
    assert scrip_master.nearest_expiry_calls == [date(2026, 9, 2)]


# ------------------------------------------------------------------- reset_daily
def test_reset_daily_clears_the_realized_vol_window_and_confirmations() -> None:
    scrip_master = _SpyScripMaster()
    strategy = _strategy(
        scrip_master,
        {"volatility_gate": {"method": "realized", "lookback": 3, "confirmations_required": 2}},
    )
    incidents: list[str] = []
    for minute in (25, 26, 27, 28):
        context = _context(_at(_WEDNESDAY, 9, minute), 20000.0, incidents)
        strategy.evaluate(cycle=None, context=context)
    assert scrip_master.nearest_expiry_calls == [date(2026, 9, 2)]

    strategy.reset_daily()
    assert len(strategy._vol_gate.samples) == 0
    assert strategy._vol_gate.confirmations == 0
    assert strategy._opening.reference_price is None
