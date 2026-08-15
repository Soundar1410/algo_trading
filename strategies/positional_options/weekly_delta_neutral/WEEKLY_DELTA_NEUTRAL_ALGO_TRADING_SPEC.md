# Weekly Delta-Neutral Strategy — `algo_trading` Implementation Specification

**Strategy ID:** `weekly_delta_neutral`  
**Runtime ID:** `positional_options`  
**Engine kind:** `positional_multi_leg_engine` (new generic engine kind)  
**Initial mode:** paper only  
**Status:** implementation specification  
**Legacy source of trading idea:** `Trading_Automation/weekly_strategies/Weekly_Strategies_Automations`  
**Target architecture:** current `algo_trading`, including the generic multi-leg durability, correlation, and restart-reconciliation work present on `strategy-straddle-920`

---

## 1. Purpose and authority

Implement the legacy weekly delta-neutral strategy in `algo_trading` while preserving the strategy's economic idea and replacing all legacy infrastructure with current `algo_trading` architecture.

This document is authoritative for the new implementation. The legacy repository is a behavioural reference only. It must not be imported, executed, queried, or copied structurally.

Where the legacy implementation conflicts with this document, this document wins. Where this document is silent, follow the established generic patterns in the current `algo_trading` branch rather than inventing a strategy-specific subsystem.

### 1.1 Required branch foundation

Implementation must begin from a commit that contains the completed generic multi-leg work from `strategy-straddle-920` (currently represented by commit `3215289` or its eventual merge descendant). Do not reimplement or fork that infrastructure.

Create a separate feature branch, for example:

```text
strategy-weekly-delta-neutral
```

Do not merge into `phase-10-controlled-live` or `main` unless separately requested.

### 1.2 Scope

In scope:

- a real multi-session positional-options runtime;
- one weekly NIFTY defined-risk delta-neutral strategy;
- contract and expiry resolution from current Dhan reference data;
- fresh full-mode option quotes and deterministic Greeks;
- safe four-leg entry, adjustment, exit, persistence, and recovery;
- paper execution, observability, notifications, and dashboard integration;
- configuration, migrations, tests, runbook, and operational scripts required for paper running.

Out of scope:

- enabling live trading;
- changing any global or runtime live gate;
- copying the legacy database, orchestrator, broker, dashboard, scheduler, login, or process layout;
- changing the rules of `straddle_920` or `ema_cross_9_21_buy`;
- weakening intraday square-off or restart behaviour;
- backtesting parameter optimization;
- automatic market-order fallback.

---

## 2. Strategy idea

The strategy opens a one-lot, defined-risk weekly NIFTY iron condor / hedged short strangle:

| Entry order | Role | Side | Option | Target delta |
|---:|---|---|---|---:|
| 1 | Long put hedge | BUY | OTM PE | -0.06 |
| 2 | Long call hedge | BUY | OTM CE | +0.06 |
| 3 | Short put | SELL | OTM PE | -0.20 |
| 4 | Short call | SELL | OTM CE | +0.20 |

The long hedges define the maximum loss. The strategy attempts to begin approximately delta neutral, monitors the entire basket across sessions, rolls the untested short when delta remains outside the adjustment band, and exits on profit, loss, risk, or actual expiry-day timing rules.

No short leg may exist without its corresponding protective long hedge.

---

## 3. Fixed trading rules

### 3.1 Instrument and size

- Underlying: NIFTY index.
- Product must support overnight carry. Never use an intraday-only product for this strategy.
- Initial paper quantity: one lot.
- Resolve lot size from the selected Dhan contract metadata at runtime.
- Never hard-code NIFTY lot size in strategy config, tests, or code.
- All four original legs must use the same resolved expiry and quantity.

### 3.2 Entry calendar

- Normal entry day: Wednesday.
- Entry window: 09:25 through 09:40 IST, inclusive at the start and exclusive after the end.
- At most one primary entry attempt per weekly cycle.
- If Wednesday is not a trading day, skip that weekly cycle. Do not silently shift entry to Thursday or another day.
- If the runtime starts after 09:40 on the entry Wednesday, do not enter that cycle.
- Do not re-enter an expiry after that expiry's cycle has completed, failed after creating exposure, or begun an exit.

### 3.3 Opening-stability filter

Measure the absolute percentage move from the session's first valid underlying reference price to the current underlying price.

- Maximum opening move: 0.80%.
- If exceeded during the normal entry window, delay entry evaluation by 20 minutes.
- If conditions remain unstable after 10:15 IST, consume/skip the cycle entry; do not retry later that day.
- The exact first-price source and its timestamp must be persisted or reconstructable deterministically.

### 3.4 Expiry resolution

- Expiry type: nearest eligible NIFTY weekly expiry strictly after entry.
- Tuesday is nominal only; never derive the active expiry from weekday arithmetic alone.
- Resolve available contracts from the current Dhan scrip master/reference data.
- Use `MarketSession.is_holiday()` / `is_trading_day()` for trading-session validation; do not maintain a second holiday algorithm.
- Select the exchange-published actual expiry and persist it as `resolved_expiry_date` before placing any entry order.
- On restart, reuse the persisted expiry. Never roll recovery into a later expiry.
- All “expiry-day” rules are evaluated against `resolved_expiry_date`. If the exchange shifts expiry to Monday, Monday receives the expiry-day controls.
- If expiry is ambiguous, absent, stale, or inconsistent between legs, block entry.

### 3.5 Contract selection

Short targets:

- call delta: +0.20;
- put delta: -0.20;
- per-side delta tolerance: 0.03.

Hedge targets:

- call delta: +0.06;
- put delta: -0.06;
- per-side delta tolerance: 0.03;
- hedge width from its corresponding short: 250–500 NIFTY points.

Select the complete four-leg candidate atomically. Candidate ranking must be deterministic and use, in order:

1. valid same expiry and required option type;
2. fresh, complete quote and Greek inputs;
3. liquidity and spread limits;
4. delta distance from the configured target;
5. hedge-width validity;
6. lowest absolute projected portfolio delta;
7. stable strike/security-ID tie-breaker.

Do not relax the filters when no candidate qualifies. Skip entry instead.

### 3.6 Quote and liquidity gates

Every entry or adjustment candidate requires a fresh Dhan Full-mode quote containing real bid/ask data.

- Maximum quote age: 5 seconds.
- Maximum sum of bid/ask spreads across all four entry legs: 8.0 points.
- Minimum volume for every leg: 1.
- Minimum open interest for every leg: 1.
- Reject crossed, non-positive, incomplete, stale, or untraded quotes.
- Do not synthesize a bid/ask spread from LTP.
- Do not progressively weaken spread, volume, OI, or delta criteria.

### 3.7 Credit and risk validation before entry

Let:

```text
initial_net_credit = total short entry proceeds
                   - total long entry cost
                   - all entry charges

put_width  = short_put_strike - long_put_strike
call_width = long_call_strike - short_call_strike
wing_width = max(put_width, call_width)

credit_to_width_ratio = initial_net_credit / (wing_width * total_quantity)

maximum_theoretical_loss =
    wing_width * total_quantity
    - initial_net_credit
    + estimated_exit_costs
```

Requirements:

- `initial_net_credit > 0`;
- `credit_to_width_ratio >= 0.18`;
- `abs(initial_net_delta_per_lot) <= 3.0`;
- sufficient configured allocated capital and margin headroom;
- estimated margin utilization <= 50% of allocated capital;
- maximum theoretical loss is finite, positive, persisted, and accepted by risk controls.

If any value cannot be computed from valid inputs, block entry.

---

## 4. Delta and Greeks contract

### 4.1 Delta units

Use conventional option delta:

- long call delta is positive;
- long put delta is negative;
- short exposure reverses the sign through the side multiplier.

```text
side_sign(BUY)  = +1
side_sign(SELL) = -1

signed_leg_delta = option_delta * side_sign * quantity
portfolio_delta = sum(signed_leg_delta for every open leg)
net_delta_per_lot = portfolio_delta / configured_lots
```

Persist the units and inputs used so delta cannot silently change meaning between entry, adjustment, dashboard, and recovery.

### 4.2 Greek source

Implement a central, generic option-Greeks service; do not embed Black–Scholes calculations inside the strategy.

Source priority:

1. current Dhan option-chain Greeks when the response is complete, correctly mapped, and fresh;
2. otherwise a centrally tested model using a vetted library and explicit inputs: spot, strike, option type, implied volatility, risk-free rate, dividend/carry assumption, evaluation timestamp, and time to the persisted actual expiry.

Requirements:

- every Greek has a source timestamp and maximum age;
- all four candidate legs use a consistent evaluation snapshot;
- time to expiry uses timezone-aware datetimes and the actual resolved expiry;
- rate and dividend assumptions are configuration/version controlled;
- invalid, stale, missing, or non-finite Greeks block entry and normal adjustment;
- unavailable Greeks must never block a risk-reducing exit.

Do not copy the legacy Greek calculator.

---

## 5. Entry execution state machine

### 5.1 Pre-effect durability

Before the first order, durably persist:

- `cycle_id`;
- strategy/runtime/mode;
- resolved actual expiry;
- entry-attempt consumption;
- all selected contracts, roles, sides, quantities, quote/Greek snapshot IDs;
- expected credit, maximum loss, capital and margin checks;
- an immutable correlation ID for every intended leg.

If this checkpoint fails, place no order.

### 5.2 Order sequence

1. Buy put hedge and confirm the fill.
2. Buy call hedge and confirm the fill.
3. Verify both hedges and their quantities through authoritative order/position state.
4. Sell put short and confirm the fill.
5. Sell call short and confirm the fill.
6. Recompute actual fill-based credit, delta, charges, maximum loss, and margin.
7. Persist the active cycle.

Sequential order placement is required for safety. The next unsafe step cannot begin based only on an acknowledgement; it requires confirmed/reconciled fill state.

### 5.3 Partial entry

- If a hedge fails, cancel unresolved entry intents and close any filled hedge safely; place no shorts.
- If one short fails after both hedges are filled, do not place additional risk. Reconcile all orders and either complete the intended protected structure within the bounded workflow or unwind every filled leg.
- A timeout or unknown response is not a failed order. Reconcile before retrying.
- Cancel an unresolved prior order and prove its terminal state before submitting a replacement.
- Never mark the cycle active until the intended four-leg exposure is confirmed.
- Any unmanageable or ambiguous exposure becomes a critical incident and blocks new entries.

---

## 6. Ongoing valuation and risk priority

### 6.1 Fill-based P&L

All P&L must include closed adjustment legs and charges:

```text
net_strategy_pnl =
    realized_pnl_all_closed_legs
    + unrealized_pnl_all_open_legs
    - all_entry_adjustment_exit_charges

credit_captured_percent =
    100 * net_strategy_pnl / initial_net_credit

loss_amount = max(0, -net_strategy_pnl)
```

`initial_net_credit` is captured once from actual original-entry fills. It must never be recomputed from only the currently open legs or rebased after an adjustment.

### 6.2 Exit and action priority

Evaluate in this order; the first applicable action wins:

1. operator/global emergency square-off;
2. unresolved broker exposure or reconciliation risk reduction;
3. emergency loss stop;
4. hard loss stop;
5. allocated-capital loss stop;
6. expiry hard exit;
7. expiry planned exit;
8. soft loss stop;
9. profit target;
10. margin-limit breach;
11. hedge repair required;
12. normal delta adjustment;
13. no action.

An exit condition always overrides an adjustment. Missing market data or Greeks must block risk-increasing actions, never exits.

### 6.3 Profit and loss thresholds

Initial paper configuration:

- allocated capital: ₹500,000;
- profit target: `net_strategy_pnl >= 0.55 * initial_net_credit`;
- soft stop: `loss_amount >= 1.25 * initial_net_credit`;
- hard stop: `loss_amount >= 1.50 * initial_net_credit`;
- emergency stop: `loss_amount >= 1.75 * initial_net_credit`;
- capital stop: `loss_amount >= 1% * allocated_capital` (₹5,000 with the initial configuration);
- maximum margin utilization: 50% of allocated capital.

These are legacy strategy parameters, not evidence of profitability. Keep them configurable and record them with each cycle so later config changes cannot rewrite history.

---

## 7. Delta adjustment

### 7.1 Trigger

- warning level: absolute net delta per lot >= 8;
- trigger level: absolute net delta per lot >= 12;
- require 3 consecutive valid checks at or beyond the trigger;
- reset the confirmation counter when delta returns below the trigger;
- target after adjustment: absolute projected net delta per lot <= 3;
- minimum 90 minutes between completed adjustment attempts;
- maximum 1 adjustment per trading day;
- maximum 3 adjustments per cycle;
- adjustments allowed from 09:25 through 14:45 on non-expiry sessions;
- no normal adjustment while an entry, exit, prior adjustment, reconciliation, or unknown order state is pending.

`adjustments_today` resets at the start of each trading date. `adjustments_this_cycle` persists for the entire cycle.

### 7.2 Direction

- Portfolio too negative: the untested side is the short put. Roll the short put upward.
- Portfolio too positive: the untested side is the short call. Roll the short call downward.

The replacement must:

- use the same persisted expiry;
- preserve or restore the protective hedge relationship;
- meet fresh quote, liquidity, spread, and Greek requirements;
- reduce projected absolute delta;
- preferably achieve `abs(projected net delta per lot) <= 3`;
- not increase maximum theoretical loss beyond configured risk approval.

If no safe candidate exists, skip adjustment and continue monitoring for exit. Do not loosen filters.

### 7.3 Adjustment state machine

Use the generic durable claim/submit/reconcile pattern established by the current multi-leg infrastructure:

1. durably claim the adjustment count and target leg;
2. confirm the applicable hedge is open and sufficient; repair it first if necessary;
3. close the old short;
4. reconcile the close to a confirmed terminal state;
5. wait for the next valid evaluation boundary;
6. submit the replacement short once;
7. reconcile its fill;
8. calculate and persist pre/post delta, realized adjustment P&L, charges, and new risk.

Never open the replacement while the old short's close is unknown. Never retry a successfully claimed action after restart. Unknown submission state blocks new risk and enters controlled reconciliation.

---

## 8. Expiry-day behaviour

Evaluate these rules on `resolved_expiry_date`, not on the word “Tuesday.”

- From 12:00 IST: tighten monitoring and do not make aggressive inward rolls.
- From 14:30 IST: no normal delta adjustment; exit instead when intervention is required.
- At 15:05 IST: begin planned complete exit.
- At 15:15 IST: hard complete-exit deadline.
- No leg may intentionally remain open past the resolved expiry session.

Exit order:

1. close short legs;
2. reconcile short closures;
3. close long hedges;
4. reconcile the account and mark the cycle complete only when flat.

`market_fallback_enabled: false` must not defeat the hard exit. Use bounded aggressive limit repricing from the freshest permitted quote/broker state, cancel and reconcile before retrying, and preserve idempotency. Do not automatically submit a market order unless that capability is separately specified, supported, tested, and approved.

If flattening cannot be proven, keep the cycle critical/unresolved, continue controlled reconciliation, alert the operator, and block new entries. Never mark it complete merely because retry limits were exhausted.

---

## 9. `algo_trading` architecture

### 9.1 Package and runtime layout

Use the repository's established boundaries:

```text
strategies/positional_options/weekly_delta_neutral/
    __init__.py
    strategy.py
    config.py
    models.py
    selection.py
    risk.py

runtimes/positional_options/
    __init__.py
    __main__.py
    config_adapter.py
    supervisor.py
    worker.py
    positional_multi_leg_engine_worker.py

config/runtimes/positional_options.yaml
config/strategies/weekly_delta_neutral.yaml
```

Files may be consolidated when a smaller design is clearer, but strategy rules must not leak into the runtime, broker, repository, or dashboard.

### 9.2 Generic engine boundary

The existing `MultiLegEngine` is intraday: it creates a date-scoped basket, resets strategy state at `_start_day()`, and invokes session square-off on stop/end-of-day. Do not reuse it unchanged for an overnight weekly position.

Add a generic `POSITIONAL_MULTI_LEG_ENGINE` member to `EngineKind` and implement a generic positional multi-leg engine or an equivalently explicit lifecycle policy that:

- holds positions across trading sessions;
- permits the process/feed to stop after a session without closing the basket;
- distinguishes “stop runtime” from “square off exposure”;
- reloads and reconciles the same cycle on the next session;
- retains original credit, legs, adjustments, and risk state across days;
- resets only daily counters at a verified new trading date;
- forces exit on the persisted expiry date;
- uses the generic multi-leg basket/leg models, correlation, execution gateway, broker factory, live guards, notifications, health, and reporting;
- introduces no branch for `weekly_delta_neutral` inside common engine code.

Existing intraday engine semantics and tests must remain unchanged.

### 9.3 Cycle identity

Resolve the repository's documented positional gap with a durable `cycle_id` rather than overloading `trading_date`.

Requirements:

- `cycle_id` is immutable and unique for one strategy/mode/underlying/resolved expiry attempt;
- orders, fills, positions, baskets, legs, adjustments, incidents, and dashboard rows can all be correlated to it;
- `trading_date` remains the date of each event/order and is not removed;
- one non-terminal cycle per `(runtime_id, strategy_id, execution_mode)`;
- one consumed/completed cycle per resolved expiry, preventing same-expiry re-entry;
- database migration is additive, versioned, indexed, and tested from empty and prior schema versions;
- do not edit an already-released migration in place;
- migration backup, checksum, and rollback/startup validation follow current Phase 10 rules.

### 9.4 Persistence projections

Reuse and extend the generic `strategy_baskets` / `strategy_legs` design. Add only the missing generic fields/tables needed for cross-day lifecycle, for example:

- cycle identity and state;
- resolved expiry and entry attempt state;
- immutable original credit/risk basis;
- per-day and per-cycle adjustment counters;
- adjustment event ledger;
- Greek/quote decision snapshots or references;
- reconciliation and exit state.

The order/fill/position ledger remains authoritative for trading effects. Basket and leg rows are recoverable projections. Critical pre-effect claims must persist before action; post-effect projection failures must generate incidents and be repaired by reconciliation, not pretend the trade did not occur.

### 9.5 Restart reconciliation

On every worker start, before new entry or adjustment:

1. load the one non-terminal cycle, if present;
2. load its intended and projected legs;
3. cross-check order intents, orders, fills, and authoritative positions;
4. query the paper/live broker source appropriate to the strategy mode;
5. rebuild derivable projection state;
6. adopt only an exact, manageable match;
7. subscribe to every open contract plus required underlying/option-chain instruments;
8. block and raise a critical incident for unknown, duplicate, naked, wrong-expiry, wrong-side, wrong-quantity, or otherwise unmanageable exposure.

Recovery may correct a projection when authoritative data proves the truth. It must never manufacture a fill, silently adopt unknown positions, switch expiry, or repeat a durably consumed order/adjustment.

### 9.6 Market data

- Reuse `MarketDataInstrument` and grouped segment/mode subscriptions.
- NIFTY underlying uses the correct index segment.
- Option contracts use their resolved derivatives segment.
- Full mode is required for option entry and adjustment quotes.
- Dynamic subscriptions must be applied without waiting indefinitely for an unrelated tick.
- Enforce staleness independently for underlying, option quote, option chain/Greeks, and position marks.
- Market-data degradation blocks new/adjustment risk but keeps exits and reconciliation active.

### 9.7 Execution and live safety

Route all orders through the current broker factory, `ExecutionGateway` / `OrderLifecycle`, repository, correlation IDs, account reservations, and account-wide risk controls.

- Strategy config defaults: `enabled: false`, `mode: paper`, `live_approved: false`.
- Positional runtime defaults: `enabled: false`, `live_execution_allowed: false`.
- Global `live_trading_enabled` remains false.
- No strategy code may call Dhan directly.
- No dashboard or diagnostic may import an order-capable surface.
- Do not modify or bypass `LiveCallGuard`.
- Paper and live data remain strictly separated.

---

## 10. Configuration contract

The final YAML must be validated through typed models. Unknown keys and contradictory values fail at load time. This is an illustrative required shape, not permission to bypass the repository's config adapter:

```yaml
strategy_id: weekly_delta_neutral
enabled: false
mode: paper
live_approved: false
engine: positional_multi_leg_engine
expiry_policy: force_square_off_before_expiry
square_off_before_expiry_days: 0

parameters:
  underlying: NIFTY
  lots: 1
  timeframe: 5m
  allocated_capital: 500000.0

  schedule:
    entry_day: WEDNESDAY
    entry_window_start: "09:25"
    entry_window_end: "09:40"
    adjustment_start: "09:25"
    adjustment_end: "14:45"
    expiry_adjustment_cutoff: "14:30"
    planned_exit: "15:05"
    hard_exit: "15:15"

  selection:
    short_call_delta: 0.20
    short_put_delta: -0.20
    short_delta_tolerance: 0.03
    hedge_call_delta: 0.06
    hedge_put_delta: -0.06
    hedge_delta_tolerance: 0.03
    minimum_hedge_width_points: 250
    maximum_hedge_width_points: 500
    maximum_entry_delta_per_lot: 3.0
    maximum_combined_spread_points: 8.0
    minimum_volume: 1
    minimum_open_interest: 1
    quote_max_age_seconds: 5
    minimum_credit_to_width_ratio: 0.18

  opening_filter:
    maximum_move_percent: 0.80
    delay_minutes: 20
    skip_after: "10:15"

  adjustment:
    warning_delta_per_lot: 8.0
    trigger_delta_per_lot: 12.0
    confirmation_checks: 3
    target_delta_per_lot: 3.0
    minimum_minutes_between: 90
    maximum_per_day: 1
    maximum_per_cycle: 3

  exits:
    profit_credit_capture_percent: 55.0
    soft_loss_credit_multiple: 1.25
    hard_loss_credit_multiple: 1.50
    emergency_loss_credit_multiple: 1.75
    maximum_cycle_loss_capital_percent: 1.0
    maximum_margin_utilization_percent: 50.0

  execution:
    order_type: LIMIT
    market_fallback_enabled: false
    persist_before_submit: true
    cancel_before_retry: true
```

There must be no `lot_size` key. Resolve it from contract metadata.

Config validation must reject at least:

- live mode without the complete existing live-approval chain;
- zero/negative capital, lots, widths, timeouts, or loss limits;
- hedge delta not farther OTM than its short delta;
- minimum hedge width greater than maximum;
- inconsistent time ordering;
- adjustment target not below trigger;
- soft/hard/emergency loss multiples not strictly increasing;
- entry or adjustment schedule after the hard exit;
- any attempt to use an intraday product for an overnight position.

---

## 11. Dashboard and operations

Activate the existing Positional Options section at `http://localhost:8501/` from real read-only data. Preserve the established dashboard architecture: typed read models, bounded read-only queries, no inline SQL in pages, no database writes, and strategy selection when multiple positional strategies exist.

Required views:

- overview: cycle state, expiry, original credit, net P&L, delta, margin, next action;
- live positions: all legs, roles, strike, type, side, quantity, fills, marks, leg P&L;
- orders and fills;
- closed cycles/trades;
- performance by strategy and date range;
- strategy comparison;
- signals, adjustment events, reconciliation events, notifications, and errors;
- health: runtime, feed, quote/Greek freshness, broker, DB, recovery, live-gate state.

Unavailable fields must display `—` with a clear reason. Do not fabricate marks, Greeks, fills, P&L, or health.

Add/update operational commands consistently with existing scripts:

```text
scripts.start_strategy weekly_delta_neutral
scripts.stop_runtime --runtime-id positional_options
scripts.status --runtime-id positional_options
scripts.validate_environment --runtime-id positional_options
```

Starting the positional runtime must not stop, restart, or interfere with the intraday-options runtime. Account-wide risk and live-order coordination must still aggregate across runtime groups.

---

## 12. Notifications and incidents

Use the existing notifier and persistent incident/error paths. Notify at minimum:

- cycle entry attempted/blocked/completed;
- each leg fill or unresolved order;
- partial-entry unwind;
- adjustment warning, claim, old-leg close, replacement fill, failure;
- profit/stop/margin/expiry exit trigger;
- planned and hard exit progress;
- restart adoption or reconciliation mismatch;
- stale feed, stale Greeks, persistence failure, unknown exposure;
- final cycle summary.

Redact credentials and broker-sensitive payloads. Notification failure must never undo or block a safety exit.

---

## 13. Required acceptance matrix

### 13.1 Entry and selection

- Wednesday 09:25 valid market/chain -> protected four-leg paper entry.
- Before window, after window, non-Wednesday -> no entry.
- Wednesday holiday -> cycle skipped, no Thursday shift.
- Runtime restart inside window after attempt consumed -> no duplicate entry.
- Actual holiday-shifted expiry is selected and persisted.
- Stale/ambiguous scrip master or expiry -> blocked.
- Hard-coded or mismatched lot size -> refused.
- Missing/stale/crossed Full-mode quote -> blocked.
- Missing/invalid Greeks -> blocked.
- Wide spread, low OI/volume, invalid width, low credit ratio -> blocked without relaxation.
- Entry delta outside tolerance -> blocked.
- Same-expiry completed/consumed cycle -> no re-entry.

### 13.2 Execution safety

- Hedges fill before either short is submitted.
- Hedge failure -> no shorts.
- One short fails -> bounded protected completion or complete unwind.
- Timeout/unknown response -> reconcile before retry.
- Critical pre-effect persistence failure -> no order.
- Post-fill projection failure -> incident plus successful recovery from authoritative ledgers.
- Every order/fill/position has strategy, cycle, basket, leg, mode, and correlation identity.

### 13.3 Adjustment

- Three consecutive trigger checks are required.
- Counter resets inside the band.
- Negative delta rolls the put upward; positive delta rolls the call downward.
- Replacement reduces projected absolute delta and uses same expiry.
- Old short confirmed closed before replacement.
- Unknown old close -> no replacement and entry block.
- Hedge deficiency -> repair before short replacement.
- Maximum one/day, three/cycle, and 90-minute interval survive restart.
- Daily count resets next trading day; cycle count does not.
- No adjustment after an exit trigger or expiry cutoff.

### 13.4 P&L and exits

- Profit target includes realized adjustment P&L and all charges.
- Initial credit never rebases after adjustment.
- Soft, hard, emergency, capital, and margin exits trigger at exact boundaries.
- Every exit closes shorts before hedges.
- Missing Greeks/quotes cannot suppress emergency/hard/expiry exit.
- Planned exit begins at 15:05 on actual expiry day.
- Hard exit begins at 15:15 and is not disabled by market fallback config.
- Incomplete flattening stays unresolved/critical; it is never reported complete.

### 13.5 Multi-day and recovery

- Normal process stop after Wednesday session preserves exposure.
- Thursday restart adopts the exact Wednesday cycle without new orders.
- Weekend and configured holiday gaps preserve state.
- Actual expiry-day rules follow persisted expiry, including Monday holiday-shift case.
- Crash at every claim/submit/fill/persist boundary is idempotently recoverable.
- Unknown, duplicate, wrong-side, wrong-quantity, wrong-expiry, orphan, or naked exposure fails closed.
- No non-terminal cycle permits another entry.

### 13.6 Regression and safety

- Existing `straddle_920` acceptance matrix remains unchanged and passing.
- Existing EMA Rev 3.1 acceptance matrix remains unchanged and passing.
- Intraday runtimes still square off exactly as before.
- Paper/live routing and all committed live gates remain unchanged/fail-closed.
- Dashboard remains recursively read-only.
- Starting/stopping positional runtime does not disturb intraday runtime.

---

## 14. Verification gates

Run targeted tests first, then the repository gates:

```bash
pytest
ruff check .
mypy common strategies runtimes dashboards scripts --strict
python -m scripts.assert_no_live_config_committed
```

Also run bounded paper-mode integration tests with deterministic clocks across at least:

- Wednesday entry;
- next-session restart;
- weekend/holiday restart;
- adjustment and crash recovery;
- actual expiry-day planned/hard exit;
- simultaneous intraday and positional runtime status/account-risk aggregation.

No test may contact an order API. Any real market-data smoke test must be bounded, read-only, explicit, credential-safe, and separately reported.

---

## 15. Explicit legacy exclusions

Do not copy from `Trading_Automation`:

- Python imports or packages;
- its database schema or DB access layer;
- scheduler, orchestrator, login, process, dashboard, or notification structure;
- hard-coded `lot_size: 75`;
- synthetic bid/ask spread or LTP-as-quote fallback;
- relaxed liquidity filters;
- active-leg-only credit/P&L calculations;
- weekday-only expiry assumptions;
- daily-reset lifecycle for an overnight position;
- unsafe order retry or market fallback behaviour;
- any claim that a submitted order is filled without authoritative reconciliation.

Only the economic strategy idea and the numeric starting parameters in Sections 2–8 are carried forward.

---

## 16. Definition of done

The implementation is complete only when:

1. the real `positional_options` supervisor/worker can run `weekly_delta_neutral` in paper mode;
2. a protected four-leg cycle can survive process and trading-day boundaries under one durable `cycle_id`;
3. entry, adjustment, exit, and recovery are fill-based, correlated, idempotent, and fail-closed;
4. actual expiry and current lot size come from authoritative contract data;
5. real fresh option bid/ask and deterministic Greeks drive risk-increasing decisions;
6. the Positional Options dashboard displays genuine read-only cycle data with a strategy selector;
7. every acceptance row above passes without weakening existing tests;
8. full pytest, Ruff, strict mypy, and no-live-config gates pass;
9. configs remain disabled and paper-only;
10. no live order API was called and no Phase 10 live gate was enabled.

The final implementation report must use: **Root Cause / Architecture Change / Strategy Rules Implemented / Persistence and Recovery / Tests Changed / Acceptance Matrix / Regression / Safety Confirmation / Known Limitations / Commit and Branch**.
