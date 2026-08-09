# Strategy Requirement & Design Spec — `ema_cross_9_21_buy`

**NIFTY 5-minute EMA 9/21 crossover · ATM weekly options · BUY-only · intraday**

| Field | Value |
|---|---|
| Strategy id | `ema_cross_9_21_buy` |
| Delivers | Phase 9 (first real strategy) + Phase 10 (live enablement) |
| Engine | `trading_engine` (`common.engine.engine.TradingEngine`) |
| Strategy contract | `common.engine.strategy.BaseStrategy` |
| Mode at delivery | `paper` (live gate fail-closed; see §10) |
| Status | Draft — open decisions in §12 must be closed before implementation |
| Revision | Rev 2 — MTM daily cap, scrip-master sizing, 09:15 start, premium gap handling, per-trade exit reset |

> This document is a requirement + design spec, not the code. It records the
> behaviour the strategy must exhibit, the decisions already made, the decisions
> still open, and the exact existing modules each requirement maps to. Where a
> value is marked **(TBC)** it must be confirmed against the scrip master or a
> product decision before implementation, not hardcoded from this document.

---

## 1. Purpose & Scope

Build **one** complete, end-to-end intraday options strategy on the preserved
engine so that Phases 9 and 10 exercise the entire pipeline — signal generation,
option selection, order submission (paper), per-position and day-level risk,
square-off, and the live gate — through a single, well-understood strategy.

Additional strategies are explicitly **out of scope** until this one is complete
end to end (see §15). The 9/21 EMA crossover is chosen because it is simple to
reason about and easy to validate against a chart, so any failure surfaced during
forward testing is far more likely to be a pipeline defect than a strategy-logic
subtlety.

---

## 2. Strategy Summary

On NIFTY 5-minute closed candles, a **fresh** EMA(9)/EMA(21) crossover during the
current session generates a directional signal: a bullish cross buys an ATM
weekly **CE**, a bearish cross buys an ATM weekly **PE**. The strategy is
BUY-only (always long premium; direction is expressed by CE vs PE, never by
writing options), holds **one position at a time**, and **reverses** on the
opposite fresh crossover. Positions are managed out by an option-premium
`MOMENTUM_LOW_OR_HIGHEST_CLOSE` exit (4% activation, 8% best-close retracement),
by a reversal, by a **3% daily loss cap measured on live mark-to-market (realised
+ open unrealised P&L)**, or by the mandatory 15:15 square-off. New entries run
from **09:15** and stop at **14:45**.

| Parameter | Value | Configurable |
|---|---|---|
| Underlying | NIFTY | yes |
| Signal timeframe | 5 minutes, closed candles only | yes |
| Fast / slow EMA | 9 / 21 (on close) | yes |
| Option | ATM, weekly, CE (bullish) / PE (bearish) | yes |
| Direction | BUY only (long premium) | fixed by design |
| Concurrency | 1 position at a time | fixed by design |
| Reversal | Yes, on opposite fresh crossover | yes |
| Entry window | 09:15 → 14:45 | yes |
| Mandatory square-off | 15:15 | yes |
| Daily loss cap | 3% of capital base, on live MTM (realised + unrealised) **(base TBC, §12.1)** | yes |
| Premium exit | `momentum_low_or_highest_close`, 4% activation / 8% trail | yes |

---

## 3. Instrument & Option Selection

- **Underlying:** NIFTY spot/index, candles built on NSE index feed.
- **Traded instrument:** NIFTY **weekly** options on NFO.
- **Moneyness:** ATM (`Moneyness.ATM`), `steps: 0` — the strike nearest to spot at
  entry. Strike spacing is 50 for NIFTY.
- **Direction → option type:** bullish crossover ⇒ **CE**, bearish crossover ⇒
  **PE**. This choice is **per-signal** and rides on `StrategySignal.option_type`
  (`OptionType.CE` / `OptionType.PE`). It is **not** part of the static
  `OptionSelection` block — that block carries only moneyness / steps / expiry.
- **Side:** `OrderSide.BUY` on every entry.

**Sizing — split of responsibility:**

- **`lot_size` comes from the scrip master at runtime** (`common.market_data.scrip_master`),
  never from config. It is exchange-driven and changes; a hardcoded value silently
  goes stale and corrupts both order quantity and the daily-loss-cap math.
- **`lots_per_trade` comes from config** (it is a strategy sizing choice, not an
  exchange fact).
- **Order quantity = `lots_per_trade` (config) × `lot_size` (scrip master)**, resolved
  at entry. This same product scales the rupee thresholds handed to the daily guard.

The available weekly expiry (weekday, and which underlyings still list weeklies) is
likewise resolved from the scrip master / option chain at runtime, not hardcoded.
See §12.2.

---

## 4. Signal Generation

### 4.1 Indicators

Two stateful EMAs over candle **close**, periods 9 and 21, from
`common.indicators.ema.EMA`. Constraint: `ema_slow > ema_fast`. EMAs are
`SESSION_SPANNING` (`common.warmup.requirements.IndicatorScope`): prior-session
history is valid and desirable, because carrying state across the overnight gap
is what makes a mid-session start behave like a bot that ran from the open.

### 4.2 Crossover confirmation

Signals are derived from the EMA **spread** (`fast - slow`) via
`common.indicators.ema.ConfirmedCrossover`, which returns `+1` only on a newly
confirmed bullish crossover, `-1` only on a newly confirmed bearish crossover, and
`None` otherwise. Two configurable gates:

- `minimum_separation` — deadband the spread must clear before a flip counts.
  Default **0** (a pure sign-flip cross, matching "closed-candle 9/21 crossover"
  literally). A small non-zero value is an optional whipsaw guard.
- `confirmation_candles` — consecutive closed candles the new side must hold.
  Default **1** (act on the closing candle of the cross).

### 4.3 Intraday Fresh-Crossover Rule *(headline requirement)*

The strategy is **strictly intraday**. A trade is allowed **only** when a fresh
9/21 crossover occurs **during the current trading day**. A crossover
*relationship* inherited from the previous day must never, on its own, produce an
entry at the open.

**Required behaviour**

1. Previous-day EMA state may be used **only** for indicator warm-up / calculation
   continuity. It must **not** create an entry at market open.
2. At the start of each trading day, **crossover-detection state is reset** so the
   strategy waits for a *new* current-day relationship flip.
3. A valid **CE** entry occurs only after EMA9 first moves to/below EMA21 and
   *subsequently* produces a fresh bullish crossover **within the same session**.
   The mirror holds for **PE** (a fresh bearish crossover within the session).

**Design mapping — the single invariant to protect**

`ConfirmedCrossover` already encodes the anti-stale behaviour: the **first** spread
it sees after a reset only *establishes context* and returns `None` — it never
emits an entry. Therefore the rule reduces to one invariant:

> **The `ConfirmedCrossover` must be un-initialised at the first current-day
> candle, while the EMAs remain warm.**

Concretely:

- In `reset()` (the engine calls this at the start of each trading day), call
  `crossover.reset()`. **Do not reset the EMAs.**
- Warm-up may feed the EMAs but must **not** leave the crossover detector
  initialised with yesterday's side. Two acceptable implementations: (a) do not
  run warm-up candles through the detector at all; or (b) run them, then call
  `crossover.reset()` after warm-up and before the first live candle. Either way,
  the detector's first *current-day* update is context-only.

**Worked example (must hold in tests)**

| Situation | Required outcome |
|---|---|
| Prev day closes `EMA9 > EMA21`; new day opens `EMA9 > EMA21` | **No CE entry** — continuation is not a signal |
| Intraday: `EMA9` falls to/below `EMA21`, then crosses back above | **CE entry** on the confirmed bullish flip |
| Prev day closes `EMA9 < EMA21`; new day opens `EMA9 < EMA21` | **No PE entry** |
| Intraday: fresh bearish crossover | **PE entry** on the confirmed bearish flip |

### 4.4 Warm-up & session continuity

The strategy opts into warm-up via `warmup_spec()`
(`StrategyWarmupSpec.from_indicators([...])`) so the two EMAs are seeded from prior
sessions. Note the warm-up **manager** that replays history is a market-data
concern and may not be injected; with no manager the engine cold-starts and
`StrategyWarmupSpec.entry_blocked_by` gates entries on the cold seed. Behaviour
must be correct in both cases — warmed and cold. When cold, no entry may fire
until the 21-EMA is genuinely ready (≥ 21 closed 5-minute candles).

---

## 5. Entry Rules

An entry is emitted (`SignalAction.ENTER`) when **all** hold:

1. A **fresh** current-day confirmed crossover occurred (§4.3): `+1` ⇒ CE, `-1` ⇒ PE.
2. The current time is **within the entry window 09:15 → 14:45** (inclusive start at
   session open, no new entries after the cutoff). In practice the first 5-minute
   candle closes at 09:20 and is context-only under §4.3, so the earliest possible
   fill is later than 09:15; the explicit start simply bars any pre-open action and
   bounds the window.
3. **No position is open** (one-at-a-time), **or** the open position is on the
   opposite side and is being reversed (§9).
4. The day-level guard is **not halted** (loss cap / kill switch not tripped, §6.4).
5. Indicators are **ready** (warm-up satisfied or ≥ 21 bars seen).

Entry signal payload: `option_type = CE|PE`, `side = BUY`,
`option_selection = ATM/0/weekly` (or strategy default), `reason` set for logs.

---

## 6. Exit Rules

A position can be closed by four mechanisms. Their interaction and priority are in
§6.5; the reversal-vs-entry-cutoff interaction is in §9.

### 6.1 Premium exit — `MOMENTUM_LOW_OR_HIGHEST_CLOSE`

Maps **verbatim** to `common.exit.combined_candle_exit.CombinedCandleExit`
(registered `momentum_low_or_highest_close`). It is evaluated on the **traded
option's own premium candles**, driven by position **side** (a bought PE is "long"
in premium terms just like a bought CE). Requires `needs_option_candles = True` so
the engine builds the per-position premium stream and calls `on_option_candle`.

The position exits when **either** leg fires:

- **Best-close trail** — close retraces **8%** from the best completed close since
  entry, armed only after a **4%** favourable move from entry (activation gate).
- **Momentum break** — close breaks the previous completed premium candle's extreme
  against the position (for a long: `close < previous candle low`).

```yaml
exit:
  mode: momentum_low_or_highest_close
  trail_percentage: 8.0
  activation:
    enabled: true
    minimum_favourable_move_percentage: 4.0
```

> **Behavioural caveat (must be an explicit product decision):** the 4% activation
> gates **only the trailing leg**. The momentum-break leg has **no** activation
> gate and can fire as early as the *second* premium candle after entry (it needs
> one prior premium candle for the range comparison), well before +4%. This acts
> as a fast structural stop. If the intended model is "nothing happens until +4%,"
> this is not that — decide whether the early structural exit is wanted or whether
> the momentum leg should also be gated.

**Premium candle-gap behaviour (required).** When the traded option's tick stream
has a hole and one or more premium buckets are skipped, the engine calls
`on_option_candle_gap()` before rebuilding the premium stream. On that signal the
exit must:

- **Forget the momentum leg's previous-candle range reference** — the first premium
  candle *after* the gap must **not** be able to trigger a momentum-break exit, because
  its "previous candle" would sit on the far side of the hole and the range comparison
  would be meaningless. The momentum leg stays suppressed for exactly that first
  post-gap candle, then resumes normally.
- **Retain the best-close trail state** — `_extreme` / `_highest_close` /
  `_lowest_close` and the activation flag (`_activated`) are **kept**, because the
  trail measures favourable progress since entry and a data hole does not undo it.

This is the premium-side counterpart to the underlying `on_candle_gap` (§4.4), which
for this strategy is a no-op (EMAs are session-spanning and decay a hole out; no
`SESSION_LOCAL` indicator is used). `CombinedCandleExit` has no gap hook today, so this
is **new work**: add a gap-notify that sets a one-candle momentum-suppress flag while
leaving trail state untouched.

**Per-trade state reset (required).** All position-specific premium-exit state must be
cleared on **every** position close, so nothing leaks into the next trade: the trail
extreme (`_extreme`, `_highest_close`, `_lowest_close`), the activation flag
(`_activated`), the `momentum_fired` / `trail_fired` flags, and any gap-suppress flag.
`CombinedCandleExit.reset()` already clears this set; the requirement is to **call it on
every close** (via `on_position_closed`) — not only on open — and to re-arm cleanly on the
next entry. The one exception is a mid-trade process restart, where
`exit_state_snapshot()` / `restore_exit_state()` deliberately restore the *current*
open position's state; a genuinely new trade always starts clean.

### 6.2 Reversal on opposite crossover

When a position is open and a fresh **opposite** confirmed crossover occurs, the
strategy exits the current leg and enters the opposite one (CE⇄PE). This is handled
as a single "target side" signal: if flat, enter; if holding the opposite side,
exit-then-enter. Because the premium exit is premium-candle/tick driven and the
crossover is underlying-candle driven, the premium exit will usually act first when
it is going to — so by the time an opposite 5-minute candle closes, the position is
often already flat and the opposite cross is simply a fresh entry.

### 6.3 Mandatory square-off

All open positions are squared off at **15:15**, unconditionally. Engine/day-level
(`common.engine.square_off` / `common.risk.squareoff`), highest priority.

### 6.4 Daily loss cap (live MTM)

A **3%** daily loss cap halts new entries and squares off for the rest of the day.
The cap is measured on **live mark-to-market: realised P&L booked so far *plus* the
open position's unrealised P&L**, evaluated **on every option tick** — not on
realised P&L only. So the cap can trip mid-trade, before the losing position is
closed. Maps to `common.engine.daily_guard.DailyRiskGuard`.

Two evaluation paths, both required and both latching `halted`:

- `check_open_mtm(open_pnl)` — called on **every tick** for the open position;
  evaluates `realised_so_far + open_pnl` against `daily_max_loss`. This is what
  makes the cap MTM-based rather than realised-only. (Method already exists in
  `daily_guard.py`; the engine must be wired to call it each tick.)
- `register_trade(net_pnl)` — called on each **close** to book realised P&L and
  re-check.

> **Important:** `daily_max_loss` is an **absolute rupee** amount ("already scaled
> for size by the caller"), **not** a percentage. The strategy's config-building
> code must convert `daily_max_loss = 0.03 × capital_base`, where the capital base
> is an open decision (§12.1). Any per-lot amounts must be multiplied by
> `lots_per_trade × lot_size` before constructing the guard.

### 6.5 Exit priority order

When more than one condition could act on the same candle/tick, resolve in this
order (highest first):

| Priority | Mechanism | Scope |
|---|---|---|
| 1 | 15:15 mandatory square-off | day-level, unconditional |
| 2 | 3% daily loss cap (live MTM, per tick) / kill switch | day-level latch |
| 3 | Catastrophic hard stop (backstop, §7) | per-position, per-tick |
| 4 | `momentum_low_or_highest_close` premium exit | per-position, premium candle |
| 5 | Reversal on opposite crossover | underlying candle (exit leg) |

---

## 7. Risk Management

Two independent layers:

- **Per-position `RiskManager`** (`common.engine.risk`, abstract — a concrete one
  must be authored in Phase 9; none is ported). The spec's primary position
  management is the premium exit (§6.1), so the risk manager may be minimal.
  **Recommended:** implement at least a **catastrophic hard % stop** as a backstop,
  because the premium exit only re-evaluates at premium-candle closes and a fast
  adverse move between closes would otherwise be unmanaged. Loss is bounded (always
  long premium), but a hard floor is cheap insurance. A `risk_manager` **must** be
  supplied regardless — it is an abstract property — even if every threshold is set
  to the disabled token `none`.
- **Day-level `DailyRiskGuard`** (§6.4): 3% loss cap evaluated on **live MTM
  (realised + open unrealised P&L) every tick** via `check_open_mtm`, plus the
  realised re-check on close via `register_trade`; optionally a daily profit target,
  a max-trades-per-day limit, and an emergency kill switch (all on `DailyRiskConfig`,
  off by default unless configured).

---

## 8. Timing

| Event | Time | Field |
|---|---|---|
| Session entries allowed from | 09:15 | `risk.entry_start` |
| Session entries allowed until | 14:45 | `risk.entry_cutoff` |
| Mandatory square-off | 15:15 | `risk.square_off_at` |

All times are exchange-local (IST). No entry may open before **09:15** or after
**14:45** — this includes the entry leg of a reversal (§9). Exits remain active for
the whole session until square-off.

---

## 9. Position & Reversal Semantics

- **One position at a time.** No pyramiding, no simultaneous CE and PE.
- **Reversal** = atomic exit-then-enter on the opposite fresh crossover.
- **Reversal after the 14:45 cutoff degrades to exit-only:** the opposite cross may
  *close* the current position but must **not** open the opposite one, because new
  entries are barred after the cutoff.
- If the position was already closed by the premium exit before the opposite cross
  arrives, that cross is a **fresh entry** (subject to all §5 conditions), not a
  reversal — the same "target side" code path handles both.

---

## 10. Configuration (proposed)

```yaml
strategy_id: ema_cross_9_21_buy
enabled: true

# Paper only at delivery. Setting `live` will NOT start live: the broker factory
# consults the live gate and refuses; live order placement lands in Phase 10.
mode: paper
live_approved: false

engine: trading_engine

instrument:
  underlying: NIFTY
  exchange: NSE
  option_exchange: NFO
  strike_step: 50
  # lot_size is NOT configured here — it is resolved from the scrip master at
  # runtime (exchange-driven, changes). lots_per_trade is a config sizing choice.
  # Order quantity = lots_per_trade * lot_size(scrip master). See §3 / §12.2.
  lots_per_trade: 10

option_selection:
  moneyness: ATM
  steps: 0
  expiry: weekly        # nearest weekly, resolved from scrip master/option chain

trade_side: BUY

strategy:
  name: ema_cross_9_21_buy
  timeframe: 5m
  params:
    ema_fast: 9
    ema_slow: 21              # must be > ema_fast
    minimum_separation: 0     # deadband; 0 = pure sign-flip cross
    confirmation_candles: 1    # closed candles the new side must hold
    premium_candle_interval: 5m   # interval for the premium exit stream (TBC, §12.3)

risk:
  entry_start: "09:15"         # no entries before this (session open)
  entry_cutoff: "14:45"        # no new entries after this
  square_off_at: "15:15"       # unconditional square-off

  # Day-level guard. daily_max_loss is ABSOLUTE RUPEES; the strategy converts
  # from a percentage using capital_base (see §12.1). The cap is evaluated on
  # LIVE MTM (realised + open unrealised) every tick, not realised-only (§6.4).
  daily:
    capital_base: 0            # (TBC, §12.1) rupee base the 3% is measured against
    daily_max_loss_pct: 3.0    # -> daily_max_loss = 0.03 * capital_base
    evaluate_on: mtm           # realised + unrealised, per tick (check_open_mtm)
    daily_profit_target: none
    max_trades: 0              # 0 = unlimited
    kill_switch: false

  # Per-position backstop (see §7). Optional but recommended.
  risk_manager:
    name: hard_stop            # concrete RiskManager authored in Phase 9
    catastrophic_stop_pct: none   # e.g. 40 to floor a long-premium loss

exit:
  mode: momentum_low_or_highest_close
  trail_percentage: 8.0
  activation:
    enabled: true
    minimum_favourable_move_percentage: 4.0
  # Behavioural requirements enforced by the strategy/engine, not tunable here:
  #  - premium candle gap: suppress the momentum leg for the first post-gap
  #    candle, retain trail extreme + activation (§6.1 "Premium candle-gap").
  #  - reset ALL premium-exit state on every position close (§6.1 "Per-trade reset").

paper_execution:
  slippage:
    options:
      mode: ticks
      market_order_ticks: 1
  submission_latency_ms: 250
  tick_size: 0.05
  allow_ltp_fallback: true
  ltp_fallback_extra_ticks: 1
  max_quote_age_ms: 2000       # live feed: reject stale quotes (skeleton used null)
```

> Do **not** derive this config by copying `config/strategies/skeleton_fixture.yaml`
> — that file carries an explicit "do not use as a template" warning. Build the
> shape from the spec's section 6 and the modules referenced here.

---

## 11. Architecture Mapping

| Requirement | Existing module | New for Phase 9? |
|---|---|---|
| Strategy contract | `common.engine.strategy.BaseStrategy` | subclass (new) |
| EMA(9), EMA(21) | `common.indicators.ema.EMA` | reuse |
| Fresh-crossover detection | `common.indicators.ema.ConfirmedCrossover` | reuse |
| Premium candle stream | `needs_option_candles=True` → `on_option_candle` | wire (new) |
| Premium exit | `common.exit.combined_candle_exit` | reuse |
| Premium candle-gap suppress | `on_option_candle_gap` → exit gap-notify | **add gap hook to exit (new)** |
| Per-trade exit reset | `CombinedCandleExit.reset()` via `on_position_closed` | wire (new) |
| Daily 3% cap (live MTM) | `DailyRiskGuard.check_open_mtm` per tick + `register_trade` on close | wire + %→₹ (new) |
| Per-position backstop | `common.engine.risk.RiskManager` | **author concrete (new)** |
| Square-off / entry window | `common.engine.square_off`, `risk` config (09:15/14:45/15:15) | reuse |
| `lot_size` (runtime) | `common.market_data.scrip_master` | wire |
| `lots_per_trade` (config) × `lot_size` = quantity | strategy sizing | wire (new) |
| Weekly expiry selection | `scrip_master` / option chain | wire |
| Warm-up | `common.warmup.requirements.StrategyWarmupSpec` | wire |
| Signal payload | `StrategySignal(action, option_type, side, …)` | produce |

**Contract choice is settled by the spec:** premium candles, option selection, and
a per-position risk policy exist **only** on `BaseStrategy` (not the lighter
worker-seam `Strategy` protocol). Implement the strategy as a `BaseStrategy`
subclass registered via `@register_strategy("ema_cross_9_21_buy")`, with
`needs_option_candles = True`.

---

## 12. Open Decisions / To Be Confirmed

**12.1 Capital base for the 3% cap (blocking).** "3% daily loss cap" is
underspecified until the denominator is fixed. Decide: 3% of deployed capital? a
fixed notional? start-of-day equity? The config converts to the guard's absolute
rupee `daily_max_loss`. Recommend an explicit `capital_base` in config so the
percentage is auditable. Basis is settled: the cap is evaluated on **live MTM
(realised + open unrealised P&L) every tick** (§6.4), so a losing open position can
trip it before it closes.

**12.2 Sizing (settled).** `lot_size` is sourced from the scrip master at runtime;
`lots_per_trade` is config; quantity = `lots_per_trade × lot_size` (§3). No lot size
is hardcoded in config. Remaining check: confirm the scrip master returns the current
NIFTY lot size and the correct nearest weekly expiry (both exchange-driven and
recently churny) — this is a runtime-data check, not a config value to decide.

**12.3 Premium-candle interval for the exit.** An 8% retracement measured on
5-minute premium closes behaves very differently from 1-minute. Pick deliberately;
proposed default 5m, matching the signal timeframe.

**12.4 Momentum-leg activation.** Confirm whether the un-gated momentum-break leg
(§6.1 caveat) is wanted as a fast structural stop, or should also sit behind the 4%
activation.

**12.5 Catastrophic backstop stop.** Confirm whether to add a hard % floor in the
`RiskManager` (§7) and its level.

**12.6 Whipsaw guards.** Confirm defaults for `minimum_separation` (0?) and
`confirmation_candles` (1?). Non-zero values reduce entries in choppy sessions.

---

## 13. Edge Cases & Behavioural Notes

- **First current-day candle is never an entry** — it establishes crossover context
  (§4.3). Earliest possible fire is after a genuine intraday flip.
- **Cold start:** no entry until the 21-EMA is ready (≥ 21 bars). Warmed start:
  ready from the first candle, but still gated by the fresh-crossover rule.
- **Momentum leg needs history:** it cannot fire on the first premium candle after
  entry (no prior premium candle to compare); earliest is the second.
- **No entry before 09:15 or after 14:45**; earliest realistic fill is after 09:20
  (first candle closes) and is later still because that candle is context-only.
- **Reversal after 14:45:** exit-only (§9).
- **Daily cap tripped mid-trade:** because the cap is on live MTM, an open position
  whose unrealised loss pushes `realised + open` past −3% trips the halt and squares
  off **before** the trade closes — it does not wait for realised P&L.
- **Premium candle gap:** the first premium candle after a skipped bucket cannot fire
  the momentum-break leg (its "previous candle" is across the hole); the best-close
  trail extreme and activation flag survive the gap (§6.1).
- **Per-trade reset:** trade N's trail extreme / activation / fired flags / gap flag
  never influence trade N+1; state is cleared on every close (§6.1).
- **Gap-stitched underlying candle** (`on_candle_gap`): EMAs are `SESSION_SPANNING`
  and are **left alone** across a stitched gap (they decay the hole out); no VWAP or
  other `SESSION_LOCAL` indicator is used here, so there is nothing to reset.
- **Exact EMA tie** (`spread == 0`): treated as no-side by `ConfirmedCrossover`;
  cancels a pending candidate but keeps the last confirmed side.

---

## 14. Testing Considerations

- **Fresh-crossover rule:** the four rows of the §4.3 table, plus the reset
  invariant (warm EMAs + un-initialised detector at day open ⇒ no open-bar entry).
- **Reversal:** CE→PE and PE→CE, including the premium-exit-first case (opposite
  cross becomes a fresh entry) and the after-14:45 exit-only case.
- **Exit priority:** construct candles/ticks where two mechanisms fire together and
  assert the §6.5 order.
- **Daily cap (live MTM):** an open position's unrealised loss pushing
  `realised + open` past −3% trips the halt mid-trade (`check_open_mtm`), *and* a
  realised loss on close trips it (`register_trade`); verify the %→₹ conversion
  against `lots_per_trade × lot_size` (lot size from scrip master).
- **Timing:** no entry before 09:15 or after 14:45; unconditional square-off at 15:15.
- **Premium exit:** 4% activation arms the trail; 8% retracement from best close
  exits; momentum break exits pre-activation.
- **Premium candle gap:** first post-gap candle does not fire the momentum leg;
  trail extreme + activation are retained across the gap.
- **Per-trade reset:** open trade B after trade A closed in profit near a trailing
  extreme — assert B starts with no extreme, un-activated trail, and no residual
  fired/gap flags.
- **Warm vs cold start:** correct behaviour with and without an injected warm-up
  manager.
- Follow the repo's discipline: `ruff check`, `mypy` strict, and behaviour-level
  tests (not just line coverage).

---

## 15. Out of Scope

- Any second strategy — added only after this one is complete end to end.
- Option **writing/selling** (this strategy is BUY-only).
- Multiple simultaneous positions / pyramiding.
- Non-NIFTY underlyings; non-weekly expiries.
- Live order placement mechanics beyond wiring the Phase 10 live gate.

---

## Appendix A — Signal & model reference

- `StrategySignal(action, timestamp, option_type=None, side=BUY,
  option_selection=None, reason="", exit_reason=None)` — `option_type` (CE/PE) is
  required for `ENTER`, ignored for `EXIT`.
- `OptionSelection(moneyness=Moneyness.ATM, steps=0, …)` — static moneyness/steps.
- `OptionType.CE | OptionType.PE`; `OrderSide.BUY | OrderSide.SELL`.
- `SignalAction.ENTER | EXIT`.
- Exits read `position.side` (premium basis) and/or `position.contract.option_type`.
