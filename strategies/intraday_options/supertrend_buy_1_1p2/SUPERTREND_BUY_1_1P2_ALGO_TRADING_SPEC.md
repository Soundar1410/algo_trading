# SuperTrend Buy 1/1.2 Strategy — `algo_trading` Implementation Specification

**Strategy ID:** `supertrend_buy_1_1p2`  
**Display name:** SuperTrend Buy 1/1.2 — NIFTY ATM Option  
**Legacy source:** `Soundar1410/Trading_Automation`, under `option_strategies/Trading_Strategies_Automation_v2/strategies/supertrend_fast/`  
**Target repository:** `Soundar1410/algo_trading`  
**Initial operating mode:** PAPER only, disabled by default

## 1. Objective

Port the trading behavior of the legacy `supertrend_fast` strategy into the current `algo_trading` architecture.

“Same strategy” means preserving the legacy signal, contract-selection, position, exit, session, and daily-loss rules. It does **not** mean copying legacy framework code, imports, process control, database access, notification code, or dashboard code.

The implementation must reuse the current platform’s generic components and safety boundaries. It must not introduce a strategy-specific branch into `TradingEngine`, the runtime supervisor, broker routing, persistence, dashboard, or auto-start controller.

## 2. Naming Decision

Use `supertrend_buy_1_1p2` instead of `supertrend_fast`.

Reasons:

- `fast` is ambiguous and becomes misleading if the period or multiplier is later configured differently.
- `supertrend_buy_1_1p2` identifies the SuperTrend-driven BUY-only structure and its defining parameters: ATR period `1` and multiplier `1.2`.
- `1p2` is used instead of `1.2` because a dot would conflict with Python package/import naming.
- NIFTY, the exact fresh-flip behavior, and ATM weekly option selection remain explicit strategy rules in this specification and configuration.
- The name remains readable in logs, configuration, dashboards, database rows, and Telegram messages.

Use consistently:

- Strategy ID: `supertrend_buy_1_1p2`
- Package: `strategies/intraday_options/supertrend_buy_1_1p2/`
- Config: `config/strategies/supertrend_buy_1_1p2.yaml`
- Suggested class: `SupertrendBuy1x1p2Strategy`

Do not retain `supertrend_fast`, `supertrend_1`, or other legacy aliases as production identifiers.

## 3. Authoritative Legacy Sources Reviewed

The strategy requirements were extracted from these legacy files:

- `strategies/supertrend_fast/strategy.py`
- `strategies/supertrend_fast/config/config.yaml`
- `strategies/supertrend_fast/tests/test_strategy.py`
- `strategies/supertrend_fast/main.py`
- `strategies/supertrend_fast/trade_manager.py`
- `strategies/supertrend_fast/app.py`
- Framework `indicators/supertrend.py`
- Framework `exit/combined_candle_exit.py`
- Framework `execution/engine.py`
- Global `config/strategies.yaml`
- `NiftyFixedStrikeSuperTrend_Master_Specification.md`

When the legacy implementation and prose disagree, stop and report the exact conflict before choosing behavior. Do not silently invent a rule.

### 3.1 Recorded conflict and resolution (operator-approved)

`NiftyFixedStrikeSuperTrend_Master_Specification.md` describes a **different**
strategy from `supertrend_fast`'s actual code: it documents a strike fixed at
09:16 for the whole trading day, SuperTrend applied to the CE and PE **premium**
charts (not the underlying), multiplier `1` (not `1.2`), and two simultaneous
open positions (CE and PE independently). None of that matches `strategy.py`,
`config/config.yaml` or `tests/test_strategy.py`, which are mutually
consistent with each other and with this document's own rules (sections 4–14).

**Resolution: the authoritative parity source for this port is the legacy
`supertrend_fast` strategy's own code, config and tests.**
`NiftyFixedStrikeSuperTrend_Master_Specification.md` is deliberately **not**
used as a parity source — it is listed above only because it was reviewed and
the conflict it presents is recorded here, not silently discarded.

## 4. Strategy Summary

The strategy observes completed 5-minute NIFTY underlying candles and maintains a stateful SuperTrend indicator.

| Fresh SuperTrend event | Action when flat | Action when opposite option is open |
|---|---|---|
| Trend flips from DOWN to UP | Buy current ATM weekly NIFTY CE | Close PE, then buy current ATM weekly CE if new entries are still allowed |
| Trend flips from UP to DOWN | Buy current ATM weekly NIFTY PE | Close CE, then buy current ATM weekly PE if new entries are still allowed |
| No fresh flip | No entry | Continue managing the existing option |

The initial SuperTrend direction is context only. It is never itself an entry signal.

## 5. Market, Clock, and Candle Semantics

- Underlying: NIFTY index.
- Exchange/session calendar: NSE, using the existing `MarketSession` holiday and trading-day logic.
- Time zone: Asia/Kolkata.
- Signal timeframe: 5 minutes.
- Signals are evaluated only from completed NIFTY candles.
- No intrabar SuperTrend entry or reversal is permitted.
- Entry window opens at 09:15 IST.
- New-entry cutoff is 15:15 IST.
- Engine-owned hard square-off is 15:20 IST.
- No overnight position is allowed.
- At exactly or after 15:15, an existing position may still be exited, but an opposite signal must not open a replacement.
- At 15:20, the hard square-off must run regardless of strategy indicator state or optional exit configuration.

Timestamp boundary behavior must be deterministic under injected clocks in tests.

## 6. SuperTrend Definition

Use the existing canonical implementation in `common/indicators/supertrend.py`. Do not copy or independently reimplement SuperTrend inside the strategy.

Required parameters:

- ATR period: `1`
- Multiplier: `1.2`
- ATR method: Wilder ATR
- Price source and band-carry behavior: exactly the existing TradingView-compatible implementation already used by the repository.

Required signal semantics:

- Indicator state is `+1` for UP and `-1` for DOWN.
- A signal exists only when a previous initialized trend exists and the trend changes.
- First initialization/seed produces no entry.
- Repeated candles in the same trend produce no entry.
- One flip produces at most one actionable signal.

The parameters must be configurable but ship with the legacy values above. Changing them later is a strategy-spec change, not an implementation detail.

## 7. Warm-up and Context Trust

SuperTrend is continuity-sensitive. A cold or partial replay can seed a different trend and therefore cannot be trusted for fresh-crossover detection.

Requirements:

- The strategy’s `warmup_spec()` must declare continuity required.
- Warm-up must use the existing hardened `WarmupManager` and deterministic engine clock.
- Only `WARMED` grants trusted context and permits entries.
- `PARTIAL`, `COLD_START`, stale, truncated, unordered, duplicated, gapped, or missing-`now` replay must block entries.
- Exit management and hard square-off must remain available even when entry context is untrusted.
- Historical candles replayed during warm-up seed indicator state only; they must never emit orders.
- The first live candle may enter only if it creates a fresh flip relative to the trusted warmed state.
- A mid-session restart must reconstruct the indicator state without replaying historical trades.
- Trust must reset at every new run/day according to the existing engine lifecycle.

Choose the minimum warm-up bar count from the existing SuperTrend component’s continuity contract and repository conventions. Do not reduce it merely to make a test pass. Record the chosen value and reasoning in the final report.

### 7.1 Recorded decision — `min_bars = 75` (operator-approved)

**Chosen value: `min_bars = 75`, `continuity_required = true`.**

`SuperTrend.warmup_requirement()` declares `min_bars = period`, which is `1` for this strategy. That number is a correct statement about **ATR readiness** and an unsafe one about **trend-context trust**: the SuperTrend direction is latched and path-dependent, so a replay of a single recent candle seeds a direction outright and the first live crossing can then be read as a fresh flip that never happened — or the opposite direction held and a real flip swallowed. ATR calculation readiness and trustworthy trend-context reconstruction are different requirements, and the bar count must express the second.

The floor is raised inside the strategy’s own `warmup_spec()` (`max(indicator_min_bars, 75)`, inheriting `continuity_required` from the indicator). The shared `common/indicators/supertrend.py` contract is **not** modified, so no other `SuperTrend` consumer inherits this strategy’s trading-risk decision, and no `TradingEngine` branch is added.

**Describe the value accurately.** A 09:15–15:20 lifecycle contains **73** completed five-minute buckets (09:15 through the 15:15–15:20 bar — `common.warmup.session_buckets.session_bucket_count`). `75` is therefore **not** “one complete session”. It is a conservative 75-completed-bucket trust floor that **intentionally spans trading sessions**:

- **At market open** — the previous session’s 73 completed buckets plus two buckets from the preceding valid session.
- **Mid-session** — the latest 75 completed buckets across the current and previous valid sessions.
- **Weekends and configured holidays remain legitimate boundaries**, walked by `MarketSession.prior_trading_day` rather than read as missing buckets. This is why the committed configuration declares `parameters.holidays`.
- Missing, stale, unordered, duplicate or in-session-gapped coverage remains non-`WARMED`, and `StrategyWarmupSpec.entry_blocked_by` latches entries off for the whole day.
- Warm-up replay still seeds indicator state only and never emits an order.

Sizing consequence: `WarmupManager._lookback_sessions` requests `ceil(75 / 73) = 2` prior sessions plus today, which the committed `warmup_max_lookback_sessions: 3` accommodates.

### 7.2 Recorded decision — strategy-scoped trading calendar (operator-approved)

The engine's own `MarketSession` reads its holiday calendar from `parameters.holidays`
in the strategy configuration. `config/global.yaml`'s verified NSE list feeds only the
unattended auto-start gate, and no strategy configuration in this repository had ever
declared one — so every running engine's session relied on the weekday rule alone.

`config/strategies/supertrend_buy_1_1p2.yaml` therefore carries the verified NSE 2026
calendar, copied from `config/global.yaml`. **This is strategy-scoped**: it applies to
`supertrend_buy_1_1p2` only, changes nothing for `ema_cross_9_21_buy` or
`straddle_920`, and is not affected by edits to `config/global.yaml`.

It matters twice: no entry on a closed day (acceptance row 18.1 "Holiday/weekend"),
and a correct warm-up walk-back — the 75-completed-bucket trust floor spans sessions,
and `MarketSession.prior_trading_day` is what decides which prior sessions those are.

**Maintenance obligation.** The list is annual. A 2027 calendar must be committed
before January 2027, or every 2027 holiday will be treated as an ordinary trading day
by this strategy, and its warm-up will expect buckets that never existed — downgrading
a good replay to `PARTIAL` and blocking entries for that day. Re-verify against NSE's
own circular after any ad-hoc closure, in both this file and `config/global.yaml`.

## 8. Contract Selection and Quantity

On an actionable fresh flip:

- Resolve the current NIFTY spot at signal time.
- Select the current ATM strike using the platform’s canonical option resolver and exchange strike intervals.
- UP flip selects CE; DOWN flip selects PE.
- Select the nearest valid weekly expiry from the current Dhan scrip master, including holiday-shifted expiries.
- Resolve the real security ID, segment, tick size, expiry, and exchange lot size from current reference data.
- Never hardcode the production NIFTY lot size. Test/simulated resolvers may receive an explicit fixture lot size.
- Legacy quantity is `10` lots per trade. Preserve this as configurable `lots_per_trade: 10` for parity.

Ten lots is a parity requirement, not a recommendation that the size is safe for live capital. The strategy must remain PAPER and disabled by default.

Contract selection must happen again on every new entry or reversal. Do not reuse a stale ATM contract from an earlier signal.

### 8.1 Recorded clarification — expiry resolution timing (operator-approved)

The bullets above, read together, could be misread as "expiry is re-resolved on
every entry." That is not the accurate description of what the implementation
does, and the distinction matters for anyone reasoning about which entries in a
long-running session could ever land on a different expiry:

- **ATM strike, contract security ID and exchange lot size are resolved fresh
  for every new entry or reversal.** Each signal computes its own strike from
  the spot at signal time and looks that strike up in the already-loaded scrip
  master, which returns that row's own security ID and lot size.
- **Weekly expiry is selected once, when the Dhan option-chain resolver is
  constructed** — i.e. when the worker process starts, from the scrip master's
  nearest listed expiry at that moment
  (`common.engine.selection.DhanOptionChainResolver.__init__`, whose own
  docstring states this explicitly: "The expiry is fixed for the session at
  construction ... so every contract a run selects belongs to the same series
  even if the run crosses an expiry boundary at midnight"). It is **not**
  re-resolved by `OptionSelector.select()` on each individual signal.
- **The normal daily worker restart is what refreshes it.** Each new worker
  process rebuilds the resolver from scratch and therefore picks up the
  scrip master's then-current nearest listed (holiday-shifted, if applicable)
  expiry at that restart — which is the mechanism spec row 18.2's "Weekly
  expiry shifted by holiday" acceptance criterion actually relies on, not a
  per-signal re-resolution inside a single continuous run.

## 9. Entry and Reversal Execution

- Position side is always BUY.
- Maximum one open strategy position at a time.
- Same-leg duplicate signals must not create another order.
- An opposite fresh flip while a position is open performs a close-before-open reversal.
- The replacement may be submitted only after the existing position is confirmed closed.
- If close outcome is unknown or failed, fail closed: do not open the replacement.
- If the close succeeds after the entry cutoff, remain flat.
- Entry fills must use the platform’s current fresh-quote/tick requirements; do not use a stale cached price merely to force a fill.
- Reuse `TradingEngine`, `PositionManager`, `ExecutionGateway`, `OrderLifecycle`, `PaperBroker`, correlation IDs, and current persistence paths.

No strategy-specific execution loop, broker client, database connection, or process is permitted.

## 10. Premium-Candle Exit

Reuse `common/exit/combined_candle_exit.py` and its existing supporting components. Do not duplicate this logic in the strategy.

Evaluate exits on the currently traded option’s own completed 5-minute premium candles, not on the NIFTY candle.

The two exit legs use OR logic:

### 10.1 Momentum structure exit

For the long option position, exit when:

`current completed option candle close < previous completed option candle low`

This leg is active immediately after it has enough consecutive premium candles. It does not wait for the trailing-profit activation threshold.

### 10.2 Best-close trailing exit

- Entry premium is the confirmed option entry fill price.
- Track the highest completed option-candle close since entry.
- Activate the trail after a favourable move of at least `4%` from entry premium.
- After activation, exit when the completed close has retraced at least `8%` from the highest completed close.
- The best close is monotonic and never rebased downward.

### 10.3 Combined result

- Exit if either leg fires.
- Preserve a granular reason identifying momentum, trail, or both.
- Reset all exit state on a genuinely new position.
- On a reversal, the replacement position starts with independent exit state.

### 10.4 Premium candle gaps

- When the option premium candle sequence has a gap, suppress the momentum comparison for exactly the first completed post-gap candle.
- Preserve the highest-close and trail-activation state across the gap.
- The next consecutive premium candle may again participate in the momentum comparison.
- If missing premium ticks prevent a premium candle from being constructed, do not fabricate one. Daily risk and hard square-off remain effective.

## 11. Exit-State Recovery

Use the existing strategy snapshot/restore hooks and current state repository.

Persist/restore enough state to preserve:

- Entry premium relevant to the trail.
- Highest completed option close.
- Whether the trailing exit has activated.
- Any other state required by the canonical combined exit component.

After restart:

- Preserve best-close/trailing state.
- Re-prime the previous-premium-candle comparison rather than comparing non-consecutive candles across the process gap.
- Never duplicate an already-confirmed exit or reversal.
- If persisted state and authoritative position/order state disagree, fail closed and record an incident; do not infer safety from successful file/database reads alone.

## 12. Daily Risk and Session Exit

Preserve the legacy daily loss guard:

- Starting capital reference: ₹1,000,000.
- Maximum daily loss: `3%`, equivalent to ₹30,000 under the reference capital.
- Evaluate using realised P&L plus open unrealised MTM.
- On breach: square off the open position and block all further entries for that trading day.
- The guard must not reset on a same-day worker restart.
- Reset only for the next valid trading day under the existing session lifecycle.

Use the current engine’s generic daily guard and P&L sign conventions. Do not create a second strategy-local risk ledger.

All exit priorities must remain fail-safe: hard square-off and daily-loss exits cannot be blocked by missing indicator, premium-candle, or warm-up data.

## 13. Execution-Model Reconciliation

The legacy framework used zero configured slippage. The target repository has a newer, shared paper-execution model designed to approximate real fills.

Preserve the strategy’s trading rules, but use the current `algo_trading` execution model rather than recreating legacy zero-slippage fills. Configure this strategy consistently with the repository’s current intraday paper strategies, including:

- Paper mode.
- Fresh quote/tick enforcement.
- Existing latency/slippage policy.
- Existing quote-age and fallback controls.
- Existing charges and realised-P&L accounting.

Document the exact chosen execution fields in the final report as an intentional architecture-level deviation from the legacy simulator, not a change to the signal rules.

## 14. Gap Handling

### Underlying candle gap

- Use the existing engine gap policy.
- Do not manufacture a stitched SuperTrend candle.
- Do not reset the session-spanning SuperTrend merely because one live bucket is missing.
- Entry trust must remain fail-conservative according to the existing continuity/gap lifecycle.

### Option premium candle gap

Use the combined-exit behavior in section 10.4.

Tests must distinguish the underlying signal-candle gap from the traded-option premium-candle gap.

## 15. Current-Architecture Mapping

Reuse these existing components instead of porting their legacy equivalents:

| Concern | Required target component |
|---|---|
| Strategy contract | `common.engine.strategy.BaseStrategy` |
| SuperTrend | `common.indicators.supertrend` |
| Premium exit | `common.exit.combined_candle_exit` |
| Warm-up | `common.warmup.WarmupManager` and Dhan warm-up source |
| Session/holidays | `common.engine.session.MarketSession` |
| Contract resolution | Existing Dhan option resolver/scrip master |
| Dynamic subscriptions | Existing shared feed hub and worker control channel |
| Execution | Existing gateway, lifecycle, position manager, and paper broker |
| Daily risk | Existing engine daily guard/risk machinery |
| Persistence/recovery | Existing execution and strategy-state repositories |
| Notifications | Existing common notifier and external-notification test guard |
| Dashboard | Existing config-driven strategy discovery and read-only data layer |
| Runtime | Existing `intraday_options` supervisor/worker |
| Auto-start | Existing generic auto-start discovery; no special controller branch |

Before modifying shared/common code, grep all callers and tests and prove why the existing extension hooks are insufficient. Prefer a strategy-only implementation. Any unavoidable common change must be generic, additive, regression-tested, and reported before implementation continues.

## 16. Expected Files

Expected strategy-specific additions:

- `strategies/intraday_options/supertrend_buy_1_1p2/__init__.py`
- `strategies/intraday_options/supertrend_buy_1_1p2/strategy.py`
- `config/strategies/supertrend_buy_1_1p2.yaml`
- Unit tests for strategy signals, exits, state, and boundaries.
- Integration tests through the real engine wiring.
- Runbook update.

Do not add a new runtime. Do not add a separate dashboard page solely for this strategy. Existing strategy selectors/comparison pages must discover it generically from config/data.

A migration should not be necessary. If structured persistence cannot represent required state, stop and report the exact missing field and generic schema implications before creating a migration.

## 17. Configuration Requirements

The committed configuration must ship fail-closed:

```yaml
strategy_id: supertrend_buy_1_1p2
runtime_id: intraday_options
enabled: false
mode: paper
live_approved: false
```

It must also configure, using the repository’s actual schema:

- `engine: trading_engine`
- Strategy reference/class and constructor parameters.
- Underlying NIFTY security/reference mapping.
- 5-minute signal timeframe.
- SuperTrend period `1` and multiplier `1.2`.
- `lots_per_trade: 10`.
- BUY side.
- ATM weekly Dhan contract resolver.
- Dynamic exchange lot size.
- Dhan historical warm-up with continuity required.
- 09:15 entry start, 15:15 entry cutoff, 15:20 square-off.
- Combined premium-candle exit: 4% activation, 8% retracement, momentum enabled.
- ₹1,000,000 capital reference and 3% daily loss cap.
- Current canonical paper execution/charges settings.

Do not change:

- `global.live_trading_enabled`
- Runtime `live_execution_allowed`
- Any strategy’s `live_approved`
- `auto_start.enabled`
- Existing enabled/disabled states of other strategies
- LaunchAgents

Adding the disabled config must not cause the strategy to start automatically. Paper enablement is a later, separate operator decision after implementation and review.

## 18. Required Acceptance Matrix

### 18.1 Signal and entry

| Scenario | Expected result |
|---|---|
| Initial SuperTrend state becomes UP | No entry |
| Initial SuperTrend state becomes DOWN | No entry |
| Trusted context DOWN, fresh flip UP | Buy ATM weekly CE |
| Trusted context UP, fresh flip DOWN | Buy ATM weekly PE |
| UP remains UP | No new order |
| DOWN remains DOWN | No new order |
| Same flip processed twice | One order only |
| Signal before 09:15 | No entry |
| Signal before 15:15 | Entry permitted if all gates pass |
| Signal exactly at/after 15:15 | No new entry |
| Holiday/weekend | No entry/runtime trading action |

### 18.2 Contract and quantity

| Scenario | Expected result |
|---|---|
| UP flip | CE selected |
| DOWN flip | PE selected |
| Spot moves before later signal | ATM recalculated from current spot |
| Weekly expiry shifted by holiday | Exchange-listed valid expiry selected |
| Exchange lot size changes | Current reference-data lot size used |
| Normal entry | Quantity equals 10 × resolved lot size |
| Missing/stale contract or quote | Entry blocked, no fabricated fill |

### 18.3 Reversal

| Scenario | Expected result |
|---|---|
| CE open, fresh DOWN flip before cutoff | Confirm CE close, then buy fresh ATM PE |
| PE open, fresh UP flip before cutoff | Confirm PE close, then buy fresh ATM CE |
| Close fails or outcome unknown | No replacement entry |
| Opposite flip after cutoff | Close if strategy rule requires; remain flat |
| Duplicate same-side signal while open | No pyramiding |

### 18.4 Premium exit

| Scenario | Expected result |
|---|---|
| Current premium close below previous premium low | Exit |
| Favourable move below 4% | Trail inactive |
| Favourable move exactly 4% | Trail activates |
| Activated retracement below 8% | Hold |
| Activated retracement exactly 8% | Exit |
| Both conditions fire | One exit with combined/granular reason |
| First post-gap premium candle triggers apparent momentum | Momentum comparison suppressed |
| Second consecutive post-gap candle meets rule | Exit permitted |
| Premium gap occurs after trail activation | Trail/best-close state preserved |

### 18.5 Warm-up, restart, and state

| Scenario | Expected result |
|---|---|
| Complete trusted warm-up | Entries eligible |
| Stale/partial/gapped warm-up | Entries blocked |
| Warm-up replay contains historical flips | No historical order |
| First live candle creates genuine flip | Entry permitted |
| Restart with open position | Position adopted, no duplicate entry |
| Restart after trail activation | Activation and best close restored |
| First premium candle after restart | Previous-candle momentum comparison re-primed |
| Same-day restart after daily loss breach | Entry remains blocked |
| New trading day | Daily guard resets through normal lifecycle |

### 18.6 Risk and square-off

| Scenario | Expected result |
|---|---|
| Daily P&L just above -₹30,000 | Guard not triggered |
| Daily P&L exactly -₹30,000 | Square off and block entries |
| Realised + unrealised crosses threshold | Square off and block entries |
| Missing SuperTrend/premium data at 15:20 | Hard square-off still executes |
| Position remains unresolved after close attempt | Critical/unresolved state; no new entry |

### 18.7 Architecture regression

- Existing EMA Rev 3.1 acceptance matrix remains unchanged and passing.
- Existing `straddle_920` acceptance/durability/reconciliation suites remain passing.
- Existing `weekly_delta_neutral`, positional runtime, shared-feed, dashboard, auto-start, and notification-guard suites remain passing.
- Disabled strategy is discovered but not spawned.
- Enabling it in a test fixture adds a third isolated intraday worker with the correct tick/control channels.
- Dashboard selector and comparison views show it generically without page-specific SQL or conditionals.
- Tests cannot send real Telegram/Dhan traffic, including spawned child processes.

## 19. Implementation Phases and Review Gates

Follow `CLAUDE.md` phase-by-phase discipline. Stop after each phase, report results, and wait for review.

### Phase 1 — Parity tests and strategy core

- Port the legacy regression scenarios before changing shared internals.
- Implement the strategy using existing SuperTrend and combined-exit components.
- Prove fresh-flip, no-seed-entry, and exact parameter behavior.

### Phase 2 — Engine/config integration

- Add disabled paper config.
- Wire through the existing generic strategy loader and intraday runtime.
- Prove dynamic ATM weekly resolution, lot sizing, reversal, and premium candles.

### Phase 3 — Warm-up, recovery, risk, and gaps

- Prove trusted warm-up handoff.
- Prove exit snapshot/restore and non-duplication.
- Prove daily loss and all timing boundaries.
- Prove underlying and premium gap behavior separately.

### Phase 4 — Dashboard and full regression

- Confirm generic dashboard discovery/filtering.
- Run all targeted and repository-wide gates.
- Update the implementation runbook.

Do not enable the strategy, install/reload LaunchAgents, start a runtime, call a broker/order endpoint, or perform a live-feed diagnostic during these phases.

## 20. Verification Commands

Run targeted tests first. Then run the repository’s complete required gates, including at minimum:

```bash
pytest
ruff check .
mypy common strategies runtimes dashboards scripts --strict
python -m scripts.assert_no_live_config_committed
python -m orchestration.launchd.generate_plists --check
```

Also run the explicitly named regression families in section 18.7 rather than relying only on their inclusion in the full suite.

Do not weaken, delete, skip, or rewrite an existing test merely because the new strategy exposes a failure. Diagnose whether it is a new regression, an existing defect, or a deliberately changed operational assertion, and report it honestly.

## 21. Safety Constraints

- PAPER only.
- New strategy disabled in committed config.
- No live gate changed.
- No live order-capable endpoint called.
- No runtime/dashboard/LaunchAgent started or modified.
- No legacy repo write.
- No legacy imports or runtime dependency.
- No secret printed, copied, committed, or embedded in fixtures.
- No external Telegram notification from tests or spawned descendants.
- No Phase 10/live activation work as part of this strategy port.

## 22. Final Report Format

Provide:

1. **Root Cause / Legacy Parity Summary** — what behavior was extracted and any legacy ambiguity found.
2. **Architecture Mapping** — reused components and confirmation that no strategy-specific engine/runtime branch was added.
3. **Strategy Rules Implemented** — signals, contracts, reversals, exits, risk, session, and warm-up.
4. **Intentional Deviations** — especially current paper execution versus legacy zero-slippage simulation.
5. **Tests Changed** — named files and acceptance rows.
6. **Acceptance Matrix** — every row in section 18 with PASS/FAIL/BLOCKED.
7. **Regression** — exact pytest/ruff/mypy/safety-gate results, including skips/failures without hiding them.
8. **Safety Confirmation** — disabled PAPER config, live gates unchanged, no network/order/runtime/LaunchAgent action.
9. **Files Changed and Commit** — branch and commit SHA.
10. **Operational Eligibility** — explicitly state that implementation completion does not authorize paper auto-start or live trading; enabling remains a separate operator action.

## 23. Definition of Done

The implementation is complete only when:

- Legacy trading behavior is represented by deterministic tests.
- The strategy runs through the existing intraday architecture without a special-case branch.
- Warm-up, restart, reversal, premium exit, gaps, daily loss, and square-off are fail-conservative.
- All acceptance and regression gates pass or any unrelated pre-existing failure is reproduced and disclosed precisely.
- The committed strategy remains disabled and PAPER-only.
- The runbook accurately records implementation status and remaining operational checks.
