# Morning Straddle — Exact Legacy Strategy Port to `algo_trading`

**Document status:** Implementation specification  
**Source of trading behavior:** `Trading_Automation` legacy `morning_straddle` strategy  
**Target platform:** `algo_trading`  
**Default execution mode:** Paper only  
**Live approval:** Disabled  

## 1. Purpose

Implement the existing `Trading_Automation` morning-straddle strategy in the
`algo_trading` repository without changing its trading decisions.

This is an architecture migration, not a strategy redesign:

- Strategy behavior, timing, filters, adjustment rules, risk formulas, and
  rule-evaluation order must match the executable legacy implementation.
- Authentication, market data, contract resolution, order routing,
  persistence, reconciliation, process supervision, configuration,
  observability, dashboards, testing, and live-safety controls must use the
  `algo_trading` architecture.
- The implementation must be generic enough to support future multi-leg
  strategies. Do not add morning-straddle-specific branches to the existing
  single-leg `TradingEngine`.
- All committed live-trading gates must remain disabled. Building and testing
  this strategy does not authorize live trading.

## 2. Authority and conflict resolution

When requirements conflict, apply the following precedence:

1. This document defines the required behavior of the port.
2. The executable legacy strategy and its tests are evidence for exact trading
   behavior.
3. Existing `algo_trading` architecture and safety invariants govern how that
   behavior is hosted.
4. Comments, dashboards, reports, or proposed enhancements do not override the
   executable legacy behavior.

If exact legacy behavior cannot be represented safely in the current
architecture, stop and report the conflict. Do not silently substitute a
different trading rule.

## 3. Non-negotiable parity boundary

The following changes are expressly out of scope unless separately approved in
a future specification revision:

- Do not change the strategy timeframe from 5 minutes to 1 minute.
- Do not add an expiry-day exclusion.
- Do not replace the static India VIX threshold with a percentile rule.
- Do not make missing VIX data block entry; the legacy rule is fail-open for
  this filter.
- Do not add an entry deadline or prohibit a late first entry after restart.
- Do not add a margin-availability entry filter.
- Do not add two-tick or multi-tick confirmation before adjustment.
- Do not close both legs when one leg doubles.
- Do not roll both legs or convert the position into a new straddle.
- Do not flatten the surviving leg merely because replacement of the adjusted
  leg is delayed or unavailable.
- Do not calculate risk triggers from net P&L after charges. Legacy risk uses
  gross P&L; charges are reporting data.
- Do not reorder the legacy risk checks.
- Do not rebase the profit target after an adjustment.
- Do not include the realized loss of an adjusted-out leg in the combined-stop
  calculation.
- Do not introduce strategy-specific logic into the existing generic
  `TradingEngine`.
- Do not enable live trading or alter any committed live gate.

Operational controls that prevent duplicate orders, reconcile uncertain broker
state, or fail closed when state cannot be reconstructed are architecture
safety controls, not trading-rule changes. They must not intentionally create a
different entry, adjustment, or exit decision when state is healthy.

## 4. Strategy identity and scope

| Field | Required value |
|---|---|
| Strategy ID | `straddle_920` |
| Strategy family | Intraday options |
| Underlying | NIFTY index |
| Position | Short ATM call plus short ATM put |
| Expiry | Nearest weekly expiry available from the authoritative scrip master |
| Direction | Sell both option legs |
| Lots per leg | 10 |
| Lot size | Resolved from the selected contract; never hardcoded |
| Timeframe | 5 minutes |
| Timezone | Asia/Kolkata |
| Overnight exposure | Prohibited |
| Warm-up | Not required; strategy is time/event based |
| Default mode | Paper |
| Default live approval | `false` |

Let:

- `L = 10` lots per leg;
- `lot_size` be the value resolved from the selected option contract;
- `Q = L × lot_size` be the quantity for each leg.

The strategy must not assume that NIFTY always has a particular lot size.

## 5. Trading calendar and session

- Use the configured exchange calendar and `MarketSession` facilities.
- Do not trade on weekends or configured exchange holidays.
- Session opens at 09:15 IST.
- New entries and adjustment re-entries are permitted through 15:00 IST.
- All remaining open legs must be squared off at or after 15:15 IST.
- Do not hold a position overnight.
- Expiry day is a valid trading day; there is no expiry-day skip rule.

## 6. Market-data requirements

### 6.1 Instruments

The strategy needs live data for:

1. NIFTY underlying index;
2. India VIX;
3. selected NIFTY call and put option contracts;
4. any replacement option selected after an adjustment.

### 6.2 Segment correctness

Subscriptions must carry the resolved exchange segment with each instrument:

- NIFTY index: index segment (`IDX_I`, numeric segment 0 in the current Dhan
  integration), using the authoritative index registry/security ID;
- India VIX: index segment, using the authoritative registry/security ID;
- NIFTY options: derivatives segment (`NSE_FNO`, numeric segment 2 in the
  current Dhan integration).

Do not rely on one adapter-wide default segment for this mixed subscription
set. The feed hub must group and apply subscriptions by segment and mode.

### 6.3 Fresh-tick rule

- Entry and replacement orders must wait for a fresh tick for the selected
  option contract.
- Do not use a stale cached option price to create a paper fill.
- Dynamic subscriptions must be applied even if the underlying stream becomes
  quiet; the design must not depend exclusively on a future unrelated tick to
  apply a pending subscription.
- Subscription state and data staleness must be observable.

## 7. Contract selection

At entry or replacement-selection time:

1. Read the current NIFTY underlying level.
2. Select the current ATM strike using the target repository's standard NIFTY
   strike-selection rule.
3. Select the nearest available weekly expiry from the current Dhan scrip
   master.
4. Resolve both CE and PE contracts for that strike and expiry.
5. Take security ID, exchange segment, expiry, strike, option type, and lot size
   from the resolved contract metadata.

The scrip master is authoritative. Do not hardcode an expiry, security ID, or
lot size.

For an adjusted-leg replacement, repeat this process at replacement-selection
time. The replacement uses the then-current ATM strike and the same option type
as the closed leg.

## 8. Daily state

The durable strategy state must represent at least:

- trading date;
- whether the one primary entry attempt has been consumed;
- whether the day is otherwise blocked from new primary entry;
- original opening CE entry premium;
- original opening PE entry premium;
- original opening combined-premium basis;
- each leg instance and its option type, contract, quantity, entry premium,
  state, and realized gross P&L;
- currently open legs and their current entry-premium bases;
- adjustment count;
- any leg awaiting closure;
- any pending replacement option type and lifecycle state;
- current basket identity;
- session square-off state;
- correlation and idempotency identifiers required to prevent duplicate
  orders.

The state must survive a worker restart. A process-local boolean alone is not
sufficient for one-attempt-per-day, adjustment-count, or replacement state.

## 9. Entry logic

### 9.1 Entry event

The primary entry evaluation occurs once, on the first completed NIFTY
underlying 5-minute candle handed to the strategy at or after 09:20 IST.

This is a time-based strategy. It has no indicator-history warm-up
requirement. Observability should record warm-up/context as `NOT_REQUIRED` or
an equivalent structured value rather than implying that EMA-like history was
validated.

### 9.2 One attempt per day

Before evaluating the entry filters, consume the day's primary entry attempt.
Consequences:

- If entry succeeds, no second primary basket may be opened that day.
- If VIX or news filtering rejects entry, do not retry later that day.
- If the process starts after 09:20 and the day's attempt was not previously
  consumed, the first completed candle delivered at or after that time may
  trigger the attempt. There is no separate legacy late-entry deadline.
- Restart or replay must not create another attempt after the durable state
  says it was consumed.

### 9.3 VIX filter

- If India VIX is available and `VIX > 20`, skip the day.
- If India VIX is available and `VIX <= 20`, the filter passes.
- If India VIX is unavailable, log/record the degraded filter state and
  continue without blocking entry.

The comparison is strictly greater than 20. A value of exactly 20 passes.

### 9.4 News blackout

- Maintain a manually configured list of blackout trading dates.
- If the current trading date is listed, skip the day.
- The primary attempt remains consumed; do not retry later that day.

### 9.5 Basket creation

If the entry attempt passes:

1. Resolve current ATM CE and PE contracts for the nearest weekly expiry.
2. Create one logical basket with two separately identifiable sell legs.
3. Request quantity `Q` for each leg.
4. Subscribe to both option contracts.
5. Place each paper order only after a fresh tick for that contract.
6. Persist intent, order, fill, leg, and basket correlation before considering
   that leg open.

The two legs are one logical basket but have independent execution lifecycle
records. Do not collapse them into one synthetic position.

### 9.6 Partial initial execution

Preserve the legacy outcome if only one opening leg receives a usable fresh
tick/fill:

- keep the filled leg open;
- leave the other leg pending until it can execute or the session reaches a
  point where new entry is no longer allowed;
- do not automatically flatten the filled leg merely because its partner has
  not yet filled;
- at hard square-off, close any filled exposure and cancel/terminally resolve
  the unfilled intent.

This behavior is paper-mode parity. Live enablement requires a separate review
of partial-execution risk and remains outside this specification.

## 10. Paper execution semantics

- Route through the repository's `PaperBroker` and standard execution
  lifecycle.
- For exact legacy parity, fill a sell leg from the first fresh usable option
  LTP with configured slippage equal to zero.
- Persist orders and fills through the shared repository; do not keep private
  strategy-only trade records.
- Use deterministic idempotency/correlation keys so replay or retry cannot
  duplicate an accepted order.
- Charges may be computed by the centralized, effective-date-aware charge
  model for reporting.
- Do not hardcode tax or fee rates in strategy code.
- Charges and net P&L must not feed the legacy risk-trigger calculations.

## 11. P&L definitions

For each open short leg `i`:

`open_pnl_i = (entry_premium_i - current_premium_i) × quantity_i`

Let:

- `U` = sum of gross unrealized P&L for all currently open legs;
- `R` = sum of gross realized P&L for all closed leg instances in the day's
  basket, including adjusted-out legs;
- `T = R + U` = total gross day/basket MTM used where stated below;
- `C` = recorded charges, used only for reporting;
- `net_pnl = T - C`, shown in reporting but not used for risk triggers.

## 12. Adjustment rule

### 12.1 Trigger

On every fresh option tick for an open sold leg, compare that leg's current
premium with its own entry premium.

Trigger when:

`current_premium >= 2 × entry_premium`

One tick satisfying the condition is sufficient. Do not require confirmation
on a second tick.

### 12.2 Daily limit

- Maximum adjustments per trading day: 1.
- Once the adjustment is triggered/consumed, disable further leg-doubling
  adjustments for the day.
- Ordinary stop, target, daily-loss, and time exits remain active.

### 12.3 Adjustment sequence

When an eligible leg doubles:

1. Mark the day's sole adjustment as consumed durably and idempotently.
2. Close only the triggered leg with exit reason `ADJUSTMENT`.
3. Keep the other leg open.
4. Record the option type of the closed leg as pending replacement.
5. On the next completed NIFTY 5-minute candle, resolve the then-current ATM
   contract of that same option type.
6. Subscribe to the replacement contract.
7. Sell the replacement only on a fresh option tick, subject to the 15:00 new
   entry/adjustment cutoff.
8. Attach the replacement as a new leg instance in the same logical basket.

If CE doubled, replace CE only. If PE doubled, replace PE only.

### 12.4 Delayed or unavailable replacement

If the replacement cannot be entered because no fresh tick arrives, contract
resolution fails, or the cutoff has passed:

- do not reopen the closed adjusted leg;
- do not automatically close the surviving leg;
- keep normal risk and hard-square-off checks active for remaining exposure;
- record the failed/expired replacement state explicitly.

## 13. Risk rules and exact evaluation order

Risk is evaluated on fresh option ticks. Preserve this order:

1. hard time square-off;
2. eligible leg-doubling adjustment;
3. daily maximum-loss check;
4. combined open-position stop-loss check;
5. profit-target check.

If a leg-doubling adjustment fires on a tick, process that adjustment before
the combined basket checks for that same tick, matching the legacy policy.
The remaining risk rules continue on subsequent ticks.

### 13.1 Hard square-off

At or after 15:15 IST:

- close every open leg;
- cancel or terminally resolve all pending entry/replacement intents;
- use an explicit time/square-off exit reason;
- prohibit further entry or adjustment for the day.

### 13.2 Daily maximum loss

Configured strategy capital base:

`capital_base = ₹20,00,000`

Maximum daily loss:

`daily_loss_limit = 3% × capital_base = ₹60,000`

Trigger when:

`T <= -₹60,000`

where `T = R + U` is gross P&L before charges.

On trigger, close every open leg and block further entry/adjustment for the
day.

### 13.3 Combined open-position stop-loss

Let the current open-premium basis be:

`B_current = Σ entry_premium_i`

for currently open sold leg instances only.

The loss allowance is:

`combined_stop_amount = 30% × Σ(entry_premium_i × quantity_i)`

For equal quantities this is equivalent to `30% × B_current × Q`.

Trigger when:

`U <= -combined_stop_amount`

Important parity details:

- This rule uses open unrealized P&L only.
- After an adjustment, it uses the retained leg's entry premium plus the new
  replacement leg's entry premium.
- It therefore re-bases when the replacement fills.
- Realized loss from the adjusted-out leg is not included in this rule.
- Charges are not included.

On trigger, close every currently open leg and block further entry/adjustment
for the day.

### 13.4 Profit target

At the original two-leg opening, capture permanently:

`B_original = original_CE_entry_premium + original_PE_entry_premium`

The target is:

`profit_target_amount = 50% × B_original × Q`

Trigger when:

`T >= profit_target_amount`

Important parity details:

- Use total gross P&L `T = R + U`.
- Preserve the original opening basis after an adjustment.
- Do not rebase the target to replacement premiums.
- Charges are not included.

On trigger, close every currently open leg and block further
entry/adjustment for the day.

## 14. Multi-leg architecture in `algo_trading`

### 14.1 Engine boundary

Introduce a generic sibling multi-leg execution/strategy host, for example
`MultiLegEngine`, following the same lifecycle and safety conventions as
`TradingEngine`.

Requirements:

- Do not widen the single-leg engine with `if strategy_id ==
  "straddle_920"` or equivalent branches.
- Express strategy decisions as generic basket/leg intents or signals.
- Keep broker order placement in the shared execution layer.
- Use the existing supervisor, worker, process locking, authentication,
  notifications, reconciliation, and account-risk facilities.
- Route by an explicit engine type declared in validated strategy
  configuration.
- Unsupported engine types must fail validation/construction; never fall back
  to the single-leg engine.

### 14.2 Generic domain concepts

The shared multi-leg layer must support at least:

- basket identity and lifecycle;
- independent leg-instance identity;
- option-type/role tag such as `CE` or `PE`;
- enter basket;
- enter one leg;
- exit one leg;
- exit all open legs;
- replace a leg while retaining basket identity;
- pending-subscription and pending-order state;
- exit reason;
- strategy, runtime, mode, and correlation identity on every record.

Names may follow existing repository conventions; semantics are mandatory.

### 14.3 Strategy module

Add a normal strategy package under the intraday-options strategy family.
The package should contain configuration, strategy decision logic, state
serialization/reconstruction, and focused tests. It must not construct a Dhan
SDK client or access SQLite directly.

## 15. Persistence and migrations

### 15.1 Durable model

Persist enough normalized information to answer:

- Which logical basket did an order/fill/position belong to?
- Which distinct leg instance was it?
- Was it an original leg or a replacement?
- What were its contract metadata, entry basis, exit reason, and gross/net
  results?
- Which primary attempt and adjustment number produced it?
- Is a replacement pending, filled, failed, expired, or cancelled?

### 15.2 Append-only leg trade history

The current position projection must not be the sole historical record.
Closing and reopening the same security on the same day must produce distinct
leg/trade instances without overwriting the earlier closed trade.

Integrate with the repository's existing trade-ledger/dashboard correction if
one is already present. Do not create a second competing source of truth.

### 15.3 Migration discipline

- Inspect the current migration head at implementation time and use the next
  available sequence number; do not assume a fixed migration number.
- Migration must be replay-safe and compatible with existing SQLite data.
- Validate checksum/ordering through the existing migration framework.
- Back up the operational database before applying a production migration.
- Do not edit an already-applied migration in place.

## 16. Restart, reconciliation, and uncertain state

On worker restart during the session:

1. Load durable strategy/basket/leg state.
2. Reconcile it with persisted orders/fills/positions and the broker/account
   view appropriate to the execution mode.
3. Reconstruct the consumed primary attempt, original premium basis, current
   leg bases, adjustment count, pending replacement, and exit state.
4. Resume without duplicating an entry, adjustment, replacement, or exit.

If the state is complete and internally consistent, adopt it and continue.

If exposure exists but the state required to manage it cannot be proven:

- fail conservatively;
- block new entries;
- use the existing controlled reconciliation/shutdown path to close recognized
  exposure where the architecture permits;
- emit a durable incident and operator notification;
- never guess an original premium basis or reset the adjustment count.

## 17. Configuration

Add validated configuration for the strategy using the repository's existing
configuration hierarchy. It must express at least:

- strategy ID and runtime group;
- engine type: multi-leg;
- enabled state;
- mode: paper;
- live approval: false;
- underlying: NIFTY;
- timeframe: 5 minutes;
- lots per leg: 10;
- VIX threshold: 20;
- entry evaluation time: 09:20 IST;
- last new-entry/adjustment time: 15:00 IST;
- hard square-off time: 15:15 IST;
- daily loss: 3% of ₹20,00,000;
- combined stop: 30% of current open entry-premium basis;
- profit target: 50% of original opening combined-premium basis;
- leg adjustment multiplier: 2.0;
- maximum adjustments per day: 1;
- news-blackout dates;
- zero paper slippage for parity.

Validate contradictory or unsafe values at load time. Configuration errors
must prevent the strategy from starting; do not silently substitute defaults.

## 18. Live-safety requirements

- Keep global live trading disabled in committed configuration.
- Keep strategy mode set to paper.
- Keep strategy live approval false.
- Do not bypass `LiveCallGuard`, broker factory routing, preflight, account-wide
  reservations, rate limiting, reconciliation, or controlled-live gates.
- Tests and diagnostics must not place a live order.
- Adding multi-leg support does not constitute operational approval for this
  strategy to trade live.
- Because exact legacy partial-fill behavior can leave one open leg, any future
  live proposal requires a separate written risk review and approval.

## 19. Observability, notifications, and dashboard

### 19.1 Structured events

Persist/emit structured events for at least:

- primary attempt consumed;
- VIX observed, VIX unavailable, or VIX blocked;
- news blackout;
- basket and contract selection;
- subscription requested/applied/stale;
- leg intent/order/fill/rejection;
- partial basket state;
- leg-doubling trigger;
- adjusted-leg close;
- replacement pending/selected/filled/failed/expired;
- daily-loss, combined-stop, profit-target, and hard-square-off triggers;
- restart adoption or reconciliation failure.

### 19.2 Telegram

Use the centralized notification facility. Notify important state transitions
without putting credentials or sensitive payloads in logs/messages.

### 19.3 Dashboard

Expose the strategy under Intraday Options and support the dashboard's
strategy selector. Show:

- one logical basket with drill-down to leg instances;
- original CE and PE legs plus any replacement;
- contract, strike, expiry, option type, quantity, entry/exit premium, state,
  and exit reason;
- gross realized, gross unrealized, charges, and net P&L as distinct values;
- original profit-target basis and current combined-stop basis;
- adjustment count and pending replacement state;
- orders, fills, signals/events, incidents, and health;
- paper/live mode labels.

The dashboard must remain read-only and must use the shared typed read-model
layer rather than inline SQL.

## 20. Required tests

### 20.1 Exact strategy acceptance matrix

At minimum, test all of the following:

| Scenario | Expected result |
|---|---|
| First completed 5-minute handoff at/after 09:20, VIX below 20, no blackout | Sell current ATM weekly CE and PE |
| VIX exactly 20 | Entry permitted |
| VIX above 20 | Skip entire day; no retry |
| VIX unavailable | Record warning/degraded filter and continue entry evaluation |
| News-blackout date | Skip entire day; no retry |
| Weekend or configured holiday | No entry |
| Expiry day | Entry remains permitted |
| Process begins after 09:20 with attempt unused | First eligible completed candle may consume/execute attempt |
| Restart after attempt consumed | No duplicate primary entry |
| CE reaches 2× own entry on one tick | Close CE only; retain PE; queue CE replacement |
| PE reaches 2× own entry on one tick | Close PE only; retain CE; queue PE replacement |
| Replacement-selection candle | Resolve then-current ATM of same option type |
| Replacement contract has no fresh tick | No stale-price fill; surviving leg stays open |
| First adjustment already consumed | No second leg-doubling adjustment |
| Adjustment and combined risk both appear eligible on same tick | Adjustment rule wins on that tick |
| Combined stop before adjustment | Uses currently open original-leg bases and open unrealized P&L |
| Combined stop after replacement | Uses retained plus replacement entry bases; excludes adjusted-out realized loss |
| Profit target after replacement | Uses original two-leg premium basis and total gross P&L |
| Daily loss after adjustment | Uses realized plus open gross P&L |
| Charges change net P&L | Risk trigger thresholds remain based on gross P&L |
| 15:00 cutoff reached before replacement | No replacement; surviving leg remains risk-managed |
| At/after 15:15 | Close every open leg and resolve all pending intents |
| Only one opening leg fills | Keep filled leg; other remains pending until cutoff; no automatic flatten |

### 20.2 Architecture and integration tests

Also test:

- multi-leg engine routing is generic and validated;
- existing EMA strategy continues to use the existing single-leg engine;
- the complete Rev 3.1 EMA acceptance matrix remains unchanged and passing;
- NIFTY/VIX subscriptions use the index segment and options use the F&O
  segment;
- dynamic option subscriptions are applied without waiting indefinitely for an
  unrelated tick;
- Dhan scrip-master resolution supplies expiry, strike, security ID, and lot
  size;
- basket, leg, order, fill, position, and trade-ledger correlation survives
  restart;
- reopening the same security produces a new append-only leg/trade instance;
- restart adopts complete state without duplicate orders;
- incomplete restart state blocks new entry and raises an incident;
- paper routing never invokes the live order API;
- every committed live gate remains false;
- dashboard strategy selection and leg drill-down are read-only;
- migration applies to a fresh database and upgrades a representative existing
  database safely.

### 20.3 Test-quality rules

- Do not weaken or delete existing tests.
- Add named replacements if restructuring a test is unavoidable.
- Use deterministic clocks and deterministic ticks.
- No unit/integration test may depend on current wall-clock trading hours.
- Any real-feed diagnostic must be bounded and read-only, with no order-client
  construction or live order surface.

## 21. Verification gates

Run targeted tests first, then the repository's full required gates, including:

```bash
pytest
ruff check .
mypy common strategies runtimes dashboards scripts
python -m scripts.assert_no_live_config_committed
```

Also run any migration, dashboard, end-to-end mixed-mode, feed-segment, and
read-only-boundary tests required by the current repository runbook.

Do not describe the implementation as complete if a gate fails. Clearly
separate newly introduced failures from verified pre-existing failures, and do
not weaken a test to obtain a green report.

## 22. Implementation workflow

Before editing:

1. Read this document and the current architecture/runbook in full.
2. Read the legacy morning-straddle implementation and tests in full.
3. Inspect the current migration head, execution repository, paper broker,
   strategy/engine factory, feed hub, option resolver, reconciliation, risk,
   dashboard read models, and live gates.
4. Grep all callers and tests affected by introducing an engine type,
   basket/leg identity, or new persistence fields.
5. Report any conflict that would change a trading rule before proceeding.

During implementation:

- make small, reviewable changes;
- preserve single-leg behavior;
- keep multi-leg abstractions generic;
- persist state before emitting irreversible effects where required for
  idempotency;
- keep all live gates fail-closed.

## 23. Required final report

The implementation report must contain:

1. **Root Cause / Architecture Gap** — why the existing single-leg path could
   not faithfully host this strategy.
2. **Legacy Parity Confirmation** — explicit confirmation of every
   non-negotiable rule in Section 3.
3. **Change Summary** — engine, domain, feeds, execution, persistence,
   reconciliation, configuration, dashboard, and notifications.
4. **Files and Migrations Changed** — including the actual migration number.
5. **Tests Changed** — new and modified tests with their intent.
6. **Strategy Acceptance Matrix** — every row in Section 20.1 with PASS/FAIL.
7. **Existing EMA Regression Matrix** — unchanged and passing.
8. **Regression Results** — targeted tests, full pytest, Ruff, mypy, migration
   gates, dashboard gates, and no-live-config assertion.
9. **Safety Confirmation** — paper routing only, no live order API called, and
   every committed live gate still disabled.
10. **Known Limitations** — especially exact legacy partial-execution behavior
    and any field the architecture cannot yet observe.

## 24. Definition of done

This port is done only when:

- the strategy makes the same decisions as the executable legacy strategy for
  all specified scenarios;
- it runs through generic `algo_trading` multi-leg architecture rather than
  legacy imports or a strategy-specific engine branch;
- market-data segments and dynamic subscriptions are correct;
- durable basket/leg state supports adjustment and restart without duplicate
  actions;
- append-only trade history preserves closed and replacement legs;
- paper execution, risk, persistence, reconciliation, notifications, and
  dashboard integration work end to end;
- the existing EMA strategy and its acceptance matrix are unchanged;
- the full verification suite passes;
- no code or configuration enables live trading.

---

**Plain-language contract:** copy the morning-straddle's trading brain exactly;
replace only the old plumbing with `algo_trading`'s safer, shared plumbing.
