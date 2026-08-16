"""Shared fixture-building helpers for the weekly_delta_neutral acceptance
suites (spec sections 6/7/8/13) — not a test module itself (leading
underscore; mirrors ``tests/unit/_dashboard_fakes.py``'s own convention, so
pytest never tries to collect it).

Every acceptance test in this family drives the *real*
``runtimes.positional_options.worker.build_engine`` wiring against a real
temp SQLite database — the same discipline
``test_weekly_delta_neutral_entry.py``/``_restart.py`` already established.
Nothing here hand-crafts a ``strategy_cycles``/``positions`` row directly;
every cycle a test observes was produced by a real entry sequence run
through the real engine.

**Why a custom ``ScriptedFeed``, not the existing ``SimulatedFeed``.** Some
acceptance rows (the adjustment-confirmation counter resetting when delta
returns *below* the trigger; a P&L boundary crossed by a later price move)
need the chain's own Greeks to change *between* evaluations inside one
``engine.run()`` call. ``OptionChainService`` (``common/market_data/
option_chain.py``) caches the *snapshot wrapper* for up to 2.5s of real
``time.monotonic()``, but ``GreeksService.chain_snapshot`` re-parses
``snapshot.payload`` fresh on every call, cache hit or not — so mutating
the *same* payload dict object in place (never replacing the reference)
takes effect on the very next evaluation regardless of the wrapper's own
cache state. ``ScriptedFeed`` interleaves plain ticks with zero-argument
callables so a test can express "and now the chain shows this" as a step in
its own timeline, without waiting on or patching any clock.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from common.engine.config import SessionConfig
from common.engine.feed import MarketDataFeed
from common.execution import ExecutionRepository
from common.margin import LegMarginRequest
from common.market_data.scrip_master import ScripMaster
from common.models import Tick
from common.persistence import Database, MigrationRunner
from runtimes.positional_options.config_adapter import WorkerConfig

IST = ZoneInfo("Asia/Kolkata")
NIFTY_SECURITY_ID = "13"
EXPIRY_DATE = "2026-08-26"  # a Wednesday — never a real exchange-shifted date

# ------------------------------------------------------------- strike layout
HEDGE_PUT_STRIKE = 23150.0
SHORT_PUT_STRIKE = 23500.0
SHORT_CALL_STRIKE = 24500.0
HEDGE_CALL_STRIKE = 24850.0
#: Roll-up replacement for the short put (spec 7.2: negative net delta ->
#: roll the short put *upward*) — a higher strike, larger-magnitude delta
#: within the same -0.20 +/- 0.03 target band as a fresh entry short.
ROLL_UP_SHORT_PUT_STRIKE = 23600.0
#: A *second* roll-up candidate, for a scenario that rolls the put twice
#: (e.g. once per day across a restart) — must differ from
#: ``ROLL_UP_SHORT_PUT_STRIKE`` since the first roll's replacement becomes
#: the second roll's own old leg, which the search always excludes.
ROLL_UP_SHORT_PUT_STRIKE_2 = 23650.0
#: Roll-down replacement for the short call (positive net delta -> roll the
#: short call *downward*) — a lower strike, larger-magnitude delta within
#: the same 0.20 +/- 0.03 target band.
ROLL_DOWN_SHORT_CALL_STRIKE = 24400.0
#: A hedge-repair replacement for the put hedge — same target band as a
#: fresh entry hedge, a different strike than HEDGE_PUT_STRIKE.
REPAIR_HEDGE_PUT_STRIKE = 23200.0

SECURITY_IDS: dict[tuple[float, str], str] = {
    (HEDGE_PUT_STRIKE, "PE"): "90001",
    (SHORT_PUT_STRIKE, "PE"): "90002",
    (SHORT_CALL_STRIKE, "CE"): "90003",
    (HEDGE_CALL_STRIKE, "CE"): "90004",
    (ROLL_UP_SHORT_PUT_STRIKE, "PE"): "90005",
    (ROLL_DOWN_SHORT_CALL_STRIKE, "CE"): "90006",
    (ROLL_UP_SHORT_PUT_STRIKE_2, "PE"): "90007",
    (REPAIR_HEDGE_PUT_STRIKE, "PE"): "90008",
}

LOT_SIZE = "75"


def build_scrip_master() -> ScripMaster:
    rows = [
        [
            "SEM_SMST_SECURITY_ID", "SEM_INSTRUMENT_NAME", "SEM_TRADING_SYMBOL",
            "SEM_CUSTOM_SYMBOL", "SEM_EXPIRY_DATE", "SEM_STRIKE_PRICE",
            "SEM_OPTION_TYPE", "SEM_LOT_UNITS", "SEM_EXM_EXCH_ID", "SEM_SEGMENT",
        ]
    ]
    for (strike, option_type), security_id in SECURITY_IDS.items():
        rows.append(
            [
                security_id, "OPTIDX", f"NIFTY-26AUG2026-{strike:.0f}-{option_type}",
                f"NIFTY {strike:.0f} {option_type}", f"{EXPIRY_DATE} 00:00:00",
                f"{strike:.0f}", option_type, LOT_SIZE, "NSE", "D",
            ]
        )
    buffer = io.StringIO()
    csv.writer(buffer).writerows(rows)
    return ScripMaster("NIFTY", exchange="NSE").load_from_text(buffer.getvalue())


def _leg_payload(delta: float, bid: float, ask: float) -> dict[str, Any]:
    return {
        "greeks": {"delta": delta, "gamma": 0.001, "theta": -5.0, "vega": 10.0},
        "implied_volatility": 14.0,
        "last_price": (bid + ask) / 2.0,
        "oi": 500_000,
        "top_bid_price": bid,
        "top_ask_price": ask,
        "volume": 25_000,
    }


def initial_chain_payload() -> dict[str, Any]:
    """A fresh, mutable chain payload dict — every strike this whole
    fixture family ever selects, at its normal (pre-adjustment) delta.
    Callers hold onto this dict and mutate it in place (see
    :func:`set_leg_delta`) rather than replacing it, so a cached
    ``ChainSnapshot`` sees the change without a new fetch."""
    return {
        "status": "success",
        "data": {
            "last_price": 24000.0,
            "oc": {
                f"{HEDGE_PUT_STRIKE:.6f}": {"pe": _leg_payload(-0.06, 18.0, 20.0)},
                f"{SHORT_PUT_STRIKE:.6f}": {"pe": _leg_payload(-0.20, 80.0, 82.0)},
                f"{SHORT_CALL_STRIKE:.6f}": {"ce": _leg_payload(0.20, 78.0, 80.0)},
                f"{HEDGE_CALL_STRIKE:.6f}": {"ce": _leg_payload(0.06, 17.0, 19.0)},
                f"{ROLL_UP_SHORT_PUT_STRIKE:.6f}": {"pe": _leg_payload(-0.22, 95.0, 97.0)},
                # Deliberately *outside* the short-put target band
                # (-0.20 +/- 0.03) and the hedge-put band (-0.06 +/- 0.03)
                # by default — a test that wants either as a real
                # candidate mutates it into range itself via
                # set_leg_delta, so adding a fixture strike here can never
                # silently change which candidate an *existing* test's
                # search picks.
                f"{ROLL_UP_SHORT_PUT_STRIKE_2:.6f}": {"pe": _leg_payload(-0.35, 96.0, 98.0)},
                f"{ROLL_DOWN_SHORT_CALL_STRIKE:.6f}": {"ce": _leg_payload(0.22, 93.0, 95.0)},
                f"{REPAIR_HEDGE_PUT_STRIKE:.6f}": {"pe": _leg_payload(-0.35, 19.0, 21.0)},
            },
        },
    }


def set_leg_delta(payload: dict[str, Any], strike: float, option_type: str, delta: float) -> None:
    """Mutate one leg's delta *in place* — the same dict object the chain
    fetcher keeps returning, so a later (throttle/TTL-permitting) fetch
    reflects it without the test ever replacing the payload reference."""
    side_key = option_type.lower()
    payload["data"]["oc"][f"{strike:.6f}"][side_key]["greeks"]["delta"] = delta


def chain_fetcher_for(payload: dict[str, Any]) -> Callable[[int, str, str], dict[str, Any]]:
    return lambda _sid, _seg, _exp: payload


def fake_margin_fetcher(_leg: LegMarginRequest) -> float:
    """Well under the default 500,000 allocated-capital cap for every
    scenario this fixture family builds (4-6 legs x 20,000)."""
    return 20_000.0


def underlying_tick(ts: datetime, price: float = 24000.0) -> Tick:
    return Tick(
        security_id=NIFTY_SECURITY_ID, instrument="NIFTY", last_price=price,
        exchange_time=ts, received_at=ts,
    )


def leg_tick(strike: float, option_type: str, bid: float, ask: float, ts: datetime) -> Tick:
    security_id = SECURITY_IDS[(strike, option_type)]
    return Tick(
        security_id=security_id, instrument=security_id, last_price=(bid + ask) / 2.0,
        exchange_time=ts, received_at=ts, bid_price=bid, ask_price=ask,
    )


def entry_ticks(entry_ts: datetime) -> list[Tick]:
    """Every original leg's fresh quote, then the underlying — the same
    order ``test_weekly_delta_neutral_entry.py`` uses, so the real hedge-
    first sequential entry fills all four legs in one evaluation."""
    return [
        leg_tick(HEDGE_PUT_STRIKE, "PE", 18.0, 20.0, entry_ts),
        leg_tick(HEDGE_CALL_STRIKE, "CE", 17.0, 19.0, entry_ts),
        leg_tick(SHORT_PUT_STRIKE, "PE", 80.0, 82.0, entry_ts),
        leg_tick(SHORT_CALL_STRIKE, "CE", 78.0, 80.0, entry_ts),
        underlying_tick(entry_ts),
    ]


def build_worker_config(
    trading_date: str,
    *,
    evaluation_interval_seconds: float = 0.0,
    #: Generous by design (unlike production's real 5s default): these
    #: acceptance tests exercise business logic across a scripted timeline
    #: spanning minutes-to-hours of *simulated* market time, not the
    #: freshness gate itself — that already has dedicated coverage
    #: (test_weekly_delta_neutral_selection.py,
    #: test_weekly_delta_neutral_candidate_search.py,
    #: test_margin_estimator.py). Mirrors production's own derivation
    #: (config_adapter.build_worker_config reads the engine-level
    #: GreeksService age from parameters.selection.quote_max_age_seconds
    #: too), so both stay consistent by construction here as well.
    quote_max_age_seconds: float = 3600.0,
    max_adjustments_per_day: int = 1,
    max_adjustments_per_cycle: int = 3,
    min_minutes_between_adjustments: int = 90,
    parameters_overrides: dict[str, Any] | None = None,
    cost_rates: dict[str, Any] | None = None,
    allocated_capital: float | None = None,
) -> WorkerConfig:
    parameters: dict[str, Any] = {
        "underlying": "NIFTY",
        "index_security_id": NIFTY_SECURITY_ID,
        "index_segment": "IDX_I",
        "fno_segment": "NSE_FNO",
        "selection": {"quote_max_age_seconds": quote_max_age_seconds},
    }
    if parameters_overrides:
        existing_selection = parameters.get("selection")
        selection_is_mergeable = isinstance(existing_selection, dict)
        for key, value in parameters_overrides.items():
            if key == "selection" and isinstance(value, dict) and selection_is_mergeable:
                existing_selection.update(value)
            else:
                parameters[key] = value
    if allocated_capital is not None:
        parameters["allocated_capital"] = allocated_capital
    return WorkerConfig(
        runtime_id="positional_options",
        strategy_id="weekly_delta_neutral",
        strategy_ref=(
            "strategies.positional_options.weekly_delta_neutral.strategy:"
            "WeeklyDeltaNeutralStrategy"
        ),
        trading_date=trading_date,
        lots=1,
        timezone="Asia/Kolkata",
        underlying_security_id=NIFTY_SECURITY_ID,
        underlying_instrument="NIFTY",
        underlying_segment="IDX_I",
        option_segment="NSE_FNO",
        session=SessionConfig(
            timezone="Asia/Kolkata", start_time="09:25", end_time="09:40",
            square_off_time="15:15", holidays=(),
        ),
        risk_free_rate=0.065,
        dividend_yield=0.0,
        quote_max_age_seconds=quote_max_age_seconds,
        evaluation_interval_seconds=evaluation_interval_seconds,
        max_adjustments_per_day=max_adjustments_per_day,
        max_adjustments_per_cycle=max_adjustments_per_cycle,
        min_minutes_between_adjustments=min_minutes_between_adjustments,
        parameters=parameters,
        cost_rates=cost_rates,
    )


#: Every ``CostRates`` field zeroed — for boundary-exactness P&L tests that
#: need to reason about the entry credit alone, without also computing real
#: brokerage/exchange/STT/GST/stamp-duty on every fill. A *separate*,
#: qualitative test (not a boundary one) proves charges are genuinely
#: netted, using the real, nonzero default rates instead.
ZERO_COST_RATES: dict[str, float] = {
    "brokerage_per_order": 0.0,
    "exchange_charge_rate": 0.0,
    "stt_rate_on_sell": 0.0,
    "sebi_charge_rate": 0.0,
    "gst_rate": 0.0,
    "stamp_duty_rate_on_buy": 0.0,
}


def open_repository(
    tmp_path: Any, name: str = "positional_options_test.db"
) -> tuple[Database, ExecutionRepository]:
    database = Database(tmp_path / name)
    MigrationRunner(database).run_pending()
    return database, ExecutionRepository(database)


def cycle_id_for(expiry: str = EXPIRY_DATE) -> str:
    return f"weekly_delta_neutral:{expiry}"


@dataclass
class ScriptedFeed(MarketDataFeed):
    """Replays ticks and zero-argument "mutate the world" callables in
    order — see this module's own docstring for why.

    Also tracks "now" as the most recently emitted tick's own
    ``exchange_time``, exposed via :meth:`clock` — pass that (not a single
    fixed instant) as ``build_engine``'s own ``clock=`` for any scenario
    whose ticks span more than a moment. Both ``GreeksService`` and
    ``OptionChainService`` stamp a fetched snapshot's freshness using
    *that* injected clock; a single instant fixed at, say, the entry tick's
    own time would make every later evaluation's chain/Greeks look stale by
    however far its own tick has advanced, and block risk-increasing
    decisions that should be allowed. Real production has no such gap: its
    clock is the real wall clock, which of course tracks itself.
    """

    steps: list[Tick | Callable[[], None]] = field(default_factory=list)
    initial_now: datetime = datetime(2026, 1, 1, tzinfo=IST)

    def __post_init__(self) -> None:
        MarketDataFeed.__init__(self)
        self._now = self.initial_now

    def clock(self) -> datetime:
        return self._now

    def run(self) -> None:
        self._running = True
        for step in self.steps:
            if not self._running:
                break
            if isinstance(step, Tick):
                self._now = step.exchange_time
                self._emit(step)
            else:
                step()
        self._running = False


#: A role's own entry side — the fixed convention every original leg opens
#: with (spec section 2/5.2). A leg's *closing* order always uses the
#: opposite side.
_ENTRY_SIDE = {
    "HEDGE_PUT": "BUY", "HEDGE_CALL": "BUY", "SHORT_PUT": "SELL", "SHORT_CALL": "SELL",
}


def closing_sequence_numbers(
    history_rows: list[Any], legs: list[Any]
) -> dict[str, int]:
    """One ``leg_id -> sequence_number`` entry per leg, using only that
    leg's *closing*-side order_intents row (the opposite side from its own
    entry) — never a bare max across every row for that leg.

    ``order_intents.sequence_number`` is a per-*session* counter (reset to 1
    at the start of every ``ExecutionRepository.open_session``, not a single
    monotonic counter across a cycle's entire cross-day life) — comparing
    raw sequence numbers across two different sessions (e.g. the entry
    session and a later restart's exit session) is meaningless. This
    exists so a same-session "shorts closed before hedges" proof never
    accidentally compares across sessions, and never accidentally reads an
    *entry* row's sequence number when it means to read the *close*.
    """
    result: dict[str, int] = {}
    for leg in legs:
        role = leg["leg_role"]
        closing_side = "SELL" if _ENTRY_SIDE.get(role) == "BUY" else "BUY"
        for row in history_rows:
            if row["leg_id"] == leg["leg_id"] and row["side"] == closing_side:
                result[leg["leg_id"]] = row["sequence_number"]
    return result
