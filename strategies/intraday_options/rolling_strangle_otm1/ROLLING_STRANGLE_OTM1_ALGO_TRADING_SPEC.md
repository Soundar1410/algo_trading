# Rolling Strangle OTM1 — `algo_trading` Implementation Specification

**Strategy ID:** `rolling_strangle_otm1`  
**Display name:** Rolling Strangle OTM1 — NIFTY One-Strike OTM  
**Legacy source:** `Soundar1410/Trading_Automation`, under `option_strategies/Trading_Strategies_Automation_v2/strategies/points_rolling_strangle/`  
**Target repository:** `Soundar1410/algo_trading`  
**Initial operating mode:** PAPER only, disabled by default  
**Document status:** Implementation-ready strategy and architecture specification

## 1. Objective

Port the actual trading behavior of the legacy `points_rolling_strangle`
strategy into the current `algo_trading` architecture.

“Same strategy” means preserving the executable legacy entry, option selection,
rolling, risk, and daily-reset behavior, except for the operator-approved 15:15
CAS exit in section 3.4. It does **not** mean copying
legacy framework code, imports, authentication, process control, persistence,
broker, notification, or dashboard implementations.

The port must use the current platform’s generic multi-leg runtime, execution,
reconciliation, market-data, configuration, observability, and safety controls.
Do not add `rolling_strangle_otm1`-specific branches to a common engine,
runtime supervisor, broker, repository, dashboard, auto-start controller, or
live gate.

## 2. Naming

Use `rolling_strangle_otm1` consistently in the target repository. The legacy
folder name remains provenance only.

| Concern | Required value |
|---|---|
| Strategy ID | `rolling_strangle_otm1` |
| Python package | `strategies/intraday_options/rolling_strangle_otm1/` |
| Configuration | `config/strategies/rolling_strangle_otm1.yaml` |
| Suggested class | `RollingStrangleOtm1Strategy` |
| Runtime | `intraday_options` |
| Engine | Generic `multi_leg_engine` |

Do not retain `points_rolling_strangle` or the legacy registry alias
`rolling_otm_strangle` as production identifiers. Historical reports may
mention them only as legacy provenance.

## 3. Authority and Conflict Resolution

### 3.1 Legacy sources reviewed

Trading behavior was extracted from:

- `strategy.py`
- `trade_manager.py`
- `config/config.yaml`
- `tests/test_strategy.py`
- `scripts/run_paper_sim.py`
- `app.py` and `main.py` for integration context
- `Rolling_OTM_Strangle_Strategy.md` for supplementary prose

### 3.2 Precedence

If a conflict remains, use this order:

1. This specification, once operator-approved.
2. Executable legacy strategy code, committed config, and tests together.
3. Current `algo_trading` architecture and fail-closed safety invariants.
4. Legacy prose, reports, comments, and dashboards.

Do not silently choose between conflicting requirements. Stop and report any
new conflict that would change a trading decision.

### 3.3 Conflicts already resolved in this document

The legacy prose is stale in one strategy-rule field:

- It describes one maximum roll, while the committed executable configuration
  and tests define two rolls per side.

**Resolution:** implement two rolls per side.

### 3.4 Operator-approved CAS timing deviation

The executable legacy configuration uses a 15:20 square-off. The target
strategy intentionally changes this to **15:15 IST** because of the applicable
CAS restriction. This is an explicit operator-approved trading-lifecycle
change, not an accidental parity difference.

The new-entry/replacement cutoff remains **strictly before 15:10**, providing a
five-minute buffer before mandatory flattening. All strategy documentation,
configuration, tests, dashboard labels, and final reporting must describe
15:15; do not copy the legacy 15:20 value into the target implementation.

## 4. Strategy Identity

| Field | Required value |
|---|---|
| Family | Intraday options |
| Underlying | NIFTY index |
| Structure | Short OTM CE + short OTM PE |
| Direction | SELL-only option legs |
| Signal timeframe | Completed 5-minute NIFTY candles |
| Entry time | First eligible completed-candle decision at or after 09:45 IST |
| New entry / replacement cutoff | Strictly before 15:10 IST |
| Hard square-off | 15:15 IST |
| Expiry | Nearest available weekly expiry |
| Initial distance | 50 points OTM on each side |
| Roll trigger | Absolute NIFTY move of 60 points, inclusive |
| Default roll mode | Threatened leg only |
| Roll budget | 2 CE rolls and 2 PE rolls, independently |
| Lots | 10 per leg |
| Lot size | Resolved from the selected contract; never hardcoded |
| Combined stop | ₹2,000 per lot = ₹20,000 for 10 lots, inclusive |
| Overnight exposure | Prohibited |
| Indicator warm-up | None; time/price-event strategy |
| Default execution | PAPER, disabled |

Let:

- `L = 10` lots per leg;
- `lot_size` be authoritative metadata on the selected contract;
- `Q = L × lot_size` be each leg’s order quantity;
- `D = 50` points be the configured OTM distance;
- `R = 60` points be the configured rolling trigger;
- `SL_per_lot = ₹2,000`;
- `SL_total = L × SL_per_lot = ₹20,000`.

The strategy must not assume NIFTY lot size is permanently 65.

## 5. Trading Calendar, Clock, and Candle Semantics

- Time zone: `Asia/Kolkata`.
- Session: NSE, using `MarketSession` and the configured exchange holidays.
- No trading on weekends or configured exchange holidays.
- Underlying session begins at 09:15 IST.
- Entry/roll decisions use **completed** 5-minute NIFTY candles only.
- The decision timestamp is the timestamp supplied by the engine when that
  completed candle is handed to `on_candle`.
- No intrabar primary entry or roll decision is permitted.
- Primary entry becomes eligible at exactly 09:45.
- A new/replacement option leg is permitted only while `time < 15:10`.
  Exactly 15:10 is blocked.
- Every remaining open leg must be closed at or after 15:15.
- Hard square-off must remain available even when selection, quotes,
  persistence, or strategy state is degraded.
- No leg may be carried overnight.

Boundary tests must pin 09:44:59/09:45:00, 15:09:59/15:10:00, and
15:14:59/15:15:00 under an injected deterministic clock.

### 5.1 Holiday ownership

Ship a strategy-scoped, verified NSE holiday list in `parameters.holidays`,
consistent with the current repository convention. Document that it is annual
operational data requiring review before each calendar year. Do not silently
assume `config/global.yaml` supplies the strategy engine’s calendar unless the
actual loader wiring proves that behavior.

## 6. Market Data and Contract Resolution

### 6.1 Required instruments

The runtime needs:

1. NIFTY index ticks for spot and 5-minute candles;
2. selected CE and PE contract ticks for fresh paper fills and live MTM;
3. every replacement contract selected after a roll.

India VIX, Greeks, option-chain delta selection, and a news API are not inputs
to this strategy.

### 6.2 Segment correctness

- NIFTY index must use its authoritative index segment/security ID.
- NIFTY options must use each resolved contract’s derivatives segment/security
  ID.
- Dynamic subscriptions must preserve segment and mode.
- Never rely on a single adapter-wide exchange-segment default.

### 6.3 Fresh-tick rule

- A leg may fill only after a fresh tick for the exact selected contract.
- Do not create a fill from a cached/stale option price.
- Subscription requests must be observable and bounded.
- A missing or timed-out fresh quote must not fabricate an entry.
- An exit must remain fail-conservative: an unresolved close is exposure, not
  a successful flat state.

### 6.4 Expiry, strike, and quantity

At worker construction, select the nearest available weekly expiry using the
authoritative Dhan scrip master/resolver. Consistent with the current resolver,
expiry is fixed for that worker session and refreshes on the next normal daily
worker restart; do not claim it is reselected on every roll if production code
does not do that.

At every primary entry and roll replacement:

1. Use the current completed NIFTY candle close as the selection spot.
2. Resolve the current ATM strike with the platform’s standard NIFTY rounding.
3. Convert point distance to OTM strike steps:
   `steps = max(1, round(otm_distance / strike_step))`.
4. For CE, select `steps` strikes above ATM.
5. For PE, select `steps` strikes below ATM.
6. Resolve security ID, strike, option type, segment, expiry, and lot size from
   the current scrip master.
7. Set quantity to `10 × resolved lot_size` independently for each selected
   contract.
8. If selected CE and PE contract lot sizes disagree, fail closed before the
   primary basket is opened.

The default `50 / 50` produces one OTM strike step. Preserve Python’s existing
`round` behavior and the minimum-one-step floor unless a future specification
explicitly changes the trading rule. Add tests for exact, non-multiple,
half-step, and sub-one-step distances.

Never hardcode expiry, security IDs, strikes, or lot size.

## 7. Daily State

At the start of each trading day, the strategy begins with:

- primary entry not yet consumed;
- no reference spot;
- `ce_roll_count = 0`;
- `pe_roll_count = 0`;
- no pending roll side(s);
- no replacement awaiting the next candle;
- time-exit not completed;
- no open basket, unless restart recovery proves one exists.

These fields are trading-critical. Once a basket or roll exists, they must be
durable and restart-recoverable. Do not keep them only in private in-memory
strategy attributes.

## 8. Primary Entry

On the first eligible completed NIFTY candle whose decision timestamp is at or
after 09:45:

1. Durably consume the one primary attempt for the day **before** selection or
   order effects.
2. If the trading date is in `blackout_dates`, do nothing further that day.
   The consumed attempt is not retried.
3. Set and durably persist `reference_spot = candle.close`.
4. Resolve one OTM CE and one OTM PE at the configured point distance.
5. Submit two SELL leg intents, 10 lots each.
6. Each leg waits for a fresh tick for its selected contract.

There is no second primary-entry attempt later that day after selection,
subscription, quote, or fill failure. A partially filled primary basket must be
represented honestly: manage confirmed exposure, do not pretend atomicity, do
not duplicate the missing leg, and ensure hard square-off can close whatever
actually opened.

The default `blackout_dates` list is empty. It is a manual configured-date
filter, not a live news integration. Do not invent news scraping or an external
calendar service.

## 9. Rolling Rules

### 9.1 Trigger

On each completed NIFTY candle before 15:10, while a reference spot exists:

`move = candle.close - reference_spot`

- If `abs(move) < 60`, do nothing.
- If `move >= +60`, CE is the threatened side.
- If `move <= -60`, PE is the threatened side.
- Exactly ±60 triggers; the comparison is inclusive.

### 9.2 Default single-leg roll

With `single_leg_roll: true`:

1. The threatened role must have an OPEN leg.
2. That role’s own counter must be below its own maximum.
3. The opposite role’s roll counter does not gate this roll.
4. Durably claim the threatened role’s next roll count.
5. Durably update `reference_spot` to the triggering candle’s close at claim
   time, not at replacement fill time.
6. Durably record which concrete leg is being closed and that a replacement is
   pending.
7. Close only the threatened leg with `ExitReason.ADJUSTMENT`.
8. Do **not** open the replacement on the same candle.
9. Only a confirmed close may advance to `AWAITING_NEXT_CANDLE`.
10. On the next completed NIFTY candle, if its decision time is strictly before
    15:10, select and SELL a fresh OTM option of the same role using that
    candle’s current spot.
11. The unaffected opposite leg remains open throughout.

If the threatened side has exhausted its budget or has no open leg, do
nothing. A later move threatening the other side may still use that other
side’s independent budget.

### 9.3 Optional both-leg recenter mode

Preserve the legacy configurable behavior when `single_leg_roll: false`:

- both CE and PE must still have available roll budget;
- one roll is consumed from each role;
- update the shared reference spot at the triggering candle;
- close both open legs;
- re-enter both roles on the next completed candle, only if it is before
  15:10 and both closes are confirmed.

The shipped configuration remains `single_leg_roll: true`. Supporting the
legacy option does not authorize enabling it operationally.

### 9.4 Cutoff interaction

- At exactly or after 15:10, do not initiate a new roll.
- If a roll was claimed and its close completed on the prior candle, but the
  next completed candle is at or after 15:10, consume/expire the replacement
  attempt and leave that role flat.
- Continue managing any surviving open leg until risk exit or 15:15.
- Do not reopen the missing side later that day.

### 9.5 Repeated-roll durability

The current generic basket model was originally proven around a single
adjustment and exposes a scalar `adjustment_count` plus one pending replacement
role/state. This strategy requires repeated rolls, two independent role
counters, a shared reference spot, and crash-safe next-candle re-entry.

Before implementation, inspect the current `Basket`, `multi_leg_state`,
repository migrations, `MultiLegEngine._close_adjusted_leg`, and restart
reconciliation in full.

Required outcome:

- Reuse an existing generic durable state mechanism only if tests prove its
  pre-effect atomicity and restart semantics.
- Otherwise add the smallest **generic, additive** basket roll-state model and
  migration needed for per-role counts, reference spot, concrete closing leg,
  pending replacement roles/state, claim timestamp/candle, and replacement
  attempt identity.
- Do not encode state solely in `exit_state_snapshot()` if that payload is not
  durably written at the same critical pre-effect checkpoint as the roll claim.
- Do not overload a field whose documented invariant is “sole adjustment”
  without generalizing and regression-testing that invariant.
- Do not add a strategy-name condition to `MultiLegEngine`.

Required durable lifecycle for every roll:

1. `CLAIMED` — counters, reference spot, target leg(s), and attempt identity
   committed before any close.
2. `EXIT_SUBMISSION_PENDING` — about to submit/reconcile the close.
3. `EXIT_CONFIRMED` or `EXIT_UNKNOWN` — unknown never permits replacement.
4. `AWAITING_NEXT_CANDLE` — confirmed closed and waiting for a strictly later
   completed underlying candle.
5. `REPLACEMENT_PENDING` — attempt consumed before resolution/subscription.
6. `REPLACEMENT_FILLED`, `REPLACEMENT_FAILED`, or `REPLACEMENT_EXPIRED`.

A restart must resume or reconcile the current state; it must never repeat a
confirmed close, reclaim a roll count, reopen twice, or silently forget a
missing side.

## 10. Risk and Exit Rules

### 10.1 Combined loss stop

Evaluate basket loss on every fresh tick for every OPEN option leg.

The decision P&L is legacy **gross** basket P&L:

`gross_cycle_pnl = realised_gross_pnl_of_closed_rolled_legs + unrealised_gross_pnl_of_open_legs`

Charges and slippage remain reporting/execution-model data and do not change
the legacy stop threshold unless separately approved.

Exit all confirmed open legs when:

`gross_cycle_pnl <= -SL_total`

With the shipped values, `SL_total = -(₹2,000 × 10) = -₹20,000`.

- `-₹19,999` does not trigger.
- `-₹20,000` triggers.
- The threshold is inclusive.
- Realized loss from earlier rolled-out legs remains part of the same day’s
  basket stop calculation; it is not discarded when a replacement opens.
- Once triggered, block every later entry/replacement for the day.

Use a strategy/basket exit reason that is semantically accurate. If the current
enum has only `DAILY_LOSS_LIMIT` and that is what existing multi-leg code uses,
reuse it consistently rather than inventing an incompatible string.

### 10.2 Time exit and hard square-off

At or after 15:15:

- close every confirmed open leg;
- cancel/expire pending entry and replacement attempts;
- mark the basket/day closed only after exposure is reconciled;
- stop the worker according to the existing runtime lifecycle.

If a close submission outcome is unknown, record a critical unresolved state
and do not report a false successful square-off.

### 10.3 Exit priority

Engine-owned hard square-off has highest priority. For ordinary evaluation:

1. exposure-reducing unresolved/reconciliation safety;
2. combined loss stop;
3. completed-candle roll logic;
4. pending next-candle replacement;
5. primary entry.

An exit signal always beats an entry or roll replacement at the same logical
boundary. Exit management must not depend on warm-up, new contract selection,
or optional filters.

There is no profit target, individual-leg premium-doubling stop, VIX filter,
Greeks filter, or trailing stop in this strategy.

## 11. Restart and Reconciliation

On restart, use the durable repository and authoritative order/fill/position
tables to reconstruct and reconcile:

- whether the primary attempt was consumed;
- all original and replacement leg instances;
- each leg’s actual order/fill/position state;
- CE and PE roll counts;
- shared reference spot;
- current pending roll/replacement lifecycle;
- cumulative realised gross P&L from closed rolled legs;
- whether the combined stop or time exit already blocked the day.

Fail closed on contradictions, including:

- projection says closed but exposure remains;
- replacement exists while the adjusted-out leg is still open/unknown;
- duplicate open legs for one role without a valid lifecycle explanation;
- quantity, side, contract, or correlation mismatch;
- claimed roll with no resolvable order/position outcome;
- roll counters outside configured bounds;
- missing reference spot after a basket has entered;
- day marked closed while any exposure remains.

Correct in place only when authoritative order/fill/position records establish
the truth. Otherwise refuse new entries and raise/record a critical incident.

Stopping the runtime is not permission to close exposure. Only strategy risk,
operator square-off, or the session hard-exit authority may do that.

## 12. Architecture Mapping

| Concern | Required `algo_trading` component |
|---|---|
| Strategy contract | `BaseMultiLegStrategy` and multi-leg registry |
| Runtime | Existing `intraday_options` supervisor/worker |
| Feed distribution | Existing `SharedFeedHub` and tick/control channels |
| Candle construction | Existing completed 5-minute underlying candle path |
| Dynamic option data | Existing segment-aware subscription control path |
| Contract selection | `OptionSelector` + Dhan scrip master resolver |
| Execution | `MultiLegEngine`, `LifecycleGateway`, `PositionManager`, `PaperBroker` |
| Persistence | `ExecutionRepository` + generic basket/leg state |
| Recovery | Existing reconciliation, generically extended only if required |
| Hard square-off | Existing session/operator square-off authorities |
| Notification | Existing notifier factory and external-notification guard |
| Dashboard | Existing strategy-scoped generic dashboard read models |
| Auto-start | Existing config-driven discovery; no controller branch |

The implementation must not modify single-leg `TradingEngine` behavior.

## 13. Configuration Requirements

Create `config/strategies/rolling_strangle_otm1.yaml` with at least:

```yaml
strategy_id: rolling_strangle_otm1
runtime_id: intraday_options
enabled: false
mode: paper
live_approved: false

engine: multi_leg_engine
strategy_ref: strategies.intraday_options.rolling_strangle_otm1

parameters:
  underlying: NIFTY
  timeframe_minutes: 5
  entry_time: "09:45"
  stop_new_entries_after: "15:10"
  square_off_time: "15:15"
  lots_per_leg: 10
  strike_step: 50
  otm_distance_points: 50
  roll_trigger_points: 60
  max_rolls_ce: 2
  max_rolls_pe: 2
  single_leg_roll: true
  combined_stop_per_lot: 2000
  blackout_dates: []
  holidays: []  # populate with the verified current-year NSE calendar
```

Use the repository’s exact schema/key names after inspecting existing
multi-leg configs; this block defines values and intent, not permission to
invent parallel config parsing.

Additional requirements:

- `contract_resolver: dhan` for real paper-market evaluation.
- No hardcoded lot size in production config.
- No simulated contract resolver in the shipped operational configuration.
- Use the current paper broker’s canonical slippage, latency, quote-age, and
  charge model rather than copying the legacy zero-slippage simulator. Record
  this as an intentional architecture-level execution-model deviation; do not
  change the strategy’s signal or risk formula.
- Do not enable generic daily-loss settings that create a second, conflicting
  decision owner for the strategy’s ₹20,000 basket stop.
- Validate all numerical limits and time ordering at config load.

## 14. Validation Rules

Reject configuration unless:

- `lots_per_leg > 0`;
- `strike_step > 0`;
- `otm_distance_points > 0`;
- `roll_trigger_points > 0`;
- both roll maxima are non-negative integers;
- `combined_stop_per_lot > 0`;
- entry time is before new-entry cutoff;
- new-entry cutoff is before square-off;
- mode is PAPER for this first implementation;
- enabled live gates remain false;
- holiday and blackout dates parse as valid dates;
- the configured strategy reference resolves to the expected registered class.

Unknown strategy parameters must be rejected or explicitly reported; do not
silently ignore misspellings in risk-critical fields.

## 15. Dashboard and Observability

The existing generic strategy selector must discover this strategy from config
without a dashboard branch.

Expose persisted facts where available:

- health, PID, heartbeat, mode, and feed status;
- basket and open legs;
- role, contract, strike, expiry, side, quantity, entry/last price;
- original and replacement sequence;
- CE/PE roll count and configured maximum;
- shared reference spot and latest underlying spot;
- current pending roll lifecycle;
- realised, unrealised, and total P&L, clearly identifying gross decision P&L
  versus net reported P&L;
- combined-stop threshold;
- orders, fills, closed legs/trades, incidents, and square-off status.

If a value is not durably available, show `—` with an honest explanation.
Never infer or fabricate it from logs. Dashboard code remains read-only and
uses the existing bounded read-model layer.

## 16. Safety Boundary

- Ship `enabled: false`, `mode: paper`, `live_approved: false`.
- Do not change `auto_start.enabled` or any existing runtime/strategy flag.
- Do not change global `live_trading_enabled` or runtime
  `live_execution_allowed`.
- Do not install/restart LaunchAgents or start a runtime while implementing.
- Do not call an order-capable broker endpoint during verification.
- Any real Dhan diagnostic must be separately authorized, bounded, read-only,
  and limited to market data/reference data.
- Tests must inherit the external-notification guard; no real Telegram call.
- Implementation completion does not authorize PAPER enablement or live use.

Because this is a short strangle with theoretically unbounded risk, any future
live eligibility requires a separate specification and explicit approval. It
is not implied by successful paper testing.

## 17. Required Tests and Acceptance Matrix

### 17.1 Identity and architecture

- Config ships disabled, PAPER, not live-approved.
- Disabled strategy is not spawned.
- Flipping only `enabled` in a test registers an isolated third intraday worker
  with tick and control queues.
- Existing EMA, `straddle_920`, SuperTrend, and positional workers are unchanged.
- No strategy ID literal/branch outside strategy-owned files unless a generic
  registry/config reference is required.
- Dashboard discovers the strategy generically.
- Tests make no Dhan, Telegram, or external network request.

### 17.2 Primary entry

- No entry before 09:45.
- Exactly 09:45 is eligible.
- First eligible candle consumes the only daily attempt.
- Blackout date consumes the attempt and places no order.
- Empty blackout list does not block.
- CE and PE are both SELL intents.
- Both are exactly one OTM step with default values.
- Non-default distance/step rounding and one-step floor are pinned.
- Quantity is 10 × resolved lot size for at least lot sizes 65, 50, and 75.
- Mismatched selected-contract lot sizes fail closed before entry.
- Expiry comes from the resolver; no hardcoded date.
- No fill before a fresh contract tick.
- Partial primary entry is represented and managed honestly; no duplicate leg.
- Failed first attempt is not retried later that day.

### 17.3 Single-leg rolls

- +59.99 points does not roll CE; +60 does.
- -59.99 points does not roll PE; -60 does.
- Up move closes only CE; down move closes only PE.
- Reference spot updates to the trigger candle at durable claim time.
- Replacement does not open on the trigger candle.
- Replacement uses the next completed candle’s spot and a newly resolved
  contract.
- Unaffected opposite leg remains open.
- CE can roll twice and not a third time.
- PE can roll twice and not a third time.
- Exhausted CE budget does not consume/block PE budget, and vice versa.
- Roll counter is consumed before the close effect and survives restart.
- Exact 15:10 blocks a new roll.
- A confirmed pre-cutoff close whose next candle is 15:10 expires replacement
  and leaves that side flat.

### 17.4 Both-leg mode

- A qualifying move closes both legs.
- One count is consumed from both role budgets.
- Both replacements wait until the next completed candle.
- Both budgets must be available.
- One unknown/unconfirmed close prevents unsafe re-entry.
- Shipped config remains single-leg mode.

### 17.5 Risk and exit

- Gross P&L includes realised losses from prior rolled legs plus current
  unrealised P&L.
- `-₹19,999` does not trigger and `-₹20,000` triggers for 10 lots.
- Changing lot count scales the stop exactly by ₹2,000 per lot.
- Charges do not alter the legacy gross decision boundary but appear in net
  reporting.
- Combined stop closes every open leg and blocks later replacement/entry.
- 15:14:59 does not time-exit; 15:15 does.
- Hard square-off closes one-sided/partial baskets too.
- Exit remains available with missing underlying candle or selection data.
- Unresolved close remains critical exposure and is never reported flat.

### 17.6 Restart boundaries

Test real repository/engine reconstruction—not hand-built projection rows—at:

- before primary claim;
- after primary claim, before contract selection;
- after one of two primary legs fills;
- after both primary legs fill;
- after roll claim, before close submission;
- close submitted with unknown outcome;
- adjusted leg confirmed closed, before `AWAITING_NEXT_CANDLE` persistence;
- awaiting the next candle;
- replacement attempt consumed, before subscription;
- replacement pending, before fill;
- replacement filled, before projection update;
- after first and second CE rolls;
- after first and second PE rolls;
- after combined-stop claim;
- during incomplete square-off.

Every restart test must prove no duplicate order, close, roll claim, or
replacement; counters/reference spot must be unchanged; and confirmed exposure
must remain managed.

### 17.7 Calendar and state reset

- Weekend and configured holiday are skipped.
- Next trading day resets primary-attempt, counters, reference spot, and block
  state only after prior-day exposure is reconciled flat.
- Same-day restart does not reset those fields.
- Annual holiday ownership is documented and tested.

## 18. Implementation Phases and Review Gates

Follow `CLAUDE.md` phase-by-phase. Stop for review after each phase.

### Phase 0 — Inspection and conflict report

- Read this document and all named legacy/current architecture files in full.
- Grep every multi-leg strategy, config adapter, persistence/recovery path,
  dashboard reader, and test assumption affected by repeated adjustments.
- Report whether existing durable state can safely represent this lifecycle.
- Propose the smallest generic change if it cannot.
- Do not code until any new trading-rule conflict is resolved.

### Phase 1 — Pure strategy behavior

- Add the package/class and pure decision tests.
- No config discovery or runtime enablement.
- Prove entry, trigger, budgets, next-candle behavior, cutoff, risk formula, and
  daily reset.

### Phase 2 — Generic durable rolling lifecycle

- Add/reuse generic durable state and any additive migration.
- Implement pre-effect claims, uncertain-close handling, replacement lifecycle,
  and reconciliation.
- Run existing `straddle_920` durability/reconciliation regressions explicitly.

### Phase 3 — Runtime/config/composition

- Add disabled PAPER config.
- Wire through existing generic registry and supervisor.
- Prove segment-aware dynamic subscriptions, fresh-tick fills, isolation, and
  no effect on existing strategies.

### Phase 4 — End-to-end acceptance and dashboard

- Use real temporary SQLite repositories and production engine composition
  below the network boundary.
- Complete the acceptance matrix, restart matrix, dashboard coverage, and
  safety/no-network proofs.

### Phase 5 — Consolidated verification and final report

Run targeted tests first, then:

```bash
pytest
ruff check .
mypy common strategies runtimes dashboards scripts --strict
python -m scripts.assert_no_live_config_committed
python -m orchestration.launchd.generate_plists --check
```

Do not regenerate LaunchAgents from an isolated worktree merely because their
absolute project-root path differs there. Verify the generator from the real
checkout, read-only, as established by the SuperTrend work.

## 19. Final Report Format

Report:

1. Root Cause / Legacy Parity Summary
2. Recorded Legacy Conflicts and Resolutions
3. Architecture Mapping
4. Durable Roll-State Design and Why It Is Generic
5. Strategy Rules Implemented
6. Intentional Deviations
7. Files and Tests Changed
8. Complete Acceptance Matrix
9. Restart/Reconciliation Matrix
10. Regression Results (`pytest`, `ruff`, `mypy`, safety/plist gates)
11. Safety Confirmation
12. Branch and commit SHAs
13. Remaining Genuine Blockers
14. Operational Eligibility

Explicitly state that the strategy remains disabled and PAPER-only, that no
order-capable endpoint was called, and that implementation completion does not
authorize operational enablement.

## 20. Definition of Done

Implementation is complete only when:

- executable legacy behavior is preserved as specified;
- repeated per-side rolling is durably restart-safe;
- unknown effects fail closed without duplicating orders;
- every acceptance and restart row passes;
- existing multi-leg and single-leg strategies remain unchanged;
- dashboard support is generic/read-only;
- full tests, lint, typing, and safety gates pass;
- committed config remains disabled and PAPER-only;
- no Phase 10 live gate or operational process is changed.
