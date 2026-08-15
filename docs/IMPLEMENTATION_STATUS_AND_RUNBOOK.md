# Implementation status and runbook

Companion to `ALGO_TRADING_FORWARD_TESTING_ARCHITECTURE_FINAL.md` (the
architecture source of truth). This file records what is actually built, the
reference-repository reuse inventory, operating commands, known limitations and
the next phase. Updated after every phase.

| | |
|---|---|
| **Current phase** | **Phase 10 — Controlled live readiness: CODE HARDENED, fully disabled.** Production parent/worker preflight wiring, Dhan order/update handling, restart-safe account-loss emergency square-off, account-wide reserve-before-submit risk plus live MTM, shared rate limiting, broker-authoritative startup/mode-transition/session-end reconciliation, strict migration history, and restore validation exist and are tested with mocks/fakes only. `ema_cross_9_21_buy` and its Rev 3.1 matrix are unchanged. **Every committed live gate remains fail-closed** (`global.live_trading_enabled: false`, `live_execution_allowed: false`, `live_approved: false`, no `mode: live` in `config/`), enforced by `scripts.assert_no_live_config_committed`. No real Dhan order/network call was made. |
| **Next phase** | Operational evidence and explicit human decisions, not more live-enabling code: complete/review the 30-day paper run, build a second genuine strategy and keep it paper, choose/configure an approved egress-IP provider and static IP, revalidate authentication operationally, then separately decide whether to approve minimum-quantity live activation. |
| **Last updated** | 13 August 2026 — recovery hardening consumes broker trades idempotently, rejects duplicate/carry-forward position ambiguity, preserves dual failures, adds audited confirmation issue/revoke tooling and enforces locked CI installs; all committed live gates remain disabled |
| **Python** | 3.11.9 (arm64 macOS) |
| **`dhanhq` pin** | `2.2.0` — **ratified**, see [Package decisions](#4-package-decisions) |
| **Live order placement** | Code path exists but is deliberately unreachable from committed configuration. Parent and child preflight both fail closed without approved operational inputs; `OPERATIONAL LIVE ACTIVATION ELIGIBLE: NO — BLOCKED`. |

### Phase 10 end-to-end hardening addendum — 13 August 2026

- Startup and final reconciliation now consume broker order/trade evidence for
  correlations backed by an existing local intent. Missing orders/fills,
  positions and realised P&L are rebuilt idempotently; incomplete or conflicting
  trade/order quantities remain critical and block entries. Every automated
  repair is persisted with its resolution action and timestamp.
- Position comparison refuses duplicate local or broker identities instead of
  allowing dict last-row-wins normalisation. Engine handoff separately refuses
  prior-day carry-forward and more than one open position because the current
  engine cannot safely represent either state.
- A simultaneous engine failure and final-reconciliation failure now preserves
  and persists both errors and still forces a FAILED heartbeat/outcome.
- `scripts.live_confirmation` provides explicit, short-lived issue/revoke
  commands with exact confirmation phrases and append-only operator evidence.
  It manages only one independent gate and cannot enable live execution.
- CI installs `requirements.lock`, installs the project with `--no-deps`, and
  runs `pip check`, so the reviewed dependency graph is exercised rather than
  freshly resolved on each run.
- Verification for this hardening pass: the explicit Rev 3.1/Phase 10 gate
  passed **352 tests**; the full suite passed **2036**, with **28 pre-existing
  skips** and three upstream `dhanhq` deprecation warnings. Ruff, mypy over 178
  source files, the committed-live-config guard, and wheel-content inspection
  all passed. No live network or order call was made.

- Account MTM publication now evaluates the shared realised+unrealised loss on
  every live position mark. A confirmed loss breach atomically latches account
  entries off and requests emergency square-off; the latch survives process
  restart and is re-honoured during a quiet feed.
- The production supervisor supplies a rate-limited, read-only Dhan broker for
  mode-transition reconciliation. An empty/recreated local database can no
  longer be treated as proof that the broker account is flat.
- Reconciliation detects duplicate correlation IDs on the broker side before
  normalization and receives both OPEN and CLOSED local position history, making
  `LOCAL_CLOSED_BROKER_OPEN` reachable in production.
- Live shutdown performs a fresh broker order/trade/position reconciliation and
  refuses clean completion for critical mismatches, pending/UNKNOWN orders or
  non-zero broker positions. Failure marks shared provenance failed before
  releasing the account lease.
- Applied migration files are now required to remain present as well as
  checksum-identical. Package discovery includes runtime/strategy/dashboard/
  script/orchestration packages and SQL migrations; CI builds and inspects the
  wheel in addition to pytest/Ruff/mypy and the closed-live-config guard.
- Operational activation is still blocked. No second real strategy was invented
  without a strategy specification, no production egress-IP provider was chosen,
  and no committed live gate was changed.

### `strategy-straddle-920` branch addendum — 15 August 2026 (unmerged)

Cut from `phase-10-controlled-live` at `017b202` onto a dedicated feature
branch, `strategy-straddle-920` — **not merged into `phase-10-controlled-live`
or `main`.** Ports the `Trading_Automation` legacy 9:20 morning-straddle
strategy exactly, as `straddle_920`, onto a new generic multi-leg engine.
`ema_cross_9_21_buy` and its Rev 3.1 acceptance matrix are unchanged; every
committed live gate remains disabled (`mode: paper`, `live_approved: false`
for `straddle_920`, confirmed by `scripts.assert_no_live_config_committed`).

- **Generic multi-leg engine, sibling to `TradingEngine`** (never a
  modification of it — confirmed by a new AST-level negative-space test,
  `tests/unit/test_no_straddle_920_branches.py`): `common/engine/
  multi_leg_models.py` (`Basket`, `LegInstance`, `BasketSignal`, leg
  role/state vocabulary), `common/engine/multi_leg_strategy.py`
  (`BaseMultiLegStrategy`, a new registry, sibling to `BaseStrategy`'s),
  `common/engine/multi_leg_engine.py` (`MultiLegEngine`). Reuses
  `PositionManager`, `LifecycleGateway`, `HubTickFeed`,
  `SquareOffAuthority`/`PersistedSquareOffAuthority`, `OptionSelector`,
  `HeartbeatEngineReporter`/`RepositoryReportWriter` **unmodified** —
  `PositionManager`'s multi-leg/short-side fitness is proven, not assumed, by
  `tests/unit/test_position_manager_short_multi_leg.py` (11 tests).
- **`EngineKind` becomes the real, additive routing discriminator**
  (`runtimes/intraday_options/config_adapter.py`): `TRADING_ENGINE` +
  `strategy_ref` present is the exact, byte-identical Phase 9 path;
  `MULTI_LEG_ENGINE` builds a new `MultiLegEngineWorkerConfig` and drives
  `MultiLegEngine`; any other kind raises `ConfigError` at load time rather
  than silently falling back to the single-leg engine. New worker module
  `runtimes/intraday_options/multi_leg_engine_worker.py`, deferred-imported
  from `worker.py` under the same import-cost discipline
  `engine_worker.py` established (`tests/unit/test_worker_import_boundary.py`
  extended with the multi-leg seam's own boundary tests). Live mode is
  refused outright for the multi-leg engine (exact legacy partial-execution
  behaviour can leave one open short leg; a live proposal needs a separate
  written risk review this repository has not done).
- **Dynamic subscriptions are now genuinely applied without waiting for an
  unrelated tick** — not merely alarmed about after 30s. `common/market_data/
  dhan.py`'s `DhanMarketFeedAdapter.start()` gained an `on_idle` callback,
  invoked on the feed-owning thread on a bounded ~1s poll
  (`IDLE_POLL_SECONDS`) regardless of tick arrival, via a real reimplementation
  of the SDK's blocking receive (`_receive_with_timeout`, `asyncio.wait_for`
  against the same loop/socket the installed `dhanhq==2.2.0` SDK already
  owns — verified by reading the installed package, not assumed).
  `common/feed/hub.py::SharedFeedHub` wires `_apply_pending_subscriptions` to
  it, and that function now isolates each pending request's failure
  (`subscriptions_rejected` counter) so one rejected request cannot crash the
  whole feed thread or block the rest of the queue. The `MarketFeedAdapter`
  Protocol, `RecordedFeedAdapter` and `ReconnectingFeed` were all widened to
  carry the new optional `on_idle` parameter (backward compatible — every
  existing test double updated to accept and, where relevant, exercise it).
  New tests: `tests/integration/test_dynamic_subscription_wake.py` (applied
  with zero ticks, concurrent requests, shutdown with a pending request,
  rejection isolation) and a `DhanMarketFeedAdapter`-level unit test against
  a real `asyncio` loop in `tests/unit/test_dhan_adapter.py`.
- **India VIX modeled without fake option-chain metadata.** New
  `common/market_data/instruments.py::MarketDataInstrument` — security_id +
  numeric segment/mode + a role label, structurally incapable of being passed
  where an `IndexMeta` (which always implies an option chain) is expected.
  `straddle_920.yaml` supplies VIX's security id (`"21"`, corroborated
  independently in two places in the legacy `Trading_Automation` reference
  tree's own "verified constants" — the same category of fact as this
  repo's own `INDEX_REGISTRY` entries, not a hardcoded option contract id;
  re-confirm against the live Dhan instrument master before the first paper
  run that actually streams VIX) and its `IDX_I` segment explicitly; missing
  or unknown values fail config load. VIX ticks update `MultiLegEngine`'s
  `_last_vix_price` only — never fed to the underlying candle builder, so a
  VIX tick structurally cannot become a "NIFTY candle".
- **Durable basket/leg state — migration `0009`** (`multi_leg_baskets.sql`,
  current head at the time this was authored: `0008`): two new, purely
  additive (`CREATE TABLE IF NOT EXISTS`, no `ALTER TABLE`) tables,
  `strategy_baskets` and `strategy_legs` — the mutable current-lifecycle
  projection for any multi-leg strategy, generic and reusable, integrated
  with (never competing with) the existing append-only `trade_ledger`
  (migration `0008`). `basket_id`/`leg_id` reuse the identity space
  `order_intents.basket_id`/`.leg_id` already carried, unused, since before
  this branch. `common/engine/multi_leg_state.py` is the row <-> `Basket`/
  `LegInstance` bridge (kept out of `common.execution`, which must not import
  `common.engine`). Migration tests extended in `tests/unit/test_migrations.py`
  (fresh-database apply, upgrade-from-the-actual-prior-head-0008 with a real
  pre-existing `trade_ledger` row proven untouched, second-startup idempotency).
- **Restart recovery**: `recover_basket()`
  (`multi_leg_engine_worker.py`) mirrors `engine_worker.recover_position()`'s
  conservative posture exactly — any row it cannot safely interpret raises
  `UnmanageableBasketState` rather than guessing, blocking new entries and
  leaving genuine exposure visibly `OPEN` for manual handling. Proven against
  two real, sequential `run_worker` calls in
  `tests/integration/test_straddle_920_restart.py`: the second run adopts the
  open basket and does not duplicate the primary entry.
- **`straddle_920` strategy** (`strategies/intraday_options/straddle_920/`):
  exact legacy rules — one primary attempt/day consumed before VIX/news
  filters, VIX `>20` skip with fail-open on missing, single-tick leg-doubling
  adjustment (max 1/day) replacing only the doubled leg on the next completed
  candle, exact 5-step risk priority (square-off, adjustment, daily loss,
  combined stop, profit target), gross-only P&L, original-basis profit
  target never rebased, current-open-leg combined-stop basis that rebases on
  replacement, zero paper slippage, 15:15 hard square-off. 13 acceptance
  tests in `tests/integration/test_straddle_920_engine.py` against a real
  `MultiLegEngine` + `SimulatedFeed` (entry, VIX x3, blackout, CE/PE
  doubling, one-adjustment-per-day, square-off, profit target, combined
  stop, daily loss).
- **Dashboard**: generic `dashboards/data/multi_leg.py` (`BasketRow`,
  `LegRow`, filtered by `strategy_id` as data — reusable by the next
  multi-leg strategy unchanged), a new "Baskets" tab in
  `dashboards/intraday_options.py`, read-only (typed read-model only, no
  inline SQL — the existing AST boundary tests extended to cover it), proven
  end to end with a seeded basket/leg fixture in
  `tests/unit/test_dashboard_apptest.py`.
- **Verification**: targeted straddle_920/dynamic-subscription/dashboard
  suites green; full `pytest` green (exact counts in the phase's final
  report); `ruff check .` clean; `mypy common strategies runtimes dashboards
  scripts` clean over 197 source files; `scripts.assert_no_live_config_committed`
  passes. No live order API was called; every committed live gate stays
  disabled.

### `strategy-straddle-920` correction pass — 15 August 2026 (P0-1 through P1-4, still unmerged)

An independent review of commit `66234b7` found the durability/correlation/
reconciliation/test-coverage claims above were not accurate: persistence
failures were swallowed rather than fail-closed, the adjustment-close state
machine could leave a replacement coexisting with an unresolved close,
`order_intents.basket_id`/`.leg_id` were never actually populated despite
the claim, restart recovery only replayed the projection rather than
cross-checking it, several acceptance rows were inspection-only despite the
prompt requiring tests, VIX's id was asserted "corroborated" without a
current check, the strategy shipped `enabled: true`, and a misleading
hardcoded `lot_size: 75` sat in the "dhan" resolver's own config block. All
eight corrections below were made on the same branch; still not merged into
`phase-10-controlled-live` or `main`.

- **P0-1 (fail-closed persistence)**: `MultiLegEngine._persist_basket`/
  `._persist_leg` gained a `critical: bool` parameter. Critical (pre-effect)
  checkpoints — the primary-attempt consumption, a pending leg's identity
  before it is subscribed, the sole adjustment's claim before its exit is
  submitted — now raise a new `MultiLegDurabilityError` on failure, which
  the caller catches to abort *before* the guarded action; entries are also
  latched off. Post-effect projection writes (recording an already-executed
  fill/close) stay best-effort, reported through a new independent
  `record_incident` callback (wired to `repository.record_error` in
  production), and never undo or block the trade that already happened —
  `_close_all`/`_handle_square_off` always attempt every leg regardless of
  a persist failure. 5 dedicated failure-injection tests in
  `tests/integration/test_straddle_920_durability.py`.
- **P0-2 (corrected adjustment-close state machine)**: new
  `common/engine/multi_leg_models.py::AdjustmentLifecycle` StrEnum (`CLAIMED`
  → `EXIT_SUBMISSION_PENDING` → `EXIT_UNKNOWN`/`EXIT_CONFIRMED` →
  `AWAITING_NEXT_CANDLE` → `REPLACEMENT_PENDING` → terminal). The claim is
  now durably critical-persisted *before* the close is even attempted (never
  after); a new `LegState.CLOSE_SUBMISSION_UNKNOWN` (migration `0009`
  widened in place — never applied to any real database, still unmerged)
  and a `_close_leg`/`_close_leg_safely` split mean a close whose outcome
  cannot be established is never retried and never silently treated as
  `CLOSED`. Both the strategy's own replacement gate and the engine's
  `_enter_legs` independently require
  `pending_replacement_state == AWAITING_NEXT_CANDLE` — a `CLAIMED`/
  `EXIT_UNKNOWN` adjusted leg can no longer coexist with a newly entered
  replacement. 3 boundary tests alongside P0-1's in the same file.
- **P0-3 (real basket/leg correlation, threaded end to end)**: `OpenPosition.
  entry_correlation_id`, `Trade.entry_correlation_id`/`.exit_correlation_id`,
  `FillOutcome.correlation_id`, and `basket_id`/`leg_id` parameters on
  `ExecutionGateway`/`PositionManager.open`/`.close`/`.adopt`/
  `OrderLifecycle.handle_signal` are the actual writers now — `order_intents.
  basket_id`/`.leg_id` were already columns but nothing had ever set them
  before this pass. `_adopt_recovered_basket` was also found not threading
  `entry_correlation_id` into `positions.adopt()` at all and was fixed.
  `dashboards/data/multi_leg.py` now joins `trade_ledger` by the
  authoritative, unique `exit_correlation_id` rather than approximating with
  `(security_id, time)`. Proven end to end (original CE/PE entry, adjusted
  leg exit, replacement entry/exit, hard square-off, restart adoption)
  against a real database in `tests/integration/test_straddle_920_correlation.py`.
- **P0-4 (real restart reconciliation, not reconstruction)**: `recover_basket`
  now calls a new `_reconcile_basket`/`_reconcile_leg`
  (`multi_leg_engine_worker.py`) that cross-checks the projection against
  `order_intents`/`orders` (via a new `ExecutionRepository.leg_order_history`)
  and the authoritative `positions` table for every leg, and detects: an
  unconfirmed OPEN-leg entry, a position with no claiming leg, quantity/side
  mismatch, duplicate open-leg mappings, a replacement awaiting entry while
  the adjusted-out leg is still open, and an unresolved exit submission with
  no exposure to fall back on — all fail closed (`UnmanageableBasketState`).
  Where the authoritative tables *can* establish the true state — a
  `PENDING_ORDER` leg that actually filled, or a `CLOSE_SUBMISSION_UNKNOWN`
  leg whose close did or did not really take — the basket is corrected in
  place (adopted as `OPEN`/`CLOSED` as the evidence shows) rather than
  merely reported, per the explicit "the controlled square-off path can
  safely manage recognised exposure" instruction; a stale `lifecycle_state:
  CLOSED` label is corrected too. 12 tests in
  `tests/integration/test_straddle_920_reconciliation.py`, built against a
  real migrated database through the repository's own write API (never raw
  SQL against the authoritative tables).
- **P1-1 (the acceptance rows that were inspection-only)**: 10 dedicated
  tests added in `tests/integration/test_straddle_920_acceptance_gaps.py` —
  weekend blocking, configured-holiday blocking, expiry-day entry permitted,
  a late-starting process still entering on its first candle, a replacement
  leg never filling on an unrelated tick, adjustment priority over the
  combined stop on the same tick, the adjusted-out leg's realised loss
  excluded from the combined-stop check, charges excluded from every
  trigger (proven with an inflated `CostRates.brokerage_per_order` so net
  and gross diverge past the threshold), a never-filled primary leg
  reaching `EXPIRED` (not left `PENDING_ORDER`) at square-off, and exact
  zero-slippage fill prices.
- **P1-2 (VIX verification)**: new `scripts/verify_vix_security_id.py` — a
  bounded, read-only, no-credential fetch of Dhan's public daily scrip
  master (the same file `ScripMaster` already downloads), never constructing
  a broker/order client, added to the read-only script tier
  (`tests/unit/test_scripts_are_read_only.py`). **Run for real** against the
  live source on 2026-08-15 (`checked_at=2026-08-15T08:03:00Z`): confirmed
  row `NSE,I,21,INDEX,...,INDIA VIX,...` — security id `"21"` genuinely
  names India VIX in the current instrument master, corroborating the
  config value against a live source for the first time (previously only
  corroborated against the legacy reference tree's own recorded constants).
  **What remains unverified**: whether the live market-feed WebSocket
  actually delivers VIX ticks for id `21`/segment `IDX_I` during market
  hours — `scripts/diagnose_live_feed.py --security-id 21 --segment 0
  --mode ticker` is the existing, already-generic tool for that, but it
  needs real credentials and market hours, neither available in this
  session (2026-08-15 is a Saturday). `config/strategies/straddle_920.yaml`
  records both facts and stays disabled until the feed check also passes.
- **P1-3 (ship disabled)**: `config/strategies/straddle_920.yaml` now reads
  `enabled: false`, with the four remaining pre-enable steps recorded in
  the file itself (VIX feed verification, current contract/lot-size
  spot-check, environment validation, database backup/migration
  validation).
- **P1-4 (no hardcoded lot-size fallback)**: the committed config's
  `lot_size: 75` line is gone. `_build_multi_leg_engine_worker_config`
  (`config_adapter.py`) now raises `ConfigError` if `contract_resolver:
  simulated` has no explicit `parameters.lot_size` (a test/fixture value is
  required, never defaulted); the `"dhan"` resolver never reads this key at
  all — lot size comes exclusively from the resolved contract's own
  metadata, exactly as `_build_option_selector` already behaved, now
  reflected in validation rather than only in a comment.
- **Verification (this pass)**: every new test green (durability,
  correlation, reconciliation, acceptance-gaps, the VIX script's own unit
  tests); the full existing straddle_920/dynamic-subscription/dashboard/
  EMA-Rev-3.1 suites green; **full `pytest` green — 2306 collected, 0
  failed, 18 skipped (all pre-existing, environment-gated: live-feed-smoke
  credential/opt-in skips and one legacy-plist skip — none related to this
  branch)**; `ruff check .` clean; `mypy common strategies runtimes
  dashboards scripts` clean over 198 source files;
  `scripts.assert_no_live_config_committed` passes. No live order API was
  called; every committed live gate stays disabled; `enabled: false` for
  `straddle_920` specifically until the outstanding feed verification and
  the other three P1-3 steps are complete.

---

## 1. Phase status

| Phase | Scope | Status |
|---|---|---|
| 0 | Reference audit + minimal bootstrap | **Complete** |
| 1 | Walking skeleton | **Complete** |
| 2 | Dhan and shared-feed hardening | **Complete** — Block 1 (offline) + Block 2 (live) |
| 3 | Preserve custom engines and policies | **Complete.** **Part 1 complete** (live-feed shutdown); **Part 2a complete** (exit registry + SuperTrend port); **Part 2b-i complete** (signal ownership + engine core); **Part 2b-ii-A complete** (the feed seam: tick channel, runtime subscription, `HubTickFeed`); **Part 2b-ii-B-1 complete** (the execution seam: square-off authority, `LifecycleGateway`, entry-block on tick drop); **Part 2b-ii-B-2 complete** (the wiring: worker engine path, supervisor queue delivery, engine restart recovery, D20 reporting bindings). **Phase 3 complete** — its acceptance gate is met in full |
| 4 | Candle, indicator and paper-execution foundation | **Complete.** **Part 1 complete** (real contract resolution — closes 17, alarms 15). **Part 2 complete** (indicator layer — closes D21). **Part 3 complete** (continuity, timezone, wall-clock square-off — closes 4 and 7, and a live blocker). **Part 4 complete** (warm-up source and injection — closes 16). **Part 5 complete** (`PaperBroker` realism — closes 5 and D11; the live Full-mode gate item ran 6 August 2026 and passed, closing known limitation 20 along the way). **Phase 4 complete** — all five parts done, its one live gate item proven rather than asserted |
| 5 | Mixed-mode supervisor and persistence | **Complete** |
| 6 | Paper recovery and expiry handling | **Complete — all five parts.** **Part 1** (daily risk state across a restart — closes the risk-limit-bypass gap in bullet 1, and finds/fixes **D58**/limitation 22 along the way). **Part 2** (position-management state snapshot/restore — exit-policy state via `BaseExit`/`RiskManager`/`BaseStrategy` snapshot hooks, and stop/target persistence through a widened `RiskManager`/`ExecutionGateway`, fail-open on a bad snapshot, negative-control-tested). **Part 3** (MFE/MAE, square-off attempts, state-version validation and position-gated last-candle idempotency — the rest of §7's "restore at minimum" list). **Part 4** (`force_square_off_before_expiry` composed into the existing `SquareOffAuthority` seam — **D66-D68**; `simulate_exchange_settlement` refused at config load, none of spec section 11's eight settlement-policy items built — limitation 27). **Part 5** (the phase's record: bullets re-checked against what is built, not assumed; D56's persistence-identity gap given a written candidate direction, not an implementation — **D69**, limitation 30). Bullet 2's fixed strikes/basket legs/rolling counters remain blocked on `FixedStrikeEngine`/`MultiLegEngine` — D56/D34, unchanged, not this phase's to close |
| 7 | Operations | **Complete — all five parts.** **Part 1** (health snapshot layer — `common/health/snapshot.py`, `auth_events`/`feed_events` writers and producers, configurable heartbeat interval). **Part 2** (Telegram in production — real notifier construction at both entrypoints, deferred delivery, rate limiting/aggregation, `notifications`-table persistence, redacted rendering — D71-D74). **Part 3** (the Streamlit dashboard — Master/Intraday Options/System Health pages plus two honest stubs, reading through `common.health.snapshot` for operational state — D75; addendum adds `effective_live_gate` status to Master, config-sourced, the one deliberate exception to database-only reads; **14 August 2026 addendum rewrote this into a unified, tabbed application — Home/Intraday Options/Positional Options/Intraday Stocks/System Health — behind a typed `dashboards/data/` read-model layer, see "Dashboard — unified Home/..." under §8; a same-day second corrective pass added a persistent per-page strategy selector plus migration `0008`'s durable `trade_ledger` (fixing a positions-reopen data-loss bug found during manual verification), see "Dashboard — strategy drill-down and durable trade ledger" under §8**). **Part 4** (PID ownership hardened onto `create_time()`, fail-first proven — D76; two previously-hidden bugs found and fixed along the way — D77/D78; seven operator scripts plus `authenticate`, `audit_events` migration `0004`, file-based square-off request channel). **Part 5** (`common/retention/` — bounded age-based DB purge in one transaction, log compression/deletion, pre-migration backup with retained-backup count, one entry point at controlled startup — D80; `ScripMasterCache.prune()` given its first caller; `Settings.algo_log_level` wiring bug found and fixed — D79) |
| 8 | LaunchAgent validation | **Complete** |
| 9 | Real strategies | **Complete** |
| 10 | Controlled live readiness | **CODE HARDENED, fully disabled.** Generic infrastructure is production-wired and fake-tested; operational activation remains blocked by paper evidence, a separately specified second real strategy, provider/static-IP, auth revalidation and explicit-approval gates. |

### What Phase 0 delivered

Packaging (`pyproject.toml`, `requirements.lock`), `.env.example` with empty
placeholders, typed layered configuration, structured logging with mandatory
secret redaction, and SQLite migration machinery with a `schema_migrations`
table.

### What Phase 0 deliberately did NOT deliver

Engines, strategies, brokers, market data, supervisors, dashboards,
orchestration, LaunchAgents — and no second architecture document.

### What Phase 1 delivered

One diagonal slice, running end to end: shared feed hub → bounded IPC queue →
spawned paper worker process → deterministic fixture signal → paper-namespaced
order intent → `PaperBroker` fill → SQLite with `execution_mode=paper` → one
read-only Streamlit tile → one notification → restart recovery → square-off.
Plus migration `0001` with the ten walking-skeleton tables.

### What Phase 1 deliberately did NOT deliver

No real strategy (Phase 9). No `TradingEngine`, `MultiLegEngine` or
`FixedStrikeEngine` port (Phase 3). No `DhanLiveBroker` order placement (Phase
10). No auth bootstrap or token cache, no feed reconnect/backoff, no option
chain (Phase 2). No bid/ask depth, latency-selected quotes, limit orders or
partial fills (Phase 4). No LaunchAgents (Phase 8).

### What Phase 2 Block 1 delivered

Phase 2 runs in **two blocks with a hard stop between them**, so the perishable
part (a live market-hours connection) can never be a prerequisite for reviewing
the rest.

**Block 1 — offline, complete.** Every item below is built, tested, linted and
type-checked with an empty `.env` and no network:

1. **Authentication bootstrap** (`common/authentication/`) — headless TOTP login,
   token precedence (env → cache → generate), retry policy driven by an explicit
   `retryable` flag, and a credential-rejection cooldown. Entry point
   `scripts/auth_bootstrap.py`.
2. **Atomic token cache** — same-directory temp file, `fsync`, `chmod 0600`,
   `os.replace`; client-identity and expiry validation; `filelock` around
   generation so N processes perform one login.
3. **`dhanhq` pin ratified** at `2.2.0`, with the evidence in section 4. Phase 1's
   `2.1.0` was withdrawn upstream *and* functionally broken for resubscription.
4. **`DhanMarketFeedAdapter` payload shape ratified from SDK source**, correcting
   three real defects (section 3.1). Replayed against a fixture generated by the
   SDK's own payload builders.
5. **Reconnect and resubscription** (`common/feed/reconnect.py`) — bounded
   exponential backoff with downward-only jitter, consecutive-failure budget,
   union resubscribe, 805–809 reason codes, 807 → token refresh, staleness
   tracking, and candle integrity across the gap.
6. **Option-chain service and three-second throttle**
   (`common/market_data/option_chain.py`) — per-key monotonic throttle, TTL cache,
   cross-strategy deduplication, freshness metadata, degrade-not-fabricate on
   failure. Data-call throttle only; no order surface.
7. **Migration `0002`** — `auth_events`, `feed_events`, `option_chain_snapshots`.
   Additive; stores no secret.
8. **Secret handling extended** — `DHAN_ACCESS_TOKEN` added to `Settings` *and* to
   `secrets_from_settings()`, runtime-minted tokens registered with the redactor
   the moment they exist, and two real redaction gaps closed (section 3.1).

### What Phase 2 Block 2 delivered

**Block 2 — live, complete (30 July 2026).** Five read-only steps, run in order
against real Dhan servers with real credentials, each confirmed read-only before
being called:

1. **`scripts/auth_bootstrap.py`** — real TOTP login (`POST
   auth.dhan.co/app/generateAccessToken`), one transient network timeout followed
   by a successful retry, then validation via `GET /v2/profile` → accepted. Token
   cached. The logged auth URL showed `dhanClientId`/`pin`/`totp` all masked
   individually — the `dhanClientId` pattern-key fix (section 3.1) holding against
   a real request, not just its unit test.
2. **One read-only market-data call** — `POST /v2/marketfeed/ltp` for NIFTY 50 via
   the SDK's own REST client → `{"IDX_I": {"13": {"last_price": 24274.2}}}`.
3. **`scripts/capture_live_tape.py`** — captured 122 real frames (121 ticks + 1
   `Previous Close`) over 30s. **Found and fixed a real hang** on the first
   attempt: the script's main thread called `adapter.stop()` from outside the
   thread driving the SDK's `asyncio` loop, racing the WebSocket close handshake
   against an in-flight `get_data()` call — confirmed by a live run, then fixed by
   moving the stop decision into the callback the feed's own loop invokes, so it
   always runs on the same thread. Scoped to this one script; **the identical
   latent flaw was found in `common/feed/reconnect.py` and left unfixed** — see
   the known-limitations entry below.
4. **One `/optionchain` call through `OptionChainService`** — NIFTY, expiry
   2026-08-04, 225 strikes returned; a second immediate call for the same key was
   served from cache (`api_calls` stayed at 1), confirming the throttle/dedup
   works against the real endpoint, not just a scripted double.
5. **Full suite re-run against the real capture** — surfaced a real fixture/test
   coverage gap (9 failures: tests referencing frame kinds — Quote, OI, an
   untraded instrument — that a real single-instrument ticker-mode capture cannot
   produce). Resolved by keeping **two fixtures**, not one replacing the other —
   see "Fixture split" below. Final state: 533 passed, 6 skipped, `ruff`/`mypy`
   clean.

**Payload shape ratified live, matching Block 1's source inference exactly** —
same keys, same types (`LTP` a formatted string, `LTT` as `HH:MM:SS`), no
divergence. Real auth-log output confirmed both redaction fixes from section 3.1
hold outside their unit tests.

**Fixture split (post-Step-5 follow-up).** `tests/fixtures/dhan_ticker_payloads_synthesised.json`
(Block 1, restored byte-for-byte from git after being briefly overwritten) is
kept **permanently** as the exhaustive branch-coverage fixture — it alone can
supply Quote/OI/status/untraded-instrument frames. `tests/fixtures/dhan_ticker_payloads_real.json`
(Block 2's capture) is kept **permanently** alongside it, used only to ratify
that the observed shape matches source inference. Neither replaces the other;
`capture_live_tape.py`'s default output path now targets the `_real` file
explicitly so a future recapture cannot overwrite the synthesised one. Coverage
was confirmed equal-or-better, not just "tests pass": 520 previously-passing +
9 previously-broken (now fixed, identical assertions, repointed at the
synthesised fixture) + 4 new (real-fixture ratification, credential scan
parametrised over both files) = 533.

### What Phase 2 deliberately did NOT deliver

No live order placement, no `DhanLiveBroker` order methods, not even a stub
(Phase 10). **No shared cross-process order-rate limiter** — spec section 14 puts
it in the controlled-live phase, and paper mode sends no orders. No engine port
(Phase 3). No real strategy (Phase 9). No bid/ask fill model (Phase 4). No
LaunchAgents (Phase 8). No second architecture document.

### What Phase 3 Part 1 delivered

Closes **limitation 1**: the live feed can now be started and stopped. Scoped to
that, deliberately — Part 2 (the engine port) was not begun.

**The rule the fix is built on.** Extracted from the capture-script fix of Block 2
and promoted from a comment in one script to the adapter contract itself:

> The thread that called `start()` owns the adapter's connection and is the only
> thread permitted to close it. Every other thread may only signal intent.

`stop()` therefore has a thread-safe counterpart, `request_stop()`, which sets a
flag and returns without touching the connection. This is not a stylistic
preference. `dhanhq`'s `MarketFeed.close_connection()` (`marketfeed.py:70-85`)
branches on `self.loop.is_running()`; from a foreign thread it takes
`asyncio.run_coroutine_threadsafe(...).result()`, an **unbounded** wait on a loop
that only the blocked owner thread can advance. A signal handler running on the
owner thread deadlocks identically, because `asyncio.get_running_loop()` still
raises there and it also takes the cross-thread branch. That is why the fix could
not be "call `stop()` from the signal handler".

| Change | Where |
|---|---|
| `request_stop()` added to the feed contract, with the ownership rule stated in the Protocol's own docstring | `common/market_data/adapter.py` |
| Live adapter: `request_stop()` clears the loop flag only; `start()`'s `finally` now closes the socket, so the owner always releases it; `stop()` **refuses and logs** rather than hanging if called across threads while the loop is live | `common/market_data/dhan.py` |
| `ReconnectingFeed.stop()` **routes** instead of delegating — signal-only from a foreign thread, direct close from the owner or when no `start()` is in flight; plus `request_stop()`, `wait_until_stopped()`, and a tick-callback handoff so a foreign thread's request is taken up on the owning thread | `common/feed/reconnect.py` |
| `RecordedFeedAdapter.request_stop()` — same flag its replay loop already tests; it holds no connection, so there is nothing only its thread may do | `common/market_data/recorded.py` |
| `SharedFeedHub.request_stop()` — adapter signal **without** the aggregator flush. Flushing while the feed thread is still writing would race and surface as a corrupt final bar rather than a crash | `common/feed/hub.py` |
| Supervisor: feed on a dedicated daemon thread, `SIGTERM`/`SIGINT` handlers installed for the feed's lifetime and **restored afterwards**, ordered shutdown (signal → `request_stop` → join → flush → drain workers → release lock) | `runtimes/intraday_options/supervisor.py` |
| Supervisor **publishes group health**: its own session (`process_role='supervisor'`, null `strategy_id`), periodic heartbeats from the otherwise-idle main thread, and `DEGRADED` + a `CRITICAL` `errors` row + a notification when the feed cannot be closed | `runtimes/intraday_options/supervisor.py` |
| Supervisor accepts an optional `Notifier`, wrapped once in `SafeNotifier` so a channel failure can never disturb a shutdown | `runtimes/intraday_options/supervisor.py` |
| Opt-in live smoke test now calls `request_stop()` from its main thread instead of `stop()` — it was making the exact cross-thread call the contract forbids | `tests/smoke/test_live_feed_smoke.py` |

`SupervisorResult` gains `stopped_by_signal` and `clean_feed_shutdown`. Both
default to today's values, so no existing assertion changed.

**What happens when the feed cannot be stopped.** A connected socket delivering
no frames leaves its owner blocked in `recv()` with no boundary at which to notice
the request. The supervisor waits `DEFAULT_SHUTDOWN_GRACE` (10 s), then completes
the rest of the shutdown — workers drained, lock released — and **never** escalates
to the cross-thread close; the feed thread is a daemon, so process exit reclaims
the socket.

Giving up quietly would be the wrong trade, so it is not quiet: the condition
raises a **`DEGRADED` group heartbeat that is deliberately the last one written**
(a `STOPPED` afterwards would erase the alarm from the dashboard tile), a
`CRITICAL` row in `errors`, and a `feed_shutdown_unclean` notification. Full
detail, including when to expect it, is limitation 13.

#### Test evidence, including the required fail-first demonstration

| Test | What it proves |
|---|---|
| `tests/integration/test_feed_cross_thread_shutdown.py` (7 tests) | The regression the old suite could not catch. Drives a **genuinely blocking** double — `start()` pinned in a no-timeout `queue.get()`, exactly like `await ws.recv()` — with a real `threading.Thread`, and stops it from another thread. Asserts the feed thread returns, that zero closes reached the adapter cross-thread, and that the close was performed by the owning thread |
| `tests/end_to_end/test_supervisor_signal.py` (5 tests) | A **real `SIGTERM`/`SIGINT` to a real child process** (`supervisor_signal_child.py`) running a supervisor over a feed whose `start()` never returns. Asserts exit code 0, `stopped_by_signal`, `clean_feed_shutdown`, close-on-owner-thread, workers drained with exit code 0, and no cross-thread close. Two of the five drive the **unclosable** case — a feed that delivers nothing and never honours the stop — and assert the alarm on all three channels by querying the child's real SQLite database, plus the clean-run case that must not raise one |

Both were **run against the unfixed code first**, which is the point of them:

```
# The cross-thread suite, source at 433aac4 (pre-fix):
6 failed, 1 passed in 27.35s
  test_stopping_from_another_thread_returns_and_closes_on_the_feed_thread
  E  AssertionError: the feed thread never returned after a cross-thread stop()

# The signal suite, source at 433aac4 (pre-fix) — the child is killed *by* the
# signal rather than handling it, and never reports a shutdown at all:
3 failed in 2.05s
  E  assert -15 == 0   # SIGTERM: died where it stood
  E  assert  -2 == 0   # SIGINT:  likewise
  E  Failed: the child reported no result
```

The one pre-fix pass is the same-thread callback stop — already correct since
Block 2, and a useful control: the double is not simply failing everything.

After the fix both suites pass, and were run **10 consecutive times** with no
flake (threading tests earn that scrutiny).

### What Phase 3 Part 1 deliberately did NOT deliver

**No Part 2 work of any kind**: no `TradingEngine` port, no exit-policy registry,
no `framework/` code. No real strategies (Phase 9). No live order placement.
`ReconnectingFeed` is still not wired into `SharedFeedHub` — the hub holds a bare
adapter, as before. That wiring belongs with the live path, not with this fix, and
the contract now makes it safe whenever it happens.

> **Superseded in part, 31 July 2026.** The sentence "no exit-policy registry"
> above describes Part 1's scope and was accurate when written. Part 2a has since
> delivered the exit-policy registry — see the next section. `TradingEngine` is
> still not ported, and the `ReconnectingFeed`/`SharedFeedHub` statement still
> holds.

### What Phase 3 Part 2a delivered

Part 2 was split again once the exit registry turned out to be portable on its
own: the ten exit policies depend on `SuperTrend` and a candle record, but not on
`TradingEngine`. Porting them first means the engine port (Part 2b) lands against
a registry that already has its regression tests passing, which is the ordering
`CLAUDE.md` requires — tests before internals.

**Ported, with import paths rewritten and nothing else of substance changed:**

| Package | Modules | Source |
|---|---|---|
| `common/exit/` | 13 — `base`, `composite`, `__init__` (registry) + all **ten** policies | `framework/exit/` |
| `common/indicators/` | 3 — `base` (`OHLC`, `StatefulIndicator`), `supertrend` | `framework/indicators/` |
| `common/warmup/` | 2 — `requirements` (`WarmupRequirement`, `IndicatorScope`) | `framework/warmup/` |
| `common/utils/` | 2 — `timeutils.parse_hhmm` | `framework/utils/` |
| `common/models/trading.py` | `OrderSide` alias, `OptionType`, `ExitReason` (15 members) | `framework/core/models.py` |

**Tests: 44 new, all passing.** `tests/unit/test_exit_engines.py` is the
reference's own suite (34 tests) ported with import paths only — verified by
`diff` of the sorted test-name lists (empty) and an identical `assert` count (94
on both sides). `tests/unit/test_exit_registry_wiring.py` adds 10 guards that the
reference suite cannot provide, because they are properties of *this* port:
D2 (ten policies), D3 (both wiring paths), the no-`framework.*`-import rule
enforced structurally via `ast`, and the `OrderSide is Side` identity check.

**Deviations D2 and D3 confirmed against the ported code**, not merely restated
from the Phase 0 audit — see section 2.3.

**Adaptations beyond import paths.** Four, each commented at the site:
`exit/base.py` `reset()` keeps its non-abstract no-op (`# noqa: B027`);
`indicators/base.py` widens `update()`'s return annotation from `None` to `Any`
to match what every implementation already returned; `indicators/supertrend.py`
replaces the reference's `# type: ignore` on the previous-bar values with an
explicit invariant check; and the same file's `state` property now guards `_line`
alongside `_trend`. Everything else is import paths, `ruff format` rewrapping,
`mypy --strict` annotations, and `bool(...)` wrappers on seven returns whose
operands are `Any` to mypy. No exit rule's arithmetic or comparison changed.

**Not ported, deliberately:** EMA, RSI, VWAP, ATR and ADX. Nothing consumes them
yet and Phase 4 owns the indicator layer; porting five more indicators now would
add ~440 lines with no caller and no test.

#### Finding: one ported test is mislabelled (carried over from the reference)

`test_momentum_close_option_premium_is_side_aware_and_consecutive`
(`tests/unit/test_exit_engines.py:72`) does not test what its name claims. It
makes five isolated `should_exit_closes(current, previous, ...)` calls, each
passing the previous close explicitly. **No consecutive streak is ever built**,
so the `_and_consecutive` half of the name is unbacked. It is also the only test
in the file that calls `should_exit_closes()` rather than `should_exit()`.

Recorded rather than fixed, on purpose. The port's whole value is that it is
byte-comparable to the reference suite; renaming a test or adding assertions
would break that comparison for a cosmetic gain, and the *side-awareness* half of
the name — the part that matters for premium-stream exits — is genuinely covered.
**Consecutive-streak behaviour on the premium stream is therefore untested, in
this repository and in the reference.** Close it in Part 2b, where
`MomentumCloseExit` first gets a real caller and the streak path actually runs;
that is the point at which a new test is worth more than diff fidelity.

> **Closed in Part 2b-i, 31 July 2026.**
> `test_momentum_close_walks_a_real_consecutive_streak_on_the_premium_stream`
> (`tests/integration/test_engine_premium_candle_exit.py`) drives a real streak
> through the real engine: the premium series walks 105 → 108 → 85, the position
> exits on the *first* adverse close, and the bar after it is never evaluated
> because the position is gone. The mislabelled ported test was left exactly as it
> was, so the Part 2a diff comparison still holds.

### What Phase 3 Part 2b-i delivered

Part 2b was split, as Part 2 already had been twice. The engine's dependency
closure in the reference is ~3,000 lines across 16 modules, and the seams it needs
(a tick channel, a persistence bridge) are separable from the port itself. So:

* **2b-i (this part)** — the signal-ownership resolution, `TradingEngine` and its
  minimal closure, proven offline.
* **2b-ii** — the hub tick channel, worker wiring, the `OrderLifecycle`-backed
  gateway, and the remaining acceptance-gate item. See section 8.

#### The blocker, resolved: the engine installs no signal handler

The rule, promoted from Part 1's feed contract to a second resource:

> A process has **exactly one** shutdown-signal installer. Handlers set a flag and
> return; the component being shut down is *asked*, and acts on the thread that
> owns it.

`TradingEngine` therefore lost `signal.signal(SIGINT, ...)`, the `_on_sigint`
closure, the `interrupted` flag and the trailing `raise KeyboardInterrupt`. In
their place:

| Piece | What it does |
|---|---|
| `request_square_off(reason)` | Thread-safe. Sets an event and returns — never touches the broker, the feed or position state. Mirrors `MarketFeedAdapter.request_stop()` |
| `on_tick`'s first branch | **The boundary that matters.** Runs on the feed's own callback thread, which under the Part 1 contract is the only thread permitted to close the connection. This is the Block 2 capture-script fix reused: move the stop decision into the callback the feed's own loop invokes |
| `run()`'s `finally` | The second boundary, for a request that arrives when no further tick will. Safe there precisely because `feed.run()` has returned, so no other thread is inside the engine |
| `square_off_requested` / `stopped_by_request` / `wait_until_stopped(timeout)` | Status and a **bounded** join, replacing the reference's re-raise. A shutdown that was asked for and completed is an orderly end, not an exception |
| `common/process/signals.py` | The supervisor's `_shutdown_signals` body, moved out and shared. One installer implementation, so the collision cannot be reintroduced by someone hand-rolling a second |

The square-off *arithmetic* is untouched: the new path funnels into the same
`_handle_square_off`, still idempotent via `_squared_off`, still ending in
`feed.stop()`. The only behavioural difference is the timestamp — the tick's rather
than `now_ist()`, matching every other close path in the engine and making the
tests deterministic. Recorded as **D18**.

**Residual, stated rather than hidden:** a connected feed delivering nothing and
never returning offers neither boundary, so the engine cannot square off. This is
the engine-level twin of limitation 13; `wait_until_stopped(grace)` is what lets
its owner give up and report instead of blocking forever. Part 2b-ii raises the
same three-channel alarm the supervisor already raises.

#### What was ported

| Module | From | Note |
|---|---|---|
| `common/engine/engine.py` | `framework/execution/engine.py` (617) | The port |
| `common/engine/models.py` | `framework/core/models.py` | `Signal`→`StrategySignal`, `Position`→`OpenPosition` (**D19**); `OptionType`/`OrderSide`/`ExitReason` **reused** from Part 2a, not redeclared |
| `common/engine/session.py` | `framework/utils/session.py` | `MarketSession` |
| `common/engine/strategy.py` | `framework/base/base_strategy.py` | `BaseStrategy` + registry |
| `common/engine/feed.py` | `framework/market_data/feed.py` | `MarketDataFeed`, `SimulatedFeed` |
| `common/engine/positions.py` | `framework/execution/order_manager.py` | `PositionManager` + the `ExecutionGateway` seam |
| `common/engine/selection.py` | `option_selector.py` + the simulated resolver | Dhan resolver not ported |
| `common/engine/risk.py`, `daily_guard.py` | `framework/risk/` | Interface + registry + the day-level latch. **No concrete risk manager** — those are Phase 9 |
| `common/engine/regime.py` | `framework/regime/` | ~120 of 421 lines, null classifier only (**D21**) |
| `common/engine/reporting.py` | `report_generator.py`, `publisher.py` | `summarise`/`DailySummary` only; the file writer and the 459-line dashboard SQLite layer are **not** ported (**D20**) |
| `common/engine/config.py` | — | `EngineConfig`: the six values the engine actually reads, built from this repo's `ResolvedConfig` (**D19**) |
| `common/candles/builder.py` | `framework/market_data/candle.py` | Emits this repo's `Candle`; reuses the aggregator's `floor_to_interval` |
| `common/utils/timeutils.py`, `common/warmup/requirements.py` | — | **Extended**, closing the two "arrives with the engine" notes Part 2a left |

`Tick` is this repository's, not a second type: the engine reads `.last_price` and
`.exchange_time`. That matters for 2b-ii — the hub already produces exactly this
type, so the tick channel needs no converter on the hot path.

#### The position seam

`PositionManager` was ported, but its `open`/`close` talk to a narrow
`ExecutionGateway` (`buy`/`sell` → `FillOutcome`) instead of the reference's
broker. Part 2b-i ships `InMemoryGateway` (adverse slippage, charges from the
**existing** `ChargesCalculator`, no database). Part 2b-ii ships `LifecycleGateway`,
driving the existing `OrderLifecycle` so every open and close is persisted with a
correlation ID, a fill row and `execution_mode`. Nothing bypasses the audited path;
the MFE/MAE bookkeeping the ported tests pin is unchanged either way.

#### Test evidence, including the required fail-first demonstration

**30 new tests.** Composition, and why it is not the composition section 8
originally listed:

| Group | Count | Provenance |
|---|---|---|
| `tests/unit/test_engine_mfe_mae.py` | 7 | **Ported verbatim** from the reference's `tests/test_mfe_mae.py` — names and assertions unchanged, three mechanical substitutions (`OpenPosition`, `InMemoryGateway`, zero-rate charges) |
| `tests/unit/test_engine_session_gating.py` | 1 | **Ported verbatim** — the single `TradingEngine` test in `tests/test_session_candle_gating.py`, `__new__` shape and all, because it pins exactly which private attributes `_on_underlying_tick` touches |
| `tests/integration/test_engine_premium_candle_exit.py` | 12 | **Rebuilt** (see D22), including the runbook item-5 streak test |
| `tests/integration/test_engine_square_off.py` | 10 | **New** — the signal-ownership gate. No upstream counterpart exists |

The suite ran **10 consecutive times** with no flake, as Part 1 and 2a required of
threading tests.

**Fail-first, run against the port while it still carried the reference's handler:**

```
# The gate file, whole. The engine takes delivery of SIGINT, squares off, and its
# re-raised KeyboardInterrupt aborts the entire pytest session:
1 failed, 1 passed
  test_the_engine_module_installs_no_signal_handler
  E  AssertionError: common/engine/engine.py imports the signal module...
!!!!!!!!!!!!!!!!!!!! KeyboardInterrupt !!!!!!!!!!!!!!!!!!!!
/Volumes/Trading/algo_trading/common/engine/engine.py:220: KeyboardInterrupt

# Same file with the nesting test deselected, so the rest can run:
7 failed, 2 passed, 1 deselected
```

That abort *is* the bug, at full strength: a signal the owner installed a handler
for was intercepted by a nested installer, which then killed the process.

**Two of the ten passed pre-fix, and that is diagnostic rather than a gap.**
`test_running_the_engine_leaves_the_processes_signal_handlers_untouched` passes
against the unfixed engine because the reference's save/restore pair is LIFO and
correct — which is precisely why the collision was invisible. Checking *after*
`run()` returns can never catch it. The test that can is
`test_an_engine_run_cannot_displace_the_supervisors_handler`, which asks the
question from **inside** `run()` via a feed that raises `SIGINT` while the engine is
running. The weak one is kept, labelled as a control.

#### Finding: a latent race in an existing walking-skeleton gate

`test_duplicate_worker_startup_is_refused` began failing during this part, and the
cause was mine but the fragility was not. The test starts a lock-holding worker,
waits for its PID file, then spawns a contender that must be refused. The holder's
consume loop breaks after `_QUEUE_POLL_SECONDS` (0.5 s) of an idle queue, so the
contender must complete a full `spawn` — re-importing the world — inside a 0.5 s
window.

Re-exporting `EngineFixtureStrategy` from `strategies/intraday_options/__init__.py`
pulled `common.engine` (and through it the exit registry and indicators) into every
spawned worker's import graph: **measured 0.382 s → 0.604 s** per child, which was
enough to lose the race.

Fixed at the cause rather than by widening the window: the engine fixture is no
longer re-exported from the package, since `worker.py` imports that package and
will never use the engine. Tests import it from its module path. **The test itself
was not touched** — it is a walking-skeleton gate, and Part 2b-i's rule is that the
harness must not change beside the engine. The race is recorded here as a real
latent fragility for whoever next edits that test or the worker's idle timeout.

### What Phase 3 Part 2b-i deliberately did NOT deliver

No tick channel on the hub, no worker wiring, no `LifecycleGateway`, no
`MarketSession`/`SquareOffPolicy` reconciliation — all Part 2b-ii. `worker.py` and
`FixtureSignalStrategy` are **untouched**, so the walking-skeleton gates still
exercise exactly what they did before. No `MultiLegEngine` or `FixedStrikeEngine`.
No concrete risk manager and no real strategy (Phase 9). No live order placement.
No ADX/ATR indicators, and therefore no real regime classifier.

---

### What Phase 3 Part 2b-ii-A delivered

Part 2b-ii was split, as Part 2 and Part 2b already had been. The feed seam and the
execution seam are independent, and the acceptance gate cannot be measured until
both land — so **2b-ii-A** is the feed seam alone, with `worker.py`,
`FixtureSignalStrategy` and both walking-skeleton gates **untouched**, and
**2b-ii-B** is the execution seam and the wiring. See section 8.

#### The square-off decision, confirmed against the spec

Section 8 required this be confirmed before implementing, and it is — though the
implementation itself is 2b-ii-B's. Two passages decide it, both pointing the same
way as the leading candidate:

| Spec passage | What it settles |
|---|---|
| Execution §10 *Intraday square-off* — "Square-off is a runtime responsibility… **A process restart must not reset the square-off state** or allow new entries after the cutoff." | `MarketSession`'s `_squared_off` is an in-memory latch a restart resets. It cannot own the decision. |
| Architecture §13 *Risk hierarchy* — "Intraday square-off time" is a **Runtime-level** control; "Entry cutoff" and "Exit time" are **Strategy-level**. | The policy owns square-off; `MarketSession.can_enter` and the holiday calendar stay. |

**One thing section 8's framing did not cover.** `SessionConfig.square_off_time` and
`SquareOffPolicy.square_off_at` are *separately configured*, so removing the second
decider still leaves two configured times free to drift apart. The reconciliation
therefore derives one from the other at wiring time rather than only removing the
duplicate decision. Agreed shape, for 2b-ii-B:

* `TradingEngine` gains an injected `SquareOffAuthority` (`due(ts)` / `completed(ts)`),
  replacing the direct `session.is_past_square_off(...)` call at `engine.py:453`.
  The default implementation wraps the session clock, so every ported engine test
  and every offline run is unchanged.
* The worker injects an implementation backed by `SquareOffPolicy` +
  `SquareOffState`, loaded at startup — so an engine restarted at 15:25 reads
  `COMPLETED` and does not re-close a position the previous process already closed.
* `SessionConfig` is built *from* `SquareOffPolicy`, so the two times cannot drift.
* `MarketSession` keeps `can_enter`, `is_open` and the holiday calendar.

#### What was built

| Piece | What it does |
|---|---|
| Hub tick channel | A **second** bounded queue per worker, opt-in. Separate rather than mixed-type because the two streams have very different arrival rates and therefore different depths — and because keeping them apart is what leaves the candle path bit-for-bit unaffected. `WorkerChannel` stays frozen; it gains `tick_queue` and a mutable `dynamic_ids` set, so the registration record and the runtime additions stay distinguishable |
| `request_subscription()` | Thread-safe, enqueue-and-return. Applied at the top of `on_tick`, on the thread that owns the connection — Part 1's rule and D18's, one layer out (**D24**) |
| Supervisor control queues | One `mp.Queue` per opted-in worker, drained every heartbeat-loop iteration. The child's end is 2b-ii-B's |
| `common/engine/hub_feed.py` | `HubTickFeed`, the `MarketDataFeed` the ported engine consumes. Maps the supervisor's `None` sentinel to a square-off request — section 8's item 3 — and forwards `subscribe()` upstream |
| `_release_queues()` | The fix for a real hang found here, not anticipated (**D25**) |
| `DEFAULT_TICK_MAX_DEPTH = 2048` | Sized from the only tick-rate measurement this repository has: Block 2's live capture, 121 ticks / 30 s for one instrument (~4/s). Two instruments at a generous ~10/s peak is ~60-70 s of buffer |

#### Finding: undelivered ticks wedged the supervisor's own exit

Not a test artefact, and worth stating plainly because it was found by a hang
rather than by reasoning. A `multiprocessing.Queue` joins its feeder thread at
interpreter exit; a producer holding undelivered events behind a full pipe never
exits, with **no error and no exit code**. Isolated and measured: 324 items × 200 B
≈ 65 KB reproduces it (`exitcode=None, alive=True`), and `cancel_join_thread()`
clears it (`exitcode=0`).

The candle channel never came close — it carries six bars where the tick channel
carries thousands of ticks. So the tick channel introduced a shutdown path that
could itself hang, which is precisely the failure Part 1 exists to prevent. Fixed
at the cause (**D25**) and pinned by
`test_undelivered_ticks_do_not_wedge_the_supervisors_exit`, which drives >400
undelivered ticks through a real supervised run.

#### Test evidence

**39 new tests**, all passing; suite **620 → 659**.

| Group | Count | What it covers |
|---|---|---|
| `tests/integration/test_feed_tick_channel.py` | 18 | Opt-in routing, the candle channel proven unchanged, the runtime-subscription round trip and its negative control, sizing at the measured live rate, drop-oldest/counted/non-blocking under an undersized queue, and the no-ticks-no-subscription residual asserted as a fact |
| `tests/integration/test_hub_tick_feed.py` | 12 | Delivery, the sentinel → square-off mapping, idle timeout, `stop()` from inside a tick callback, upstream subscription forwarding |
| `tests/integration/test_engine_over_hub.py` | 4 | **The Part 2b-ii-A gate.** A real hub → real bounded queue → real `HubTickFeed` → real `TradingEngine` → real Part 2a exit policy, on the deployed two-thread topology |
| `tests/end_to_end/test_supervisor.py` (extended) | 5 | Opt-in plumbing, the control-queue hop, a malformed request not killing the group, and the wedge regression |

Two test doubles here pace themselves deliberately rather than sleeping, and the
reason is worth recording: the hub applies a pending subscription **at a tick
boundary**, so a double that goes silent while waiting for the round trip
reproduces limitation 15 instead of testing the round trip. Both keep the
underlying ticking, bounded, so a failure is an assertion rather than a hang.

### What Phase 3 Part 2b-ii-A deliberately did NOT deliver

No `LifecycleGateway` and no persisted-`Position` bridge. No `SquareOffAuthority`
implementation — the decision is confirmed and its shape agreed, but the code is
2b-ii-B's. No worker engine path: `worker.py` and `FixtureSignalStrategy` are
**untouched**, and the child does not yet receive the tick or control queues, so
both walking-skeleton gates still exercise exactly what they did before. No
entry-block-on-tick-drop (it needs the engine wired). No adapter-level
unsubscribe. No `MultiLegEngine`/`FixedStrikeEngine`, no real strategy, no live
order placement.

---

### What Phase 3 Part 2b-ii-B delivered — Part B-1, the execution seam

Part 2b-ii-B was split, as Part 2, Part 2b and Part 2b-ii already had been. The
rule each time has been the same: separate the work that is provable offline from
the work that changes the deployed process shape.

* **B-1 (this part)** — the `SquareOffAuthority` seam, `LifecycleGateway`, and the
  tick-drop→entry-block plumbing. `worker.py`, `supervisor.py` and both fixture
  strategies are **untouched**.
* **B-2** — the wiring: worker engine path, supervisor delivering the tick and
  control queues, engine restart recovery, the D20 reporting bindings, and the
  full 2b-ii-B acceptance gate. See section 8.

The boundary is not stylistic. Putting `common.engine` into the worker's import
graph is exactly what pushed spawn cost from 0.382 s to 0.604 s in Part 2b-i and
lost `test_duplicate_worker_startup_is_refused` its 0.5 s window. B-2 owns that
risk alone, and B-1 was measured to confirm it did not touch it: **0.100 s median
over nine runs, zero `common.engine` modules** in the worker's graph — the same
figure 2b-ii-A recorded (0.111 s).

**The 2b-ii-B acceptance gate is not claimed here.** It needs the engine in a real
process, which is B-2.

#### The square-off decision, implemented

2b-ii-A confirmed the decision against the spec; this part implements it.

| Piece | What it does |
|---|---|
| `common/engine/square_off.py` | `SquareOffAuthority` (`due(ts)` / `completed(ts)`), plus both implementations |
| `SessionSquareOffAuthority` | The default. `MarketSession.is_past_square_off`'s body, **moved rather than rewritten**, including its choice to read `ts.time()` without converting to the session timezone. That is what let the seam land without changing one ported engine test |
| `PersistedSquareOffAuthority` | Policy clock **plus** the persisted `strategy_state.square_off_state` row. Reads it once, at construction — the only moment a restart is distinguishable from a continuing run. Writes twice a day: `IN_PROGRESS` from the first `due()` that answers `True`, and `COMPLETED` from `completed()`. Nothing constructs it yet; the worker does, in B-2 |
| `engine.py:453` | `self.session.is_past_square_off(...)` → `self._square_off.due(...)`, in the same position in the chain |
| `_handle_square_off` | Calls `completed(ts)` **after** every close returned and inside the `_squared_off` guard, so a completion is never recorded optimistically |
| `MarketSession` | `is_past_square_off` **removed**, not merely bypassed. `can_enter`, `is_open` and the holiday calendar stay; `square_off` survives as an attribute because `is_open` needs an upper bound and `fingerprint` must hash it |
| `SessionConfig.from_square_off_policy()` | One configured pair, two derived strings. `EngineConfig.from_resolved(..., square_off_policy=...)` **raises** if given both a session and a policy |

**The drift was real, not hypothetical.** The two sets of defaults were already
fifteen minutes apart before this part: `SessionConfig` at `end 15:15 /
square_off 15:20` against `SquareOffPolicy` at `cutoff 15:00 / square_off 15:15`.

**One thing worth stating because it changes a stated behaviour.** An `IN_PROGRESS`
read *from disk at startup* is normalised to `PENDING` and logged at `WARNING`
(**D29**). `SquareOffPolicy.trigger_at` returns `NONE` for `IN_PROGRESS`, which is
correct for the process mid-attempt and wrong for the one that inherits it —
whoever owned that attempt is dead, so it is stalled, not in flight. `trigger_at`
itself is untouched, so `test_an_in_progress_square_off_is_not_restarted_concurrently`
stays green; the normalisation is one layer up, at load.

#### `LifecycleGateway`, and the two hazards it was designed around

Every engine open and close now goes through the **existing**
`OrderLifecycle.handle_signal`, so nothing bypasses `record_signal` →
`reserve_intent` → `broker.submit` → `record_submission` → `apply_fill`. The
gateway's verbs are *directional*, not open/close — closing a long is a `sell` —
and `ExecutionRepository._upsert_position` already nets by side, so the gateway
holds no notion of which a call is and cannot get that judgement wrong.

**Hazard (a), the `signals` UNIQUE constraint.** `UNIQUE (strategy_id,
execution_mode, instrument, candle_end_at)` exists so one completed candle produces
one order. The engine is not candle-driven and the exchange timestamp is
second-resolution, so an exit and a re-entry on one contract inside one second
would collide — and `record_signal` turns a collision into `None`, which
`handle_signal` turns into a silent skip. The gateway therefore keeps a
per-instrument last-used end time and takes `max(ts, last + 1µs)` (**D26**).
Collisions within a process become structurally impossible, ordering is preserved,
and the recorded moment stays truthful to the microsecond.

**Hazard (b), a call that did not trade must raise.** Never a fabricated
`FillOutcome`. The check is deliberately on the *fills*, not on
`ExecutionResult.traded` — that property is `True` for a broker **rejection**,
because a rejected order is still an order, and checking it would have been the
obvious mistake. The raise is still mandatory even with the disambiguator in place,
because `record_signal`'s `except sqlite3.IntegrityError` is broad: a foreign-key
failure on `session_id` or a CHECK violation also returns `None` and is reported
upstream as "duplicate signal for this candle".

`FillOutcome` is built *from* the persisted fill rows, so the engine's in-memory
`Trade` and the database agree by construction rather than by convention. The
per-component charges breakdown is empty (**D27**) because `Fill` carries only a
total; the total itself is preserved.

#### Limitation 14's open half, closed

`hub.py` has claimed since 2b-ii-A that "the engine blocks new entries for the day
once a drop occurs". **It did not.** `_block_entries` was called only from
`_warm_up`, and `HubTickFeed` surfaced no drop count — because the drop is counted
in the *supervisor's* process, on the parent's `BoundedWorkerQueue`, and the engine
runs in the child. A documentation claim with no code behind it.

The only channel from parent to child is the tick queue itself, which is already
how the shutdown sentinel travels, so a `TickDropNotice` travels in band beside it
(**D28**). `HubTickFeed` matches it by **type**, not identity — it crosses a pickle
boundary — records `ticks_dropped_upstream`, fires `on_tick_dropped`, and continues:
a notice is neither a tick nor a sentinel. `TradingEngine.block_entries()` is the
public half of the existing latch, so the "no new entries today" semantics are the
warm-up failure's, reused rather than reinvented. Entry side only, so a block can
never trap an open position — asserted, not asserted-in-a-comment.

#### Finding: the notice consumes a slot on the queue it reports on

Not anticipated in the plan, and found by an existing 2b-ii-A test failing rather
than by reasoning. `test_an_undersized_tick_queue_drops_the_oldest_and_counts_it`
broke because the freshest item on the queue was now a notice rather than a tick.
Reading the captured log made the second point visible: the drop counter was
advancing **two per incoming tick**, because publishing the notice into an
already-full queue itself evicts an item.

The existing test was **narrowed, not weakened**: the drop-oldest property is now
asserted over the ticks (the candle channel has always needed the same narrowing for
its `None` sentinel), and a new assertion was added that the notice is present.

**Corrected after first commit, on measurement rather than argument.** The
paragraph that stood here justified one-notice-per-drop on the grounds that a
bounded cadence "loses the notice entirely on a short burst in a shallow queue".
That was wrong twice over, and both errors are recorded rather than quietly fixed.

First, the doubling of the *counter* is not a doubling of *tick* loss — the counter
also counts evicted notices. Measured properly (ticks published minus ticks
delivered) against a lagging consumer, 6000 ticks:

| depth | policy | ticks delivered | extra lost | ticks processed before told |
|---|---|---|---|---|
| **2048** (deployed) | one per drop | 5284 | **358** (6.3%) | 4393 |
| | cadence of 8 | 5590 | **52** (0.9%) | 4699 |
| 256 | one per drop | 2738 | 1112 (29%) | 481 |
| | cadence of 8 | 3689 | 161 (4%) | 517 |

So one-per-drop bought a **6.5% earlier block for roughly seven times the data
loss** — a trade worth making only if the latency mattered, and at four thousand
ticks either way it does not.

Second, the claim about short bursts was tested against a cadence of *64*, which was
never the alternative. Swept across depths 4-2048 and run lengths 12-4000, a cadence
of 8 reported every overflow at depth 8 and above. It did lose the notice at
**depth 4** — which turned out to be the real invariant, and a sharper one than the
original claim: a notice survives only while it is inside the retained window, and
roughly one tick is published per drop, so *a cadence at or above the queue depth
lets every notice be evicted before the next is sent*. `drop_notice_cadence()`
therefore clamps to `min(8, depth // 2)`; at the deployed depth it returns 8
unchanged, so the clamp only bites on the shallow queues that appear in tests.

Two process notes worth keeping, because both were near misses. The first sweep that
produced this conclusion **conflated "no overflow occurred" with "notice lost"** —
the deep rows had simply never dropped anything — and would have supported the
opposite conclusion if read as printed. And the parametrised regression written from
it initially included three cells that never overflow; the guard
`assert dropped > 0, "this case must actually overflow to mean anything"` is what
caught that, and is kept for the next person.

#### Test evidence, including the required fail-first demonstration

**81 new tests**, all passing; suite **659 → 740** (6 skipped).

| Group | Count | What it covers |
|---|---|---|
| `tests/unit/test_engine_square_off_authority.py` | 29 | The default authority's exact truth table; the persisted one against a **real** `ExecutionRepository` — `COMPLETED` suppresses, `FAILED` retries, inherited `IN_PROGRESS` retries and says so, the attempt written before the close and the completion after, one write not one per tick, an unreadable value failing *towards* squaring off, and no leakage across strategies or trading dates; the derived session times and the both-arguments refusal |
| `tests/integration/test_engine_lifecycle_gateway.py` | 21 | **The B-1 gate.** A real engine over real SQLite: open → premium walk → the real Part 2a policy fires → close, then the engine's `Trade` reconciled against the persisted rows field by field. Plus both hazards: three executions on one contract in one second producing three rows, a suppressed signal raising and leaving the position **open** in the database, and a broker rejection raising rather than returning a fill |
| `tests/integration/test_tick_drop_blocks_entries.py` | 31 | Real hub overflow → notice → feed → engine refuses the entry, with the same tape entering normally as the control; an open position still exiting after a drop; the latch holding the first reason; the pickle round trip; the 2b-ii-A sizing mitigation re-asserted so this block cannot fire on a healthy run; and a 12-cell depth x run-length sweep pinning that **every** overflow is reported, which is the guarantee the cadence rests on |

**Fail-first, run against the unchanged source.** New modules give only import
errors, which is weak evidence, so the defect itself was demonstrated directly:

```
MarketSession.is_past_square_off exists: True
  session says square off at 15:25 : True
  policy (state=COMPLETED) says     : NONE
  restarted engine _squared_off     : False
  restarted engine squared off again: True

SessionConfig  end/square_off: 15:15 15:20
SquareOffPolicy cutoff/square: 15:00:00 15:15:00
LifecycleGateway: No module named 'common.engine.gateway'
```

Two deciders, disagreeing, with the engine taking the one that cannot survive a
restart — and the two configured times already fifteen minutes apart. Plus the
three collection errors from the new suites:

```
E  ModuleNotFoundError: No module named 'common.engine.square_off'
E  ModuleNotFoundError: No module named 'common.engine.gateway'
E  ImportError: cannot import name 'TickDropNotice' from 'common.feed.queues'
```

The four threading-adjacent suites (54 tests) were run **10 consecutive times** with
no flake, as Parts 1, 2a and 2b-i required.

### What Phase 3 Part 2b-ii-B-1 deliberately did NOT deliver

No worker engine path and no supervisor change: `worker.py`, `supervisor.py`,
`FixtureSignalStrategy` and `EngineFixtureStrategy` are **untouched**, the child
still receives only the candle queue, and the tick-channel sentinel is still never
published — so both walking-skeleton gates measure exactly what they measured
before. **`PersistedSquareOffAuthority` has no caller outside its tests**, by
design: B-2 wires it. No engine restart recovery and no `PositionManager.adopt` —
`LifecycleGateway` does not yet write the contract record that recovery would read,
because a write with no reader is untested code that merely looks finished. No D20
reporting bindings, no three-channel alarm for the engine's silent-feed residual.
No migration. No `MultiLegEngine`/`FixedStrikeEngine`, no real strategy, no live
order placement.

### What Phase 3 Part 2b-ii-B delivered — Part B-2, the wiring

The last part of Phase 3. Everything the previous parts built offline is now joined
into a deployable worker: `worker.py` gains an engine path, `supervisor.py` delivers
the queues it had been creating and never handing over, a restarted engine adopts the
position it already holds, and the D20 reporting protocols are bound to the stores
this repository already has. **75 new tests**, suite **740 → 815** (6 skipped,
unchanged).

**The 2b-ii-B acceptance gate is claimed here, in full.** See the gate evidence in
section 4.

#### The import boundary, which is the risk this part owned alone

Re-measured before starting, on this tree:

```
import runtimes.intraday_options.worker            0.099 s,  0 engine modules
      + common.engine + EngineFixtureStrategy      0.301 s, 16 engine modules
```

A 3x increase, against the 0.5 s window in `test_duplicate_worker_startup_is_refused`.
The equivalent drag cost Part 2b-i that gate.

So the engine path is reached through **exactly one deferred import**, and everything
behind it lives in a new module `runtimes/intraday_options/engine_worker.py` that
`worker.py` never imports at module level. `EngineWorkerConfig` stays in `worker.py`
and holds only primitives, because the child unpickles it *before* any engine import
— a single engine-owned field would drag the package in through the dataclass
definition itself (**D30**).

That is enforced by test rather than by comment, three ways, because each catches
something the others cannot (`tests/unit/test_worker_import_boundary.py`):

| Test | Catches |
|---|---|
| AST of `worker.py`, module-level imports only | The **edit**, in the diff that causes it — not a number that moved a week later |
| A fresh interpreter's `sys.modules` after `import ...worker` | A **transitive** drag through some other module, which reading `worker.py` would never reveal |
| The positive half: importing `engine_worker` *does* load `common.engine` | A boundary satisfied by an engine path that silently never loads |

Wall-clock time is deliberately not asserted — module count is the cause, and a
`< 0.5 s` assertion would be flaky for reasons unrelated to the property.

**Fail-first, demonstrated rather than argued.** Hoisting one import into `worker.py`:

```
FAILED test_the_worker_module_imports_no_engine_package_at_module_level
FAILED test_a_clean_interpreter_loads_no_engine_module_for_a_worker
0.324 engine_modules=17   0.307 engine_modules=17   0.308 engine_modules=17
```

After the part, nine runs: **0.102-0.161 s, median 0.110 s, zero engine modules**.

#### Finding: the engine could not have run on its own thread

The plan for this part put the engine on its own thread, mirroring the supervisor's
feed thread, so a coordinating thread could bound its wait on a silent feed. **That
would have crashed on the first fill**, and the reason is worth recording because
nothing in the design documents made it visible:

```
ProgrammingError('SQLite objects created in a thread can only be used in that
same thread. The object was created in thread id ... and this is thread id ...')
```

`Database.connect()` does not pass `check_same_thread=False`, so the one connection
belongs to the worker's main thread — and every write from `LifecycleGateway` down
goes through it. Loosening that for every user of `Database` to serve one caller
would trade a real safety property for a workaround, so the engine stays on the main
thread (**D31**) and the problem the second thread was for is fixed at its cause
instead.

**The silent-feed residual is now fixed, not alarmed about.** `HubTickFeed.run()`
already wakes every `poll_seconds`, so it now asks a `should_stop` predicate on each
wake. A square-off requested while the stream is quiet is honoured within one poll
interval rather than waiting for a tick that may never arrive — which matters because
a live session runs with `idle_timeout_seconds=None`, making that wait unbounded.
Returning from the loop reaches `TradingEngine.run()`'s `finally`, which is the second
of the two square-off boundaries **D18** already names, so nothing crosses a thread
and the ownership rule is untouched. This closes the engine-level half of
limitation 13.

One daemon thread remains, and it deliberately touches no database: it drains the
candle stream an engine worker does not consume (**D23**) and calls
`request_square_off` if the supervisor's sentinel appears there — documented safe from
any thread, which is exactly why it is the only cross-thread call.

**The alarm still exists and now checks the outcome rather than a proxy.** After the
run returns, if a square-off was requested and a position is *still open in the
database*, the same three channels the supervisor uses fire: a `DEGRADED` heartbeat
left as the last word, a `CRITICAL` `errors` row, and a notification. "A position is
still open and nobody is managing it" is a sharper condition than "a thread did not
finish", and it is the one an operator needs.

#### Restart recovery, and who decides open from close

The `positions` row carries `instrument`, `security_id`, `quantity` and
`average_price` but not the option type, strike, expiry or lot size, so an
`OptionContract` cannot be rebuilt from it. `LifecycleGateway` now writes that record
into the existing free-form `strategy_state.payload` column on open and removes it on
close; `PositionManager.adopt(...)` seeds an `OpenPosition` **without calling the
gateway**, because calling it would place a second order and double the exposure
recovery exists to prevent. No migration — the column already exists.

**The gateway did not gain an open/close notion, and that was the point.** Its verbs
are directional by design ("this class needs no notion of which a call is, and cannot
get that judgement wrong"). So the judgement is read off `ExecutionResult.position` —
the persisted row `apply_fill` returned, already netted by side and already flipped to
`CLOSED` at quantity zero. The database decides; the gateway only records.

Adoption is sequenced by the **engine**, at the end of `_start_day()`, because only it
knows the order: `_start_day` calls `risk_manager.reset()`, so arming before it would
be wiped, and the adopted contract needs `feed.subscribe()` on the same path the
underlying just took. A recovery provider that *raises* fails closed exactly as a
raising `warmup_spec()` does — entries latch off for the day, so an inconsistency
produces "manage nothing new", never "trade alongside something unknown".

**Two silent traps in `save_strategy_state`**, both handled once in
`common/engine/state_payload.py` rather than at each call site:

1. `payload = COALESCE(excluded.payload, payload)` means `payload=None` **preserves**
   the column. Clearing the record by passing `None` would have left a restart
   adopting a position that had already been closed. Clearing writes `{}`.
2. A write replaces the whole column, so `open_position` and `day_summary` would
   clobber each other. Every write is read-modify-write.

Both are asserted against real SQL, including on the raw column value, because both
are properties of the SQL that a mocked repository would happily agree with.

#### The supervisor, delivering what it had been creating

`channel.tick_queue.raw` and the control queue now reach the child; before this the
supervisor created both and passed neither, so an engine worker would have sat on an
empty feed. The `None` sentinel is published on the **tick** channel as well as the
candle one — B-1 found this missing, which made `HubTickFeed`'s sentinel →
`request_square_off` path, built in 2b-ii-A, unreachable in the deployed shape no
matter how correct it was.

Tick drops are reported under `f"{strategy_id}:ticks"`, **not summed** into the
existing key: a dropped tick latches that worker's entries off for the day
(limitation 14) while a dropped candle does not, and one number for both would hide
which happened.

#### Test evidence

| Group | Count | What it covers |
|---|---|---|
| `tests/unit/test_worker_import_boundary.py` | 7 | The boundary, three ways, plus the `__init__` re-export path and the primitives-only rule for `EngineWorkerConfig` |
| `tests/unit/test_engine_state_payload.py` | 15 | Both `save_strategy_state` traps against real SQL — including that an emptied payload writes `{}` and not NULL — plus two keys coexisting, no leakage across strategies or trading dates, and unusable stored data degrading to empty rather than refusing to start |
| `tests/unit/test_position_manager_adopt.py` | 12 | Adoption places no order (the gateway raises on contact, so a regression fails rather than being counted), a double adopt and an `open` on top of an adopted position are both refused, the previous run's entry charges reach the closed `Trade`, and excursion restarts at zero rather than being invented |
| `tests/integration/test_engine_worker.py` | 20 | The worker end to end through the real persisted path; both refusals (no tick queue, live mode); the D20 bindings actually writing; limitation 14's block with the same tape entering normally as the control; both sentinels; and the square-off time proven to be *derived* from the policy rather than configured twice |
| `tests/integration/test_engine_worker_restart.py` | 10 | **The restart gate.** Two sequential real runs on one database: adopt, do not re-enter, close. Plus a test that the second run *really did signal an entry*, without which the no-re-entry assertion passes for the wrong reason — and three fail-closed cases (no contract record, a stale one, a different trading date) |
| `tests/end_to_end/test_engine_worker_signal.py` | 5 | **The signal gate.** A real `SIGTERM` and a real `SIGINT` to a real worker child running the real engine |
| `tests/integration/test_hub_tick_feed.py` | +3 | A silent feed honouring a square-off request, and that the check pre-empts no queued work |
| `tests/end_to_end/test_supervisor.py` | +3 | The tick sentinel counted on the wire, the separate drop key, and an engine child proven to have received **both** queues |

The signal gate waits for a position to be genuinely open in the database before
signalling, rather than sleeping: a signal delivered to an empty book squares off
nothing, which any implementation passes. Likewise the supervisor's engine-child test
asserts `subscriptions_applied`, which the child can only reach by having received
ticks on one queue and having had the other to answer on — one number proving both
deliveries and the engine running between them.

The nine threading-adjacent suites ran **10 consecutive times**: `111 passed` on all
ten, 59.5-60.5 s each. No flake.

### What Phase 3 Part 2b-ii-B-2 deliberately did NOT deliver

No `MultiLegEngine`/`FixedStrikeEngine` — the spec schedules each for its first
consumer and there still is none. No real strategy: the only `BaseStrategy` in the
tree remains a test fixture (Phase 9). No live order placement, no `DhanLiveBroker`,
no migration. No live option-chain resolver — the engine still selects strikes through
`SimulatedOptionChainResolver`, so a live path would need
`common/market_data/option_chain.py` wired in, which belongs with the live phase.
Recorded as **limitation 17**, because it is the largest remaining gap between the
engine as tested and an engine that could trade. The
engine's own warm-up manager is still not injected by the worker, so an engine worker
cold-starts its indicators; harmless for the fixture strategy, and it must be revisited
before any continuity-required strategy runs (see limitation 16). `Database` still
opens with thread affinity, so nothing in a worker may touch SQLite off the main
thread — recorded as **D31** rather than worked around.

### What Phase 4 Part 5 delivered — `PaperBroker` realism

Closes limitation **5** and deviation **D11**. Depth now reaches the fill, the
fill is priced against the touch and put on the tick grid, limit orders rest and
settle from later quotes, partial fills are modelled and accumulate correctly, and
the spec's nine rejection rules are implemented behind a code enum.

**The one-line summary of what changed:** run the same engine over the same tape
and every fill used to come back `ltp_fallback`, priced off the signal's own
reference price. A round trip on an unchanged book therefore cost exactly zero.
It now pays the spread, twice, and says which side of the book it took.

#### Pre-work found four things that changed the work

The audit was run against the pinned SDK's source, the live Dhan scrip master and
the current tree — the same method Parts 3 and 4 used, and it paid the same way.

1. **This runbook's own Part 5 note was wrong about the failure mode, and the real
   one is worse.** The note said a `"Full Data"` frame "would be counted as
   malformed". It would not: `normalise` tests `_NON_TICK_TYPES` first, then falls
   through to the unrecognised-type branch, which increments **`non_tick_frames`**
   and logs at **debug**. Switching the feed to mode 21 without fixing
   normalisation would have given a connected socket, a debug line nobody reads,
   and **zero ticks** — no candles, no indicators, no orders. A malformed count
   would at least have been visible.

2. **Dhan publishes `SEM_TICK_SIZE` in paise, and its unit is not uniform.** There
   was no tick size anywhere in this repository, so the spec's "invalid tick-size
   price" rule had nothing to check against. The live master
   (`images.dhan.co/api-data/api-scrip-master.csv`, 202 948 rows) carries
   `5.0000` on NIFTY and SENSEX `OPTIDX` rows whose real tick is ₹0.05, and
   `0.2500` on `FUTCUR` USDINR whose real tick is ₹0.0025 — paise. But `FUTIDX`
   NIFTY and NSE `EQUITY` RELIANCE both carry `10.0000`, neither of which divides
   to the tick those instruments are commonly quoted in. Taken at face value as
   rupees, NIFTY options would sit on a ₹5 grid and every order would be refused.
   Hence **D50**: the master's value is advisory, the enforced tick is
   configuration, and no tick means the rule is skipped rather than guessed.

3. **Depth prices are formatted strings, and `"0.00"` means absence.**
   `process_full` renders every level through `"{:.2f}".format(...)`, and a level
   with no resting order renders as `"0.00"` rather than being omitted. The
   reference repository's normaliser
   (`Trading_Automation/.../framework/market_data/websocket.py`) does
   `number(top.get("bid_price"))` with **no zero guard**. Ported as written, a
   quote would look two-sided with a bid of zero, and a sell would price at
   `0 - slippage` and hit the simulator's own non-positive-price rejection — i.e.
   every exit on a one-sided book refused. `_price_or_none` maps zero and negative
   to `None`. The same reference function also stamps receipt time as the tick
   timestamp, which is the exact defect Part 3 fixed; neither was copied.

4. **The reference has no fill model to port.** Its `PaperBroker` fills at
   `ref_price ± slippage_points` with no depth, no latency, no limit orders, no
   partial fills and no rejection rules — strictly weaker than what was already
   here. CLAUDE.md's "port their regression tests BEFORE changing internals" has
   nothing to bind to for this part. **Part 5 is original work, and its evidence
   standard is new tests rather than a port diff.**

Two structural blockers the plan had not identified were also found: `FillOutcome`
carries no quantity while `LifecycleGateway` *accepted* a `PARTIALLY_FILLED` order
(**D51**), and the migration runner's replay-safety rule forbids
`ALTER TABLE … ADD COLUMN`, so the submission-time quote needed a side table.

#### Depth through the pipe

Depth was dropped at four consecutive places. `Tick` already had `bid_price` and
`ask_price` fields; nothing ever filled them.

* `common/market_data/dhan.py` — `FULL_MODE = 21`, `"Full Data"` added to
  `_TICK_TYPES` (finding 1), `_top_of_book` with the zero guard (finding 3), and a
  **per-security mode map** mirroring the existing per-security segment map. The
  underlying stays on Ticker because an index has no order book in any mode; its
  contracts go on Full. `MarketFeed.validate_and_process_tuples` already batches a
  mixed instrument list by mode, so both travel on one socket. The wrong comment
  claiming "Quote/Full add depth" is corrected: `process_quote` returns volume and
  session OHLC and no book at all.
* `MarketFeedAdapter.subscribe` gains `mode=`, documented like `segment=` and for
  the same reason; `ReconnectingFeed`, `SharedFeedHub` and the worker's control
  queue forward it. The queue's tuple is now `(security_id, segment, mode)`, and
  the two-element and bare-string shapes are still accepted rather than migrated,
  because a worker that started before the upgrade can still have entries on it.
* `scripts/capture_live_tape.py` gains `--mode {ticker,quote,full}`, still
  read-only, and warns when a full-mode capture yields no two-sided book at all.

**The last two places were closed with one object rather than four widened
signatures.** The plan proposed adding bid/ask to `ExecutionGateway`'s verbs and
then to `Signal`; that means editing `PositionManager`, a Phase 3 port, for no
gain — because two consumers want the same data and neither is on that path.
`common/broker/quotes.py::QuoteBook` is a bounded per-instrument ring of recent
quotes: `OrderLifecycle` reads `latest()` to build a real depth-carrying `Quote`
instead of one synthesised from `signal.reference_price`, and `PaperBroker` reads
`after()` for latency selection and limit settlement. It is filled by
`MarketDataFeed.add_tick_observer`, which runs **before** the engine's own handler
— the engine's reaction to a tick may be to place an order priced off exactly that
tick's quote. `Signal.reference_price` is untouched, so `Fill.slippage_amount`
stays auditable against what the strategy actually saw.

#### The fill model

* **Market orders** take the ask (buy) or bid (sell), move adversely by configured
  slippage, and round **away** from the trader onto the tick grid. Rounding follows
  the same rule as slippage for the same reason: a simulator that can round in your
  favour flatters a losing strategy.
* **The LTP fallback** now applies all four of the spec's conditions rather than
  one. The missing one was the "conservative additional slippage rule"
  (`ltp_fallback_extra_ticks`, default one tick): before this, a fill priced off a
  last *trade* cost exactly as much as one priced off a resting order, so the
  simulator was quietly indifferent to whether an instrument had a book at all.
* **Limit orders** rest in a working book and settle from `on_quote()` when an
  eligible price arrives *after* submission. They fill **at the limit, never at a
  better available price**: real price improvement depends on queue position, which
  a top-of-book simulator cannot know, and assuming it would hand every resting
  order a free edge. Nothing in the engine issues one — `OrderLifecycle` builds
  every intent as `MARKET` — and `LifecycleGateway` refuses a fill-less order, so
  the model cannot leak a phantom position. It exists because spec 5.3/5.4 ask for
  the model and its test hooks. **`PaperBroker.on_quote` therefore has no
  production caller**: the working book is always empty in a real run, so nothing
  drives settlement, and wiring a pump for an empty book would be ceremony. The
  first consumer that issues a limit order wires it, in one line, beside the
  `QuoteBook` observer that already exists.
* **Partial fills** come from a deterministic `fill_quantity_policy` hook. The
  default still fills the whole quantity in one, so no existing run moves.
* **Slippage** takes the spec's section 6 shape (`slippage: {options: {mode:
  ticks, market_order_ticks: 1}}`). `slippage_points` is still accepted as an alias
  — `from_mapping` refuses unknown keys, so dropping it would turn every
  pre-Part-5 strategy file into a startup failure — and one tick on an index option
  is ₹0.05, exactly the old default, so reshaping the config moved no fill price.

#### The nine rejection rules

`PaperRejectionCode` is a `StrEnum` and the code is prefixed onto the message, so
`orders.rejection_reason` carries it without a schema change. Every dependency the
rules need is **injected and optional**, which is the only safe default: a broker
that refused everything it could not verify would refuse every order in a runtime
with no scrip master — every offline test and every simulated-contract run.
`INVALID_INSTRUMENT` and `INVALID_QUANTITY` are wired to the real master through
`DhanOptionChainResolver.instrument_rules`; `RISK_BLOCKED` and `MARKET_CLOSED`
have no producer yet (**D52**).

#### Persistence

* **`ExecutionRepository.apply_fill` accumulated nothing.** It wrote
  `fill.quantity` and `fill.price` straight onto the `orders` row, so an order with
  two fills reported the **last** one's values as the order's own. Invisible for
  three phases because nothing produced two fills. It now reads the running total
  and the quantity-weighted average back from the `fills` rows, inside the same
  transaction and after this fill's own insert, so the two cannot disagree.
* **Migration `0003_paper_fill_realism.sql`** adds `paper_fill_quotes`, one row per
  fill, closing spec section 6's "record the submission-time quote". A side table
  rather than new `fills` columns because SQLite has no `ADD COLUMN IF NOT EXISTS`
  and the runner requires replay-safe statements (**D6**). Nothing is written when
  the broker supplied no quote detail, so a future live adapter leaves no
  misleading row of NULLs.

#### Test evidence

Suite **1131 → 1242** (11 skipped, up from 10 — one new opt-in market-hours smoke
test). `ruff` and `mypy` clean across 112 source files.

| File | What it establishes |
|---|---|
| `tests/unit/test_paper_broker_realism.py` (new, 42) | The whole model: ask/bid selection, a round trip paying the spread, adverse tick rounding in both directions, the LTP fallback costing more than the touch, latency selection and its fallback counter, resting limit orders that refuse price improvement, partial-fill accumulation, and one test per rejection code |
| `tests/unit/test_quote_book.py` (new, 12) | `after()` returns the **oldest** quote past the deadline, not the best — the difference between a latency model and lookahead |
| `tests/integration/test_depth_to_fill.py` (new, 6) | The end-to-end statement: a depth-carrying tape through the real engine, gateway, lifecycle and broker yields `fill_method == "bid_ask"`, the entry takes the ask and the exit the bid, and the round trip's cost equals the spread. The same tape without a book still trades and reports `ltp_fallback` |
| `tests/unit/test_dhan_adapter.py` (+11) | A synthesised 162-byte Full frame driven through the SDK's own `process_full`, two-sided / bid-only / empty books, the `"0.00" → None` guard, and proof that a `"Full Data"` frame now produces a tick rather than a silent non-tick. **The committed frames are regenerated from the installed SDK on every run and asserted equal**, so the fixture cannot drift from the parser it describes |
| `tests/unit/test_feed_exchange_segments.py` (+8) | Mixed-mode subscription (index on 15, contract on 21) surviving a reconnect, refusal of any mode the v2 protocol does not accept, and that the engine's `SubscriptionMode`, the adapter's constants and the capture script's table all agree |
| `tests/unit/test_scrip_master.py` (+8) | The paise→rupee conversion, missing/unparseable ticks yielding `None` rather than a default, and the `security_id` index the broker's rules lookup needs |
| `tests/integration/test_execution_persistence.py` (+7) | Two fills accumulating into one `orders` row, the quantity-weighted average, the `PARTIALLY_FILLED` status, and the submission-time quote landing in `paper_fill_quotes` |
| `tests/unit/test_migrations.py` (+1, 3 updated) | `0003` applies once, replays cleanly, and upgrades a 0001-only database — and that `fills` was **not** widened in place |
| `tests/integration/test_engine_lifecycle_gateway.py` (+1) | A partial fill raising rather than being reported as whole, with the partial still fully audited |
| `tests/smoke/test_live_feed_smoke.py` (+1, opt-in) | **The gate item — run live 6 August 2026, passed.** A real `NSE_FNO` option in Full mode delivering a two-sided book, including the exchange-time trip-wire (limitation 20) |

**Tests whose expectations moved, and why each is correct.** Three legs of the
suite now price differently, and none of it is a weakened assertion:
`test_execution_persistence`'s round-trip P&L drops by one tick per leg because the
LTP fallback finally costs the conservative extra the spec requires;
`test_paper_broker`'s slippage tests moved to the section 6 config shape with the
same numbers; and `test_engine_lifecycle_gateway` gained a `_frictionless()` helper
that turns *both* slippage knobs off, because those tests assert plumbing and
should not move when pricing changes.

#### What is asserted rather than proven

**~~The live Full-mode capture has not been run.~~ CLOSED, 6 August 2026.**
`tests/smoke/test_live_feed_smoke.py::test_a_real_option_in_full_mode_delivers_a_two_sided_book`
ran live against the real socket during market hours and passed clean, on the
default cache-reusing auth path (no fresh login) with the token validated
beforehand via a real `GET /v2/profile` call. Every assertion held: the mode
split (index on Ticker, contract on Full mode 21, one adapter, one socket), a
two-sided book (`bid_price`/`ask_price` both present, `0 < bid ≤ ask`),
`ticks_with_depth > 0`, `malformed_payloads == 0`, and —
**`tick.exchange_time <= tick.received_at`**, which is the assertion that
failed on the *first* live attempt earlier the same day and led to known
limitation 20. That fix is what made this run fully clean: the first live
attempt reached a real two-sided book already (proving the depth claim), but
failed this specific assertion; today's re-run, after the fix landed
(commit `d055f54`), passed it too. The new standing trip-wire
(`test_every_live_tick_has_a_sane_exchange_time`, added closing limitation
20) was also run live and passed. The claim "a real `NSE_FNO` option in mode
21 delivers a two-sided book" no longer rests only on the SDK's source and a
packed Full frame — it is now proven against what the exchange actually
sends. **Part 5 is complete.**

Separately, **D48 is a real residual, not a formality**: on a live feed a market
order still fills at its submission quote, because the post-latency quote does not
exist when `submit()` is called. `Fill.latency_applied` and
`PaperBroker.latency_not_applied` make that a number rather than a paragraph. D48,
D51 and D53 remain open by design — they are documented scope boundaries, not
gate items, and nothing above closes them.

### What Phase 4 Part 4 delivered — warm-up source and injection

Closes limitation **16**. Ports the reference's `WarmupManager`/`WarmupSource`,
builds a Dhan historical-candle fetch this repository did not have, and wires
both into `engine_worker.py` behind an opt-in config flag so every existing
configuration keeps today's cold-start behaviour unchanged. Suite 1063 → 1131
(10 skipped, up from 8 — two new opt-in market-hours smoke tests).

#### Pre-work found two real defects in the reference before anything was ported

Mirroring Part 3's own audit discipline, the reference's design was verified
against the actual dhanhq 2.2.0 SDK and DhanHQ's own published API
documentation before being trusted, rather than ported on the strength of its
docstring:

1. **The reference's `historical.py` imports `dhanhq` directly**
   (`dhanhq(ctx).intraday_minute_data(...)`), which this repository's
   test-enforced rule forbids — only `common/market_data/dhan.py` may import
   the SDK (`tests/unit/test_dhan_adapter.py`). Porting it verbatim would have
   broken that boundary on day one. Fixed by following this repository's own
   precedent instead: `common/market_data/dhan_historical.py` speaks Dhan's
   REST endpoint directly via `httpx`, exactly as `common/authentication/
   dhan_login.py` already does for auth and for the identical reason — the
   SDK's own historical-data call has **no retry policy and no rate-limit
   handling at all** (confirmed by reading the installed SDK source), so there
   was nothing to gain by coupling to it.
2. **The reference passes `fromDate`/`toDate` as bare `"YYYY-MM-DD"` strings.**
   DhanHQ's own documentation for `POST /v2/charts/intraday` (fetched and read
   directly, not inferred from the reference or the SDK's own docstring, which
   itself conflicts with the public docs on the lookback window) specifies a
   full datetime string, e.g. `"2024-09-11 09:30:00"`. A straight port would
   have sent a request shape the endpoint does not document supporting, for
   every single fetch. Corrected in `DhanHistoricalDataClient.fetch_intraday`,
   with a fail-first test targeting exactly this format.

Neither defect was hypothetical or found by inspection — both were verified
against real sources (the installed SDK package, DhanHQ's published docs)
before being trusted, the same standard Part 1's scrip-master work and Part
3's timezone audit already set.

#### What was built

| Piece | Where |
|---|---|
| `WarmupSource` — frozen data descriptor (`security_id`, `exchange_segment`, `instrument_type`), `from_underlying`/`from_option` | `common/warmup/source.py` |
| `WarmupManager`/`WarmupResult` — the fetch+replay engine, gated `SKIPPED_EMPTY`/`SKIPPED_SESSION_LOCAL`/`SKIPPED_VOLUME`/`COLD_START`/`PARTIAL`/`WARMED` | `common/warmup/manager.py` |
| `parse_intraday_response`, `aggregate_candles`, `_prior_trading_day`, `fetch_warmup_candles_range` | `common/warmup/historical.py` |
| `DhanHistoricalDataClient` — the REST client; injectable `http_post`, bounded retry/backoff, never imports `dhanhq` | `common/market_data/dhan_historical.py` |
| `read_secret()` — the `SecretStr`-unwrap helper, promoted out of two independent copies (`scripts/auth_bootstrap.py`, `scripts/capture_live_tape.py`) now that this part's token resolution is a third caller | `common/config/secrets.py` |
| `build_warmup_manager()`, wired into `_build()` beside `build_option_selector` | `runtimes/intraday_options/engine_worker.py` |
| `EngineWorkerConfig.warmup_source` (`"none"`/`"dhan"`, default unchanged) and `.warmup_max_lookback_sessions` | `runtimes/intraday_options/worker.py` |
| `TradingEngine.__init__`'s `warmup_manager`/`warmup_source` params tightened from `object \| None` to `WarmupManager \| None`/`WarmupSource \| None` (`TYPE_CHECKING`-only import — a real one would cycle back through `common.engine.session`); the `# type: ignore[attr-defined]` on the `.warm(...)` call site is gone | `common/engine/engine.py` |

`common/warmup/__init__.py` is **deliberately left unchanged** — see "A design
decision that reversed itself" below.

#### `aggregate_candles` could not be ported — it had to be rewritten

The reference's `Candle` is a plain **mutable** dataclass; its
`aggregate_candles` mutates a bucket's `high`/`low`/`close`/`volume` in place.
This repository's `common.models.Candle` is **frozen**, with a different
field set (`start_at`/`end_at`, not `start`; requires non-defaulted
`security_id`/`instrument`). The function was rewritten, not adapted, to
mirror `common/candles/builder.py`'s own accumulate-then-freeze shape — a
private mutable per-bucket holder, frozen into a real `Candle` only once its
interval closes — bucketed through the *same* `floor_to_interval` the live
engine's own `CandleBuilder` uses. `test_aggregate_candles_matches_
candlebuilder_bucketing` cross-checks the two independently on an equivalent
price series, so "identical to the live path" is a checked property rather
than an inference from both using the same flooring function.

`_prior_trading_day` needed the same kind of adaptation for a different
reason: the reference calls `session.is_trading_day(date)`; this repository's
`MarketSession.is_trading_day` takes a `datetime` and already routes through
Part 3's timezone fix (`local_date_in`, D40). The port builds a
timezone-aware midnight via the existing `common.utils.timeutils.combine()`
and calls that public predicate, rather than reimplementing a comparison the
project already fixed once.

#### A design decision that reversed itself: `common/warmup/__init__.py`

The plan called for re-exporting `WarmupManager`/`WarmupSource` from
`common/warmup/__init__.py`. Before doing it, the actual risk was tested
rather than assumed away: `common/engine/engine.py` already does a real
(non-`TYPE_CHECKING`) `from common.warmup.requirements import
validate_warmup_config` at module level, which fires *while*
`common/engine/__init__.py` is still executing its own `from .engine import
TradingEngine` line — before `.session` is reached. Adding eager `.manager`
imports to `common/warmup/__init__.py` would make that import trigger
`common.warmup.manager`, which imports `common.engine.session`, mid-way
through the partially-initialised `common.engine` package's own `__init__`.

Verified directly in a clean interpreter: it **works today**, because
`common/engine/__init__.py` happens to import `.config` (which `.session`
needs) before `.engine`. But that is an accident of import ordering, pinned
by no test, and silently reversible by a future edit that reorders those
imports. The alternative has zero risk and matches **100% of the existing
convention** in this codebase — every current `common.warmup.requirements`
consumer (`common/indicators/base.py`, `.vwap`, `.supertrend`,
`common/engine/strategy.py`, `common/engine/engine.py`,
`strategies/intraday_options/engine_fixture_strategy.py`) already imports
directly from the submodule, never through the package. So
`common/warmup/__init__.py` is untouched, and every new caller (including
this part's own tests) imports `common.warmup.manager`/`.source` directly.

#### Underlying-only, and why `WarmupSource.from_option` has no caller

At the point `_build()` runs, no option contract has been resolved yet — the
strategy picks its strike on its first signal — and every continuity-required
indicator in this repository runs on the **underlying's** candles (`_warm_up`'s
`_sink` feeds `strategy.on_candle` unconditionally off the underlying stream).
So `build_warmup_manager()` only ever builds `WarmupSource.from_underlying(...)`.
`from_option` is ported (six lines, and every continuity-required indicator
here already runs on the underlying regardless) but unreachable until a
fixed-strike/multi-leg engine exists to call it — recorded as **D43** rather
than dropped.

**`warmup_source="dhan"` is independent of `contract_resolver`.**
`resolve_index_meta` needs no scrip master — that is the *option* resolver's
job (Part 1), not the underlying lookup. A strategy can warm its underlying's
indicators from real history while still selecting synthetic option contracts,
or vice versa. `test_warmup_source_dhan_is_independent_of_contract_resolver`
pins this against a future coupling regression.

#### Finding: a resolved security id could silently diverge from the one the feed actually subscribes

Not anticipated in the plan — found while writing `build_warmup_manager()`,
by asking "what if these two don't agree?" rather than assuming they always
do. `WorkerConfig.security_id` (what the live feed subscribes and what the
engine is told is its underlying) and `resolve_index_meta(config.instrument,
...).security_id` (what a warm-up fetch would use) are set independently —
the latter accepts an `index_security_id` override that could go stale
relative to the former. A REST fetch for the wrong id still **succeeds**, so
a divergence would silently seed indicators from a different instrument's
history than the one ticks are actually arriving for. `build_warmup_manager`
now refuses and cold-starts (logged at `ERROR`) rather than trade on that
risk — recorded as **D46**, closed by
`test_a_mismatched_resolved_security_id_refuses_to_warm`.

#### Finding: the pre-existing cold-start fallback only warned — it did not block. Closed as a same-part amendment.

Found while building the end-to-end test, not assumed from the runbook's own
prior wording. `TradingEngine._warm_up()`'s fallback path — reached whenever
`warmup_manager`/`warmup_source` are absent, which is every existing
configuration's default and remains so unless an operator sets
`warmup_source: dhan` — logs a `WARNING` for a continuity-required strategy
("signals ... must not be read as strategy edge") but **did not call
`_block_entries`**. `entry_blocked_by()` is consulted only on the
`warmup_manager` path. This was pre-existing behaviour from Phase 3 Part
2b-ii-B-2, not something Part 4's port introduced — but limitation 16's own
prior wording ("it can only refuse to trade, or trade a strategy that
declared it did not care") did not name this third case, and read as
stronger than the code actually was.

**Not left as a documented gap — closed, in this same part.** Once found,
this was exactly the failure the whole warm-up subsystem exists to prevent
(the reference's own 2026-07-17 incident), sitting behind the *default*
configuration rather than an edge case, so it was fixed rather than merely
recorded: `validate_warmup_config` now refuses construction outright for a
continuity-required strategy with no `warmup_manager`, not only when
`warmup_from_history` is explicitly false. See **D47**.
`test_no_manager_or_source_now_refuses_construction` (renamed from the test
that first pinned the gap) asserts the refusal. No other test in the tree
relied on the old fallback for a legitimate reason — confirmed by grepping
every `continuity_required=True` construction site in the repository, not
assumed.

#### Deviations recorded

**D43 — `WarmupSource.from_option` is ported but has no caller in this
repository.** See "Underlying-only" above.

**D44 — the historical-candle fetch speaks Dhan's REST endpoint directly,
never the SDK.** Same reasoning `dhan_login.py` already gives for auth:
test-enforced SDK isolation, plus the SDK's own call has no retry or
rate-limit handling to lose by not coupling to it.

**D45 — `fromDate`/`toDate` are full `"YYYY-MM-DD HH:MM:SS"` datetimes, not
bare dates.** The reference's own bug, found against DhanHQ's documentation
and corrected here rather than carried over.

**D46 — `build_warmup_manager` refuses to warm when the resolved underlying's
security id disagrees with the worker's own.** See the finding above. Not a
port of anything in the reference — a new safety check this repository's own
wiring needed and the reference's single-process-per-instrument shape never
had to consider.

**D47 — `validate_warmup_config` now also refuses construction when no
`warmup_manager` will be supplied, not only when `warmup_from_history` is
explicitly false.** A same-part amendment closing a *pre-existing* Phase 3
gap (see the finding above), not a property of the port itself — recorded
separately from D43-D46 for that reason.

#### A small, single-process retry — and what it deliberately does not solve

The dhanhq 2.2.0 SDK has zero rate-limiting or retry logic for
`/charts/intraday`. `DhanHistoricalDataClient` adds a bounded retry (default
3 attempts, short backoff) around its own fetch call — narrowly scoped to
*this worker not giving up on one transient error*, not to *coordinating
across workers*. The reference's actual fix for the equivalent problem
(`framework/warmup/coordinator.py` — a 2026-07-17 incident where concurrent
history calls across strategy processes hit Dhan's rate limit and produced
manufactured SuperTrend flips from truncated warm-up data) is explicitly
Phase 5 scope (cross-strategy coordination) and stays out of this part. The
residual is recorded as an open limitation below, not silently declared
solved by the retry that does exist.

#### Test evidence

| File | Count | What it covers |
|---|---|---|
| `tests/unit/test_config_secrets.py` | 5 | `read_secret()` — the promoted helper |
| `tests/unit/test_warmup_source.py` | 5 | Construction from `IndexMeta`/`OptionContract`, frozen-ness |
| `tests/unit/test_warmup_manager.py` | 16 | All six `WarmupResult` statuses via a synthetic `fetch_fn`/`sink`; fail-first on the `candle.start`→`.start_at` field fix and on fetch-exception→`COLD_START` never propagating; `_lookback_sessions` scaling; the exact keyword contract `fetch_fn` is called with |
| `tests/unit/test_warmup_historical.py` | 20 | Response parsing (top-level and `data`-nested, malformed→`ValueError`, bad rows skipped); `aggregate_candles` cross-checked against `CandleBuilder`; `_prior_trading_day` weekend/holiday skipping; `fetch_warmup_candles_range`'s current-bucket exclusion and non-trading-day short-circuit; fail-first on `from_at`/`to_at` reaching the client as full datetimes |
| `tests/unit/test_dhan_historical_client.py` | 12 | Request shape (fail-first on the datetime format), auth headers, retry-then-succeed with a bounded attempt count and an injected (never-real) sleep, retry exhaustion, 401/403 not retried, no `dhanhq` import anywhere in the module |
| `tests/unit/test_engine_worker_warmup_wiring.py` | 6 | `"none"` builds nothing and touches no settings; `"dhan"` without credentials cold-starts with a logged reason; `"dhan"` with credentials builds a real manager+source; independence from `contract_resolver`; an unknown value raises; the security-id mismatch guard (D46) |
| `tests/integration/test_engine_warmup_end_to_end.py` | 4 | **The end-to-end gate.** A real `TradingEngine`, hand-built `WarmupManager`/`WarmupSource`: warm-up candles reach `on_candle` before the first live candle and produce no trade from replay; a fetch failure degrades to `COLD_START` and actually blocks the live entry that would otherwise fire; a successful `WARMED` replay permits entries normally; a continuity-required strategy with no manager at all now refuses construction (`InvalidWarmupConfig`, **D47**) rather than reaching the old WARNING-only fallback |
| `tests/smoke/test_live_feed_smoke.py` | +2 | Opt-in, market-hours only. The documented response shape holds against a real call; the still-forming-bucket filter holds against a real fetch regardless of what Dhan actually returns for the open period |

Suite **1063 → 1131** (10 skipped, up from 8). Full gate: `pytest` (1131
passed), `ruff check .` (clean), `mypy` against the project's configured
package set (`common`, `strategies`, `runtimes`, `dashboards`, `scripts` —
118 files, clean). Both walking-skeleton gates re-run: every existing
configuration defaults to `warmup_source="none"`, so behaviour is unchanged.

#### What Part 4 deliberately did NOT deliver

`framework/warmup/coordinator.py` — cross-strategy/cross-process rate-limit
coordination is Phase 5. The reference's today-only `fetch_warmup_candles`
convenience wrapper and `fetch_previous_close` — only the multi-session
`fetch_warmup_candles_range` was ported (see the deviation ledger discussion
above; the today-only variant cannot supply enough bars early in a session
and has no production caller in the reference's own factory either).
`history_provider` stays unwired — the manager+source path fully subsumes it
whenever both are present, so a second, weaker fetch path was not built just
to touch a seam already covered better. `EquityScripMaster`-adjacent work,
`MultiLegEngine`/`FixedStrikeEngine`, real strategies (Phase 9), live order
placement — all unchanged and out of scope, as in every prior part.

#### What is still asserted rather than proven

The real endpoint's behaviour for a partial/still-forming candle when
`toDate` is "now" during a live session is **unverified beyond documentation
and the new opt-in smoke test** — no captured fixture exists anywhere in this
repository for `/v2/charts/intraday`, unlike the tick payload (Phase 2, both
source-ratified and live-captured) or the scrip master CSV (Phase 4 Part 1,
parsed and downloaded for real). The code excludes anything at or after the
current bucket boundary regardless of what Dhan returns, so correctness does
not depend on the answer — but the two new smoke tests can only narrow this,
not settle it, without a market-hours run this session did not make.

---

### What Phase 4 Part 3 delivered — continuity, the timezone rule, and the wall clock

Closes limitations **4** and **7**, and fixes a **live-blocking defect** the
part's own audit turned up. Suite 984 → 1063.

#### The headline: the engine would have traded nothing on a live feed

`MarketSession.is_open`/`can_enter`/`is_holiday` and
`SessionSquareOffAuthority.due` compared a timestamp's **raw** wall-clock time
against IST session bounds, with no conversion. `SquareOffPolicy` converted, so
the two deciders resolved the same instant differently — and
`DhanMarketFeedAdapter` produces **UTC-aware** ticks. Verified rather than
inferred: `reconstruct_exchange_time("04:30:00", …)` returns
`2026-08-03 04:30:00+00:00`.

Demonstrated with one real-shaped tick at 10:00 IST, mid-session:

```
hub aggregator accepted the tick?  rejected_out_of_session=0  -> ACCEPTED
engine session gate is_open?       False
```

`_on_underlying_tick` returns early when `is_open` is False, so pointed at a live
feed the engine would have built **no candles, evaluated no signals and placed no
orders, for the entire session**, reporting nothing wrong. The hub's own bars were
fine — `CandleAggregator` converts — so the divergence was between the hub and
the engine, which is the hardest kind to notice from the outside.

**Why the suite could not see it.** Every fixture in the tree is IST-offset
(`nifty_tick_tape.json` starts `2026-07-29T09:15:00+05:30`), and no test drove a
session or square-off predicate with a UTC-aware timestamp. Part 1's live
rehearsal proved a tick *arrives*; it never ran the engine. An aware-but-
unconverted datetime is worse than a naive one precisely because it looks correct
at every read site.

**The fix.** One shared helper — `common.utils.timeutils.local_time_in` /
`local_date_in` — promoted from `SquareOffPolicy._local_time`, which was the only
place already getting it right. Every session and square-off predicate now routes
through it, and `squareoff.py` became a caller rather than keeping a private copy.
**A naive datetime is refused, not guessed**: system-local is the bug class being
closed, and assuming IST would hide that a caller lost its timezone upstream.

The organising assertion in `tests/unit/test_session_timezone_rule.py` is not "is
this answer right?" but **"do these two spellings of one instant agree?"** — a
property impossible to satisfy by accident. Against the pre-Part-3 code 14 of its
tests fail, 10 of them the substantive agreement checks.

#### Two more things the audit found

**The reconnect layer was never wired.** `ReconnectingFeed` had **no constructor
call** in `common/`, `runtimes/` or `scripts/` — only in tests. The supervisor
passed the raw adapter to `SharedFeedHub`, so `on_feed_gap` was never supplied and
`mark_feed_gap` never fired: limitation 4's entire existing mitigation was dead
code in the deployed runtime, and so was Phase 2's backoff and resubscription
work. Part 3 wires it. **Limitation 2 stays open** — none of it is exercised
against a real socket drop, and this part does not claim otherwise.

**`CandleBuilder` had no gap concept at all.** The engine builds its own bars from
raw ticks (**D23**), and that builder has no `mark_feed_gap`, no session window
and no duplicate guard. A twenty-minute hole yielded **one wide bar, unmarked**.
Since the hub's discard rule protects only the hub's bars, this was the real
continuity exposure.

#### The continuity policy (limitation 4)

1. **Holes are left absent. Nothing is ever forward-filled.** A forward-filled bar
   is a fabricated print; on an option premium series it invents a price that
   never traded and every indicator downstream consumes it as real. The
   conservative floor `mark_feed_gap` already implemented is now the *policy*,
   held by decision rather than by deferral.
2. **`Candle.spans_gap`**, defaulting False. It records *how* the interval closed,
   not *whether*, so the "no `is_complete` flag on purpose" rule is intact. It
   travels with the bar across the IPC queue.
3. **The hub discards; the builder emits and marks.** Deliberately different: the
   hub fans out to every worker, so a stitched bar would corrupt all of them at
   once, and another bar will come. The builder has no discard path, and dropping
   a bar there would starve an indicator with no signal — the failure limitation
   14 calls "worse in kind".
4. **The indicator rule**, keyed off the scope Part 2 made real. A `spans_gap` bar
   never reaches indicators or produces a signal; it reaches
   `BaseStrategy.on_candle_gap` instead. `common.indicators.reset_session_local`
   resets `SESSION_LOCAL` indicators (VWAP — session-cumulative, so missing volume
   is never recovered) and leaves `SESSION_SPANNING` ones alone (EMA/RSI/ATR/ADX/
   SuperTrend are exponentially forgetting and self-correct — the same convergence
   Part 2 measured when justifying its tolerances).

**The detection rule is bucket distance, not elapsed silence**, and the first
implementation got that wrong. Ticks at 09:16 and 09:22 are six minutes apart —
longer than a five-minute interval — but they land in consecutive buckets, so no
bar is missing and nothing was stitched. Measuring elapsed time marked most bars
on any legitimately sparse stream, which an illiquid option leg certainly is. A
whole bar is missing only when the buckets are more than one interval apart.

#### The wall-clock square-off net (limitation 7)

An optional `on_poll` callable on `HubTickFeed`, invoked where `should_stop` is
checked so it fires on the busy and idle paths alike — the only thing in a worker
that runs on a timer. The worker injects a closure that reads `now_ist()`, asks
the **same** `SquareOffAuthority`, and on True calls `request_square_off`.

**One owner is preserved**: the authority still decides, the net only supplies the
clock reading the tick stream failed to supply. `PersistedSquareOffAuthority`
already returns False for a `COMPLETED` day, so a restart cannot re-close — no new
state, no migration. The close goes through the existing **D18** path, so there is
no second square-off code path. It runs on the worker's main thread, so the
authority's SQLite write is safe under **D31**.

**The trading-date guard, and how it was found.** `trigger_at` is a *time-of-day*
decision with no notion of which day; a wall clock always reports today. On first
implementation the net fired in **25 tests** before a single tick was processed,
because they replay a 2026-07-16 tape at whatever the real time happens to be. The
wall clock is now authoritative only for the day it belongs to; off that day the
candle clock remains the only decider, which is the pre-Part-3 behaviour and the
safe direction.

#### Deviations recorded

**D40 — session predicates refuse a naive datetime.** They used to accept one and
read it as system-local. Fail-closed with a message naming the argument, because
neither available guess is safe.

**D41 — the hub discards a gap-spanning bar; the engine's builder emits and marks
it.** Two builders, two behaviours, for the reasons in point 3 above. Recorded so
the asymmetry reads as a decision rather than an oversight.

**D42 — a `spans_gap` bar is skipped entirely, so the strategy does not count it.**
`on_candle` both updates indicators and produces signals, so declining to trade on
stitched data means the bar does not reach the strategy at all — and a strategy
counting bars sees one fewer. Surfaced by
`test_premium_candle_state_does_not_leak_across_a_re_entry`, whose tape has an
incidental 20-minute underlying hole; its `enter_on_candle` moved from 3 to 2 to
match, with the reason recorded in the test.

#### Test evidence

| File | Count | What it covers |
|---|---|---|
| `tests/unit/test_session_timezone_rule.py` | 35 | The agreement property across IST/UTC/New_York for every predicate; the exact 04:30-UTC case; hub-and-engine agreement on one real-shaped tick; that the adapter really does emit UTC (so the tests keep covering the real case); session boundaries in UTC; late-evening and small-hours date resolution; naive refused everywhere |
| `tests/unit/test_wall_clock_square_off.py` | 13 | The net's decision: fires past the time, silent before, asks the authority rather than deciding, once not per-poll, silent off the trading date, guard compares in IST; plus the `on_poll` hook ordering and that a raising hook is not swallowed |
| `tests/integration/test_wall_clock_square_off_threads.py` | 6 | **The limitation-7 gate**, on real threads with a real database: a feed that dies before the square-off bar still squares off, the close persisted as a real SELL through the audited path, and the run ending on the net rather than the idle timeout — plus three negative controls |
| `tests/unit/test_candle_continuity.py` | 16 | Bucket-distance detection and its boundary; the mark landing on the stitched bar and not its successor; scaling with the interval; the hub still discarding; nothing forward-filled; the indicator scope rule including the unreadable-scope fallback |
| `tests/integration/test_candle_gap_policy_wiring.py` | 9 | That the policy **runs**: a stitched bar reaching `on_candle_gap` and not `on_candle`, producing no position, with a clean-stream control; and `on_feed_gap` reaching the hub's aggregators through the supervisor's *own* feed |

**Three properties verified by breaking the code.** Reverting the session
predicates to unconverted comparison fails 14 tests. Reverting gap detection to
elapsed silence fails the boundary test. Unwiring `on_feed_gap` fails 3 wiring
tests. All restored.

#### What Part 3 deliberately did NOT deliver

No warm-up source (Part 4). No `PaperBroker` change (Part 5). **Limitation 2 stays
open**: wiring `ReconnectingFeed` puts Phase 2's backoff and resubscription on the
live path for the first time, but none of it has been exercised against a real
socket drop, and this part claims nothing about it. The engine's own square-off
remains candle-clock-driven by design — the wall clock is a *net*, not a
replacement. Live order placement remains unimplemented and fail-closed.

---

### What Phase 4 Part 2 delivered — the indicator layer

Ports EMA, RSI, VWAP, ATR and ADX from the reference into `common/indicators/`,
plus the `adx_atr` regime classifier, plus a `pandas-ta-classic` cross-check
oracle. **Closes deviation D21.** Suite 896 → 984.

#### Three statements this section makes plainly, because the flattering version is available and wrong

**1. Only 14 reference tests were ported — there is no ported indicator suite.**
The reference repository has **no dedicated indicator test file**: no
`test_indicators.py`, no per-indicator suite, and no `conftest.py` anywhere in
its tree. What came across is 4 `ConfirmedCrossover` tests, 6 `AdxAtrClassifier`
tests, 3 warm-up declaration tests and 1 continuity-required test — 14 functions,
16 collected (one is parametrised ×3), all in
`tests/unit/test_indicators_ported.py`. **Every other test covering these five
indicators in this repository was written here**, and was therefore never
validated against the reference's own behaviour. Phase 3 Part 2a could say "the
ten exit policies pass the reference's own regression suite unmodified, names and
assertion count identical to source". **No equivalent claim is available for the
indicators, and none is made.**

**2. RSI has no reference coverage whatsoever.** Nothing in the reference
repository constructs `RSI` — not a test, not a strategy, not a framework
consumer. A repo-wide grep returns hits only in `rsi.py` itself and its package
`__init__`. Its correctness here rests on the `pandas-ta-classic` cross-check
(agreement to `7.4e-16`, i.e. float precision) plus tests written in this
repository. It is the **only** one of the five whose behaviour was never
exercised by the system it came from, and it should be treated as the
least-proven of the five until a strategy consumes it in Phase 9.

**3. `pandas-ta-classic` is never on the live incremental path.** The adapter
(`common/indicators/vectorised.py`) computes batch values for the cross-check
and, from Part 4, for warm-up replay. No value the engine trades on is produced
by it. This is a deviation from how the architecture document's
"pandas-ta-classic adapter" bullet might be read: routing live values through the
library would change numbers the ported regression tests were written against,
which the project rules forbid. **Enforced structurally, not by convention** —
`tests/unit/test_indicator_oracle_boundary.py` AST-walks every shipped package
for the import *and* proves at import time, in a clean interpreter, that loading
any indicator or `common.engine.regime` does not pull the library in. Verified by
breaking it: adding the import to `regime.py` fails 2 of its tests.

#### The ceiling on this part's evidence, stated rather than implied

An agreement test against a different implementation catches a transcription
error, an off-by-one in a smoothing loop, a swapped high/low, a wrong alpha. It
**cannot** catch a formula both implementations share and both get wrong — two
implementations of the same misunderstanding agree perfectly. Part 1 could prove
its port against the reference's own regression test *and* against real broker
data; Part 2 can do neither for three of the five. That is the honest ceiling and
the green tick does not raise it.

#### The tolerances, measured before they were asserted

Probed against `pandas-ta-classic` 0.6.52 on a fixed-seed 300-bar series,
comparing the last 150 bars. Each asserted tolerance is one order of magnitude
above the measured figure and tied to a **named** structural cause — none was
chosen by widening until a test passed:

| Indicator | Measured | Asserted | Cause of the difference |
|---|---|---|---|
| RSI(14) | `7.4e-16` | `1e-12` | **none** — same Wilder formulation, same SMA seed |
| VWAP | `7.5e-16` | `1e-12` | **none** — same cumulative typical-price × volume |
| EMA(21) | `2.98e-09` | `1e-8` | first-close seed vs pandas-ta's |
| ATR(14) | `2.85e-06` | `1e-5` | first-TR seed vs SMA-of-first-`period` seed |
| ADX(14) | `5.87e-05` | `1e-4` | EWM from the first DX vs Wilder's second seeding pass |

Every inexact case is an exponentially-forgetting smoother, so the seeding
difference **decays** rather than accumulating. That is why the comparison runs
on a tail, and why the fixture length and tail offset are themselves asserted:
a shorter series legitimately diverges more, and a test that quietly shortened it
while keeping the tolerance would be measuring nothing.
`test_a_short_series_really_does_diverge_more` is the negative control — it
proves the head genuinely exceeds the tail tolerance, so pinning the tail is not
cargo cult.

ADX deserves a note. The reference's own docstring says its ADX "approximates
with a running EWM from the first DX onward — good enough for a threshold-based
filter, **not intended for exact TA-Lib parity**." Measured agreement is far
closer than that implies, because the seeding gap decays; the disclaimer is
about short series, and the fixture-length assertion is what keeps it honest.

#### What was built

| Piece | Where |
|---|---|
| `EMA`/`EMAState`, and `ConfirmedCrossover` alongside it | `common/indicators/ema.py` |
| `RSI`/`RSIState`, `VWAP`/`VWAPState`, `ATR`/`ATRState`, `ADX`/`ADXState` | `common/indicators/{rsi,vwap,atr,adx}.py` |
| `AdxAtrClassifier`, registered as `adx_atr` | `common/engine/regime.py` |
| The oracle adapter — `ema`, `rsi`, `atr`, `adx`, `vwap` over a frame | `common/indicators/vectorised.py` |
| `pandas_ta_classic.*` mypy override, scoped and explained | `pyproject.toml` |

`VWAP` is the only indicator overriding `warmup_requirement()`, declaring
`IndicatorScope.SESSION_LOCAL` **and** `requires_volume=True`. It is the one case
the `base.py` default gets wrong: warming a session-cumulative indicator with a
prior session's candles does not seed it, it corrupts it for the whole day.

#### Deviations recorded

**D38 — `pandas-ta-classic` is the oracle, not the live path.** Statement 3
above. The arch-doc bullet asks for an adapter and this delivers one; what it
does not do is compute tradable values, because that would move numbers the
ported regression tests were written against.

**D39 — `AdxAtrClassifier` lives in `regime.py`, not its own module.** D21
predicted "a new file plus one decorator, with no change here", and the *file*
half of that turned out to be wrong. The decorator registers at **import time**,
so a classifier in its own module is registered only if something imports that
module — and a classifier that silently does not exist is a worse failure than a
longer file. This repository had already collapsed the reference's five regime
modules into one, so the class joins them and registration is unconditional.
`test_registration_needs_no_second_import` proves it in a clean interpreter.

#### Test evidence

| File | Count | What it covers |
|---|---|---|
| `tests/unit/test_indicators_ported.py` | 16 | **The 14 ported reference tests** (16 collected), grouped by their source file, imports changed and nothing else |
| `tests/unit/test_indicators_behaviour.py` | 41 | Written here. Construction validation, `reset()` including a reused-vs-fresh equality check, `is_ready` transitions per indicator, the `RuntimeError` before a first value, and each indicator's own edge — RSI pinned at 100 on an unbroken rally, ATR's first true range and its gap handling asserted against *both* candidate formulas, VWAP hand-computed, ADX's `update`/`state` agreement |
| `tests/unit/test_indicator_oracle.py` | 9 | The cross-check at the tolerances above, plus the fixture-length assertion and the short-series negative control |
| `tests/unit/test_indicator_oracle_boundary.py` | 9 | Statement 3, three ways: AST walk over every shipped package, a positive half so the rule cannot pass by the adapter being deleted, and clean-interpreter import proofs for all six indicators and for `common.engine.regime` |
| `tests/unit/test_regime_classifier_wiring.py` | 13 | D21's closure: registered and resolvable, registration needing no second import, **and that it is available without being switched on** — `regime_enabled` still defaults false, and a disabled axis ignores a named classifier |

**One of the new tests was wrong and the code was right.** An ATR gap test
asserted `> 5.0` while the comment beside it derived `52/14 = 3.71`; the
assertion contradicted its own arithmetic. Rewritten to assert the exact value
against **both** candidate formulas — gap-aware `52/14` versus bar-only `5/14` —
so it now says which implementation it is rejecting instead of clearing a
threshold.

**The tolerances are load-bearing, verified by breaking the code.** Changing
ATR's Wilder alpha from `1/period` to `2/(period+1)` produced a maximum relative
difference of `1.166e-01` — four orders of magnitude above the asserted `1e-5`,
so the tolerance is tight enough to catch a real formula error rather than merely
wide enough to pass.

#### What Part 2 deliberately did NOT deliver

No warm-up source and no injection — the oracle's batch form exists and Part 4
wires it. No candle-continuity policy (Part 3). No `PaperBroker` change (Part 5).
No `framework/rolling/` port, so the reference's deeper VWAP coverage
(`CombinedVwapConfirmation`) did not come across; it is roll-confirmation logic
belonging with multi-leg work that has no consumer here. No real strategies, so
`ConfirmedCrossover` and RSI both ship without one — recorded rather than hidden,
since it is the reason their coverage is what it is. Live order placement remains
unimplemented and fail-closed; nothing in this part touches the broker, the feed,
the engine's trading decisions or the database.

---

### What Phase 4 Part 1 delivered — real contract resolution

Closes **limitation 17** and alarms **limitation 15**. The part exists because the
engine selected strikes through `SimulatedOptionChainResolver`, so every
`security_id` it chose was of the form `SIM:<underlying>:<expiry>:<strike>:<CE|PE>`
and matched nothing at the broker. That blocked any live-feed rehearsal of the
engine path, and — as this part established — it also blocked Part 5.

#### The runbook's own stated fix was wrong, and is corrected here

Limitation 17 and section 8 both said the fix was "an `OptionChainResolver` backed
by `OptionChainService`". **It cannot be.** Dhan's `/v2/optionchain` response is
keyed by strike and carries prices, open interest and greeks; it has **no
per-strike `security_id`**, so it cannot name a tradable contract at all. The
reference repository reached the same conclusion and left the reasoning in its
docstring — the daily instrument master is "more reliable than the rate-limited
Option Chain API and works outside market hours" — and that is what was ported.

`OptionChainService` is untouched and keeps its real job: live per-strike quotes
and greeks. It still has **no production caller**, which is now a deliberate
statement rather than an oversight.

#### Limitation 17 was bigger than it was recorded as

A real `security_id` is not sufficient to subscribe one. `DhanMarketFeedAdapter`
held **one** `exchange_segment` for every instrument (`dhan.py:194`, applied at
`:253`), while an options runtime needs two simultaneously: the underlying index
is `IDX_I` (segment 0) and its contracts are `NSE_FNO` (2). This was a second,
independent blocker inside the same goal and is fixed in the same part.

**Why it would have been hard to find later.** A wrong segment does not raise.
Dhan accepts the subscription and delivers nothing, so the failure presents as a
quiet market — indistinguishable at a glance from out-of-hours. No existing test
could have caught it either, because every test drove one instrument type.

#### What was built

| Piece | Where |
|---|---|
| `ScripMaster` — parses Dhan's daily `api-scrip-master.csv` into `(expiry, strike, CE/PE) → OptionRow`, with `nearest_expiry`, `strikes_for_expiry` and `atm_band` | `common/market_data/scrip_master.py` |
| `IndexMeta` / `INDEX_REGISTRY` / `SEGMENT_CODES` / `resolve_index_meta` / `segment_code` — the underlying's spot id, its own segment, and its options' segment | same |
| `ScripMasterCache` — a day-stamped local copy under `data/cache/`, written atomically | same |
| `DhanOptionChainResolver` — a dict lookup, **no per-trade API call**; plus `ContractNotListed` | `common/engine/selection.py` |
| Per-instrument exchange segments, remembered across a reconnect | `common/market_data/dhan.py`, `adapter.py`, `recorded.py`, `feed/reconnect.py`, `feed/hub.py` |
| `build_option_selector` — the `simulated`/`dhan` switch, and where the option segment is decided | `runtimes/intraday_options/engine_worker.py` |
| `(security_id, segment)` on the control queue, with `_parse_subscription_request` still accepting a bare id | `engine_worker.py`, `supervisor.py` |
| The limitation-15 alarm | `supervisor.py::_check_stuck_subscription`, `hub.pending_subscription_age_seconds` |

#### Deviations recorded

**D33 — the resolver reads the scrip master, not the option chain.** Supersedes
the fix named in limitation 17 and section 8. Reason above: the chain response
carries no per-strike `security_id`. Consequence: resolution needs one CSV
download per trading day and then costs nothing, instead of an API call per entry
that a rate limit could delay at the worst possible moment.

**D34 — `EquityScripMaster` was not ported.** The reference's NSE cash/derivative
universe serves its equity scanner. Intraday stocks are Phase 5 and nothing here
consumes it; porting ~110 lines of unexercised parser now is the same judgement
Part 2a made about the five unported indicators.

**D35 — a stale master raises rather than falling back to the last expiry.** The
reference returned `self._expiries[-1]` when every listed expiry was in the past.
That resolves contracts which can no longer be traded, so it fails *towards* a
silent bad entry. This port raises `ScripMasterError` naming the staleness.
Deliberately a behaviour change from the reference, and the only one.

**D36 — the option's exchange segment is decided in the worker, not the engine.**
`TradingEngine`'s feed contract is `subscribe(security_id)` — one string — and
widening it would mean touching the engine, whose ported session-gating test pins
it attribute by attribute. It does not need widening: everything the engine
subscribes at runtime other than the underlying *is* an option contract, so
`_subscription_sender` gives the underlying the hub's default and everything else
the option segment. Cost: a future runtime that subscribed something which was
neither would need this revisited.

**D37 — `nearest_expiry` resolves "today" in IST.** The reference used a naive
`datetime.now().date()`. At 23:30 UTC it is already tomorrow in Mumbai, so for
half an hour a night the reference would pick the wrong series.

#### Test evidence

| Group | Count | What it covers |
|---|---|---|
| `tests/unit/test_scrip_master.py` | 37 | The reference's own regression test ported with its assertions and fixture unchanged, then: both option types, the exchange lot size, the NIFTYNXT50 prefix collision, NSE-vs-BSE filtering, unparseable rows skipped, an empty result raising, expiry selection across four dates including expiry day itself, the stale-master raise, the IST default, the ATM band and its short edge, segment resolution, the cache (fetch-once-per-day, refetch next day, no partial file on a crashing fetch, an empty cached file missing rather than resolving zero contracts, pruning), and the resolver end to end through `OptionSelector` |
| `tests/unit/test_feed_exchange_segments.py` | 12 | An underlying and its option on different segments through one adapter; a reconnect restoring each to its own; one instrument refused a second segment; the union preserved across segments; delta-only sends; the hub forwarding a runtime subscription's segment and defaulting when none is named; `ReconnectingFeed` carrying it through and **not** relabelling earlier instruments |
| `tests/unit/test_engine_worker_contract_resolution.py` | 22 | The default still simulated; the `dhan` path yielding real ids and `NSE_FNO`; the exchange lot size beating the configured one; selector and resolver agreeing on the expiry; an unknown resolver name refused; the build proven to reach no network; the option carrying a segment while the underlying does not; and ten malformed control-queue shapes dropped rather than crashing the group |
| `tests/unit/test_stuck_subscription_alarm.py` | 10 | The age clock (oldest not newest, cleared on drain, no leak into a later request, cleared even for an unregistered worker) and the alarm's three channels, fired once however long the condition persists |
| `tests/smoke/test_live_feed_smoke.py` | +2 | **The rehearsal.** Skipped by default |

Suite **815 → 896**, 8 skipped (6 pre-existing + the 2 new opt-in rehearsals).

**Two properties were verified by breaking the code, not by reading it.** Reverting
the underlying-prefix guard to a `startswith` failed 5 tests — the NIFTYNXT50 rows
were indexed into the NIFTY master and overwrote its lot size, which is precisely
the silent mis-sizing the guard exists to stop. Restoring the reference's
fall-back-to-last-expiry failed the stale-master test. Both were restored
immediately afterwards.

**One of the new tests initially passed for the wrong reason and was fixed rather
than kept.** `test_non_option_instruments_are_ignored` asserted that every indexed
row had a CE/PE option type — which every lookup is keyed by, so it would have held
even if the `FUTIDX` row had been indexed. It now asserts on that row's security id.

#### What Part 1 deliberately did NOT deliver

No indicator work, no candle-continuity policy, no warm-up source, no `PaperBroker`
change — Parts 2 through 5. No `EquityScripMaster` (**D34**). No expiry-list API
call: the master already lists every expiry, so the endpoint would be a second
source of the same truth. Live order placement remains unimplemented and
fail-closed, and `build_broker` still refuses live in every configuration. The
`simulated` resolver remains the **default**, so every existing configuration
behaves exactly as it did.

**Limitation 15 is alarmed, not closed.** The underlying condition is unchanged —
the hub still applies a pending subscription only at a tick boundary (**D24**),
because the alternative is a cross-thread call into the SDK's loop, which is the
hang Part 1 of Phase 3 exists to prevent. What changed is that it can no longer
happen silently.

---

### What Phase 5 delivered — mixed-mode supervisor and persistence

**A pre-work audit found most of Phase 5's machinery already built but
unwired.** The typed per-strategy `enabled`/`mode`/`live_approved`/`engine`
config (`StrategyConfig`), the fail-closed `effective_live_gate` AND-chain, the
broker factory's refusal to reroute a blocked live strategy to paper
(`LiveExecutionBlocked`), and the mode-separated schema (`execution_mode` CHECK
column and UNIQUE keys on every trading table, since migration `0001`) all
predate this phase. `load_resolved_config` existed since Phase 0 with **no
caller outside a test**, and there was no CLI entrypoint anywhere in the
repository. So Phase 5 is predominantly wiring plus proof, not new subsystems.

**Config discovery and the config-to-worker adapter.**
`common.config.discover_enabled_strategies(config_root, runtime_id)` enumerates
`config/strategies/*.yaml` in sorted filename order and resolves every enabled
one against the given runtime. **Single-runtime limitation, recorded rather
than solved**: `StrategyConfig` carries no `runtime_id` of its own — neither
does the spec's own "required resolved strategy fields" list (section 9) — so
membership in a runtime group is implied only by naming convention (the spec's
own example is `io_supertrend_fast_v1`). This function cannot yet tell "belongs
to a different runtime" apart from "belongs to this one"; it is correct exactly
as long as one runtime's supervisor is the only caller, which is true today
(only `intraday_options` exists). See **limitation 21**.
`runtimes.intraday_options.config_adapter.build_worker_config` turns one
`ResolvedConfig` into a `WorkerConfig`, mapping `strategy.parameters` (required:
`instrument`, `security_id`; optional: `quantity`, `entry_on_candle`,
`exit_on_candle`, `paper_execution`, `cost_rates`) and `strategy.risk`'s
`entry_cutoff`/`square_off_at` into a `SquareOffPolicy`. **Deliberately
fixture-path only** — `WorkerConfig.engine` is always `None` regardless of
`StrategyConfig.engine`, because populating an `EngineWorkerConfig` needs
per-strategy parameters (`strategy_ref`, `timeframe`, `strike_step`, ...) that
no real strategy exists yet to supply; CLAUDE.md defers real strategies to
Phase 9, and synthesising them now would be the same "untested code that
merely looks finished" judgement **D34** already made about
`EquityScripMaster`.

**The entrypoint.** `runtimes.intraday_options.__main__` (also installed as
`algo-intraday-options` via `[project.scripts]`) discovers enabled strategies,
builds their `WorkerConfig`s, evaluates `effective_live_gate` for each, and
hands them to the supervisor. It refuses to start at all — before touching Dhan
credentials — when `runtimes/<id>.yaml` has `enabled: false`. Auth bootstrap and
`DhanMarketFeedAdapter` construction follow the identical pattern
`scripts/capture_live_tape.py` already established (same `AuthBootstrap`,
`read_secret`, lazy SDK import).

**Mixed-mode admission — the one real behaviour change.**
`IntradayOptionsSupervisor.add_worker` used to `raise ValueError(...
"paper-only")` for any non-paper `WorkerConfig`, which — once a config could
hold one paper and one live strategy — would have aborted the **entire group**
on the live strategy's presence, directly violating the mixed-mode gate's "the
paper strategy continues safely" (spec line 2974). `add_worker` now takes an
optional `live_gate: LiveGateDecision | None`, consulted only for a live-mode
worker (irrelevant for paper), and returns `WorkerChannel | None` instead of
raising: a blocked live strategy is refused **individually** — logged, queued
in `self._blocked_workers`, and recorded once `run()` opens the repository, via
`errors` (`severity="ERROR"`, `component="supervisor.live_gate"`) and delivered
through the notifier (`event_type="live_strategy_blocked"`) — while every paper
strategy in the group spawns and trades normally. Never rerouted to paper: a
missing `live_gate` (a caller that forgot to pass one) is treated as a block,
the same fail-closed default `effective_live_gate` itself uses for a missing
preflight. The never-reachable-until-Phase-10 case (gate somehow open) still
refuses, with its own message, for the same reason `common.broker.factory`'s
hard stop does — belt-and-braces against a future change that opens the gate
before `DhanLiveBroker` exists to serve it.

**Duplicate-worker prevention — audited and proven, not rebuilt.** Bullet 4
("prevent a second worker for the same strategy ID even when one configuration
says paper and another says live", spec line 2520) turned out to already be
satisfied by construction: `worker_lock`'s identity is
`f"{runtime_id}.{strategy_id}"` (`common/process/locks.py`) — there is no
`mode` parameter for it to include. Two new regression tests pin that fact
against a future "helpful" change that folds `execution_mode` into the
identity for consistency with the mode-separated tables:
`test_worker_lock_identity_has_no_room_for_mode` and
`test_two_worker_locks_for_the_same_strategy_id_collide_regardless_of_caller_intent`
(unit, `tests/unit/test_process_locks.py`), and
`test_a_live_mode_contender_is_still_refused_as_a_duplicate` (end-to-end, real
spawned processes, `tests/end_to_end/test_walking_skeleton.py`) — the last
proves a live-mode contender is refused **before it ever reaches the live
gate**, because the lock decides first. **Lock acquisition stays in the child
worker**, not the supervisor, despite the spec's startup-flow step 4 ("For
each enabled strategy, acquire its worker lock") reading as supervisor-side —
see **D55**.

**Mode separation, proven against real persisted state
(`tests/end_to_end/test_mode_separation.py`).** Runs a real mixed-mode group to
completion and queries the resulting SQLite file directly, deliberately not
simplified to "assert zero `execution_mode='live'` rows" — that version passes
under the exact failure it exists to catch, since a silently-rerouted-to-paper
strategy would write `execution_mode='paper'` rows and a mode-keyed count would
still read zero. Every negative assertion is keyed on `strategy_id` instead,
swept across every table found to carry that column via `sqlite_master`/
`PRAGMA table_info` rather than a hand-written list — nine tables carry both
`strategy_id` and `execution_mode` (`errors`, `fills`, `notifications`,
`order_intents`, `orders`, `positions`, `runtime_sessions`, `signals`,
`strategy_state`); `runtime_heartbeats` carries `strategy_id` alone, which a
mode-keyed sweep would have missed entirely; `paper_fill_quotes` carries
neither and is reached transitively via `orders.id`. `errors`/`notifications`
are the one deliberate carve-out — a row naming the blocked strategy there is
the *required* behaviour (an operator reading only the database must be able
to see it was deliberately blocked, not silently missing), not a leak. A
positive control (the paper strategy's rows are non-zero) runs first, since
without it every negative assertion is satisfied just as well by a run that
crashed on startup and wrote nothing. **D54** records that no production code
path in this repository calls `repository.record_notification` — the block
follows the same established pattern every existing alarm in `supervisor.py`
already uses (`record_error` for persistence, `notifier.send` for delivery),
so the delivered side is asserted against `RecordingNotifier.events` in tests,
not a `notifications` table row.

**Instrument-class rollout (spec bullet 6) — corrected on review, and split
in two.** The bullet ("add positional options and intraday stocks one at a
time; keep positional stocks a placeholder") was first read as "no strategy
exists yet, defer all of it" and deferred wholesale — too broad, on review.
The spec names three things and treats them differently: two get incremental
movement, one alone is named as the placeholder, which only makes sense if
"no consumer yet" was not meant to excuse all three equally. Split
accordingly, see **D56**: `config/runtimes/positional_options.yaml`
(`enabled: false`) and `runtimes/positional_options/__init__.py` now exist as
inert scaffolding — real, loadable, zero behavioural change — matching
exactly the precedent `intraday_options.yaml` set in Phase 1. The
supervisor/worker/persistence layer stays genuinely deferred, because it
depends on two design questions Phase 6 owns, not on there being no strategy.

### What Phase 5 deliberately did NOT deliver

- **A real membership mechanism for a second runtime.** See limitation 21.
- **`EngineWorkerConfig` wiring from `StrategyConfig.parameters`.** Every
  worker this phase's adapter builds runs the Phase 1 fixture path. Phase 9's
  first real strategy is what needs this, and is where it belongs.
- **A `positional_options` supervisor, worker or config adapter.** The runtime
  YAML and package placeholder do exist (see above); the working parts do
  not, pending Phase 6's persistence/square-off design. See **D56**.
- **`intraday_stocks` scaffolding of any kind.** Unlike `positional_options`,
  no placeholder was added — D34 already named it as needing a real consumer
  before its scanner-driven universe is worth porting, and that judgement is
  unchanged here.
- **A real risk gate.** `OrderIntent.risk_decision` is still hardcoded to
  `ALLOWED` (**D52**, unchanged) — Phase 5/6 was already named as its owner
  before this phase started, and nothing here changes that.
- **LaunchAgent wiring for the new entrypoint.** Phase 8's job; `__main__.py`
  is run manually (`python -m runtimes.intraday_options` or
  `algo-intraday-options`) until then.

---

## 2. Reference-repository reuse inventory

Audited read-only on 29 July 2026. **No file under `Trading_Automation` was
written or modified.** No credential, database, token, log or runtime artefact
was copied. The new repository has **no runtime dependency** on the old one.

### 2.1 Where the reusable code actually is

Of the four sub-repos in the `Trading_Automation` monorepo, only one contributes
reusable code:

| Sub-repo | Contribution |
|---|---|
| `option_strategies/Trading_Strategies_Automation_v2/` | **All three engines, broker factory, exit registry, shared feed, supervisor** (~20.8k LOC framework, ~8.6k LOC top-level tests) |
| `weekly_strategies/Weekly_Strategies_Automations/` | Migration-runner *pattern* only |
| `stock_strategies/Stock_Strategies_Automations/` | Nothing reusable yet (scanner-driven stocks, Phase 9) |
| `common/` (monorepo root) | **Nothing.** Contains live credentials and an access token — never to be read or copied |

All paths below are relative to
`Trading_Automation/option_strategies/Trading_Strategies_Automation_v2/`.

### 2.2 Component inventory

#### `TradingEngine` — `framework/execution/engine.py` (617 lines)

Single-leg, underlying-driven. Underlying ticks build candles; the strategy is
consulted on candle close; option ticks drive risk management and pending-entry
fills. Dependency-injected (feed, broker, selector, strategy, position manager,
report generator), so the same orchestration runs live or simulated.

- **Preserve:** tick-routing split (underlying → candles → signal; option → risk),
  entry-window gating via `MarketSession.can_enter`, mandatory square-off
  including on SIGINT/KeyboardInterrupt, the opt-in per-position premium candle
  stream (`needs_option_candles`), dependency injection at the constructor.
- **Adapt:** replace the direct `MarketDataFeed` with the new shared-feed IPC
  consumer; replace `dashboard.publisher` with the new persistence repositories;
  route orders through the new order-intent/correlation-ID lifecycle rather than
  calling the broker directly; add `execution_mode` to everything persisted.
- **Tests to port:** `tests/test_opening_candle_coverage.py` (670),
  `tests/test_session_candle_gating.py` (107), `tests/test_mfe_mae.py` (187),
  `strategies/ema_cross/tests/test_premium_candle_exit.py`.

#### `MultiLegEngine` — `framework/execution/multi_leg_engine.py` (746 lines)

Baskets: several legs open simultaneously, each with its own stop, plus combined
P&L across the basket. Deliberately a sibling of `TradingEngine`, not a
modification — the single-leg tick routing assumes one open position.

- **Preserve:** the basket/leg model, per-leg plus combined risk evaluation, the
  `keep_watching` tracker that keeps updating a closed leg's price series for
  re-entry decisions, VIX tick handling, combined-premium candle construction.
- **Adapt:** same three adaptations as `TradingEngine`, plus basket/leg IDs must
  become first-class persisted columns rather than in-memory identity.
- **Tests to port:** `tests/test_multi_leg_combined_candle.py` (90),
  `tests/test_multi_leg_leg_candles.py` (211),
  `tests/test_multi_leg_basket_watch.py` (160).
- **Port when:** the first multi-leg consumer is scheduled — not before.

#### `FixedStrikeEngine` — `framework/execution/fixed_strike_engine.py` (922 lines)

Dual-chart: SuperTrend runs on the CE option's own price chart and the PE
option's own chart independently, so the engine builds *two* candle series. The
strike is locked for the session at a configured selection time. Two independent
positions with per-chart risk plus a shared `DailyRiskGuard`.

- **Preserve:** independent CE/PE candle and indicator state with no leakage
  between legs, session strike locking, per-chart risk under one day-level
  breaker, the warm-up coordinator's fail-closed behaviour.
- **Adapt:** same three adaptations; selected strikes must be persisted for
  restart recovery (Phase 6) rather than held only in memory.
- **Tests to port:** `strategies/nifty_fixed_strike_tight_buy/tests/test_strategy.py`,
  `.../tests/test_combined_candle_exit.py`, plus
  `tests/test_warmup_fail_closed_gate.py` (579) and `tests/test_warmup_hub.py` (906).
- **Port when:** the first fixed-strike consumer is scheduled — not before.

#### `build_broker(cfg)` — `framework/broker/broker_factory.py` (43 lines)

One function, one branch: `cfg.mode is TradingMode.LIVE` → `DhanBroker` (requires
a `DhanContext`), else `PaperBroker`.

- **Preserve:** the shape exactly — one factory function is the *only* place that
  decides which broker a strategy runs against, called once at worker startup.
  Also preserve the existing refusal to build a live broker without a context.
- **Adapt (important):** the current factory has *no safety gate* — `mode: live`
  alone is enough to construct a live broker. The new factory must consult
  `common.config.effective_live_gate()` and **refuse to start** when the gate
  blocks. It must never fall back to `PaperBroker`. `common/config/models.py`
  already implements that gate, fail-closed.
- **Tests to port:** none exist for the factory. New tests required, proving both
  strategy-wise routing and that a blocked live strategy is *not* rerouted to paper.

#### Broker interface — `framework/broker/base.py`, `paper.py`, `charges.py`

- **`base.Broker`** — minimal: `connect`, `buy_market`, `sell_market`, `get_ltp`.
  **Adapt:** the spec requires a considerably wider contract (modify, cancel,
  order status, order book, trades, positions, exit basket, health, correlation-ID
  lookup). Treat `base.py` as a starting shape, not a target.
- **`PaperBroker`** — **rewrite, do not port.** It fills at
  `ref_price ± fixed slippage_points` with no bid/ask, no latency, no tick/lot
  validation, no limit orders, no partial fills and no rejection rules. The spec's
  paper-fill model is a genuinely different component.
- **`ChargesCalculator`** (`charges.py`, 109 lines) — **directly reusable.**
  Brokerage plus statutory charges with a per-leg breakdown, already separated
  from fill logic exactly as the spec wants.

#### Exit-policy registry — `framework/exit/` (11 modules, ~700 lines)

A name→class registry (`register_exit_engine` decorator), a `CompositeExit` that
ORs children in fixed priority so the reported reason is deterministic, and a
config-driven `build_exit_engine`.

- **Preserve:** the registry, the decorator, `CompositeExit`'s fixed-priority
  evaluation, and the rule that **every** child is advanced each candle (so a
  stateful engine that is not first to fire still tracks its state), and all ten
  registered policies.
- **Adapt:** only the import paths and the config-object shape.
- **Tests to port:** `tests/test_exit_engines.py` (378) — the single highest-value
  port in the whole audit. It is self-contained, depends only on
  `framework.core.models` / `framework.exit` / `framework.indicators`, and covers
  every registered engine. Port it **before** touching any exit internals. Also
  `tests/test_momentum_close_strategy_configs.py` and
  `tests/test_momentum_low_or_highest_close_strategy_configs.py`.

#### Shared feed and process supervisor — `framework/orchestration/`

`shared_feed.py` (458), `process_supervisor.py` (244), `strategy_manager.py` (455),
plus `framework/market_data/shared_feed.py` (34).

- **Preserve:** one WebSocket in the supervisor fanned out to per-child
  `multiprocessing.Queue`s; `IpcFeed` implementing the same `MarketDataFeed`
  interface so the engine is unchanged; the bounded queue (`_QUEUE_MAXSIZE =
  20_000`, chosen to stay under macOS's `SEM_VALUE_MAX`); `spawn` context so each
  child installs its own signal handlers in its own main thread; `RestartPolicy`
  as a *pure* decision object separate from process handling; clean exit (code 0)
  never restarted, non-zero restarted with bounded back-off.
- **Adapt:** the current hub **broadcasts every tick to every child** and relies on
  each engine to ignore what it does not hold. The spec requires a
  reference-counted subscription registry, a per-worker subscription union, and
  per-worker queue depth/age/drop metrics. Also: the current live feed has **no
  dynamic subscription** (it pre-subscribes an ATM band); the spec requires
  dynamic subscribe/unsubscribe for option rolls.
- **Tests to port:** `tests/test_orchestration.py` (162),
  `tests/test_startup_manager.py` (261), `tests/test_startup_resilience.py` (134),
  `tests/test_shutdown_finalization.py` (59).

#### Migration runner — `weekly_strategies/.../persistence/migrations.py` (57 lines)

Pattern reused in `common/persistence/migrations.py`: sequential `.sql` files
applied once each, recorded in `schema_migrations`. Extended with a name column,
a `filelock` cross-process lock, post-batch integrity checks and a replay-safety
guard. See deviation D6.

### 2.3 Intentional deviations

| # | Deviation | Reason |
|---|---|---|
| **D1** | Config loader **written fresh**, not ported | `framework/config/loader.py` walks parent directories and `sys.path`-injects the monorepo's `common/` package to import `common.credentials`. That is exactly the cross-repo runtime dependency the spec forbids. |
| **D2** | Exit registry has **10 policies, not the 9** the spec lists | The extra is `trailing` (`framework/exit/trailing_exit.py`). It is a real, registered, config-selectable policy and will be preserved along with the other nine. **Confirmed in Part 2a**: all ten are registered in `common/exit/` and asserted by name in `test_all_ten_policies_are_registered_not_the_nine_the_spec_lists`, which also asserts `len(...) == 10` so an eleventh cannot be added silently. |
| **D3** | `momentum_low_or_highest_close` is **not** in `CompositeExit._KEY_TO_ENGINE` | Deliberate in the reference repo: it evaluates on the traded option's *own premium candle stream*, not the underlying's, so strategies instantiate it directly via `get_exit_engine()`. Porting the composite map verbatim would silently drop it. Both wiring paths must be preserved. **Confirmed in Part 2a**: `_KEY_TO_ENGINE` was ported with its nine keys and the combined exit still absent, and three tests now pin it — the map excludes it, `get_exit_engine()` still reaches it, and no config block can select it even when the mode is named after it. |
| **D4** | `PaperBroker` **rewritten**, not ported | The existing one fills at `ref_price ± fixed slippage` with none of the spec's required realism. Only `ChargesCalculator` carries over. |
| **D5** | Broker factory **gains a safety gate** | The existing factory builds a live broker from `mode: live` alone. The new one must consult `effective_live_gate()` and refuse to start when blocked. This is a behavioural change, made deliberately, for safety. |
| **D6** | Migrations are **replay-safe rather than transactional** | `sqlite3.executescript()` issues an implicit COMMIT before running, so a migration cannot be applied and recorded in one transaction. Safety comes from enforced idempotency (`CREATE ... IF NOT EXISTS` only, destructive statements rejected) plus recording last: a crash between the two leaves the next startup replaying a no-op. |
| **D7** | Env overrides can **only disable** live trading | `ALGO_LIVE_TRADING_ENABLED` is honoured when it parses false and ignored when true. An operator needs a fast kill switch; nobody needs to enable real money from an environment variable, where a stale export is indistinguishable from a decision. |
| **D8** | `effective_live_gate()` exists in Phase 0 with no consumer | It can only return *blocked* in this phase (`preflight_passed` defaults False and no preflight exists). Included now because a config model that accepts `mode: live` without a fail-closed evaluator beside it is a footgun. |
| **D9** | Feed hub fans out **completed candles, not ticks** | Spec section 6 (and core principle 6) describe distributing *normalised ticks* to workers. Aggregating once, centrally, guarantees every worker sees byte-identical bars and makes "prevent duplicate candle publication" structural rather than conventional. Cost: a worker cannot pick its own timeframe off the raw stream; it aggregates further from completed bars. A tick channel can be added in Phase 2 without reshaping the queues. |
| **D10** | **No engine port in Phase 1** | The slice runs on a minimal `Strategy` protocol with a deterministic fixture implementation, shaped like `TradingEngine`'s signal interface but not derived from it. Porting the real engines is Phase 3, and doing it early would have meant porting them against a skeleton with no exit policies to receive them. |
| **D11** | ~~`PaperBroker` is **deliberately minimal here**~~ **CLOSED (Phase 4 Part 5)** | Phase 1 implemented a fill at the submission-time quote plus adverse slippage, a *recorded* latency value, and exactly one rejection rule (duplicate correlation ID) — that one because it is a correctness property of idempotent submission, not a realism feature. Part 5 delivered the rest: bid/ask depth through the pipe, latency-selected quotes, resting limit orders, partial fills and all nine rejection rules. Read **D48**, **D51** and **D53** for what is *not* claimed — this deviation is closed, not perfected. |
| **D12** | **`dhanhq` pin moved to `2.2.0`**, superseding `CLAUDE.md`'s "default to the stable 2.1.0" | Superseded by the compatibility spike that same rule mandated, which is the documented mechanism for changing it. `2.1.0` is **yanked** on PyPI (publisher reason: "Breaking changes"), its `subscribe_symbols`/`unsubscribe_symbols` read `ws.closed` which no longer exists on `websockets>=14` (so resubscription — a Phase 2 deliverable — raises `AttributeError`), and its `disconnect()` never closes the socket. The tick/quote payload builders are byte-identical across the two versions, so normalisation is unaffected. Full evidence in section 4. |
| **D13** | **`LTT` is reconstructed, not parsed** | Dhan's SDK renders the exchange timestamp as `strftime('%H:%M:%S')` against UTC, discarding the date, so no timestamp parser can consume it directly. The date is recovered by choosing whichever of yesterday/today/tomorrow places the wall clock closest to receipt — correct for any true latency under twelve hours. Recorded as a deviation because it is inference rather than transmitted data. Mitigated by counting every fallback, so a format change becomes visible instead of silent. |
| **D14** | **Authentication uses `httpx` directly, not the SDK's `DhanLogin`** | `dhanhq` 2.2.0 ships a `DhanLogin` class hitting the same endpoint, but importing it into `common/authentication/` would break the one-file SDK-isolation rule that a test enforces. Its version also has no retry policy, no rate-limit detection, and swallows every failure into a bare `Exception` — so coupling to it would cost exactly the fail-fast behaviour that protects the account from repeated rejected logins. |
| **D15** | **Reconnection is owned by this project, not by the SDK's runner** | 2.2.0 added a `run()`/`start()` threaded loop that retries on a flat one-second sleep, with no jitter, no bounded attempt budget, and no way to distinguish reason code 807 (token expired — needs a new token) from a transient drop (needs patience). The spec requires one owner for reconnect and subscription state (line 1439) and bounded backoff with jitter (line 1555); using both would give it two owners. |
| **D16** | **Graceful group shutdown lives in `supervisor.py`, not a separate `shutdown.py`** | The spec's runtime folder standard (section 4) lists `shutdown.py` beside `supervisor.py`, and "graceful group shutdown" as a supervisor responsibility. (The neighbouring responsibility, "group persistence and health publication", is now partly delivered too — the supervisor writes its own session and heartbeats — but that is spec-aligned rather than a deviation, so it is not recorded as one.) Phase 3 Part 1 implements the responsibility in full but keeps it in `supervisor.py`, because the shutdown is not separable from the thing being shut down: it is signal handlers scoped to the feed thread's lifetime, an ordering constraint between that thread and the aggregator flush, and the same lock the run acquired. Splitting it across a module boundary would mean exporting the feed thread, the stop event and the ownership state purely to satisfy a filename — turning an invariant the type checker can see into one a reviewer has to remember. Revisit when there is a second thing to shut down (Phase 5's mixed-mode supervisor). |
| **D17** | **The feed runs on its own thread, contradicting the Phase 1 note that it is driven inline** | Phase 1's `supervisor.py` docstring recorded that the feed is driven on the supervisor's own thread. That was viable only for the recorded adapter, whose `start()` returns; a live adapter's does not, leaving no thread to notice a signal. Recorded as a deviation because it reverses a documented earlier decision rather than adding to it, and because it is load-bearing for limitation 13: the feed thread is a **daemon**, which is what lets the process exit even when a silent socket cannot be closed. |
| **D18** | **`TradingEngine` installs no signal handler** | The reference installed `signal.signal(SIGINT, ...)` inside `run()`, which nests inside the supervisor's own handler and silently wins delivery — the blocker section 8 recorded. Square-off on interrupt is preserved as *behaviour*; what changed is who triggers it: an externally-set `threading.Event`, acted on at boundaries owned by the thread already running the engine. The `raise KeyboardInterrupt` tail becomes a reported `stopped_by_request` flag, because a worker returns an outcome rather than an exit code derived from an exception, and a requested shutdown that completed is not a failure. The square-off arithmetic is untouched; the only behavioural difference is that the close is timestamped from the tick rather than `now_ist()`, matching every other close path in the engine. Section 8 required this be recorded as its own deviation, since it changes signal behaviour in a "port unchanged" module. |
| **D19** | **Engine models renamed, and `AppConfig` replaced** | `Signal`→`StrategySignal` and `Position`→`OpenPosition`: both names are already taken in this repository by the *persisted* models, and two live types called `Position` is exactly the confusion that produces a wrong exit rather than an error. `Tick` and `Candle` are **reused** rather than re-ported, so the hub's bars and ticks reach the engine with no converter. `AppConfig` (294 lines) becomes `EngineConfig`, holding the six values the engine actually reads — D1 already rejected porting the reference's config layer, and a second config system would give this repository two answers to "is live enabled?". |
| **D20** | **Reporter, report generator and notifier substituted** | The engine's three reporting seams bind to this repository's `HeartbeatWriter`/`ExecutionRepository`/`SafeNotifier` (Part 2b-ii) rather than to a ported `EngineReporter`. The reference's file-writing `ReportGenerator` and the whole `dashboard/publisher.py` stack — including its 459-line SQLite `PortfolioDatabase` — are not ported: this repository already persists every order, fill and position behind migrations and an `execution_mode` column, and a second reporting database beside it is the parallel universe the audit warns against. `summarise`/`DailySummary` (pure) did come across. |
| **D21** | ~~**Regime tagger ported null-classifier only**~~ **CLOSED** (Phase 4 Part 2) | Was ~120 lines of 421, because the one real classifier (`adx_atr`) is built on ADX and ATR and Part 2a deliberately did not port those — nothing consumed them. Part 2 ports them, so the classifier's inputs exist and `AdxAtrClassifier` is now registered. It is also what gives ADX and ATR a consumer and their only 6 reference regression tests. **The prediction that this would be "a new file plus one decorator" was half wrong** — see **D39**: the decorator registers at import time, so a classifier in its own module registers only if something imports it. The regime axis is still purely observational and `regime_enabled` still defaults to false, so this ships available and not switched on — asserted by `test_regime_classifier_wiring.py`, not merely intended. |
| **D22** | **The Part 2b test-port list was wrong, and is corrected** | Section 8 listed four reference suites. Verified against the source: `test_opening_candle_coverage.py` (670) exercises **`FixedStrikeEngine`** via `nifty_fixed_strike_wide_sell.app.build_engine` and never touches `TradingEngine` — excluded. `test_session_candle_gating.py` holds **one** `TradingEngine` test of three; the others need MultiLeg/FixedStrike. `test_mfe_mae.py` tests `PositionManager`/`PaperBroker`, not the engine, but is worth porting for the MFE/MAE contract. `test_premium_candle_exit.py` (9 tests) is the only end-to-end engine suite and builds the **real EMA-cross strategy**, which `CLAUDE.md` defers to Phase 9 — so its nine properties are rebuilt against a test-only double, with the real Part 2a exit policy still making the exit decision. The diff-fidelity loss on those nine is real: they prove the engine's behaviour, not that a ported strategy still matches itself. There is also **no upstream coverage of the signal path at all**, so the ten gate tests are written here. **The rebuilt nine are the one part of this port that is not diff-provable, so the property-by-property mapping is written out below rather than asserted** — including the one property that does not map. |

| **D23** | **The engine aggregates the underlying a second time, beside the hub** | D9 aggregates centrally so every worker provably sees identical bars. A worker driving the ported `TradingEngine` off the new tick channel builds its own bars from the same ticks, so that guarantee no longer holds for it: a dropped tick quietly changes *that worker's* OHLC rather than removing a whole bar visibly. Accepted because the alternative is worse — the hub aggregates at 60 s while the engine wants `cfg.timeframe`, so hub-fed bars would need a new candle→candle aggregator plus an injected entry point bypassing `_on_underlying_tick`, which the ported session-gating test pins attribute by attribute. It also moves *toward* spec section 6, which describes distributing normalised ticks; D9 was always the deviation. Mitigated three ways: the depth is sized from a measured tick rate (`DEFAULT_TICK_MAX_DEPTH`, and a test asserts zero drops at that rate), every drop is counted and surfaced in `queue_stats()`, and — since Part 2b-ii-B-1 — a `TickDropNotice` reaches the child so the engine blocks new entries for the day once a drop occurs (**D28**). Recorded as limitation 14. |
| **D24** | **Runtime subscriptions are applied at a tick boundary, not when requested** | The engine picks an option contract mid-session, but the hub subscribes a fixed union at `start()` and the worker is a different process. `request_subscription()` enqueues and returns; `on_tick` drains it and calls `adapter.subscribe()` on the thread that owns the connection. The same shape as Part 1's `request_stop()` and D18's `request_square_off()`, one layer out. Notably this is *stricter than necessary*: `dhanhq` 2.2.0's `subscribe_symbols` routes through `asyncio.run_coroutine_threadsafe` and would tolerate a cross-thread call, unlike `close_connection`. Relying on that would put this repository's ownership rule inside the SDK, where an upgrade could revoke it silently. Cost: a subscription needs a tick to be applied — limitation 15. |
| **D25** | **Undelivered queue contents are abandoned at shutdown, not flushed** | A `multiprocessing.Queue` joins its feeder thread at interpreter exit, so a producer holding undelivered events behind a full pipe never exits — **measured at ~65 KB**, which the tick channel reaches in a few hundred ticks. This was found as a real hang in Part 2b-ii-A, not reasoned about in advance. `_release_queues()` therefore calls `cancel_join_thread()` on the queues the supervisor owns, after the sentinel and after the workers are joined. Recorded as a deviation because it discards data on a shutdown path: the same judgement the queues already make under load (a lagging consumer gets the freshest data or none, never a stalled producer), and nothing at risk is a trading record — those reach SQLite before the broker is called. The alternative is a shutdown that can itself hang, which is the failure Part 1 exists to prevent. |
| **D26** | **Engine-originated `signals` rows carry a synthesised one-second window, with a microsecond disambiguator** | The dedup key is `(strategy_id, execution_mode, instrument, candle_end_at)`, which assumes a candle-driven producer. `TradingEngine` is tick-driven, so it has no bar to record and no natural uniqueness: an exit and a re-entry on one contract inside one second would collide, and `record_signal` turns a collision into a silent `None`. `LifecycleGateway` therefore records a degenerate bar — all four OHLC values are the reference price, the window is one second ending at the execution decision — and takes `max(ts, last_used_for_this_instrument + 1µs)`. Recorded as a deviation because the `candle_*` columns now mean something different depending on which producer wrote the row: for a candle-driven strategy they are the bar evaluated, for the engine they are the instant decided. Inventing a five-minute bar the engine never evaluated would have preserved the column's apparent meaning while destroying its truth. Per instrument rather than global, because the constraint does not span instruments and pushing unrelated contracts apart would distort their timestamps for nothing. Monotonic rather than random, so the rows stay in execution order |
| **D27** | **`LifecycleGateway` reports a charges total with no component breakdown** | `InMemoryGateway` populates six components (brokerage, exchange, STT, SEBI, GST, stamp duty) because it calls `ChargesCalculator.for_leg` itself. The persisted path cannot: `PaperBroker` calls `total_for_leg` and `Fill` carries only the total, which is the number that reaches SQLite. Recomputing the split from rates inside the gateway would produce a breakdown that could silently disagree with the persisted total — two answers to one question, which is the failure mode D19 and D20 were both written to avoid. `Trade.charges` is unaffected: `PositionManager` sums the totals. Revisit if and when `Fill` carries a breakdown |
| **D28** | **A dropped tick is reported to the worker in band, on a cadence** | The drop is detected and counted in the supervisor's process; the consumer that must act on it runs in the child. There is no other parent→child channel, and the tick queue already carries the `None` shutdown sentinel, so a `TickDropNotice` rides beside it. It is matched by **type**, not identity, because it crosses a pickle boundary — a module-level singleton would not survive. **The cadence was corrected after the part was first committed, on measurement rather than argument.** The notice is pushed into a queue that is full by definition, so it evicts a real tick. One notice per drop — the first implementation — cost **358 extra lost ticks per 6000** at the deployed depth of 2048 against a lagging consumer (6.3% of delivery), to make the entry block engage **6.5% sooner** (after 4393 ticks rather than 4699). A cadence of 8 costs **52** (0.9%) for that same latency: roughly seven times less data for a difference in blocking latency that is not material when both are in the thousands. The original justification recorded here — that a bounded cadence "loses the notice entirely on a short burst in a shallow queue" — was **wrong**, and is corrected rather than quietly dropped: it was true of a cadence of 64, which was never the alternative on the table. The cadence is clamped to `min(8, depth // 2)` (`drop_notice_cadence`), which is the invariant it actually depends on: a notice reaches the consumer only while it is inside the retained window, and roughly one tick is published per drop, so a cadence at or above the depth lets every notice be evicted before the next is sent. Swept across depths 4-2048 and run lengths 12-4000: a fixed 8 reported every overflow at depth 8 and above but **lost the notice entirely at depth 4**; clamped, every overflowing case reported. At the deployed depth the clamp returns 8 unchanged, so it only bites on the shallow queues that appear in tests. |
| **D29** | **A square-off left `IN_PROGRESS` by a dead process is retried, not honoured** | `SquareOffPolicy.trigger_at` returns `NONE` for `IN_PROGRESS`, which is right for the process that wrote it — it is mid-attempt — and wrong for a process that reads it at startup, because whoever owned that attempt is gone. Honouring it literally would leave a position open past square-off with nothing that will ever close it, which is the failure execution §10 exists to prevent; §10's own staged behaviour includes a retry window for exactly this. `PersistedSquareOffAuthority` therefore normalises an inherited `IN_PROGRESS` to `PENDING` at load and logs it at `WARNING`. Recorded as a deviation because it changes what a persisted state means depending on who read it. `trigger_at` is untouched and its unit test still passes: the normalisation is one layer up |
| **D30** | **The engine's strategy travels as a dotted `module:Class` reference plus keyword arguments, not a registry name** | The ported `common.engine.strategy.get_strategy(name, cfg)` takes exactly one positional config argument, which the engine's strategies — whose constructors are keyword-only — do not fit. Two ways out: change the ported registry to suit its first caller, or carry a form that can express what already exists. The second leaves the port untouched, which is the whole point of porting it. The reference is resolved in the child by `engine_worker.load_strategy`, which `isinstance`-checks the result against `BaseStrategy` so a typo naming another class in the same module fails at startup rather than on the first tick. It also keeps `EngineWorkerConfig` free of engine-owned types, which the import boundary requires: the child unpickles that dataclass before any engine module is loaded, so a single engine-typed field would drag the package in through the definition itself |
| **D31** | **The engine runs on the worker's main thread; `Database` keeps its thread affinity** | Planned as a second thread, mirroring the supervisor's feed thread, so a coordinating thread could bound its wait on a silent feed. `Database.connect()` does not pass `check_same_thread=False`, so the connection belongs to the thread that created it and the first fill from `LifecycleGateway` raises `ProgrammingError`. Passing `check_same_thread=False` would have loosened a real safety property for every user of `Database` to serve one caller, so the engine stays on the main thread instead and the case the second thread existed for is fixed at its cause: `HubTickFeed` asks a `should_stop` predicate on every poll wake, so a shutdown requested during a silent stretch is honoured within one poll interval rather than waiting for a tick that may never come. Returning from that loop reaches `TradingEngine.run()`'s `finally`, which **D18** already names as the second square-off boundary, so no new mechanism is introduced. Consequence to remember: **nothing in a worker may touch SQLite off the main thread.** The one remaining helper thread drains the candle queue and calls `request_square_off`, which is documented safe from any thread precisely because it only sets a flag |
| **D32** | **The engine's day summary goes into `strategy_state.payload`, not a report file or a second store** | **D20** deferred this binding to Part 2b-ii and warned against the reference's parallel `PortfolioDatabase`. Every leg is already persisted through `LifecycleGateway` into `signals`/`order_intents`/`orders`/`fills`/`positions`, so re-writing the trades would be exactly that parallel universe. What those tables cannot hold is the engine's own analytics — MFE/MAE, the regime tags, the exit reason it chose, the day's net — so `RepositoryReportWriter` writes only those, into the free-form column that already exists, merged so it coexists with the `open_position` record the gateway keeps there. Recorded as a deviation because the reference wrote CSV/JSON/HTML files to `report.output_dir` and this writes none |
| **D33** | **The real resolver reads Dhan's daily scrip master, not `OptionChainService`** | Supersedes the fix that limitation 17 and section 8 both named. `/v2/optionchain` is keyed by strike and returns prices, OI and greeks — it carries **no per-strike `security_id`**, so it cannot name a tradable contract. The instrument master can, works outside market hours, and turns resolution into a dict lookup with no per-trade API call, so entering a position can never be delayed or refused by a rate limit. The reference reached the same conclusion and left the reasoning in its docstring. `OptionChainService` is untouched and keeps live per-strike quotes and greeks as its job |
| **D34** | **`EquityScripMaster` was not ported** | The reference's NSE cash/derivative universe exists for its equity scanner. Intraday stocks are Phase 5 and nothing in this repository consumes it, so porting ~110 lines of parser now would produce untested code that merely looks finished — the same judgement Part 2a made about the five unported indicators |
| **D35** | **A stale scrip master raises rather than falling back to the last listed expiry** | The reference returned `self._expiries[-1]` when every expiry was in the past. That resolves contracts which can no longer be traded, so it fails *towards* a silent bad entry rather than away from one. This port raises `ScripMasterError` naming the staleness. The only behaviour change from the reference in this part, and it is pinned by a test that fails against the reference's version |
| **D36** | **The option's exchange segment is decided in the worker, not the engine** | `TradingEngine`'s feed contract is `subscribe(security_id)` — one string, no segment — and widening it means touching the engine, whose ported session-gating test pins it attribute by attribute. It does not need widening: everything the engine subscribes at runtime other than the underlying **is** an option contract, so `_subscription_sender` gives the underlying the hub's default segment and everything else `option_segment`. This also keeps instrument knowledge in the wiring, where the rest of the resolution already lives. Cost: a future runtime subscribing something that is neither would need this revisited |
| **D37** | **`nearest_expiry` resolves "today" in IST, not in the process's local naive time** | The reference used `datetime.now().date()`. At 23:30 UTC it is already tomorrow in Mumbai, so for half an hour every night the reference would select the expiring series instead of the next one. Routed through `common.utils.timeutils.now_ist`, which is the clock the engine already uses |
| **D38** | **`pandas-ta-classic` is the cross-check oracle, never the live incremental path** | The architecture document's Phase 4 bullet asks for a "pandas-ta-classic adapter and fixtures", and `common/indicators/vectorised.py` is one — but it computes batch values for the cross-check tests and (from Part 4) warm-up replay only. **No value the engine trades on comes from it.** Routing live values through the library would change numbers the ported regression tests were written against, which the project rules forbid weakening. Recorded as a deviation because a reader could reasonably read the arch-doc bullet as "compute indicators with pandas-ta". Enforced structurally by `tests/unit/test_indicator_oracle_boundary.py` — an AST walk over every shipped package plus clean-interpreter import proofs — rather than by this note. |
| **D39** | **`AdxAtrClassifier` lives in `common/engine/regime.py`, not its own module** | **D21** predicted "a new file plus one decorator, with no change here", and the *file* half was wrong. `@register_regime_classifier` runs at **import time**, so a classifier in its own module is registered only if something imports that module — leaving a classifier that silently does not exist, which is a worse failure than a longer file. This repository had already collapsed the reference's five regime modules into one, so the class joins them and registration is unconditional. `test_registration_needs_no_second_import` proves it in a clean interpreter rather than by inspection. |
| **D40** | **Session and square-off predicates refuse a timezone-naive datetime** | They used to accept one and let Python read it as system-local, which is the bug class Part 3 exists to close; reading it as IST instead would hide that a caller lost its timezone upstream. Neither guess is safe, so `common.utils.timeutils.local_time_in` raises `NaiveDatetimeError` naming the argument. This is a behaviour change for any caller that was passing naive datetimes — none in this repository was, and a test now asserts the refusal on every predicate. |
| **D41** | **The hub discards a gap-spanning bar; the engine's own builder emits and marks it** | Two builders, deliberately two behaviours. `CandleAggregator` fans out to *every* worker, so a stitched bar would corrupt all of them at once, and another bar is always coming — discard is affordable. `CandleBuilder` (**D23**) is one chart in one engine and has no discard path; dropping a bar there would starve an indicator with no signal that it happened, which limitation 14 already calls "worse in kind" than a visible loss. So it emits and sets `Candle.spans_gap`, and the consumer decides. Recorded so the asymmetry reads as a decision rather than an oversight. |
| **D42** | **A `spans_gap` bar is skipped entirely, so a strategy does not count it** | `BaseStrategy.on_candle` both updates indicators and produces signals, so "do not trade on stitched data" means the bar does not reach the strategy at all — it reaches `on_candle_gap` instead. Consequence worth stating: a strategy counting bars sees one fewer, which is correct (as far as it is concerned the bar did not happen) but is a real behavioural change. Surfaced by `test_premium_candle_state_does_not_leak_across_a_re_entry`, whose tape carries an incidental 20-minute underlying hole; its `enter_on_candle` moved from 3 to 2 and the reason is recorded in the test rather than in a commit message. |
| **D43** | **`WarmupSource.from_option` is ported but has no caller** | Every continuity-required indicator in this repository runs on the underlying's candles, and no option contract exists yet at the point `build_warmup_manager()` runs (the strategy picks its strike on its first signal). Kept — six lines, reference-faithful — for whichever of `MultiLegEngine`/`FixedStrikeEngine` arrives first, rather than dropped and re-derived later. |
| **D44** | **The historical-candle fetch speaks Dhan's REST endpoint directly via `httpx`, never the SDK** | The reference calls `dhanhq(ctx).intraday_minute_data(...)` directly, which this repository's test-enforced SDK-isolation rule forbids outside `common/market_data/dhan.py`. `common/market_data/dhan_historical.py` follows `dhan_login.py`'s established precedent instead — the same reasoning applies doubly here, since the installed 2.2.0 SDK's historical-data call has no retry policy or rate-limit handling at all. |
| **D45** | **`fromDate`/`toDate` are full `"YYYY-MM-DD HH:MM:SS"` datetimes, not bare dates** | The reference passes `current.strftime("%Y-%m-%d")`. DhanHQ's own documentation for `POST /v2/charts/intraday`, fetched and read directly rather than inferred, specifies a full datetime string. A straight port would have sent an undocumented request shape on every fetch. Found and corrected during the port, not carried over; pinned by a fail-first test. |
| **D46** | **`build_warmup_manager` refuses to warm when the resolved underlying's security id disagrees with the worker's own** | `WorkerConfig.security_id` (what the live feed subscribes) and `resolve_index_meta(...).security_id` (what a warm-up fetch would use) are set independently, and the latter accepts an `index_security_id` override that could go stale. A REST fetch for the wrong id still succeeds, so an unchecked divergence would silently seed indicators from a different instrument's history. Not a port of anything in the reference — this repository's own wiring introduced the risk, so this repository's own wiring closes it. |
| **D47** | **`validate_warmup_config` now also refuses construction when no `warmup_manager` will be supplied, not only when `warmup_from_history` is explicitly false** | A same-part amendment, distinct from D43-D46 (which are about the port itself) — this closes a **pre-existing Phase 3 Part 2b-ii-B-2 gap**, discovered while building Part 4's own end-to-end test. The function's docstring claimed "the engine's runtime gate fails it closed anyway" for a continuity-required strategy with no manager; that was false — `entry_blocked_by()` is reached only on the `warmup_manager` path, so every existing config (default `warmup_from_history: true`, no manager unless `warmup_source: dhan` is set) sailed through construction and cold-started with only a `WARNING`. `validate_warmup_config(strategy, cfg, warmup_manager=None)` now raises `InvalidWarmupConfig` when a continuity-required strategy has `warmup_from_history` false **or** no manager supplied — the two were independently-reasoned mechanisms with a gap between them, now one check. `warmup_manager` is optional so no pre-Part-4 call site (there was exactly one, and it now passes it) needed to change shape. No test in the tree relied on the old fallback for a legitimate reason — the one that did (`test_no_manager_or_source_reaches_the_pre_existing_fallback_unchanged`) was written in this same part specifically to pin the gap, and is flipped to assert the refusal instead. |

| **D48** | **Simulated latency *selects* a quote; it does not wait for one** | Spec 5.2 asks the simulator to price against "the quote available after the simulated latency". At the moment `PaperBroker.submit()` is called on a live feed, that quote **does not exist yet** — the deadline is 250 ms in the future. The alternatives were to block the feed callback thread, or to make everything from `PositionManager` down asynchronous, which is a redesign of the Phase 3 execution port rather than a widening of the simulator. So a market order asks `QuoteBook.after()` for the first quote at or past its deadline, uses it when one exists, and otherwise fills at the submission quote. **Every fill records which happened** in `Fill.latency_applied`, and the broker counts the fallbacks in `latency_not_applied`, so this is a per-fill observation rather than a claim the configuration makes. On a live feed a market order is nearly always the fallback case; on a replayed tape, and for every resting limit order, it is not. |
| **D49** | **`modification_latency_ms` / `cancellation_latency_ms` are not implemented** | Spec 5.2 lists all three latencies. There are no modify or cancel verbs on `Broker` — they arrive with their first consumer, as `broker/base.py` has said since Phase 1 — so accepting configuration for them would be a setting that describes behaviour the system does not have. `submission_latency_ms` is implemented; the other two arrive with the verbs. |
| **D50** | **The enforced tick size comes from configuration, not from `SEM_TICK_SIZE`** | The column is parsed, converted paise→rupees and carried on `OptionRow.tick_size`, but the fill model enforces `paper_execution.tick_size` and treats the exchange's value as advisory. Verified against the live master: NIFTY and SENSEX `OPTIDX` rows carry `5.0000` for a real ₹0.05 tick and `FUTCUR` USDINR carries `0.2500` for ₹0.0025 — paise — but in the same file `FUTIDX` NIFTY and NSE `EQUITY` RELIANCE both carry `10.0000`, neither of which divides to the tick those instruments are commonly quoted in. The unit is therefore not uniformly trustworthy outside index options, and reading it at face value as rupees would put NIFTY options on a ₹5 grid and refuse every order. With no tick known from either source the rule is **skipped**, not guessed at. |
| **D51** | **A partial fill is refused at the gateway rather than propagated into the engine's book** | `FillOutcome` carries a price and charges and no quantity, so there is no way to tell `PositionManager` "you got 25 of the 75 you asked for" without widening the Phase 3 port and its ported regression tests. `LifecycleGateway._require_a_fill` therefore raises on `PARTIALLY_FILLED`. **The branch's previous absence was silent**: a partial has fills and an average price, so it passed every check and the engine recorded a full-size `OpenPosition` for exposure the broker never gave it. The partial is still persisted in full — `orders.filled_quantity` is now a running total — it is simply not reported as a fill. |
| **D52** | **`MARKET_CLOSED` and `RISK_BLOCKED` have no producer yet** | Both rules are implemented and tested. `RISK_BLOCKED` fires on `OrderIntent.risk_decision`, which `OrderLifecycle` currently hardcodes to `ALLOWED` — a real risk gate is Phase 5/6. `MARKET_CLOSED` needs an injected session predicate and is **off by default**, which is a hazard-driven decision rather than an omission: the engine already gates *entries* on the session, while an exit or a square-off legitimately fires at or after the square-off time, so a broker enforcing this by default would refuse exactly the orders that must never be refused. |
| **D53** | **`max_quote_age_ms` defaults to off** | The staleness rule compares the quote's exchange timestamp against the wall clock, which is meaningful live and meaningless on a replayed tape — where every timestamp is historical and *every* order would be refused. Tape replay is this repository's default execution path, and a setting that breaks every replay is one that gets switched off everywhere and then protects nothing. **A live-feed configuration must set it** (e.g. `2000`). |
| **D54** | **A blocked live strategy's admission is recorded to `errors` and delivered through the notifier, never written to the `notifications` table** | `repository.record_notification` is defined (`common/execution/repository.py`) but has **no caller anywhere in production code** — every existing alarm in `supervisor.py` (the limitation-15 stuck-subscription alarm, the limitation-13 unclosable-feed alarm) already follows this same two-step pattern: `record_error` for the persisted record, `notifier.send(NotificationEvent(...))` for delivery, and nothing calls `record_notification`. Phase 5's mixed-mode admission refusal follows the established pattern rather than becoming the first caller of an unused method; `tests/end_to_end/test_mode_separation.py` asserts delivery against `RecordingNotifier.events`, not a `notifications` table row. Revisit if/when something actually wires `record_notification` up — at that point this deviation and the unused-method fact both need re-examining together, not separately. |
| **D55** | **Worker-lock acquisition stays in the child process, not the supervisor** | The spec's PID/lock startup flow (section 8, step 4) reads "For each enabled strategy, acquire its worker lock and reject duplicate execution" from inside what the surrounding steps describe as the supervisor's own sequence. The implementation acquires in `run_worker` (`runtimes/intraday_options/worker.py`), inside the spawned child, and Phase 5 keeps it there rather than moving it to match the literal wording. A lock held by the parent across `multiprocessing.spawn` does not transfer to the child that actually trades — the parent and child are different OS processes with independent `flock` state — so parent-side acquisition would protect the wrong process: two children could still race for the same strategy's state while the parent's lock sat acquired and irrelevant. `test_duplicate_worker_startup_is_refused` and Phase 5's own `test_a_live_mode_contender_is_still_refused_as_a_duplicate` both depend on the lock being held by the process whose state it protects. |
| **D56** | **Positional-options rollout is split: inert scaffolding shipped now, the working supervisor/worker/persistence layer genuinely deferred to Phase 6/9 — corrected from an earlier draft that deferred all of it** | Spec bullet 6 reads "add positional options and intraday stocks one at a time; keep positional stocks a placeholder" — naming three things and treating them differently. An earlier version of this deviation read "no strategy exists yet" as sufficient reason to defer everything, which was too broad: the bullet singling out "positional stocks" as *the* placeholder only makes sense if "no consumer yet" wasn't meant to excuse the other two equally, and **D34** — written back in the reference audit — already said "Intraday stocks are Phase 5", i.e. this project's own earlier documentation expected incremental movement here. Corrected split: (1) `config/runtimes/positional_options.yaml` (`enabled: false`) and `runtimes/positional_options/__init__.py` are real, loadable, and shipped — matching the exact precedent `intraday_options.yaml` set in Phase 1, and completely inert since nothing scans `config/runtimes/*.yaml` on its own. (2) The supervisor/worker/config-adapter layer stays deferred, and this part **is** a genuine structural blocker, not merely "no consumer": `positions`/`strategy_state`/`order_intents` all key their UNIQUE identity on `trading_date` (migration `0001`), which fragments a position held across sessions into unrelated rows, and `SquareOffPolicy` is same-day wall-clock exit with no multi-day concept — both are Phase 6's explicit job ("restore open paper positions... by strategy and mode", not by date; `force_square_off_before_expiry`; settlement simulation). Building it now would mean either reusing same-day-shaped persistence incorrectly or inventing Phase 6's answer out of order. `intraday_stocks` gets no placeholder at all — D34's "needs a real consumer" reasoning for its scanner-driven universe is unchanged and still applies there. |
| **D57** | **`discover_enabled_strategies` has no way to scope strategy files to one runtime** | Recorded as its own deviation, separate from D56, because it is a gap in the *mechanism* rather than a deferred *feature*: `StrategyConfig` carries no `runtime_id`, and neither does the spec's own "required resolved strategy fields" list (section 9), so a strategy's runtime membership is established only by filename convention (spec's own example: `io_supertrend_fast_v1`). `discover_enabled_strategies(config_root, runtime_id)` therefore resolves *every* enabled file under `config/strategies/` against whichever `runtime_id` it is called with — correct today because `intraday_options` is the only caller, unsafe the day a second runtime's supervisor calls it against the same directory. See limitation 21. |
| **D58** | **`_touch_strategy_state` accumulated the wrong thing — fixed while building its first reader** | Phase 6 Part 1 needed `strategy_state.daily_realised_pnl` to mean "today's total realised P&L" in order to restore `DailyRiskGuard` across a restart, and found that it did not: the UPSERT wrote `daily_realised_pnl = excluded.daily_realised_pnl` (overwrite, not accumulate) while its caller passed *the position's own* cumulative `realised_pnl` under the parameter name `realised_delta`. A strategy that closes one contract and opens a **different** one the same day (a different `security_id`, hence a different `positions` row) silently lost the first contract's booked P&L from this column the moment the second contract's first fill landed. Exactly the shape of the `orders.filled_quantity`/`average_fill_price` bug Phase 4 Part 5 already found and fixed on the sibling column (`test_two_fills_on_one_order_accumulate_rather_than_overwrite`) — missed here because nothing had read `daily_realised_pnl` back until now. `ExecutionRepository._upsert_position` now returns the true per-call delta (this fill's own contribution, computed from the position's realised P&L *before* the update, not its new cumulative total), and `_touch_strategy_state`'s SQL adds it to the stored value rather than replacing it. `test_daily_realised_pnl_accumulates_across_contracts_not_just_the_last_one` (`tests/integration/test_execution_persistence.py`) pins it — demonstrated failing against the pre-fix code first, since the single-contract-per-day shape every other test in the file uses cannot distinguish "accumulate" from "overwrite" (there is only ever one value to keep). See limitation 22. |
| **D59** | **`ExecutionGateway`'s "deliberately narrow, two verbs" Protocol was widened — the first change to it since Phase 3 Part 2b-i** | Phase 6 Part 2 needed a risk manager's reported `stop_price`/`target_price` to reach `positions.stop_price`/`.target_price`, and there was no possible producer for either at any depth: `RiskManager.new_position(self, lots: int = 1) -> None` received no entry price, so no manager — not even a hypothetical Phase 9 one built against the pre-Part-2 interface — could ever compute an absolute price level. Put to the user directly as the one genuine either-way fork in the part (widen now vs. add only inert no-op properties and defer the plumbing to Phase 9); decided: widen now. `RiskManager.new_position` gained an optional `entry_price` kwarg; `RiskManager` gained no-op `stop_price`/`target_price` properties; `ExecutionGateway.buy`/`.sell`, `InMemoryGateway.buy`/`.sell`, `LifecycleGateway.buy`/`.sell`/`._execute` and `PositionManager.open()` each gained optional `stop_price`/`target_price` kwargs (default `None`), threaded to `OrderLifecycle.handle_signal`, which already accepted them (the Phase 1 fixture worker's own existing precedent). Every addition is optional with a `None` default and no existing call site needed to change to keep behaving exactly as before — `tests/integration/test_engine_lifecycle_gateway.py` and the `PositionManager`/`InMemoryGateway` regression suites pass unmodified. `TradingEngine._open()` was reordered to arm the risk manager *before* `positions.open()` (previously the other way around) so a manager's computed levels are known before the entry fill — safe because neither call consulted the other under the old order either. |
| **D60** | **Exit-state restart recovery is fail-*open*, the only recovery mechanism in Phase 6 that is** | Position recovery (`recover_position`) and daily-risk recovery (`recover_daily_risk`, Part 1) both fail closed — block entries, write a `CRITICAL` error — because both guard against *double exposure* or *trading past a limit*. A missing, foreign, or unusable exit-state snapshot (a trailing peak, a reversal streak) carries no such risk: it only degrades **exit timing quality** on an *already-adopted* position (a trailing stop re-arms from the current price instead of its true peak), never safety. `TradingEngine._restore_exit_state` therefore logs and continues with the strategy's already-`reset()` (empty) state on any failure — no entry block, no `errors` row — exactly the philosophy `PositionManager.adopt` already documents for MFE/MAE restarting at zero ("zero is visibly the floor rather than plausibly the truth"). `test_a_stale_exit_state_snapshot_for_another_contract_is_ignored` proves both halves: the position still adopts cleanly, and no `component='engine.recovery'` error row is written for this case — asserting its *absence* is what would catch a future change that quietly promoted this to fail-closed without updating this record. |
| **D61** | **A new per-candle database write exists on the engine path — there was none before, and its multi-worker contention cost is unmeasured** | `strategy_state.payload` was previously touched only on a *fill* (`LifecycleGateway._record_contract`, via `apply_fill`), never once per candle. Exit-policy state changes on every candle a position is open for (a trailing peak can advance, or a reversal streak extend, without producing an exit signal that candle), so persisting only at entry/exit fills would leave the *mid-position* value — exactly what a crash-and-restart needs — permanently stale; proving the part's own test scenario is structurally impossible without a new per-candle write. `TradingEngine._persist_exit_state`, called from `_on_candle_close` and `_check_premium_candle_exit`, gated on a *completed* candle (`CandleBuilder.add()` returning non-`None`), so this fires once per `cfg.timeframe` per open position — not once per tick — and each write is one `merge_payload` call (a SELECT + UPSERT on `strategy_state`), the same shape every existing write to that row already uses. Both call sites are guarded by "only when a position is open," and skipped on a candle that itself triggers a close, since `_close()` persists (clears) the key once the position it belonged to is actually gone. **What is not verified**: candle boundaries are wall-clock aligned, so multiple strategies in one runtime group sharing a timeframe tend to complete candles in the same second — a synchronised write burst against the shared group database, not uniformly-distributed load, and structurally different from most existing `strategy_state` writes because this one fires unconditionally on *every* candle for *every* open position rather than only on the writes those already produce. `journal_mode=WAL` / `busy_timeout=5000ms` (`common/persistence/database.py`) already governs every write across every worker in a group and is untouched by this change, but no test or benchmark exercises this specific burst pattern against it — nothing in the suite measures write latency or lock-wait time under concurrent multi-worker load at all, for this write or any other. Bounded by the same conservative default (5 s busy-timeout) as every write already ships with; not yet shown to be *comfortable* at a realistic group size. Revisit before Phase 7 operations work or before a group grows past a handful of concurrently-timeframed strategies, whichever comes first. |
| **D62** | **`CURRENT_STATE_VERSION` lives in `common/models/trading.py`, not next to the key constants it governs** | Phase 6 Part 3 needed one literal both the write side (`common.execution.repository.save_strategy_state`) and the read side (`common.engine.state_payload.read_payload`) agree on. The natural-looking home — `common/engine/state_payload.py`, beside `OPEN_POSITION_KEY`/`EXIT_STATE_KEY` — would have made `common.execution` import from `common.engine` at runtime, the exact direction Parts 1-2 kept `TYPE_CHECKING`-only on purpose (`gateway.py`, `square_off.py`). `common/models/trading.py` is a genuine leaf both packages already depend on, so it adds no new coupling in either direction. `save_strategy_state` now stamps it explicitly on every write rather than relying on the schema's `DEFAULT 1` — the two happened to agree because nothing had ever bumped it, and relying on that agreement implicitly would let a future migration that changes the default silently disagree with what this code believes it wrote. |
| **D63** | **`square_off_attempts` accumulates via the same "add a delta in SQL" pattern D58 fixed `daily_realised_pnl` onto, and every persisted write counts, not only retries** | `save_strategy_state` had no `square_off_attempts` parameter at all before Phase 6 Part 3, and the SQL never referenced the column. Two semantic readings were possible: increment only on a *fresh* attempt (the `due()` → `IN_PROGRESS` transition specifically, which `PersistedSquareOffAuthority._load_state` already treats as "a new attempt started" when it inherits a stalled `IN_PROGRESS`), or increment on every `_save()` call (both the `IN_PROGRESS` and `COMPLETED` writes). Decided: every call, matching the plan's literal wording ("increment... in `_save`") and staying simplest — a monotonic count of persisted state transitions for the day, always >= 2 on a day that reaches `COMPLETED`, higher only when a crash forced a retry. `increment_square_off_attempts: bool = False`, written as `square_off_attempts = square_off_attempts + ?` — race-free without a read first, the same reasoning D58 already established for the sibling column. |
| **D64** | **`recover_position` gained its own try/except around `read_payload`, to preserve the `CRITICAL`-row precedent a bare propagation would have broken** | `read_payload`'s "never raises on bad data" rule is narrowed for exactly one case (Phase 6 Part 3): a `state_version` this build does not recognise raises `UnsupportedStateVersion` rather than being silently misread, because unlike a payload that merely fails to decode, a version mismatch means the payload might be a shape this build cannot safely interpret. Checked directly rather than assumed: `recover_position` called `read_payload` unwrapped, so a raise would have propagated past `_record_recovery_failure`'s `CRITICAL`-row write, reaching only `TradingEngine`'s generic except-block — which blocks entries but writes no error row, breaking the same precedent every other position-recovery failure (`OPEN_POSITION_KEY` missing, an unusable contract record, a stale `security_id`) already gets. `recover_position` now wraps the call, mirroring the try/except already around the `OptionContract(...)` construction two lines below it. `recover_exit_state` needed no equivalent change — its call site is already inside `TradingEngine._restore_exit_state`'s existing fail-open wrapper (D60), so it inherits the right severity for any exception type without a special case — see **D65** for why that path is, in practice, never actually reached by a `state_version` failure specifically. |
| **D65** | **Position-gated `last_candle_end_at`, decided directly with the user — the one genuine either-way fork in Part 3 — and a planned test corrected mid-build once its premise proved architecturally unreachable** | Nothing wrote `last_candle_end_at` on the engine path before this part. Two options: write it unconditionally on every candle (matching §7's literal day-level framing, independent of position state) or gate it on "position open" like Part 2's `_persist_exit_state`, reusing that checkpoint rather than adding a new always-on one. The first adds new write frequency on top of limitation 24's already-unmeasured contention question; the second does not. Decided with the user: gate it. Idempotent replay is guaranteed exactly when it matters most (indicator and MFE/MAE double-counting risk during active management); a flat day's candles are not idempotently resumable — recorded as limitation 25, not left implicit. Separately, the Part 3 plan draft claimed exit-state recovery's fail-open path (D60) would be independently provable through the same `state_version` corruption that fails position recovery closed. Building the test found this is architecturally impossible: `state_version` gates the whole `strategy_state` row, not a key within it, and `recover_exit_state` is only ever called from inside `_adopt_recovered_position` — *after* `recover_position` has already succeeded. A version bad enough to block one blocks both, and position recovery, which runs first, always intercepts it first. The test was rewritten to assert what is actually true (both fail together, position recovery's fail-closed path wins) rather than left asserting the original, unreachable claim. D60's fail-open mechanism remains real and independently provable for the failures Part 2's own test already covers (a foreign `security_id`, or no snapshot at all) — those do not depend on `state_version` and are unaffected by this finding. |
| **D66** | **The expiry-lead rule composes into `SquareOffPolicy.trigger_at` as an optional keyword, checked after persisted state and ahead of the time-of-day ladder — not a second decider, and not a change to `SessionSquareOffAuthority`** | Phase 6 Part 4 needed a contract held past its own expiry to force-close regardless of time of day. The runbook's own composition rule for this seam — stated four times already for the wall-clock net (§8, limitation 7 write-up): "the authority still decides… so there is no second square-off code path" — applies unchanged here. `trigger_at(moment, *, state, expiry=None)` checks `state in {COMPLETED, IN_PROGRESS}` first (so persisted completion still suppresses an overdue day exactly as it suppresses a post-`square_off_at` restart), then `holding_overdue(moment, expiry)`, then the existing ladder — every existing call site that omits `expiry` is byte-identical to before. `PersistedSquareOffAuthority` gained a keyword-only `expiry` constructor argument, resolved once by its caller (the worker knows which contract it holds; the authority does not) and passed straight through on every `due()` call — no new persisted state, no new write path; an expiry-driven close still latches `IN_PROGRESS` once and writes `COMPLETED` from the same `completed()`. `SessionSquareOffAuthority` (the offline, clock-only default) is deliberately untouched: it has no contract to ask about expiry and no persistence to make an expiry rule meaningful anyway. |
| **D67** | **An unparseable or missing expiry makes the rule inert, not overdue — the opposite failure direction from an unreadable persisted `square_off_state`** | `PersistedSquareOffAuthority._load_state`'s existing rule fails an unreadable value *towards* squaring off (limitation-worthy corruption degrades to `PENDING`, which still lets the clock decide) because nothing else will ever close the position if it does not. The expiry rule cannot borrow that direction: `SimulatedOptionChainResolver`'s placeholder expiry (`"WEEKLY"`, the default every existing simulated/fixture config carries) is the *common* case, not the exceptional one, and failing it towards `SQUARE_OFF` would force-close a fixture run on its very first tick — the one behaviour Part 4 was required not to introduce (no test in the existing suite configures a real expiry, so every one of them would have broken). Since the ordinary time-of-day ladder still runs when the expiry rule is inert, there is no unsafe direction to fail towards, unlike the persisted-state case. `holding_overdue` therefore returns `False` for `None`, an empty string, or anything `date.fromisoformat` on the leading 10 characters rejects — the same 10-character slicing `common.engine.regime._is_expiry_day` already uses on `OptionContract.expiry`. `PersistedSquareOffAuthority.__init__` logs the unparseable case once, at construction, rather than per `due()` call (see runbook limitation 28). |
| **D68** | **`expiry_policy`/`square_off_before_expiry_days` are typed top-level `StrategyConfig` fields, not `risk:` dict keys** | Spec section 11 shows `expiry_policy: force_square_off_before_expiry` as a bare YAML key with no parent block, and section 9's "required resolved strategy fields" list does not name it at all — the config-hierarchy home was genuinely unspecified, and put to the user directly during planning. `StrategyConfig.risk` is `dict[str, Any]`; `_StrictModel`'s `extra="forbid"` reaches every top-level field but not inside an untyped dict, so a key placed in `risk` could carry a silent typo (`square_off_before_expiry_day`, singular) with no refusal at all — exactly the failure `_StrictModel`'s own docstring exists to prevent for every other safety-relevant field. Decided: both fields are typed, top-level, `_StrictModel`-covered fields on `StrategyConfig`, alongside `mode`/`live_approved`/`engine`, not inside `risk`. `config_adapter._square_off_policy` now takes the whole `StrategyConfig`, not just its `risk` dict, to read them. |
| **D69** | **D56's persistence-identity question gets a written candidate direction, not an implementation — checked and found too large and too speculative to build now, put to the user directly** | Phase 6 Part 5 re-examined D56's claim that "restore open paper positions and strategy/risk state by strategy and mode" (spec bullet 1) is not fully closed while `positions`/`strategy_state`/`order_intents` key their UNIQUE identity on `trading_date`, fragmenting any position held across sessions. Before deciding what to do about it, the actual blast radius was checked rather than assumed: `trading_date` is a mandatory, exact-match parameter on 6+ `ExecutionRepository` methods (`open_positions`, `load_strategy_state`, `save_strategy_state`, `previous_incomplete_session`, and others), every recovery function in `engine_worker.py` (`recover_position`, `recover_daily_risk`, `recover_exit_state`), `PersistedSquareOffAuthority`'s own key, the fixture path in `worker.py`, and `WorkerConfig.trading_date` itself — plus dozens of existing tests asserting on that exact shape. Building a cross-session identity now would mean guessing what "a cycle" operationally means (calendar days? an explicit roll event? adjustment legs?) with no real positional strategy to answer that question — precisely the "inventing Phase 6's answer out of order" trap D56 itself named, just relocated from Phase 5 to now instead of avoided. Put to the user directly (implement now vs. document only); decided: document only. The candidate direction, recorded so a future implementer does not start from zero: introduce `cycle_id` as an **additional** column on the three tables, assigned when a position/cycle opens and held stable until it fully closes, however many `trading_date`s that spans — `trading_date` itself is untouched and keeps its own, independently correct, "state must never leak between days" guarantee for the intraday positions that exist today (migration `0001`'s own stated reason for including it). A positional worker would eventually query "the open cycle for this strategy/mode" via `cycle_id` rather than `trading_date`. Explicitly not built: no migration, no repository method, no test. Revisit only once Phase 9 supplies a real positional strategy whose actual session/rollover/adjustment shape can validate — or invalidate — this direction. See limitation 30 and `runtimes/positional_options/__init__.py`. |
| **D70** | **A `feed_events` sink may only enqueue, never write — the feed runs on its own thread, and the repository's `sqlite3` connection belongs to the thread that opened it** | Phase 7 Part 1's first version of the health-event wiring had `ReconnectingFeed`'s new `on_health_event` callback call `repository.record_feed_event(...)` directly. `ReconnectingFeed.start()` is driven from a dedicated feed thread (`supervisor.py`'s own module docstring: "a live deployment... enforces the ownership rule... `start()` blocks on a worker thread"), while `ExecutionRepository`'s `Database` connection is opened once, in `run()`, on the supervisor's own thread. `sqlite3.Connection` objects refuse cross-thread use by default (`sqlite3.ProgrammingError: SQLite objects created in a thread can only be used in that same thread`) — caught immediately by `test_the_feeds_health_events_reach_the_repository_once_run_opens_it`, not discovered later or in production. Fixed with the same pattern the supervisor already uses for the opposite direction (a worker's runtime-subscription request travelling *to* the feed thread via `_control_queues`/`_drain_control_queues`): the sink (`self._feed_health_events.put`, a plain `queue.Queue[FeedHealthEvent]`) only enqueues from the feed thread; a new `_drain_feed_health_events`, called from the supervisor's existing 1-second poll loop (the same loop that already drains control queues and checks the stuck-subscription alarm) is the only code that ever calls `repository.record_feed_event`, and it always runs on the thread that opened the connection. A write that raises inside the drain is logged and dropped, the same isolation `SafeNotifier` gives Telegram — a diagnostic row must never be able to interrupt the poll loop. |
| **D71** | **`SafeNotifier` gained its own generic rate limiter/aggregator; the two hand-rolled per-call-site latches it was meant to replace stay, because both also gate non-notification state** | Phase 7 Part 2's plan called for "replacing, not duplicating, the two hand-rolled latches" (`TradingEngine._entry_blocked is not None`, the supervisor's `_stuck_subscription_alarmed`). Checked directly before touching either: `_block_entries`'s own docstring states the latch is what keeps entries off for the day — "the latch is set first, then announced, so a disabled or throwing notifier cannot turn trading back on" — and the supervisor's latch also gates a forced `DEGRADED` heartbeat beat and an `errors`/`feed_events` row, not only the Telegram send. Deleting either to make room for a purely notification-layer replacement would have changed behaviour outside notifications. Resolved by keeping both exactly as they were and giving `SafeNotifier` its own key-based suppression (`(runtime_id, strategy_id, event_type, message)`, 60s default window) that every call site gets automatically — including every one that never had a latch of its own. In practice the two existing latches make `SafeNotifier`'s aggregation inert at those two sites (the caller never repeats the call) and it is the *only* protection everywhere else. `test_repeated_failures_are_suppressed_not_amplified` replaces `test_repeated_failures_are_counted_not_amplified`, whose old body asserted only that a counter incremented and never verified the "not amplified" the name promised — the Phase 7 Part 1 audit's own finding, closed here. |
| **D72** | **The notifier sentinel (`NOTIFIER_FROM_SETTINGS`) must be an `Enum` member, not a plain sentinel object — a plain one does not survive the `spawn` pickle round trip with its identity intact** | `run_worker`'s arguments cross the `spawn` boundary through `pickle` (module docstring: "a fresh interpreter... unpickles its arguments"). The first version of this sentinel was a bare `object()`-holding class instance; unpickling it in the child constructs a *new* instance, so `notifier is NOTIFIER_FROM_SETTINGS` silently evaluated `False` inside every spawned worker regardless of what the parent passed, and the un-recognised sentinel value fell straight through to being used *as* a notifier — `AttributeError: '...' object has no attribute 'send'` the moment anything called it. Caught immediately, not in production: the existing supervisor end-to-end suite already spawns real workers through the real `context.Process(target=run_worker, ...)` path, and nine of those tests failed the moment this shipped (`test_undelivered_ticks_do_not_wedge_the_supervisors_exit` and siblings in `test_supervisor.py`/`test_supervisor_signal.py`/`test_mode_separation.py`). Fixed by making the sentinel an `enum.Enum` member — pickle's specifically-documented, guaranteed-identity-preserving singleton mechanism — rather than inventing a custom `__reduce__`. `tests/unit/test_notifier_sentinel.py` adds a direct, minimal regression test (a real `pickle.dumps`/`loads` round trip, plus the negative control: a plain module-level sentinel class demonstrably does *not* survive the same round trip) alongside the indirect coverage the spawning tests already provided. |
| **D73** | **`TradingEngine.__init__` was silently double-wrapping an already-built `SafeNotifier` in a second one — type-valid (`SafeNotifier` structurally satisfies `Notifier`), functionally wrong** | `worker.py` builds exactly one `SafeNotifier` per process and, on the engine path, hands it straight through (`engine_worker.run_engine(notifier=safe_notifier, ...)` → `TradingEngine(notifier=notifier, ...)`). `TradingEngine.__init__` unconditionally did `self.notifier = SafeNotifier(notifier or NullNotifier())` — wrapping the inner `SafeNotifier` in an outer one. This predates Part 2 and was harmless while `SafeNotifier` had no state worth duplicating; it stopped being harmless the moment Part 2 gave it success/failure counters, an aggregation window and (for the engine path specifically) `deferred=True`/`on_failure` — all of which would have lived on the *inner*, untouched `SafeNotifier`, while `TradingEngine` only ever calls `.send()` on the outer one. Found while designing the deferred-mode wiring, not by a failing test — no existing assertion checked notifier identity or counted double-delivery. Fixed: `TradingEngine.__init__` now checks `isinstance(notifier, SafeNotifier)` and reuses it exactly as given; a bare `Notifier` still gets the fallback wrap, which now defaults `deferred=True` since that branch is reachable from `on_tick` regardless of caller. `test_the_engine_reuses_an_already_built_safenotifier_rather_than_double_wrapping` and `test_a_bare_notifier_is_wrapped_deferred_by_default` cover both branches directly. |
| **D74** | **`NotificationEvent.rendered()` now runs its own output through the active logging redactor — closing a real gap between what `common/logging/redaction.py`'s docstring claimed ("printed, persisted **or notified**") and what was actually enforced** | `SecretRedactingFilter` is a `logging.Filter`, reachable only via a handler `addFilter()` call — it was never wired anywhere near `TelegramNotifier.send()` (which builds its payload straight from `event.rendered()`, no `logging` call involved) or `record_notification`'s DB write. The specific Telegram guarantee was sound regardless — the bot token is read from `SecretStr` at send time and never stored on `NotificationEvent`, so nothing token-shaped could reach `rendered()` — but the *general* claim was aspirational, and Part 2 gives `rendered()` three new fields plus a real `notifications.message` column to write into, raising the stakes of leaving it that way. `rendered()` now calls `common.logging.active_redactor()` and returns the redacted text when a redactor is active (every production process; `setup_logging()` runs before any notifier is built), unredacted when none is (most unit tests, which never call `setup_logging`) — a real second layer where the docstring already claimed one, not a fix to a live leak. `test_a_known_secret_is_redacted_from_the_rendered_message` and `test_rendering_is_unredacted_when_no_logging_has_been_configured` cover both states. |
| **D75** | **A heartbeat's age can compute fractionally negative from SQLite/Python clock skew; `common.health.snapshot` now clamps it to zero rather than reporting a beat "from the future"** | `_process_health` and `_strategy_healths` both compute `(julianday('now') - julianday(beat_at)) * 86400.0` — SQLite's own clock — against a `beat_at` string written moments earlier from Python's `datetime.now(UTC)`. Found as an intermittent failure of Phase 7 Part 1's own `test_the_group_heartbeat_is_the_strategy_id_is_null_row` on a full-suite run, not written into the plan on purpose: `heartbeat_age_seconds == -0.0010058283805847168` where the test asserted `>= 0.0`. The two clocks are each individually correct; a read moments after the write occasionally samples them close enough that the subtraction goes fractionally negative. Not chased out of the SQL (`MAX(0, ...)` there would hide the same measurement one layer down, and any future caller of the raw query would rediscover it); fixed with a new `_non_negative_age()` helper applied in Python at both computation sites, the same judgement `PositionManager.adopt` already applies to MFE/MAE restarting at zero rather than a small negative — a truly negative age is not a fact worth reporting as one. `test_a_negative_age_from_clock_skew_between_sqlite_and_python_clamps_to_zero` pins the fix directly and deterministically, rather than relying on re-running the previously-flaky test enough times to trust it. |
| **D76** | **The PID-reuse incident was demonstrated on unmodified code before `common/process/locks.py` was touched, per the plan's fail-first standard — not assumed from reading the code** | `current_owner()` checked liveness alone (`_process_is_alive`, a bare `os.kill(pid, 0)`), and the module's own docstring already (falsely) claimed a command/start-marker check that nothing in the code performed. `test_a_live_process_that_is_not_ours_is_not_an_owner` (`tests/unit/test_process_locks.py`) was added first and run against the unmodified module: it spawns a real, signalable `time.sleep(60)` subprocess (deliberately not `pid: 1` — `_process_is_alive` treats a `PermissionError` from an unsignallable PID as "alive", so a `pid: 1` fixture would exercise the code path without reproducing the actual hazard: SIGTERM to `launchd` fails with `EPERM` and harms nothing), writes its PID into a PID-file fixture claiming the identity `intraday_options.supervisor`, and asserts `current_owner() is None`. Observed failure on unmodified code: `current_owner()` returned a populated `LockOwner(pid=29477, ...)` for the unrelated live process — proof that a PID recycled onto any live, signalable process today reads as a valid owner. Fixed by discriminating on `psutil.Process.create_time()` (exact, kernel-assigned once at process creation; a reused PID cannot share it) rather than the recorded command string, which is unstable in exactly the cases that matter — a `spawn`ed worker's `sys.argv` is a multiprocessing bootstrap line, and a repository move changes every recorded path, so matching on it risked the *inverse* incident (refusing to stop a supervisor that genuinely is ours). The paired over-strictness guard, `test_a_genuinely_live_holder_is_still_recognised`, covers a lock this process actually holds and a spawned child with a multiprocessing-bootstrap-shaped `command` string in its fixture — proving a command mismatch alone no longer matters — and passed both before and after the fix, so the tightened check provably did not swing the other way. `clear_stale_pid_file()` (verified stale cleanup — spec step 6's second clause, previously unimplemented) and `IntradayOptionsSupervisor` acting on `EXIT_DUPLICATE` (previously recorded and never inspected — a refused worker was silently a zero-length run) round out Part 4's PID hardening. See "PID-reuse drill, by hand" in Part 4's own writeup below for the same defect confirmed once more against the real `scripts/stop_runtime.py`. |
| **D77** | **`multiprocessing.Process(target=run_worker, ...)` discarded every `WorkerOutcome.exit_code` a real spawned worker computed — `worker_process.exitcode` was always 0 regardless — found by Part 4's own new test, not by reading the code for it** | `multiprocessing` calls its `target` and discards the return value; only an uncaught exception or an explicit `sys.exit()` changes a spawned child's real OS exit code, and `run_worker` only ever returned `WorkerOutcome`. `test_a_duplicate_worker_is_reported_not_silent` (`tests/end_to_end/test_supervisor.py`, written to prove the supervisor now acts on `EXIT_DUPLICATE` — see D76) asserted `result.worker_exit_codes["skelfix"] == EXIT_DUPLICATE` against a real spawned duplicate and got `0`. This was not specific to `EXIT_DUPLICATE`: the integrity-check-failure exit path and the per-candle exception handler's `exit_code = 1` in `worker.py` were equally invisible to `worker_exit_codes` through any real `spawn`ed process, every one of them silently reporting success. Fixed with a new `run_worker_process()` wrapper — calls `run_worker()` then `sys.exit(outcome.exit_code)` — used **only** as the `multiprocessing.Process` target; `run_worker()` itself is unchanged, because dozens of existing tests call it directly, in the test's own process, and expect a returned `WorkerOutcome`, not a raised `SystemExit`. `supervisor.py`'s `context.Process(target=...)` now points at `run_worker_process`. |
| **D78** | **`strategy_token()`'s 4-character truncation lets two different strategy ids collide on the same `correlation_id`, which the parallel-worker test only reached once D77 stopped masking the crash as a clean exit** | Re-running the suite after fixing D77 surfaced a second, previously-hidden failure: `test_two_workers_receive_identical_bars` raised `sqlite3.IntegrityError: UNIQUE constraint failed: order_intents.correlation_id`. `common/execution/correlation.py` truncates a strategy id to 4 characters for the embedded token; `"skelone"` and `"skeltwo"` both truncate to `"skel"`, and since `next_sequence_number` is scoped by the full, untruncated strategy id, both strategies independently compute sequence 1 for their first order — byte-identical `correlation_id` strings. Previously invisible because D77 silently turned the resulting crash (exit code 1) into a reported 0. Not fixed by redesigning the correlation-ID format — too large a change, under time pressure, to a heavily-used, well-tested module — but by refusing the collision at admission time: `strategy_token()` is now a public function (the exact token `build_correlation_id` will embed, explicitly **not** guaranteed unique), and `IntradayOptionsSupervisor.add_worker()` refuses the second strategy whose token collides with an already-admitted one, recording an `errors` row (`component="supervisor.correlation_token_collision"`) and a notification — the same individual-refusal shape the existing live-gate admission check already uses, never crashing the group or corrupting `order_intents`. The triggering test was renamed to non-colliding strategy ids (`alphaskel`/`bravoskel`) to verify its own original intent again; `test_a_worker_whose_correlation_token_collides_is_refused_not_crashed` covers the original colliding pair directly. |
| **D79** | **`Settings.algo_log_level` was read from the environment since Phase 0 and never once reached `setup_logging(level=...)` — every process has been running at `INFO` regardless of configuration** | Found during the Phase 7 audit, not by a failing test: nothing asserted on the *effective* root logger level, only on redaction and rotation. `setup_logging`'s `level` parameter defaults to `"INFO"` and none of the four production call sites (`runtimes/intraday_options/__main__.py`, `runtimes/intraday_options/worker.py`, `scripts/auth_bootstrap.py`, `scripts/capture_live_tape.py`) passed it — each called `setup_logging(log_dir=..., settings=settings)` and relied on the default, even though `settings.algo_log_level` was sitting unread on the same object one line above. `ALGO_LOG_LEVEL=DEBUG` in `.env` has therefore never changed anything a real process did. Fixed at all four sites: `setup_logging(level=settings.algo_log_level, log_dir=..., settings=settings)`. |
| **D80** | **`common/persistence/migrations.py` claimed a future destructive migration "arrives with backup and rollback machinery" — Phase 7 Part 5 built the backup half, and the comments needed correcting to stop implying the other half exists too** | Two places said it: the `_DESTRUCTIVE_RE` guard's own comment, and the `MigrationError` message `_reject_destructive` raises when a migration script contains a rejected statement. Part 5's `common.retention.backup_database` now runs before every migration on every controlled startup, so a pre-migration snapshot genuinely exists by the time a migration applies — but nothing restores from it, checks it against a running schema, or replays writes made since it was taken. Left uncorrected, the comment and the error message a future implementer reads right before attempting a destructive migration would overstate what already exists and understate what that migration still needs to bring. Both are rewritten to say precisely that: backup exists (Part 5), rollback does not, and a genuinely destructive migration still needs rollback machinery built and tested before this guard is revisited. |
| **D81** | **The legacy-system guard was proven against the real, currently-active legacy installation on this machine, not a synthetic stand-in — and the plan's own "Legacy exclusion" decision was to detect and document, never to unload it from inside this session** | `common/process/legacy_guard.py` was run for real, unmocked, during this phase and found both signals genuinely positive: `launchctl list com.soundarraj.tradingautomation.starttrading` returned 0 (loaded), and a live `weekly_strategies` process was found under `/Volumes/Trading/Trading_Automation` by a real `psutil.process_iter` scan — independently corroborating the runbook's own Phase 0 audit note that this component was "paper-mode only" and still running. `tests/unit/test_legacy_guard.py` keeps two tiers apart on purpose: synthetic fixtures (portable, run in CI) prove the matching logic — including the trap a naive `/Volumes/Trading/` mount-root prefix would fall into, since this repository lives on the same mount — and a pair of real-machine tests read the *actual* installed plist with `plistlib` and assert the module's `LEGACY_LAUNCHD_LABEL` constant equals its real `Label` byte for byte, skipping (not failing) on a machine without that plist. The matching pre-existing-test hazard this surfaced: `tests/unit/test_intraday_options_main.py`'s suite would otherwise depend on whatever the legacy system happens to be doing on whichever machine runs it — fixed with its own autouse `_legacy_system_inactive` stub, the same shape `isolated_env` already gives `.env`. |
| **D82** | **`supervised_launch.py` writes to `errors`, not `audit_events` — a schema constraint, checked before writing the test that would have hit it** | The natural first instinct was `ExecutionRepository.record_audit_event`, matching every other operator-facing script. `audit_events.action` (migration 0004) has a closed vocabulary enforced by a `CHECK` baked directly into its `CREATE TABLE IF NOT EXISTS`, and the migration runner is additive-only — it rejects `DROP`, which any standard SQLite `CHECK`-widening rebuild (rename, recreate, copy, drop the old table) needs. Widening that vocabulary for one new action would need a real migration this phase does not otherwise call for. Reconsidered from the semantics, not just the schema: a launch attempt is an automated lifecycle event, not an operator issuing a live-impacting command with confirmation — exactly what `errors` (unconstrained `component`/`severity`) already models for `IntradayOptionsSupervisor`'s own lifecycle events (`component="supervisor.correlation_token_collision"`). Severity follows spec section 14's own table: `INFO` for a clean stop, `WARNING` per retryable attempt or deliberate refusal, `ERROR` once every attempt is exhausted. `tests/unit/test_supervised_launch.py::test_errors_rows_are_written_for_each_attempt_and_the_final_give_up` reads the real rows back rather than trusting the call was made. |
| **D83** | **`rotate_launchd_logs` renames a file `launchd` (or the current process) may still hold open — verified safe on this project's actual target platform, not assumed from POSIX documentation, before it was built on that assumption** | Limitation 34 was reopened mid-Phase-8 once the actual volume was traced: every production `setup_logging()` call leaves `console=True`, so `launchd`'s captured `stdout`/`stderr` files are a full, unbounded second copy of the whole application log stream, not a handful of incidental lines. Closing it needs a rename step ahead of `sweep_logs`, but a rename against a file the currently-running process might still be writing to is exactly the kind of "should be fine per the spec" claim this codebase does not accept without checking (the same standard **D76** held `create_time()` to). Checked directly, on this machine, before any production code depended on it: `os.open` a file append-mode (mirroring how `launchd`'s `posix_spawn` file actions redirect `StandardOutPath`), `os.rename` the path out from under that open descriptor, write more through the *original* handle, then `os.open` a fresh handle at the now-vacant canonical path — confirmed on Darwin 25.6.0 arm64 that the renamed inode receives every byte written both before and after the rename, and the canonical path gets a genuinely empty file, never the renamed one's contents. This is what makes `rotate_launchd_logs` safe to run unconditionally, every controlled startup, even mid-way through a `launchd`-owned file's active lifetime. The second half of the safety argument — that a file renamed moments ago can never be compressed or deleted by the `sweep_logs` call that immediately follows it in the same `run_retention` call — rests on `RetentionConfig.log_max_age_days`/`.log_compress_after_days` both being declared `gt=0` (a one-day floor), pinned directly by `test_a_freshly_rotated_file_is_never_touched_by_the_same_runs_sweep` rather than left as an inference from the two `Field` declarations. The gap itself was demonstrated before the fix, not assumed: `test_a_launchd_style_file_accumulates_unbounded_without_rotation` proves `sweep_logs` alone is a permanent no-op against an unrotated file however old it gets, and `test_rotation_then_sweep_makes_the_launchd_log_visible_to_retention` proves the identical file, rotated first, is deleted by that same call. |
| **D84** | **`supervised_launch.py::run()` had no exception handling around the call to `supervisor_main.main()` — an unhandled exception there escaped the whole bounded-restart mechanism the module exists to provide, and did so silently (no `errors` row, no retry)** | Raised independently, not found by this session's own review: verification was requested of a claim that the wrapper "only classifies returned exit codes, with no exception handling around the call." Read directly against the code, the claim was correct — the single call site had no `try`/`except` at all. A fail-first test (`test_an_unexpected_exception_is_retried_like_a_transient_failure`) run against the unmodified module confirmed the consequence directly: a `ValueError` raised from the stubbed `supervisor_main.main` propagated straight out of `sl.run(...)` uncaught, never reaching a single `_record_attempt` call — exactly the "launchd's own `KeepAlive` is unbounded" failure mode the module's own docstring says this file exists to prevent, just reached through a bug rather than a classified exit code. Fixed by wrapping the call in `except Exception` (never `except BaseException`) inside the retry loop: an unexpected exception is logged with its full traceback (`_log.exception`, the same convention `common/feed/reconnect.py` and `runtimes/intraday_options/worker.py` already use), folded into the existing `max_attempts`/`backoff_seconds` retry path via a new optional `exception` parameter on `_record_attempt` (its type and message land in the `errors.message` text, alongside the existing `runtime_id`/attempt-number columns), and — once attempts are exhausted — returns `EXIT_GAVE_UP` exactly like a retryable exit code would. All five `TERMINAL_EXIT_CODES` and both `RETRYABLE_EXIT_CODES` are unchanged; this adds a third path into the same mechanism, it does not touch the classification sets. `except Exception`, specifically not `except BaseException`, is what keeps a deliberate `SystemExit`/`KeyboardInterrupt` propagating untouched — pinned by `test_a_system_exit_is_never_caught_or_retried`, which (correctly) already passed against the unmodified module too, since an uncaught `SystemExit` was never touched by anything either before or after this fix; it exists to guard the boundary against regressing, not to demonstrate the bug. This is a new finding, not a reopening of any existing "known limitation" — no prior limitation entry named this gap. |
| **D85** | **`legacy_guard.py`'s launchd check collapsed "confirmed not loaded" and "`launchctl` unavailable/errored/timed out" into the same `False`, directly contradicting the "Fail-closed" comment already sitting at its own call site** | Also raised independently and verified, not assumed correct or wrong going in. `runtimes/intraday_options/__main__.py`'s call site carried (and still carries) the comment "Fail-closed — a legacy system that cannot be determined either way is not 'not detected'" directly above `if legacy_status.active:` — but `_launchd_label_loaded`'s `except (OSError, subprocess.TimeoutExpired)` branch returned the same `False` that a confirmed-`returncode != 0` result did, and `LegacySystemStatus.active` was `launchd_label_loaded or process_running`, a plain boolean OR with no way to distinguish the two. `tests/unit/test_legacy_guard.py::test_launchctl_unavailable_is_reported_not_raised` even pinned the collapsed value directly (`launchd_label_loaded is False`) as expected behaviour — the contradiction was built-in and tested as if it were correct. Fixed with a new `LaunchdLabelState(StrEnum)` (`ACTIVE`/`INACTIVE`/`UNKNOWN`, the same pattern as `common/health/heartbeat.py::HealthState`) replacing the boolean: `LegacySystemStatus.active` is now `launchd_state is not INACTIVE or process_running` — `ACTIVE` and `UNKNOWN` both refuse, only a confirmed `INACTIVE` plus no independently-detected process allows a start. A new `undetermined` property (`UNKNOWN` and no process found) lets both call sites (`runtimes/intraday_options/__main__.py`, `scripts/validate_environment.py::_check_legacy_system`) give the operator a "state could not be determined... resolve why launchctl could not be queried" message distinct from a confirmed "appears active... unload it first" one, and stop `validate_environment` printing "OK: legacy Trading_Automation system not detected" for a check that was never actually able to run. Fail-first, and stronger than a per-test assertion failure: run against the unmodified module, every test file that imports the new `LaunchdLabelState` symbol — `test_legacy_guard.py`, `test_intraday_options_main.py`, and a new `tests/unit/test_validate_environment.py` (the call site had no dedicated test file before this) — fails to even collect (`ImportError: cannot import name 'LaunchdLabelState'`), confirmed directly by stashing the source changes and re-running with the test changes in place. The fix is structurally required before any of these tests can execute at all, not just to make one assertion pass. The existing mount-root/process-scan matching tests (`test_this_repositorys_own_process_does_not_match` and neighbours) and the plist-label real-machine tests are untouched — this fix is scoped to the launchd signal only, per the original verification finding. Not a reopening of any existing "known limitation" either — like D84, this was never previously recorded as an open gap; both defects existed since D81/D82 first built these modules in this same Phase 8 and are closed within it, never having reached a released phase boundary as documented limitations. |

#### D22 in detail: the rebuilt premium-candle mapping

Every other port in Phase 3 is provable by diff — sorted test-name lists, identical
`assert` counts. These nine are not, because the reference's suite is welded to a
real strategy. So the mapping is stated property by property, with the fidelity of
each one named rather than implied.

Reference: `strategies/ema_cross/tests/test_premium_candle_exit.py` (9 tests).
Rebuild: `tests/integration/test_engine_premium_candle_exit.py` (12 = 8 mapped +
1 substitute + 3 additions).

**Equal or stronger (6).**

| # | Reference property | Rebuilt as | Fidelity |
|---|---|---|---|
| 2 | CE exits from its premium candle while the underlying is flat — one trade, CE, a premium-candle reason | `test_a_ce_position_exits_on_a_lower_premium_close` | **Equal.** The reference holds the underlying deliberately flat after entry; the rebuild sends no underlying ticks at all, so the isolation is stricter. The reference accepts any of `{MOMENTUM_LOW, HIGHEST_CLOSE_TRAIL, MOMENTUM_AND_TRAIL}` because its *combined* policy can report three reasons; the rebuild uses `MOMENTUM_CLOSE` and asserts exactly `MOMENTUM_LOW`. The reference's `[CANDLE_EXIT]`/`[CANDLE_EXIT_SUMMARY]` log assertions are **not** carried over: those lines are emitted by `EMA1TradeManager`, a strategy component, not by the engine |
| 3 | PE symmetric to CE | `test_a_pe_position_exits_on_its_own_premium_stream` | **Stronger.** Adds a contradictory CE stream running the other way and asserts only the PE contract's closes ever reached the strategy |
| 4 | A stalled premium feed is reported loudly, does not phantom-exit, and square-off still force-closes | `test_a_premium_tick_gap_is_logged_and_square_off_still_closes_the_position` | **Equal.** Same three assertions, plus `option_candles_seen == 0` |
| 6 | Stop loss wins the reported reason over the candle exit, and exactly one trade books | `test_the_risk_manager_stop_takes_priority_over_the_premium_candle_exit` | **Equal** |
| 7 | Stray ticks after a close never book a duplicate exit | `test_a_stray_tick_for_a_closed_position_does_not_exit_twice` | **Equal.** The reference exits via SL and the rebuild via the premium rule; incidental to the property |
| 9 | Resumed ticks after a gap reset the stale candle and the strategy's history | `test_a_gap_resets_the_premium_candle_and_re_primes_the_streak` | **Stronger.** The reference calls `_check_premium_candle_exit` directly; the rebuild runs the engine end to end and proves the *consequence* — the post-gap bar closes at 60, far below the pre-gap 108, and does **not** exit. The reference's two structural assertions (bar rebuilt from the resuming tick's bucket, streak reference cleared) were added so the map is exact rather than behaviour-only |

**Same property, different layer (2).**

| # | Reference property | Rebuilt as | Difference |
|---|---|---|---|
| 1 | Entry is driven by the underlying alone; the premium stream has zero influence | `test_entry_is_gated_on_the_underlying_stream_not_the_premium_stream` | The reference tests this **at the strategy level** — it drives `EMA1Strategy.on_candle` 30 times directly and never builds an engine. The rebuild tests the **engine's routing**: premium ticks arrive first and produce nothing; entry fires only when an underlying candle closes. Its "`on_option_candle` was never called" phrasing cannot come across, being a fact about the EMA strategy rather than the engine |
| 5 | A second entry starts with fresh state, not the first trade's | `test_premium_candle_state_does_not_leak_across_a_re_entry` | The reference tests **the trade manager** with `SimpleNamespace` fakes (`extreme_close` resets on `on_position_closed`). The rebuild drives a real re-entry through the engine, on a **different strike**, with a deliberate leak detector: the new contract's first bar closes at 78, *below* the previous contract's last close of 85, so leaked state would exit immediately and book three trades instead of two |

> The first version of property 5 asserted only that state was clean *after* a
> close and never re-entered — the same "the name promises a streak the test never
> walks" flaw recorded against the ported `test_momentum_close_...consecutive` in
> section 1. Reworking it to perform a genuine re-entry immediately caught a real
> leak **in the test double**: the strategy's streak reference survived across
> positions, so the new leg's first bar was compared against the old contract's
> last close. Fixed in `EngineFixtureStrategy.on_position_closed`, mirroring what
> `EMA1TradeManager` does. The rebuilt test now covers both halves of the property
> — the engine clearing its candle builder *and* the strategy clearing its streak.

**Does not map (1) — an accepted coverage gap.**

Reference #8, `test_conflicting_underlying_and_premium_exits_are_rejected`: a config
that enables `momentum_low` alongside the premium block must raise at
`EMA1TradeManager` construction, so two candle-exit systems never race one position.

There is **no equivalent in Part 2b-i**, and the neighbouring
`test_a_continuity_required_strategy_with_warm_up_disabled_is_rejected` is a
**substitute, not a port** — also fail-fast-at-construction, but a different rule
(`InvalidWarmupConfig`). The plan called this "config conflict rejection", which was
loose.

The reason it does not map is structural: exit *composition* is owned by
`CompositeExit`/`build_exit_engine` (Part 2a) and by a strategy's own trade manager
(Phase 9) — never by `TradingEngine`. The nearest existing coverage is Part 2a's D3
tests, which assert `momentum_low_or_highest_close` is absent from `_KEY_TO_ENGINE`
and cannot be selected by config; related, but not the same guarantee. Recorded as a
genuine residual gap, correctly landing in **Phase 9**, when a strategy first
composes exits and there is something for the rule to protect.

**Additions beyond the nine (3).** `test_momentum_close_walks_a_real_consecutive_streak_on_the_premium_stream`
(closes runbook item 5), `test_a_strategy_whose_warmup_spec_raises_is_blocked_from_entering`
(entries latch off, exits stay live), and
`test_the_premium_builder_reuses_the_interval_of_the_underlying`.

---

## 3. What exists now

```
pyproject.toml                     packaging, ruff, mypy, pytest config
requirements.lock                  78 fully pinned packages
.env.example                       empty placeholders only
README.md
docs/
  ALGO_TRADING_FORWARD_TESTING_ARCHITECTURE_FINAL.md   (source of truth)
  IMPLEMENTATION_STATUS_AND_RUNBOOK.md                 (this file)

common/config/
  settings.py      SecretStr-backed env/.env secrets; has_*_credentials()
  paths.py         one project root; every path derived; fails if root missing
  models.py        typed global/runtime/strategy models + effective_live_gate()
  loader.py        layered YAML resolution, strict validation, env overrides
  fingerprint.py   stable SHA-256 digest of a resolved config

common/logging/
  redaction.py     SecretRedactingFilter — literal + pattern masking
  setup.py         console + rotating file, structured key=value context

common/persistence/
  database.py      WAL, foreign_keys=ON, busy_timeout, synchronous=FULL,
                   explicit transactions, read-only connection factory
  migrations.py    forward-only runner, filelock, integrity checks, replay guard
  migrations/versions/0001_walking_skeleton.sql   the ten spec tables

--- added in Phase 1 ---

common/models/
  market.py        Tick, Candle — frozen, picklable, self-validating
  trading.py       Signal, OrderIntent, Order, Fill, Position + enums

common/market_data/
  adapter.py       MarketFeedAdapter Protocol
  recorded.py      RecordedFeedAdapter — deterministic tape replay (all tests)
  dhan.py          DhanMarketFeedAdapter — THE ONLY module importing dhanhq

common/candles/aggregator.py   completed-bars-only, session-aware, no rewrites
common/feed/
  hub.py           SharedFeedHub — subscribes the union once, fans out CANDLES
  queues.py        BoundedWorkerQueue — drop-oldest, counted, never blocks

common/broker/
  base.py          Broker Protocol + Quote
  paper.py         PaperBroker — bid/ask fills, latency-selected quotes, resting
                   limit orders, partial fills, nine rejection rules (Part 5)
  quotes.py        QuoteBook — recent quotes per instrument; read by the fill
                   model for its latency deadline and by the lifecycle for depth
  costs.py         ChargesCalculator — config-driven rates
  factory.py       build_broker() — consults the live gate, REFUSES live

common/execution/
  correlation.py   p_/l_ namespaced IDs, length-checked, date-validated
  repository.py    the spec's transaction boundaries, idempotent fills
  lifecycle.py     signal → intent → broker → fill → position

common/risk/squareoff.py       cutoff before square-off; restart-safe state
common/notifications/          Notifier Protocol, SafeNotifier, Telegram
common/health/heartbeat.py     12 health states, rate-limited beats
common/process/locks.py        filelock + PID-ownership; duplicate refusal

strategies/intraday_options/fixture_strategy.py   TEST-ONLY signal fixture
runtimes/intraday_options/
  worker.py        spawn-safe module-level entrypoint; recovery sequence
  supervisor.py    one shared feed, one child process per strategy
dashboards/app.py  one read-only tile

config/
  global.yaml                     live_trading_enabled: false
  runtimes/intraday_options.yaml  enabled: false
  strategies/skeleton_fixture.yaml   the test fixture, mode: paper

--- added in Phase 2 (Block 1) ---

common/authentication/
  exceptions.py    taxonomy with an explicit `retryable` flag, not inheritance
  jwt_claims.py    read the unverified `exp` claim; None means "don't know"
  totp.py          pyotp provider (lazy import) + clock-skew diagnostic
  dhan_login.py    the ONLY module talking to auth.dhan.co; httpx, not the SDK
  token_cache.py   atomic 0600 write, identity + expiry checks, filelock,
                   and the credential-rejection cooldown
  bootstrap.py     token precedence, retry policy, fails closed

common/feed/reconnect.py       bounded backoff + jitter, union resubscribe,
                               805-809 reasons, 807 -> token refresh, staleness
common/market_data/option_chain.py   per-key 3s throttle, TTL cache, dedup,
                                     freshness; NO order surface
common/persistence/migrations/versions/0002_feed_and_auth_health.sql

scripts/
  auth_bootstrap.py      pre-market bootstrap; --status makes no network call
  capture_live_tape.py   Block 2 tape capture; read-only; scrubs the output

--- added in Phase 3 Part 1 ---

common/market_data/adapter.py  request_stop() added to the Protocol; the thread-
                               ownership rule is now part of the contract
common/market_data/dhan.py     request_stop(); start() closes on its own thread;
                               stop() refuses a cross-thread close rather than hang
common/feed/reconnect.py       stop() routes by ownership; request_stop();
                               wait_until_stopped(); owner-thread close handoff
common/feed/hub.py             request_stop() — adapter signal without the flush;
                               request_subscription() — same shape, one layer out
                               (Part 2b-ii-A, D24)
runtimes/.../supervisor.py     feed on its own daemon thread; SIGTERM/SIGINT
                               handlers, installed and restored; ordered shutdown;
                               group session + heartbeats; DEGRADED/error/notify
                               when the feed cannot be closed

tests/integration/test_feed_cross_thread_shutdown.py   real threads, blocking double
tests/end_to_end/test_supervisor_signal.py             real SIGTERM to a real process
tests/end_to_end/supervisor_signal_child.py            its child; not a test module

--- added in Phase 3 Part 2b-i ---

common/engine/
  engine.py        TradingEngine — ported; installs NO signal handler (D18)
  models.py        StrategySignal, OpenPosition, OptionContract, Trade (D19)
  config.py        EngineConfig/SessionConfig — the six values the engine reads
  session.py       MarketSession — is_open / can_enter / calendar
                   (is_past_square_off REMOVED in 2b-ii-B-1; see square_off.py)
  strategy.py      BaseStrategy ABC + strategy registry
  feed.py          MarketDataFeed ABC + SimulatedFeed (the engine's consumer side)
  positions.py     PositionManager + ExecutionGateway seam + InMemoryGateway
  selection.py     OptionSelector, resolve_strike, SimulatedOptionChainResolver
  risk.py          RiskManager ABC + registry + opt_float (no concrete manager)
  daily_guard.py   DailyRiskGuard — the day-level latch
  regime.py        RegimeTagger + NullClassifier only (D21)
  reporting.py     summarise/DailySummary + the reporter/report protocols (D20)

common/candles/builder.py      per-chart CandleBuilder; emits common.models.Candle
common/process/signals.py      shutdown_signals() — moved out of supervisor.py;
                               ONE handler installer per process
common/utils/timeutils.py      extended: now_ist/now_tz/parse_timeframe_minutes
common/warmup/requirements.py  extended: StrategyWarmupSpec, validate_warmup_config

strategies/intraday_options/engine_fixture_strategy.py   TEST-ONLY; deliberately
                               NOT re-exported from the package (spawn import cost)

tests/unit/test_engine_mfe_mae.py                    7, ported verbatim
tests/unit/test_engine_session_gating.py             1, ported verbatim
tests/integration/test_engine_premium_candle_exit.py 12, rebuilt (D22)
tests/integration/test_engine_square_off.py          10, new: the signal-ownership gate

--- Phase 3 Part 2b-ii-A (the feed seam) ---

common/feed/queues.py          DEFAULT_TICK_MAX_DEPTH = 2048, sized from a
                               measured tick rate (Block 2: ~4 ticks/s/instrument)
common/feed/hub.py             opt-in tick channel; request_subscription() applied
                               at the on_tick boundary (D24); drop_subscription();
                               queue_stats() covers both queues
common/engine/hub_feed.py      HubTickFeed — the MarketDataFeed the engine consumes;
                               sentinel None -> square-off request; subscribe()
                               forwarded upstream
runtimes/.../supervisor.py     per-worker tick + control queues (opt-in via
                               add_worker(tick_channel=True)); control queues
                               drained each heartbeat iteration; _release_queues()
                               so undelivered ticks cannot wedge exit (D25)

tests/integration/test_feed_tick_channel.py          18, new: routing, sizing, D24
tests/integration/test_hub_tick_feed.py              12, new: the worker-side feed
tests/integration/test_engine_over_hub.py            4, new: the Part 2b-ii-A gate
tests/end_to_end/test_supervisor.py                  +5: plumbing, control-queue
                                                     hop, the D25 wedge regression

--- Phase 3 Part 2b-ii-B-1 (the execution seam) ---

common/engine/square_off.py    SquareOffAuthority protocol (due/completed);
                               SessionSquareOffAuthority (default, clock-only);
                               PersistedSquareOffAuthority (policy + persisted
                               state; inherited IN_PROGRESS is retried — D29)
common/engine/gateway.py       LifecycleGateway — drives OrderLifecycle so every
                               open/close is persisted; monotonic per-instrument
                               candle_end_at (D26); raises rather than fabricating
                               a FillOutcome; GatewayExecutionError
common/engine/engine.py        square_off_authority injected; public block_entries()
                               and entries_blocked
common/engine/config.py        SessionConfig.from_square_off_policy(); from_resolved
                               refuses a session and a policy together
common/feed/queues.py          TickDropNotice + drop_notice_cadence() (D28)
common/feed/hub.py             publishes the notice on a cadence, per worker
common/engine/hub_feed.py      recognises the notice; on_tick_dropped hook;
                               ticks_dropped_upstream

tests/unit/test_engine_square_off_authority.py       29, new
tests/integration/test_engine_lifecycle_gateway.py   21, new: the B-1 gate
tests/integration/test_tick_drop_blocks_entries.py   31, new: limitation 14 closed
tests/integration/test_feed_tick_channel.py          1 narrowed (drop-oldest now
                                                     asserted over the ticks)

--- Phase 3 Part 2b-ii-B-2 (the wiring) ---

runtimes/intraday_options/
  engine_worker.py             NEW. Every common.engine import in the worker process
                               lives here, behind worker.py's one deferred import
                               (D30/D31). Builds the engine, drives it on the MAIN
                               thread, drains the unused candle queue on a daemon
                               thread, recovers an open position, raises the
                               three-channel alarm if one is left open
  worker.py                    EngineWorkerConfig (primitives only, picklable);
                               WorkerConfig.engine; run_worker gains tick_queue and
                               control_queue; close_previous_session and
                               resolved_config_stub made shared
  supervisor.py                passes tick + control queues to the child; publishes
                               the None sentinel on the tick channel too; reports
                               tick drops under "<strategy_id>:ticks"

common/engine/state_payload.py NEW. read_payload/merge_payload — the two
                               save_strategy_state traps handled in one place
common/engine/reporting_bindings.py
                               NEW. HeartbeatEngineReporter + RepositoryReportWriter
                               (D20 bound, D32). Deliberately NOT re-exported from
                               common/engine/__init__.py: it needs HealthState at
                               runtime, which would drag common.execution into every
                               common.engine import
common/engine/positions.py     PositionManager.adopt() — seeds a position without
                               calling the gateway
common/engine/models.py        AdoptedPosition
common/engine/engine.py        recover_position hook, consulted at the end of
                               _start_day(); fails closed if it raises
common/engine/gateway.py       optional repository -> writes/clears the contract
                               record; executions counter
common/engine/hub_feed.py      should_stop, asked on every poll wake — closes the
                               engine half of limitation 13

tests/unit/test_worker_import_boundary.py            7, new: the boundary, enforced
tests/unit/test_engine_state_payload.py              15, new
tests/unit/test_position_manager_adopt.py            12, new
tests/integration/test_engine_worker.py              20, new
tests/integration/test_engine_worker_restart.py      10, new: the restart gate
tests/end_to_end/test_engine_worker_signal.py        5, new: the signal gate
tests/end_to_end/engine_worker_signal_child.py       child process for the above
tests/integration/test_hub_tick_feed.py              +3
tests/end_to_end/test_supervisor.py                  +3

tests/    523 unit, 253 integration, 39 end-to-end, 6 smoke (skipped)
  fixtures/nifty_tick_tape.json                       24 ticks, 6 one-minute buckets
  fixtures/dhan_ticker_payloads_synthesised.json       SYNTHESISED in Block 1 from the
                                                       SDK's own parsers; kept permanently
                                                       for exhaustive branch coverage
                                                       (Quote/OI/status/untraded frames a
                                                       real capture cannot supply)
  fixtures/dhan_ticker_payloads_real.json              CAPTURED in Block 2 from a real
                                                       connection; proves the observed
                                                       shape matches source inference
```

### 3.1 Defects found and corrected in Phase 2

Five real bugs, all in code Phase 1 shipped or in the reference implementation
being ported. Recorded because the *reason* each existed is the useful part.

| # | Defect | Why it mattered | How it was found |
|---|---|---|---|
| 1 | `_parse_exchange_time` fell back to receipt time on **every tick** | `LTT` is `strftime('%H:%M:%S')` — no date — so `datetime.fromisoformat` raised every time. Candles were bucketed by local arrival rather than exchange time, inverting the aggregator's documented contract. A 100 % fallback rate was indistinguishable from healthy operation. | Reading the SDK's `utc_time()` source, then confirming `fromisoformat("09:15:03")` raises. |
| 2 | Live candle volume was **always zero** | Phase 1 read `last_quantity`, which appears in no Dhan frame. Quantity is `LTQ`, and only in Quote Data. | Generating the fixture from `process_ticker` and finding no such key. |
| 3 | Ordinary traffic **inflated `malformed_payloads`** | `process_status` returns the bare string `"Markets Open"` and `process_data` returns `None` for unknown frames. Both failed the `isinstance(payload, dict)` guard, so a genuine shape problem could never be distinguished from normal operation. | Reading `process_data`'s dispatch table. |
| 4 | `stop()` **never closed the socket** | It called the `async` `disconnect()`, producing an un-awaited coroutine. `close_connection()` is the synchronous wrapper. In 2.1.0 `disconnect()` also never called `ws.close()` at all. | Reading the SDK's method signatures while assessing the pin. |
| 5 | An HTTP 200 with no token was **retried** | The reference implementation decided retryability by substring-matching the response message. Dhan reports its ~1-per-2-minute generation cap in exactly that shape, so a rephrasing upstream would have reclassified it as transient — three attempts in ten seconds against a two-minute limit. | Tracing the reference's `_parse_response` fall-through while designing the rejection path. |

Two more found in this phase's *own* new code, before it shipped:

- **`refresh()` returned the cached token**, defeating its only purpose. On Dhan
  code 807 the cached token still looks valid by its `exp` claim, so an 807
  recovery loop would have re-presented a dead token forever. Fixed by making the
  under-lock cache check discriminating (`replacing=`) rather than skipped, so
  concurrent 807s still collapse to one login.
- **A flaky spawn test.** `FRESH = _jwt(time.time() + 86400)` was recomputed in
  each spawned child, so children straddling a second boundary minted different
  "same" tokens. Surfaced only under a slower socket-blocked run. Fixed with
  absolute epochs.

Two redaction gaps closed, both verified by observing the actual output rather
than assumed:

- **`dhanClientId` was never pattern-redacted.** `\bclientid\b` cannot match
  inside `dhanClientId` — there is no word boundary between "dhan" and "Client".
  It was masked only when the literal value happened to be registered, which is
  exactly the case pattern redaction exists to cover.
- **The greedy value match swallowed whole query strings.** `?dhanClientId=X&pin=Y&totp=Z`
  collapsed into a single redaction beginning at the first *recognised* key, so
  later secrets were masked by accident of ordering, earlier ones were exposed,
  and any non-secret parameter trailing a secret was destroyed. Fixed by
  terminating a value at `&`.

### Verification results (Phase 1, 29 July 2026)

| Check | Result |
|---|---|
| `pytest` | **281 passed, 2 skipped** |
| `ruff check .` | **All checks passed** |
| `ruff format --check .` | **75 files already formatted** |
| `mypy` (strict) | **Success: no issues found in 52 source files** |

All four run clean. Nothing was skipped, weakened or marked `xfail`. The 2 skips
are the opt-in live-feed smoke tests, which are skipped by design unless
`ALGO_LIVE_SMOKE=1` and real credentials are present.

Test distribution: 223 unit, 38 integration, 20 end-to-end, 2 smoke (skipped).

`mypy` scope was widened this phase from `packages = ["common"]` to include
`strategies`, `runtimes` and `dashboards`. The new packages were otherwise
unchecked, and widening immediately caught a real type error in the supervisor.

#### Walking-skeleton gate evidence

| Spec gate | Evidence |
|---|---|
| One feed event reaches a worker through the shared adapter | `test_the_supervisor_spawns_a_worker_that_trades` — 24 ticks in, 6 candles out, worker exit 0 |
| One completed candle creates one deterministic paper order | `test_one_completed_candle_creates_one_deterministic_paper_order` — exactly one intent; the persisted signal's `candle_end_at` matches the bar |
| Fill and position in SQLite with `execution_mode=paper` | `test_fill_and_position_are_persisted_with_execution_mode_paper` — every row across five tables is `paper`; every correlation ID starts `p_` |
| One dashboard tile works | `test_the_dashboard_tile_renders_from_a_read_only_connection`, plus `test_the_dashboard_connection_cannot_write` (a write raises `readonly database`) |
| One Telegram event works | `test_a_telegram_event_is_produced`; `test_a_failing_notifier_does_not_stop_trading` proves a raising notifier cannot stop trading |
| **Restart restores the open paper position** | `test_restart_restores_the_open_paper_position` — quantity, average price, entry correlation ID, stop and target all match after restart; `test_a_restarted_worker_does_not_reopen_a_position_it_already_holds` proves it does not double up |
| **Duplicate worker startup is refused** | `test_duplicate_worker_startup_is_refused` — two real `spawn` processes; the PID file names the *holder child* (not the test process), the contender exits `EXIT_DUPLICATE`, opens no session, and the holder is unaffected |

#### Corrections made during this phase

Three, all found by tests and fixed in the code or the test as appropriate:

1. **A real bug in `build_correlation_id`.** Digit-counting accepted
   `29-07-2026` — eight digits, but day-first — producing a valid-looking ID for
   the year 2907 that no query would match and no operator would notice. It now
   parses the date with `strptime` and rejects anything that is not a real
   `YYYY-MM-DD`.
2. **A wrong test expectation, not a code bug.** A square-off test set the
   square-off time to the first candle, which fires before any entry can happen —
   the worker correctly checks square-off *before* opening new positions. The
   test was rewritten to square off on a later bar so there is a real position to
   close, and a companion test now covers the entry-cutoff path directly.
3. **A tautological assertion.** The duplicate-worker gate ended with
   `assert lock_file.exists() or True`, which can never fail. It was replaced
   with assertions that actually bind: the PID file names the holder child
   process, the contender has a different PID, the refused worker opened no
   session row, and the holder removes its PID file on clean exit.

---

### Verification results (Phase 2 Block 1, 30 July 2026)

Run with the pin at `dhanhq==2.2.0`:

| Check | Command | Result |
|---|---|---|
| Tests | `.venv/bin/python -m pytest` | **529 passed, 6 skipped** |
| Lint | `.venv/bin/ruff check .` | **All checks passed!** |
| Format | `.venv/bin/ruff format --check .` | **94 files already formatted** |
| Types | `.venv/bin/mypy` | **Success: no issues found in 64 source files** |

The 6 skips are the opt-in live-feed smoke tests, skipped by design unless
`ALGO_LIVE_SMOKE=1` and real credentials are present. Nothing was weakened,
skipped or marked `xfail`. Test distribution: 306 unit, 78 integration, 20
end-to-end, 6 smoke.

`mypy` scope was widened again this phase to include `scripts`, so the two Block 2
entry points are type-checked rather than exempt.

**Phase 1 regression:** all 281 Phase 1 tests pass unchanged on 2.2.0, including
both walking-skeleton acceptance gates and the SDK-isolation test. The pin bump
was the change most likely to break them, so their passing is part of the pin
evidence, not a separate footnote.

#### The credential-free, network-free claim — verified, not asserted

Block 1's central promise is that all of it runs with an empty `.env` and no
network. That was checked by running the whole suite with every Dhan and Telegram
variable unset **and** with `socket.socket` raising on `AF_INET`/`AF_INET6` plus
`getaddrinfo` and `create_connection` raising outright:

```
529 passed, 6 skipped     (three consecutive runs)
```

This is how the flaky spawn test in section 3.1 was found: the harness runs
slightly slower, which was enough to cross the second boundary the test depended
on. A property worth asserting is worth asserting under load.

### Verification results (Phase 3 Part 1, 30 July 2026)

| Check | Command | Result |
|---|---|---|
| Tests | `.venv/bin/python -m pytest` | **546 passed, 6 skipped** |
| Lint | `.venv/bin/ruff check .` | **All checks passed!** |
| Format | `.venv/bin/ruff format --check .` | **97 files already formatted** |
| Types | `.venv/bin/mypy` | **Success: no issues found in 64 source files** |

Test distribution: 408 unit, 112 integration, 26 end-to-end, 6 smoke (skipped by
design). The 13 new tests are 7 cross-thread shutdown tests, 5 real-signal tests
(3 shutdown, 2 operational-visibility), and one supervisor test locking in that an
ordinary end-of-tape run is not misreported as signalled. Nothing was weakened, skipped or marked `xfail`; every pre-existing test
passes unchanged, including both walking-skeleton gates and all of
`tests/integration/test_feed_reconnect.py` — the file whose blind spot this phase
was about.

### Verification results (Phase 3 Part 2a, 31 July 2026)

| Check | Command | Result |
|---|---|---|
| Tests | `.venv/bin/python -m pytest` | **590 passed, 6 skipped** |
| Lint | `.venv/bin/ruff check .` | **All checks passed!** |
| Format | `.venv/bin/ruff format --check .` | **119 files already formatted** |
| Types | `.venv/bin/mypy` | **Success: no issues found in 84 source files** |

Test distribution: 452 unit (was 408), 112 integration, 26 end-to-end, 6 smoke
(skipped by design). The 44 new tests are all unit: 34 ported engine tests and 10
port-specific wiring guards.

**The "existing tests still pass" claim was measured, not assumed.** `HEAD`
(`7e4d1e2`) was checked out into a detached worktree and its suite run with the
same interpreter: **546 passed, 6 skipped**. Working tree: **590 passed, 6
skipped**. The delta is exactly +44 and the skip count is identical, so no
pre-existing test was modified, silenced or newly skipped. Reproduce with:

```bash
git worktree add /tmp/baseline HEAD --detach
cd /tmp/baseline && PYTHONPATH=$PWD /Volumes/Trading/algo_trading/.venv/bin/python -m pytest
git worktree remove /tmp/baseline --force
```

`mypy`'s source-file count rose from 64 to 84 — the 20 ported modules. Note that
bare `mypy .` fails on a pre-existing `dashboards/app.py` dual-module-name error;
`.venv/bin/mypy` with no arguments (which uses the configured `packages` list) is
the project's invocation and is clean.

The new tests are the only concurrency tests in the suite, so they were run **10
consecutive times** on top of the full-suite runs: 10 passes, no flake.

### Verification results (Phase 3 Part 2b-i, 31 July 2026)

| Check | Command | Result |
|---|---|---|
| Tests | `.venv/bin/python -m pytest` | **620 passed, 6 skipped** |
| Lint | `.venv/bin/ruff check .` | **All checks passed!** |
| Format | `.venv/bin/ruff format --check .` | **139 files already formatted** |
| Types | `.venv/bin/mypy` | **Success: no issues found in 100 source files** |

Test distribution: 460 unit (was 452), 134 integration (was 112), 26 end-to-end, 6
smoke (skipped by design). The 30 new tests are 8 unit (both verbatim ports) and 22
integration (12 rebuilt premium-candle, 10 signal-ownership).

**The "existing tests still pass" claim was measured again, not assumed.** `HEAD`
(`05a4a4d`) was checked out into a detached worktree and its suite run with the
same interpreter: **590 passed, 6 skipped**. Working tree: **620 passed, 6
skipped**. The delta is exactly +30 and the skip count is identical, so no
pre-existing test was modified, silenced or newly skipped:

```bash
git worktree add /tmp/baseline-2bi HEAD --detach
cd /tmp/baseline-2bi && PYTHONPATH=$PWD /Volumes/Trading/algo_trading/.venv/bin/python -m pytest
git worktree remove /tmp/baseline-2bi --force
```

`mypy`'s source-file count rose from 84 to 100 — the 16 new modules. The
signal-ownership suite was run **10 consecutive times**: 10 clean, no flake.

Note that `pytest` here must be invoked without an extra `-q`: `addopts = "-q"` is
already set in `pyproject.toml`, so a second one suppresses the summary line
entirely and a run can look like it produced no result at all.

### Verification results (Phase 3 Part 2b-ii-A, 31 July 2026)

| Check | Command | Result |
|---|---|---|
| Tests | `.venv/bin/python -m pytest` | **659 passed, 6 skipped** |
| Lint | `.venv/bin/ruff check .` | **All checks passed!** |
| Format | `.venv/bin/ruff format --check .` | **143 files already formatted** |
| Types | `.venv/bin/mypy` | **Success: no issues found in 101 source files** |

Test distribution: 460 unit (unchanged), 168 integration (was 134), 31 end-to-end
(was 26), 6 smoke (skipped by design). All 39 new tests are integration or
end-to-end, which is where a seam between threads and processes can actually be
observed.

**Baseline measured again, not assumed**, by the same method Part 2b-i used —
`HEAD` (`40038c6`) checked out into a detached worktree and run with the same
interpreter: **620 passed, 6 skipped**. Working tree: **659 passed, 6 skipped**.
The delta is exactly +39 with an identical skip count, so no pre-existing test was
modified, silenced or newly skipped.

```bash
git worktree add /tmp/baseline-2biiA HEAD --detach
cd /tmp/baseline-2biiA && PYTHONPATH=$PWD /Volumes/Trading/algo_trading/.venv/bin/python -m pytest
git worktree remove /tmp/baseline-2biiA --force
```

**Every test crossing a thread or process boundary ran 10 consecutive times** — the
standing rule from Parts 1, 2a and 2b-i — covering the two new suites, the gate,
the cross-thread shutdown suite, the supervisor, the signal suite and the walking
skeleton: `72 passed` on all ten runs, 22.2-22.6 s each. No flake.

**The duplicate-worker race was re-measured, not assumed safe.** Part 2b-i recorded
that re-exporting an engine symbol pushed child import cost 0.382 s → 0.604 s and
lost a 0.5 s race. `common.engine.hub_feed` therefore must not enter the worker's
import graph, and does not:

```
$ python -c "import runtimes.intraday_options.worker; ..."
common.engine modules: []
common.exit modules:   []
median worker import: 0.111s over 5 runs
```

### Verification results (Phase 3 Part 2b-ii-B-2, 1 August 2026)

| Check | Command | Result |
|---|---|---|
| Tests | `python -m pytest` | **815 passed, 6 skipped** |
| Lint | `ruff check .` | **All checks passed!** |
| Format | `ruff format --check .` | **158 files already formatted** |
| Types | `mypy` | **Success: no issues found in 106 source files** |

Test distribution: 523 unit (was 489), 253 integration (was 220), 39 end-to-end (was
31), 6 smoke (skipped by design). End-to-end grew for the first time since Part 1,
which is the point of this part: it is the one that changes the deployed process shape.

**Baseline measured again, not assumed**, by the same method every part since 2b-i has
used — `HEAD` (`faa527f`) checked out into a detached worktree and run with the same
interpreter: **740 passed, 6 skipped**. Working tree: **815 passed, 6 skipped**. The
delta is exactly **+75** with an identical skip count, so no pre-existing test was
silenced or newly skipped. **No existing test was modified this part** — the three
additions to `test_hub_tick_feed.py` and `test_supervisor.py` are appended tests, not
edits to existing ones.

**The duplicate-worker race was the risk this part owned, and it is measured on both
sides of the boundary.** Nine runs after the change:

```
$ python -c "import runtimes.intraday_options.worker; ..."   # nine runs
0.124 engine_modules=0   0.161 engine_modules=0   0.125 engine_modules=0
0.142 engine_modules=0   0.110 engine_modules=0   0.102 engine_modules=0
0.103 engine_modules=0   0.106 engine_modules=0   0.104 engine_modules=0
```

Median **0.110 s**, zero `common.engine` modules — statistically unchanged from B-1's
0.100 s and 2b-ii-A's 0.111 s, *with the engine now wired into the worker*. Both
walking-skeleton gates were then run explicitly: `2 passed in 0.74s`.

**And measured with the boundary deliberately broken**, because a boundary nobody has
seen fail is a boundary nobody has tested. Hoisting one `common.engine` import into
`worker.py`:

```
0.324 engine_modules=17   0.307 engine_modules=17   0.308 engine_modules=17
FAILED test_the_worker_module_imports_no_engine_package_at_module_level
FAILED test_a_clean_interpreter_loads_no_engine_module_for_a_worker
```

3x the import cost and two red tests, from a one-line edit that looks harmless in
review. That is the failure mode `tests/unit/test_worker_import_boundary.py` exists
to make loud.

**The nine threading-adjacent suites ran 10 consecutive times** — the standing rule
from Parts 1, 2a, 2b-i, 2b-ii-A and B-1 — covering both new integration suites, the
new signal gate, `HubTickFeed`, the tick-drop suite, the 2b-ii-A gate, the
cross-thread shutdown suite and both supervisor suites: `111 passed` on all ten,
59.5-60.5 s each. No flake.

**Safety re-confirmed for this part.** `DhanLiveBroker` still does not exist —
`grep -rn "DhanLiveBroker" --include="*.py"` returns four hits across three files, all
prose in docstrings or the refusal message in `common/broker/factory.py`. All 12
broker-factory and all read-only-script tests pass unchanged (`23 passed`). The engine
path reaches the broker only through the **existing** `build_broker(...)` gate, using
the same `resolved_config_stub` the fixture path uses — asserted by
`test_live_mode_is_still_refused_on_the_engine_path`, which drives a full engine
worker in `LIVE` mode and gets zero order intents and zero fills. No `framework.*`
import exists anywhere, so there is still no runtime dependency on the reference tree.

**Reference tree: source and config unchanged.** The recorded baseline re-run before
the commit still returns
`2026-07-28 10:29:14 .../option_strategies/.../tests/test_warmup_coordinator.py` — the
same value recorded at Parts 2a, 2b-ii-A and B-1. No file under `Trading_Automation`
was written by this project, and it was read only through `find`/`stat`.

**One refinement to that check, recorded rather than glossed.** Run across *all*
source+config extensions including `.json`, the newest file is
`common/access_token.json` at `2026-07-31 09:03:40` — and nine files under that tree
have mtimes from today. Every one of them is a **runtime artefact** (`.log`, `.pid`,
`.db`, and that token cache) belonging to the separately-running legacy systems the
"Operational risk" note already describes, which are still live and writing their own
state. None is source or config, and none is anything this repository has code to
write. The baseline check should therefore exclude runtime artefacts explicitly, which
it now does; the earlier "source+config" phrasing would have swept the token cache in
and reported a change that never happened.

### Verification results (Phase 4 Part 1, 1 August 2026)

| Check | Command | Result |
|---|---|---|
| Tests | `python -m pytest` | **896 passed, 8 skipped** (was 815 + 6) |
| Lint | `ruff check .` | **All checks passed!** |
| Format | `ruff format --check .` | **All files formatted** |
| Types | `mypy` | **Success: no issues found in 107 source files** |

`mypy` is run bare — the configured `packages` list. A bare `mypy .` still fails on
the pre-existing `dashboards/app.py` dual-module-name error, unrelated to this part.

### Verification results (Phase 4 Part 2, 1 August 2026)

| Check | Command | Result |
|---|---|---|
| Tests | `python -m pytest` | **984 passed, 8 skipped** (was 896 + 8) |
| Lint | `ruff check .` | **All checks passed!** |
| Format | `ruff format --check .` | **173 files already formatted** |
| Types | `mypy` | **Success: no issues found in 113 source files** |

### Verification results (Phase 4 Part 4, 5 August 2026)

| Check | Command | Result |
|---|---|---|
| Tests | `python -m pytest` | **1131 passed, 10 skipped** (was 1063 + 8) |
| Lint | `ruff check .` | **All checks passed!** |
| Format | `ruff format --check .` | **191 files already formatted** |
| Types | `mypy` | **Success: no issues found in 118 source files** |
| `Trading_Automation` untouched | `find` over the recorded baseline extension set (read-only, absolute path) | Unchanged: **`2026-07-28 10:29:14 .../tests/test_warmup_coordinator.py`**, reproduced against the same command Part 2b-ii-A recorded |

#### Phase 4 Part 4 gate evidence

**Acceptance gate (Part 4) — met, against the task's own stated criteria:**

| Requirement | Evidence |
|---|---|
| An engine worker warm-starts a continuity-required strategy from real historical bars and reaches the same indicator state a from-the-open run would | `test_aggregate_candles_matches_candlebuilder_bucketing` proves warm-up and live candles bucket identically (cross-checked, not assumed from shared use of `floor_to_interval`); `test_warmup_replay_seeds_the_strategy_before_the_first_live_candle` proves the replayed candles genuinely reach `on_candle` before the first live one, counted rather than inferred |
| A fetch failure degrades to a cold start with the existing `WARNING`, never a wrongly-seeded indicator | `test_a_fetch_failure_degrades_to_cold_start_and_blocks_the_first_live_entry` — a real `TradingEngine`, a raising `fetch_fn`, and the live entry that would otherwise fire is refused, not merely a `COLD_START` status computed in isolation |
| `validate_warmup_config`'s existing refusals still hold | `tests/integration/test_engine_premium_candle_exit.py`'s two pre-existing warm-up tests re-run unchanged and still pass — `test_a_continuity_required_strategy_with_warm_up_disabled_is_rejected`, `test_a_strategy_whose_warmup_spec_raises_is_blocked_from_entering` |
| No `dhanhq` import outside the adapter, even with a new historical-data module in the tree | `tests/unit/test_dhan_adapter.py` re-run after adding `common/market_data/dhan_historical.py`: still names only `common/market_data/dhan.py` |
| Every existing configuration's behaviour is unchanged | `EngineWorkerConfig.warmup_source` defaults to `"none"`; both walking-skeleton gates re-run green; `test_warmup_source_none_builds_nothing_and_touches_no_settings` asserts `load_settings` is never even called on the default path |
| The reference's two real defects are fixed, not carried over | `test_fetch_intraday_builds_the_documented_from_and_to_date_format` (fail-first against the bare-date bug); the SDK-isolation structural check above (fail-first against a direct `dhanhq` import) |
| Live still fail-closed | `DhanLiveBroker` still absent; nothing in this part touches the broker or order path — the new REST client speaks only to `/v2/charts/intraday`, a read-only endpoint |
| **Same-part amendment (D47):** a continuity-required strategy with no `warmup_manager` at all must fail at construction, not merely cold-start with a `WARNING` | `test_no_manager_or_source_now_refuses_construction` — raises `InvalidWarmupConfig` naming the missing manager; full suite re-run (`1131 passed, 10 skipped`, unchanged) confirmed no other test in the tree relied on the old fallback |
| **Not claimed:** the real endpoint's partial-candle behaviour during market hours | No captured fixture exists for this endpoint anywhere in this repository. The two new opt-in smoke tests (`tests/smoke/test_live_feed_smoke.py`) narrow this against a real call but cannot settle it without a market-hours run, which this session did not make |
| **Not claimed:** cross-worker rate-limit coordination | `DhanHistoricalDataClient`'s retry is single-process, single-call scoped by design (see D44's discussion and the deviation ledger); the coordinator that would solve simultaneous-multi-strategy-startup collisions is explicitly Phase 5 |

### Verification results (Phase 4 Part 3, 1 August 2026)

| Check | Command | Result |
|---|---|---|
| Tests | `python -m pytest` | **1063 passed, 8 skipped** (was 984 + 8) |
| Lint | `ruff check .` | **All checks passed!** |
| Format | `ruff format --check .` | **179 files already formatted** |
| Types | `mypy` | **Success: no issues found in 113 source files** |

#### Phase 4 Part 3 gate evidence

**Acceptance gate (Part 3) — met in full.**

| Requirement | Evidence |
|---|---|
| No naive datetime reaches a session or square-off decision | `tests/unit/test_session_timezone_rule.py` — refused on all four session predicates, on the authority and on the policy. Fail-closed with the argument named |
| The same instant produces the same decision however it is spelled | The organising assertion, parametrised over IST/UTC/New_York × pre-open/mid-session/after-square-off, for every predicate. **This is the assertion that would have caught the defect on day one** |
| The live defect is fixed, and stays fixed | `test_a_real_utc_tick_mid_session_is_inside_the_session` and `test_the_hub_and_the_engine_agree_about_one_real_shaped_tick`; `test_the_adapter_really_does_produce_utc_ticks` pins the premise so the case cannot silently stop being covered |
| **A feed that dies before the square-off bar still squares off, on real threads** | `tests/integration/test_wall_clock_square_off_threads.py` — a real `run_worker`, a real database, a tape that opens a position and then goes silent for ever. `orders_placed == 2` asserted so the gate cannot pass vacuously on a run that never opened anything |
| The close is real, not just an empty book | The same suite: `order_intents` reads `["BUY", "SELL"]` and two fills, through the audited path |
| It is the net, not the tape running dry | Run ends well inside half the idle timeout; three negative controls (square-off still in the future, trading date not today, restart of a completed day) |
| A restart does not re-close a completed day | Two sequential real runs on one database; the order count is unchanged by the second |
| A gap-spanning bar's fate is asserted as behaviour | `tests/unit/test_candle_continuity.py` — discarded by the hub, emitted-and-marked by the builder, never forward-filled; `tests/integration/test_candle_gap_policy_wiring.py` — not fed to indicators, produces no position, with a clean-stream control |
| The detection rule is the right one | `test_a_silence_that_crosses_a_boundary_but_empties_no_bucket_is_not_a_gap` — the case that made the first implementation wrong, now the boundary that pins it |
| `on_feed_gap` is wired and reaches the hub | `test_a_feed_drop_reaches_the_hubs_aggregators_through_the_supervisors_own_feed`, driven through the **supervisor's own** feed rather than a hand-assembled one. Unwiring it fails 3 tests |
| Wrapping did not break the recorded path | `test_the_wrapper_does_not_break_a_recorded_tape` asserts a clean return with zero reconnect attempts — the property every recorded test depends on |
| Both walking-skeleton gates pass | `29 passed` |
| Worker import boundary re-measured | `7 passed`; **0.133 s median over 7 runs, zero `common.engine` modules** |
| No default test needs credentials or network | Full suite with `DHAN_*`, `TELEGRAM_*`, `ALGO_LIVE_SMOKE` unset and `socket.socket`/`create_connection`/`getaddrinfo` raising: **1063 passed, 8 skipped** |
| Live still fail-closed | `DhanLiveBroker` still absent; no broker or order path touched |
| `Trading_Automation` untouched | Baseline unchanged: **`2026-07-28 10:29:14 .../tests/test_warmup_coordinator.py`** |
| **Not claimed:** limitation 2 | Wiring `ReconnectingFeed` puts Phase 2's backoff and resubscription on the live path for the first time. **None of it has been exercised against a real socket drop**, so limitation 2 stays open exactly as written |

#### Phase 4 Part 2 gate evidence

**Acceptance gate (Part 2) — met, with its ceiling stated rather than papered over.**

| Requirement | Evidence |
|---|---|
| Reference indicator tests pass unmodified | **14 of them, and there are no more** — `tests/unit/test_indicators_ported.py`, 16 collected, imports changed and nothing else. The reference has no dedicated indicator test file; see statement 1 above for why this number is small and what it does *not* let this part claim |
| The pandas-ta cross-check agrees within a stated, justified tolerance | `tests/unit/test_indicator_oracle.py` — five tolerances, each one order of magnitude above a measured figure and each tied to a named structural cause. Table above |
| The tolerance is real, not decorative | Breaking ATR's Wilder alpha to `2/(period+1)` produced `1.166e-01`, four orders above the asserted `1e-5`. Restored immediately |
| The fixture length is part of the assertion | `test_the_fixture_is_long_enough_for_the_tolerances_to_mean_anything` pins `BARS`/`TAIL`; `test_a_short_series_really_does_diverge_more` is the negative control showing the head genuinely exceeds the tail tolerance |
| VWAP declares `SESSION_LOCAL` and `requires_volume` | The ported `test_indicator_declarations`, plus `test_resetting_vwap_starts_a_new_session_cleanly` for the behaviour the declaration exists to protect |
| ADX and ATR have a real consumer; **D21 closed** | `AdxAtrClassifier` registered as `adx_atr`, carrying the reference's 6 classifier tests |
| Closing D21 switched nothing on | `tests/unit/test_regime_classifier_wiring.py` — `regime_enabled` still defaults false, a disabled axis ignores a named classifier, and the default tagger is still `NullClassifier` |
| `pandas-ta-classic` never on the live path | `tests/unit/test_indicator_oracle_boundary.py`, three ways. Verified by adding the import to `regime.py`: 2 tests fail. Restored |
| Both walking-skeleton gates still pass | `29 passed` |
| No default test needs credentials or network | Full suite with `DHAN_*`, `TELEGRAM_*`, `ALGO_LIVE_SMOKE` unset and `socket.socket`/`create_connection`/`getaddrinfo` raising: **984 passed, 8 skipped** |
| Live still fail-closed | Nothing in this part touches the broker, the feed, the engine's trading decisions or the database. `DhanLiveBroker` still absent |
| `Trading_Automation` untouched | Newest source+config mtime unchanged at the baseline: **`2026-07-28 10:29:14 .../tests/test_warmup_coordinator.py`** |
| **Not claimed:** equivalence with Part 2a's port evidence | Part 2a proved ten exit policies against the reference's own regression suite. Three of these five indicators had no such suite and RSI had nothing at all. An agreement test against a second implementation catches transcription errors, not a shared misunderstanding of a formula |

#### Phase 4 Part 1 gate evidence

**Acceptance gate (Part 1) — met, with one item explicitly not yet claimed.**

| Requirement | Evidence |
|---|---|
| The reference's resolver regression test passes with its assertions unmodified | `test_existing_index_option_master_behaviour_is_preserved`, with the reference's own `dhan_scrip_master_sample.csv` copied verbatim. Only the import path and the `ScripMaster` construction differ |
| A real contract resolves to a real `security_id` and the **exchange's** lot size, offline | `test_the_resolver_returns_a_real_security_id_not_a_synthetic_one`; `test_the_lot_size_comes_from_the_exchange_not_from_configuration`; and `test_the_exchange_lot_size_beats_the_configured_one`, which pins that a configured 50 loses to the master's 75 — the half of limitation 17 that silently mis-sizes every position |
| No default test reaches the network for the master | `test_building_the_dhan_resolver_needs_no_network` replaces the fetcher with one that raises; plus the whole-suite run below with sockets blocked |
| A mixed-segment subscription set is proven offline | `tests/unit/test_feed_exchange_segments.py` — an underlying on `IDX_I` and its option on `NSE_FNO` through one adapter, and a reconnect restoring **each to its own**, which is the failure that would otherwise reconnect the feed into silence |
| One instrument cannot be moved between segments | `test_one_instrument_cannot_be_moved_to_a_second_segment` — a contradiction, refused rather than resolved by picking one |
| The segment survives every layer between engine and socket | `ReconnectingFeed` (`test_the_reconnect_wrapper_carries_the_segment_through`, and the negative control that it does not relabel earlier instruments), the hub, and the control queue |
| The control queue's new shape does not break the old one | `test_the_supervisor_still_reads_a_bare_id`, plus ten malformed shapes dropped rather than crashing the group — including `("8103", True)`, which without the `bool` guard would subscribe to segment 1 |
| An engine worker configured `dhan` resolves and fills paper-only, end to end | `tests/unit/test_engine_worker_contract_resolution.py` over the real `build_option_selector`; the full engine-worker integration suite unchanged on the `simulated` default |
| The default is unchanged, so no existing config moves | `test_the_default_is_still_the_simulated_resolver`; the 20 `test_engine_worker.py` tests and both restart/signal gates pass with no behavioural edit |
| Limitation 15 is alarmed on all three channels, once | `tests/unit/test_stuck_subscription_alarm.py` — `CRITICAL`/`component='feed'` row, `subscription_not_applied` notification, forced `DEGRADED` heartbeat; `test_the_alarm_fires_once_however_long_it_persists` |
| Both walking-skeleton gates still pass, with the spawn import cost re-measured | `29 passed`; **0.129 s median over 7 runs, zero `common.engine` modules**. The median is above Part 2b-ii-B-2's 0.110 s on the same machine under different load; the invariant the gate protects — **zero** engine modules — is unchanged, and that is what is asserted |
| No default test needs credentials or network | Full suite re-run with `DHAN_*`, `TELEGRAM_*` and `ALGO_LIVE_SMOKE` unset **and** `socket.socket`/`create_connection`/`getaddrinfo` all raising: **896 passed, 8 skipped** |
| No flake in the threading-adjacent suites | Eight suites (98 tests) run three consecutive times: `98 passed` each, 36.6-36.8 s |
| Live still fail-closed | `DhanLiveBroker` absent; `build_broker` unchanged and all 12 factory tests pass; the new resolver touches no order path and `OptionChainService` gained no caller |
| `Trading_Automation` untouched | Newest source+config mtime re-run against the recorded baseline: **`2026-07-28 10:29:14 .../tests/test_warmup_coordinator.py`** — unchanged |
| **The offline half of the rehearsal has been RUN against the real master** | `ALGO_LIVE_SMOKE=1 pytest -k test_the_scrip_master_resolves_a_real_contract` → **1 passed in 7.70s**, 1 August 2026. Real numbers below |
| **The market-hours half has now been RUN against the real socket** | `ALGO_LIVE_SMOKE=1 pytest -k test_a_real_option_contract_delivers_ticks_on_the_fno_segment` → **1 passed in 2.62s**, 6 August 2026, on the default cache-reusing auth path. The **feed** half of "real contracts work" is proven, not just asserted — a real resolved option contract delivered a live tick on `NSE_FNO` |

**What the real master actually returned** (NIFTY, 1 August 2026):

```
downloaded + parsed   3.92 s, 25.3 MB CSV
exchange lot size     65
expiries listed       18   (first: 2026-08-04, 2026-08-11, 2026-08-18, ...)
nearest expiry        2026-08-04   (chosen by the resolver, unprompted)
strikes for it        225, range 18500-29700
resolved CE           security_id 65697   "NIFTY 04 AUG 24100 CALL"   lot 65
resolved PE           security_id 65698   "NIFTY 04 AUG 24100 PUT"    lot 65
```

**Two things this run established that the fixture tests could not.**

1. **The configured lot size was wrong, and by 30 %.** `EngineWorkerConfig.lot_size`
   defaults to **50**; NIFTY's actual exchange lot is **65**. Every position an
   engine worker sized from configuration was therefore mis-sized, and nothing in
   the repository could have noticed — the synthetic resolver simply echoed the
   configured value back. This is precisely the half of limitation 17 that reads
   as bookkeeping until you see the number.

2. **The strike count independently corroborates Phase 2 Block 2.** That block's
   one live `/optionchain` call returned **225 strikes** for NIFTY expiry
   **2026-08-04** (section 1, Block 2 step 4). The instrument master — a different
   endpoint, a different format, four days later — lists **225 strikes** for the
   same expiry. Two independent sources agreeing is worth more than either alone,
   and it is the closest thing to a correctness check the parser can get without
   market hours.

**A defect in the gate was found by being asked to run this test.** The file's
module-level `pytestmark` required credentials for *every* test in it, so the
scrip-master test — whose own docstring said it needed none, because the master is
a public CSV — could not be run without exporting a token it never uses. Split
into a shared `ALGO_LIVE_SMOKE=1` gate plus a `needs_credentials` mark applied to
the seven tests that authenticate. The default run still skips all eight.

#### Phase 3 Part 2b-ii-B-2 gate evidence

**Acceptance gate (Part 2b-ii-B-2) — met in full.**

| Requirement | Evidence |
|---|---|
| Restart recovery works with the engine wired, not just the fixture strategy | `tests/integration/test_engine_worker_restart.py` — two sequential real `run_worker` runs on one database: the first leaves a position open, the second adopts it from the contract record, does not re-enter, and closes it. Reconciled against the persisted row (`average_price`, `entry_correlation_id`, lots derived from `quantity`) |
| The restarted engine does not double the exposure | `test_the_restarted_engine_does_not_re_enter` (`BUY` intents stay at 1, one `positions` row), with `test_the_second_run_really_did_signal_an_entry` so that assertion cannot pass for the wrong reason |
| Recovery fails **closed** on any inconsistency | `test_an_open_position_with_no_contract_record_blocks_entries_rather_than_trading` and `test_a_stale_contract_record_for_another_instrument_is_refused` — both leave the worker exiting 0 with entries latched off and a `CRITICAL` `errors` row, never a second position. Plus `test_recovery_does_not_reach_across_trading_dates` (spec §12) |
| Adoption places no order | `tests/unit/test_position_manager_adopt.py` — the gateway raises on contact, so a regression fails the test rather than being counted. `test_opening_on_top_of_an_adopted_position_is_still_refused` covers the doubling directly |
| **A real `SIGINT`/`SIGTERM` to a real worker child squares off and exits 0** | `tests/end_to_end/test_engine_worker_signal.py` — a real process running the real engine, signalled only once a position is genuinely open in the database (waited for, not slept on). Both signals: exit 0, `positions_still_open == 0`, `square_off_state == 'COMPLETED'`, `shutdown_reason == 'signal'`, and the closing leg persisted as a real `SELL` intent + fill, not a forgotten position |
| The shutdown is prompt, not merely eventual | The same test asserts `elapsed < 30 s` after the signal; observed ~1 s |
| `SIGINT` does not escape as a traceback | `test_sigint_does_not_escape_as_a_traceback` |
| Both walking-skeleton gates still pass, **with the spawn import cost re-measured** | `2 passed in 0.74s`; nine runs at **0.110 s median, zero `common.engine` modules**, plus the deliberately-broken measurement above showing what the boundary prevents |
| The import boundary is enforced, not described | `tests/unit/test_worker_import_boundary.py` — static (fails on the edit), real-interpreter `sys.modules` (catches a transitive drag), and the positive half (a dead branch cannot satisfy it) |
| Live still fail-closed | `DhanLiveBroker` absent; `test_live_mode_is_still_refused_on_the_engine_path` drives a full engine worker in `LIVE` mode to zero intents and zero fills; broker-factory and read-only-script suites pass unchanged |
| The supervisor delivers both queues (§8 item 5) | `test_an_engine_child_receives_both_queues_and_uses_them` — a real spawned engine child, with `subscriptions_applied` as the proof: it can only ask for a contract by having received ticks on one queue and having had the other to answer on |
| The tick channel gets the shutdown sentinel | `test_the_tick_channel_gets_the_shutdown_sentinel_too` counts it on the wire (`published == ticks_published + 1`); `test_the_shutdown_sentinel_squares_off_the_open_position` proves the consequence |
| Tick drops are reported (§8 item 5) | `test_tick_drops_are_reported_apart_from_candle_drops` — a separate `:ticks` key, absent for a worker with no tick channel |
| D20 reporting bound (§8 item 8) | `test_the_day_summary_reaches_the_strategy_state_payload` (MFE/MAE genuinely non-zero, not a passing zero) and `test_running_and_terminal_heartbeats_are_published` |
| The three-channel alarm for the engine's residual (§8 item 8) | `_raise_silent_engine_alarm`, on the sharper condition "a square-off was requested and a position is still open". The residual it guarded is itself now **fixed** — `test_a_silent_feed_still_honours_a_square_off_request` fails to terminate without the `should_stop` check |
| Strategy-wise broker routing keeps the Phase 1 gate (§8 item 7) | The engine path calls the same `build_broker(resolved_config_stub(config), ...)`; all 12 broker-factory tests pass unchanged |
| `PersistedSquareOffAuthority` has a real caller | `test_a_completed_square_off_is_not_repeated_after_a_restart` through a real worker; `test_the_square_off_time_is_derived_from_the_policy_not_configured_twice` pins that moving the policy moves the engine's session with it |
| Paper only; no `MultiLegEngine`/`FixedStrikeEngine`; no real strategies | None ported. The only `BaseStrategy` in the tree remains a test fixture |
| No migration | `strategy_state.payload` already existed (`0001_walking_skeleton.sql`); `MigrationRunner` unchanged and `schema_migrations` still has one version |

### Verification results (Phase 3 Part 2b-ii-B-1, 31 July 2026)

| Check | Command | Result |
|---|---|---|
| Tests | `python -m pytest` | **740 passed, 6 skipped** |
| Lint | `ruff check .` | **All checks passed!** |
| Format | `ruff format --check .` | **148 files already formatted** |
| Types | `mypy` | **Success: no issues found in 103 source files** |

Test distribution: 489 unit (was 460), 220 integration (was 168), 31 end-to-end
(unchanged), 6 smoke (skipped by design). End-to-end is unchanged on purpose —
B-1 does not touch a process boundary.

**Baseline measured again, not assumed**, by the same method Parts 2b-i and 2b-ii-A
used — `HEAD` (`80ed04a`) checked out into a detached worktree and run with the same
interpreter: **659 passed, 6 skipped**. Working tree: **740 passed, 6 skipped**. The
delta is exactly +81 with an identical skip count, so no pre-existing test was
silenced or newly skipped.

One pre-existing test *was* modified, and it is called out rather than buried:
`test_an_undersized_tick_queue_drops_the_oldest_and_counts_it` asserted
`kept[-1]` was the freshest tick, which the drop notice invalidated by becoming the
newest item on the queue. It was **narrowed, not weakened** — the drop-oldest
property is now asserted over the ticks, exactly the narrowing the candle channel
has always needed for its `None` sentinel — and it gained an assertion that the
notice is present. That failure is also what surfaced the doubled loss rate recorded
in D28.

**The five threading-adjacent suites ran 10 consecutive times** — the standing rule
from Parts 1, 2a, 2b-i and 2b-ii-A — covering both new integration suites, the
2b-ii-A gate, the `HubTickFeed` suite and the tick-channel suite: `86 passed` on all
ten runs, 14.0-14.4 s each. No flake.

**The duplicate-worker race was re-measured, not assumed safe**, because B-1 is the
part immediately before the one that will threaten it:

```
$ python -c "import runtimes.intraday_options.worker; ..."   # nine runs
0.099 engine_modules=0   0.099 engine_modules=0   0.099 engine_modules=0
0.099 engine_modules=0   0.100 engine_modules=0   0.100 engine_modules=0
0.101 engine_modules=0   0.114 engine_modules=0   0.169 engine_modules=0
```

Median **0.100 s**, zero `common.engine` modules — statistically unchanged from
2b-ii-A's 0.111 s. Both walking-skeleton gates were then run explicitly:
`2 passed in 0.85s`.

**Safety re-confirmed for this part.** `DhanLiveBroker` still does not exist —
`grep -rn "DhanLiveBroker" --include="*.py"` returns four hits, all prose in
docstrings or the refusal message in `common/broker/factory.py`. All broker-factory
and read-only-script tests pass unchanged. B-1 added no network call, no endpoint
and no credential handling; `LifecycleGateway` reaches the broker only through
`OrderLifecycle`, which is the same path the walking skeleton already used.

**Reference tree unchanged.** The recorded source+config mtime baseline re-run
before the commit still returns
`2026-07-28 10:29:14 .../tests/test_warmup_coordinator.py` — the same value recorded
at Parts 2a and 2b-ii-A. No file under `Trading_Automation` was written or read for
this part.

#### Phase 3 Part 2b-ii-B-1 gate evidence

| Requirement | Evidence |
|---|---|
| A real persisted `Position` proven against a real exit engine | `test_the_engine_opens_and_closes_through_the_persisted_lifecycle` — real SQLite, real `PaperBroker`, real `OrderLifecycle`, real engine, and the exit decided by the real Part 2a `MOMENTUM_CLOSE` policy |
| The engine's view and the database reconcile | `test_the_position_reconciles_between_the_engine_and_the_database` (status, quantity, instrument, average price, realised P&L, entry correlation ID) and `test_the_engines_prices_and_charges_are_the_persisted_ones` — the latter matters most, because `FillOutcome` is built *from* the fill rows, so agreement is structural rather than asserted twice |
| Nothing bypasses the audited path | `test_both_legs_are_persisted_end_to_end` (2 rows in each of signals/order_intents/orders/fills) and `test_every_persisted_row_is_paper_namespaced` (`p_` correlation IDs, one per leg) |
| Hazard (a): the `signals` UNIQUE constraint | `test_two_legs_on_one_contract_in_one_second_both_persist` — three executions on one contract at one timestamp produce three rows with distinct, **ordered** `candle_end_at` values; plus tests that the bump is per-instrument, does not drift a later timestamp, and keeps the recorded window truthful to the microsecond |
| Hazard (b): a call that did not trade raises | `test_a_suppressed_signal_raises_instead_of_fabricating_a_fill`, `test_a_broker_rejection_raises_rather_than_returning_a_fill` (which is the case `ExecutionResult.traded` gets wrong), and `test_a_suppressed_close_leaves_the_position_open_rather_than_phantom_closed` — the consequence asserted, not argued |
| Square-off survives a restart | `test_a_completed_square_off_is_not_repeated_after_a_restart` against a real repository; `test_an_inherited_in_progress_attempt_is_retried_and_reported` for D29; `test_the_attempt_is_recorded_before_the_close_is_attempted` for the ordering |
| The duplicate decider is gone, not bypassed | `test_the_session_no_longer_answers_the_square_off_question`; the default authority's truth table pinned by a five-case parametrisation so the moved behaviour is recorded rather than trusted |
| The two configured times cannot drift | `test_the_session_times_are_derived_from_the_policy`, `test_a_moved_policy_moves_both_session_boundaries`, and `test_supplying_both_a_session_and_a_policy_is_refused` |
| Limitation 14's entry block | `test_a_real_hub_overflow_blocks_a_real_engines_entries` (nothing hand-published), with `test_the_same_tape_does_enter_without_a_drop` as the control and `test_an_open_position_still_exits_after_a_drop` proving the block is entry-side only |
| The notice reaches the child on every overflow | `test_every_overflow_is_reported_at_every_depth` — 12 cells of depth x run length, each asserted to actually overflow first — plus `test_the_cadence_is_clamped_below_the_queue_depth` for the invariant and `test_the_notice_is_sent_on_a_cadence_not_once_per_drop` so the measured trade-off cannot be reverted silently |
| Both walking-skeleton gates | `2 passed in 0.85s`, with the spawn import cost re-measured at 0.100 s median / zero `common.engine` modules |
| Live still fail-closed | `DhanLiveBroker` absent; broker-factory and read-only-script suites pass unchanged |

#### Phase 3 Part 2b-ii-A gate evidence

| Requirement (runbook §8 Part 2b-ii item 1) | Evidence |
|---|---|
| A tick channel alongside the completed-candle stream | `test_an_opted_in_worker_receives_the_raw_ticks_in_order`; the candle path proven untouched by `test_the_candle_channel_is_unchanged_by_the_tick_channel` (bar-for-bar equality between an opted-in and a plain worker) |
| Opt-in per worker | `test_a_worker_that_did_not_opt_in_has_no_tick_queue` (and `hub.ticks_published == 0`); `test_a_worker_gets_no_tick_channel_unless_it_asks` through a real supervised run |
| Part 1's bounded-queue + drop-oldest-and-count policy reused | `test_an_undersized_tick_queue_drops_the_oldest_and_counts_it`; `test_publishing_ticks_never_blocks_the_feed_callback` |
| **Sized for realistic tick arrival, not candle-rate assumptions** | `test_a_minute_at_the_measured_live_rate_drops_nothing` — four minutes of two instruments at Block 2's measured 4 ticks/s against the real default depth, zero drops; `test_the_default_tick_depth_buffers_more_than_a_minute_of_peak_arrival` pins the stated justification as arithmetic |
| The engine actually runs on it | `tests/integration/test_engine_over_hub.py` — real hub, real bounded queue, real `HubTickFeed`, real `TradingEngine`, real Part 2a exit policy, on the deployed two-thread topology; the contract is chosen mid-session and proven absent from the configured union |
| Supervisor sentinel reaches the engine (item 3, the half that is A's) | `test_the_shutdown_sentinel_asks_the_engine_to_square_off`; `test_ticks_queued_after_the_sentinel_are_not_delivered` |
| Live still fail-closed | Unchanged: the only new adapter call is `subscribe()`, which is read-only. All 12 `tests/unit/test_broker_factory.py` tests pass; no `DhanLiveBroker` order method exists |
| Both walking-skeleton gates still pass | `tests/end_to_end/test_walking_skeleton.py` unchanged and green, 10 consecutive runs; `worker.py` and `FixtureSignalStrategy` untouched |

#### Phase 3 Part 2b-i gate evidence

| Requirement (runbook §8 Part 2b) | Evidence |
|---|---|
| The signal ownership rule is decided, recorded as a deviation, and covered by a test that fails without it | Decided *before* porting; recorded as **D18**; covered by `tests/integration/test_engine_square_off.py`. The fail-first run is in "What Phase 3 Part 2b-i delivered" — against the unfixed port the engine takes delivery of `SIGINT` and its re-raised `KeyboardInterrupt` aborts the whole pytest session |
| The ported engine's own regression tests pass unmodified | The 8 that exist and apply do, verbatim. The list section 8 gave was wrong on three of four entries — corrected and recorded as **D22** rather than worked around |
| Both walking-skeleton gates still pass | Yes, and `worker.py`/`FixtureSignalStrategy` were not touched, so they exercise exactly what they did before. One of them exposed a latent race, fixed at its cause without editing the test — see the finding above |
| Live is still fail-closed | All broker-factory tests pass unchanged; `DhanLiveBroker` still does not exist; no engine code path can construct a live broker |
| Paper mode only; no `MultiLegEngine`/`FixedStrikeEngine`; no real strategies | None ported. The only `BaseStrategy` implementation in the tree is a test-only fixture |
| No runtime dependency on `Trading_Automation` | No `framework.*` import anywhere; the reference repository was read only |
| A real `Position` is proven against a real exit engine | **Partly, and deliberately deferred.** A real `OpenPosition` now drives the real Part 2a `MOMENTUM_CLOSE` policy end to end through the engine. Pairing the *persisted* `common.models.Position` with an exit engine needs the `LifecycleGateway`, which is Part 2b-ii |

#### Phase 3 Part 1 gate evidence

| Requirement (runbook §8 Part 1) | Evidence |
|---|---|
| The new cross-thread test fails on today's code, passes after the fix | Both runs captured in "What Phase 3 Part 1 delivered": `6 failed, 1 passed` pre-fix on the assertion *"the feed thread never returned after a cross-thread stop()"*; all pass after. The signal suite likewise fails pre-fix with the child killed **by** the signal (`-15`, `-2`) |
| `supervisor.py` can start a live feed and stop it cleanly in response to a signal | `tests/end_to_end/test_supervisor_signal.py` — a real `SIGTERM`/`SIGINT` to a real child process over a feed whose `start()` never returns; exit code 0, `stopped_by_signal`, `clean_feed_shutdown`, workers drained |
| A genuine `threading.Thread` cross-thread cycle against a realistically blocking double, not `_ScriptedAdapter` | `tests/integration/test_feed_cross_thread_shutdown.py` — the double blocks in a no-timeout `queue.get()`, and its `stop()` reproduces the SDK's cross-thread branch as a bounded deadlock so a failing run reports rather than hangs |
| Every existing recorded-adapter test still passes unchanged | 546 passed, including both walking-skeleton gates. The only edits to existing test files were **additive**: `request_stop()` on `_ScriptedAdapter`, and the smoke test's illegal cross-thread `stop()` corrected to `request_stop()` |
| Paper mode only; no engine port | No `framework/` file was read or ported; `DhanLiveBroker` still does not exist; all 12 broker-factory tests pass unchanged |
| The residual silent-feed case is **operationally visible**, not just logged (added during review) | `test_a_feed_that_cannot_be_closed_raises_an_alarm_an_operator_would_see` queries the child process's real database and asserts a `DEGRADED` group heartbeat as the **last** state, a `CRITICAL` `errors` row, `shutdown_reason='feed_did_not_stop'`, and a `feed_shutdown_unclean` notification; `test_a_clean_run_publishes_group_health_and_ends_stopped` asserts the clean run ends `STOPPED` and raises **no** alarm |

#### Phase 2 gate evidence

| Requirement | Evidence |
|---|---|
| Auth bootstrap with TOTP, reusing proven logic | `tests/unit/test_auth_login.py` (23 tests), `tests/unit/test_auth_totp.py`; ported from the reference's `dhan_auth.py`, translated to `httpx` |
| **A wrong PIN costs exactly one request** | `test_a_credential_rejection_costs_exactly_one_request`; `test_a_second_invocation_after_a_rejection_makes_zero_requests`; `test_concurrent_processes_produce_one_rejection_between_them` — **four real spawned processes, one attempt in total** |
| No lockout risk from retries | `test_every_auth_error_declares_retryability_explicitly` asserts no permanent error subclasses the retryable one, so `except`-clause order cannot change its meaning |
| Atomic token cache, crash-safe | `test_a_crash_between_write_and_replace_leaves_the_old_cache_intact`; `test_a_failed_write_leaves_no_temporary_file_behind`; `test_the_temporary_file_is_written_in_the_destination_directory` (same-filesystem is a correctness requirement, not tidiness) |
| Correct permissions; never logged or committed | `test_the_cache_file_is_owner_only` (0600); `test_a_rejection_reason_carries_no_credential`; `token_cache*.json` and `data/cache/` both gitignored |
| Redaction covers the new secrets | `test_the_manual_access_token_is_a_known_secret_value`; `test_a_runtime_minted_token_is_masked_once_registered`; `test_the_auth_url_is_redacted_parameter_by_parameter`; `test_query_parameter_order_does_not_change_what_is_masked` |
| `dhanhq` version decision with evidence | Section 4, plus 281 Phase 1 tests passing on the new pin |
| Payload shape ratified, normalisation corrected | `tests/unit/test_dhan_adapter.py` (55 tests) replaying a fixture built from the SDK's own parsers; `test_the_fixture_carries_the_sdk_shape_not_a_guess` fails loudly if a future SDK changes it |
| Reconnect and resubscribe without duplicates | `test_the_full_subscription_set_is_resent_after_a_reconnect`; `test_resubscription_does_not_duplicate_instruments` |
| **No corrupt or duplicate bar across the gap** | `test_a_gap_within_one_interval_discards_that_bar`; `test_a_gap_spanning_several_intervals_publishes_nothing_for_them`; `test_no_duplicate_bar_is_published_for_the_interval_that_spanned_the_gap` |
| Option-chain 3s throttle holds under burst | `test_the_throttle_holds_three_seconds_per_key`; `test_a_burst_for_one_key_collapses_to_a_single_call`; `test_a_concurrent_burst_from_real_threads_collapses_to_a_single_call` (8 real threads → 1 call) |
| Throttle is a data-call limiter only | `test_the_service_exposes_no_order_capability`; `test_the_module_never_imports_a_broker` |
| **Live stays fail-closed** | All 12 `tests/unit/test_broker_factory.py` tests pass unchanged; `build_broker()` still refuses every live configuration, and no `DhanLiveBroker` order method exists |
| Scripts are read-only | `tests/unit/test_scripts_are_read_only.py` — no broker import, no `/orders` reference, no `put`/`delete`/`patch` call, and `--status` proven offline by making the socket layer raise |

### Verification results (Phase 5, 6 August 2026)

| Property | Evidence |
|---|---|
| `load_resolved_config` has a real, non-test caller | `runtimes.intraday_options.__main__.build_supervisor` and `.main`; `discover_enabled_strategies` calls it once per enabled strategy file |
| Discovery is deterministic and filters correctly | `test_discovery_returns_only_enabled_strategies`, `test_discovery_resolves_every_enabled_strategy_against_the_given_runtime` (sorted filename order), `test_discovery_returns_empty_when_no_strategies_directory_exists`, `test_discovery_propagates_a_broken_strategy_file` (`tests/unit/test_config_loader.py`) |
| The adapter maps required/optional parameters and risk times correctly, and refuses a malformed config | 11 tests, `tests/unit/test_config_adapter.py`, including `test_engine_is_always_none_regardless_of_strategy_engine_kind` (the Phase 9 boundary) |
| A blocked live strategy never stops the group | `test_a_live_mode_worker_is_refused_and_never_spawned`, `test_a_blocked_live_worker_does_not_stop_the_paper_strategy` (`tests/end_to_end/test_supervisor.py`) — the paper strategy's exit code is 0, the live strategy never appears in `worker_exit_codes` |
| **Mode separation holds against real persisted state, keyed on `strategy_id` not `execution_mode`** | `test_mode_separation_survives_a_mixed_paper_and_live_run` (`tests/end_to_end/test_mode_separation.py`) — schema-swept across all 9 `execution_mode`-bearing tables plus `runtime_heartbeats`, plus `paper_fill_quotes` via an explicit join; positive control on the paper strategy's rows runs first |
| **Duplicate-worker prevention already covers mode, proven not merely argued** | `test_worker_lock_identity_has_no_room_for_mode`, `test_two_worker_locks_for_the_same_strategy_id_collide_regardless_of_caller_intent` (`tests/unit/test_process_locks.py`); `test_a_live_mode_contender_is_still_refused_as_a_duplicate` (`tests/end_to_end/test_walking_skeleton.py`, real spawned processes) — the live-mode contender is refused by the lock before it ever reaches the live gate |
| `test_duplicate_worker_startup_is_refused` is unchanged by this phase | Same assertions, same pass, confirming the Phase 1 proof needed no modification — see the Part 1 gate discussion above |
| No regressions anywhere else | Full suite: **all tests pass**, no new skips beyond the pre-existing opt-in live/smoke gates |
| `ruff check .` | Clean |
| `mypy` (`common runtimes strategies dashboards scripts`, strict) | Clean, 119 source files |

## 4. Package decisions

### `dhanhq` — pinned `2.2.0`, **ratified** (Phase 2, 30 July 2026)

- **Pinned exactly**, never a range, per project rules.
- The spike the spec requires (section 5, line 1443) was run by inspecting both
  releases' source and querying PyPI. `2.1.0` was rejected on three independent
  grounds, each verified rather than assumed:

| Finding | How it was verified |
|---|---|
| `2.1.0` is **yanked** on PyPI, publisher reason **"Breaking changes"** | `pip index versions dhanhq` omits it; `pip download 'dhanhq==2.1.0'` warns and prints the reason. It still installs *because* we pin it exactly — a yanked version is only skipped by range resolution, so the pin was hiding the withdrawal. |
| `2.1.0` **cannot resubscribe** on this machine | `marketfeed.py:461,498` test `self.ws.closed`; installed `websockets 16.1.1` `ClientConnection` has no such attribute (`hasattr(...) is False`). Resubscription after reconnect is a Phase 2 deliverable, so this was a hard blocker, not a nicety. |
| `2.1.0`'s `disconnect()` **never closes the socket** | `marketfeed.py:88` sends a disconnect frame and returns without `ws.close()`; 2.2.0 adds `await self.ws.close()`. It is also `async`, so Phase 1's `stop()` built an un-awaited coroutine and closed nothing at all. |

- **What 2.2.0 changes that matters here:** guards the `ws.closed` removal via
  `_is_ws_closed()`; actually closes the socket; replaces the deprecated
  `asyncio.get_event_loop()` with `new_event_loop()`; adds callback hooks; adds a
  `DhanLogin` class for TOTP token generation and `GET /v2/profile` validation.
- **What it does not change:** `process_ticker`, `process_quote` and `utc_time`
  are **byte-identical** between the two releases (verified by `diff`). The
  payload shape — and therefore all normalisation work — is unaffected by the
  bump. This is why the pin decision and the shape ratification are independent.
- **Not adopted from 2.2.0:** `DhanLogin` (deviation D14) and the built-in
  `run()`/`start()` reconnect loop (deviation D15).
- **`websockets>=14` is now pinned as a floor** rather than left to the SDK's
  resolution, so a fresh install cannot land on a pre-14 release where 2.2.0's
  guard is moot and the old attribute silently reappears.
- **Regression evidence:** all 281 Phase 1 tests pass unchanged on 2.2.0,
  including both walking-skeleton acceptance gates and the SDK-isolation test.

### Ratified after Block 2 (30 July 2026)

Honest scope of what was actually established, and what still hasn't been:

- **Ratified from source:** the `get_data()` payload *shape*, because
  `process_data()` constructs the dict itself from the binary frame — the wire
  contributes bytes, not keys.
- **Ratified live in Block 2:** authentication (`generateAccessToken` +
  `GET /v2/profile`), the WebSocket accepting our subscription packet
  (122 real frames captured), a real market-data REST call (`/marketfeed/ltp`),
  a real option-chain call, and that live `LTP`/`LTT` *values* match what the
  source implied — no divergence found.
- **Still NOT ratified against a real connection:** reconnection behaviour
  against an actual disconnect (backoff, resubscription and gap-handling are
  tested only against a scripted double — limitation 2), and whether a 807
  frame arrives before or after the socket closes in practice.

### Everything else

All spec section 17 default dependencies were declared and locked in Phase 0,
even though Phase 0 imports only a few. This was to surface any macOS install
problem at the cheapest moment. Result: **no install problems.**
`pandas-ta-classic` (the package most likely to be difficult) resolved to
`0.6.52` and installed without a build step.

`py_vollib` is in an optional `[greeks]` group and is **not installed**.

### Lockfile format

`requirements.lock`, not `uv.lock` — `uv` is not installed on this Mac, and the
spec permits either. Regenerate with the command in the file header.

---

## 5. Commands

```bash
# Setup
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install -e . --no-deps
cp .env.example .env                            # fill in locally

# Checks
.venv/bin/python -m pytest
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy
```

### Start / stop / recovery

```bash
# Run the walking skeleton against a recorded tape (no credentials, no network):
.venv/bin/python -m pytest tests/end_to_end -v

# Read-only dashboard (one tile):
.venv/bin/streamlit run dashboards/app.py

# Opt-in live feed smoke test — market hours, real credentials, READ-ONLY.
# Places no order. Skipped by default.
ALGO_LIVE_SMOKE=1 .venv/bin/python -m pytest tests/smoke -v
```

#### Stopping a running supervisor (Phase 3 Part 1)

`SIGTERM` and `SIGINT` both trigger an orderly shutdown: the feed is asked to
finish, the supervisor waits for its thread to return, partial bars are flushed,
workers are drained on their sentinel, and the runtime lock is released.

```bash
kill -TERM <supervisor-pid>     # or Ctrl-C in the foreground
```

Expect `received SIGTERM; beginning orderly shutdown` in the log, followed by the
workers exiting. **`kill -9` is not the way to stop it** — it skips all of the
above and leaves the lock and PID files for the next start to clean up.

If the log instead shows *"the feed did not finish within 10.0s of being asked to
stop"*, the socket was connected but silent (limitation 13). Everything else still
shut down; the connection is released by process exit. Outside market hours that
is the expected message, not a fault.

#### Running a worker on the ported engine (Phase 3 Part 2b-ii-B-2)

A worker drives the Phase 1 fixture strategy unless `WorkerConfig.engine` is set, in
which case it drives `TradingEngine` off the hub's tick channel. The engine worker
**must** be registered with `tick_channel=True` — without a tick queue it refuses to
start rather than falling back:

```python
supervisor.add_worker(
    WorkerConfig(
        ...,
        square_off_policy=SquareOffPolicy(),  # owns BOTH square-off times
        engine=EngineWorkerConfig(
            strategy_ref="package.module:ClassName",  # dotted, not a registry name (D30)
            strategy_kwargs={...},
            timeframe="5m",
            lot_size=65,
            strike_step=50,
        ),
    ),
    tick_channel=True,  # required: creates the tick and control queues
)
```

The session's entry cutoff and square-off time are **derived** from
`square_off_policy` and must not be configured separately — that is what
`SessionConfig.from_square_off_policy` exists to prevent (their defaults had already
drifted fifteen minutes apart before Part 2b-ii-B-1).

Stopping is the same `SIGTERM`/`SIGINT` as the supervisor, and works whether the
signal goes to the supervisor (which sentinels both queues) or directly to the worker.
Either way the engine squares off on the thread that owns its feed, and the closing
order goes through the normal audited path.

**What to check after an engine worker stops:**

| Symptom | Meaning |
|---|---|
| `errors` row, `component='engine'`, *"positions are still OPEN"* | A square-off was asked for and the book is **not** flat. The health tile stays `DEGRADED`. Close the positions manually |
| `errors` row, `component='engine.recovery'` | An open position was found whose contract could not be rebuilt. The worker ran with entries blocked and did **not** manage that position |
| Health `BLOCK_NEW_ENTRIES` during the session | Either the entry cutoff passed, or a tick was dropped upstream and this worker latched entries off for the day (limitation 14). `dropped_events["<strategy>:ticks"]` distinguishes them |

### Authentication (Phase 2)

Credentials come from `.env` only. Nothing below prints, logs or echoes a secret.

```bash
# Local state only — makes NO network call. Safe at any hour.
.venv/bin/python -m scripts.auth_bootstrap --status

# Pre-market bootstrap: TOTP -> token -> validate via GET /v2/profile -> atomic
# cache. Run once before the open; runtimes then read the cache.
.venv/bin/python -m scripts.auth_bootstrap

# After fixing DHAN_PIN or DHAN_TOTP_SECRET in .env: clears the rejection
# cooldown so the corrected credential does not have to wait out the timer.
.venv/bin/python -m scripts.auth_bootstrap --force

# Replace a token Dhan has stopped honouring (disconnect reason code 807).
.venv/bin/python -m scripts.auth_bootstrap --refresh
```

**If authentication is refused.** A wrong PIN or TOTP costs **exactly one**
request to Dhan and then records a cooldown, so re-running the command — or
starting eight workers — makes no further attempts until it is cleared. Read the
exit code: `2` = no credentials in `.env`, `3` = suppressed by the cooldown
(nothing was sent to Dhan), `1` = the attempt failed. Fix `.env`, then `--force`.

A rejected TOTP is often a drifted local clock rather than a wrong secret; the
error message says so and how to check, because the two produce identical
responses from Dhan.

### Operator commands (Phase 7 Part 4)

`scripts/authenticate` is a pure alias for `scripts/auth_bootstrap` above — either
name works. Every command below writes at most one row, to `audit_events`; none
opens a second writer against `positions`/`orders`/`fills`.

```bash
# Read-only status/preflight — safe at any hour, no network call.
.venv/bin/python -m scripts.status --runtime-id intraday_options [--json]
.venv/bin/python -m scripts.validate_environment

# Start (thin wrappers over `python -m runtimes.intraday_options`):
.venv/bin/python -m scripts.start_runtime intraday_options
.venv/bin/python -m scripts.start_strategy io_supertrend_fast_v1   # still through a supervisor

# Stop — reads the PID file, verifies the recorded process is the one that
# actually wrote it (create_time, not liveness alone — D76), sends SIGTERM.
# Refuses, and signals nothing, if ownership does not verify.
.venv/bin/python -m scripts.stop_strategy --strategy-id io_vwap_straddle_v1
.venv/bin/python -m scripts.stop_runtime --runtime-id intraday_options

# Square off one strategy. Never touches `positions` directly: writes a request
# file the running worker itself polls and closes through its own square-off
# path. --confirm is mandatory.
.venv/bin/python -m scripts.square_off --strategy-id io_supertrend_fast_v1 --confirm
```

**The PID-reuse drill** (the plan's own end-to-end verification item, run by hand
against the real script, not only its unit tests):

```bash
# 1. Start a throwaway process to stand in for a reused PID.
sleep 300 &
SLEEPER_PID=$!

# 2. Point a runtime's PID file at it (the shape `stop_runtime` reads).
python3 -c "
import json, psutil, pathlib
pid = $SLEEPER_PID
path = pathlib.Path('data/runtime/pid/intraday_options.supervisor.pid')
path.write_text(json.dumps({
    'pid': pid, 'identity': 'intraday_options.supervisor', 'command': 'unrelated',
    'acquired_at': '2026-08-07T09:00:00+00:00', 'create_time': 0.0,  # wrong on purpose
}))
"

# 3. It must refuse — and say why — and the sleeper must still be running.
.venv/bin/python -m scripts.stop_runtime --runtime-id intraday_options
kill -0 "$SLEEPER_PID" && echo "sleeper still running, as required"
kill "$SLEEPER_PID"
```

### Block 2 — live read-only ratification (completed 30 July 2026)

Run **only** with `.env` populated, during market hours. Every call is read-only
and none can place, modify or cancel an order. The commands below are what was
actually run to close out Block 2:

```bash
.venv/bin/python -m scripts.auth_bootstrap                    # 1. auth + validate
# 2. one read-only /marketfeed/ltp call — via the SDK's own REST client
#    (dhanhq._market_feed.MarketFeed(dhan_context).ticker_data(...)), a one-off
#    verification rather than a checked-in script; see runbook history.
.venv/bin/python -m scripts.capture_live_tape --seconds 30    # 3. record a tape
# 4. one /optionchain call — via common.market_data.option_chain.OptionChainService
#    wrapping dhanhq._option_chain.OptionChain, likewise a one-off verification.
.venv/bin/python -m pytest                                    # 5. full suite vs real fixture

# Repeatable regression path for future live checks (not what ran this session):
ALGO_LIVE_SMOKE=1 ALGO_SMOKE_EXPIRY=YYYY-MM-DD \
  .venv/bin/python -m pytest tests/smoke -v
```

**The Phase 4 Part 5 gate item — closed, 6 August 2026.** Depth is only real on a
Full-mode subscription to a real `NSE_FNO` option, so it could only be confirmed
with the socket, during market hours (09:15–15:30 IST). It has been: the command
below ran live and passed, including the exchange-time trip-wire added closing
limitation 20. Kept here as the repeatable command for any future re-verification:

```bash
# The gate assertion: a real option in mode 21 delivers a two-sided book.
ALGO_LIVE_SMOKE=1 .venv/bin/python -m pytest tests/smoke \
  -k full_mode_delivers_a_two_sided_book -v

# Optional, to keep the frames for later inspection. --segment 2 and a real
# NSE_FNO security id are both required: an index carries no book in any mode.
.venv/bin/python -m scripts.capture_live_tape \
  --mode full --segment 2 --security-id <NSE_FNO id> --seconds 30 \
  --out tests/fixtures/dhan_full_payloads_real.json
```

Read the printed `ticks_with_depth` and `ticks_one_sided_book` counters. Zero
ticks with a rising `non_tick_frames` would mean `"Full Data"` is not being
recognised at all — the failure mode described under Part 5's finding 1.

**Recovery.** A worker recovers automatically on restart: it acquires its lock,
runs integrity checks, closes the previous incomplete session, adopts any open
paper position and resumes. There is no manual recovery command, by design — an
operator-driven recovery step is a step that gets skipped at 09:15.

**Stopping.** Publish `None` to a worker's queue (the supervisor does this
during shutdown), or terminate the process; the lock is released by the OS
either way, so a killed worker does not block its own restart.

A supervised launch entry point (`orchestration/scripts/`) and LaunchAgents
arrive in Phase 7 and Phase 8. Phase 1 is driven from tests and the supervisor
class, which is deliberate: the spec requires LaunchAgents only after manual
start/stop/crash/restart tests pass.

---

## 6. Known limitations

1. ~~**`supervisor.py` has no live-feed shutdown path at all, and
   `ReconnectingFeed` shares an untested cross-thread race.**~~ **FIXED in
   Phase 3 Part 1** (30 July 2026). The diagnosis is kept below because it is the
   evidence for the fix's design, and because the failure mode it describes is one
   any future adapter can reintroduce.

   - **The original hang, and the Block 2 fix.** The capture script ran the feed's
     blocking `start()` loop on a background thread while the main thread waited
     out a deadline and then called `adapter.stop()` from *outside* that thread.
     The SDK's WebSocket close handshake raced a `get_data()` call still in flight
     on the same `asyncio` event loop from a different thread — loops are not safe
     to drive from two threads at once — and the process hung on a live
     connection, confirmed by a real capture run. Fixed then, in that one file
     only, by moving both the stop decision and the `adapter.stop()` call into the
     callback the feed's own loop invokes.
   - **`supervisor.py` had no shutdown path at all**, not merely an unsafe one.
     `run()` called `self._hub.start()` then `self._hub.stop()` sequentially on
     one thread, which was safe only because every caller passed the **recorded**
     adapter, whose `start()` returns once the tape is exhausted. A live
     `DhanMarketFeedAdapter.start()` loops `while self._running:` and never
     returns by itself, and nothing installed a SIGTERM handler anywhere in the
     repository. Pointed at a live feed it would have hung at startup, recoverable
     only by an external kill.
   - **`ReconnectingFeed` carried the identical latent race**, delegating straight
     to `self._adapter.start()` / `.stop()` with no thread-safety. Untested
     because every test in `tests/integration/test_feed_reconnect.py` drives it
     with `_ScriptedAdapter`, whose `start()` returns synchronously and quickly —
     so no test had ever called `.stop()` from a different thread than `.start()`.
   - **What now holds.** The ownership rule is part of the `MarketFeedAdapter`
     contract, `request_stop()` is its thread-safe half, the supervisor handles
     `SIGTERM`/`SIGINT` and shuts down in order, and both properties are covered
     by tests that were demonstrated failing against the pre-fix code — a real
     cross-thread start/stop cycle against a blocking double, and a real signal to
     a real process. See "What Phase 3 Part 1 delivered" in section 1.
   - **What is still open:** the residual silent-feed case, now limitation 13, and
     limitation 2 below — none of this was exercised against a real socket drop.
     Both remain required reading before Phase 10.
2. **Reconnection is tested against a scripted double, not a real socket drop.**
   The backoff, resubscription and gap-handling logic is covered, but Dhan's
   actual disconnect behaviour — including whether a 807 frame arrives before or
   after the socket closes — has not been observed.
3. **The 807 → token-refresh path depends on an instance-level override.** The
   SDK prints the disconnect reason and returns `None`, so the code is recovered
   by wrapping `server_disconnection` on our own feed object. It is covered by a
   test, but it reads SDK internals and would need revisiting on an SDK upgrade.
4. **~~A candle spanning a feed gap is discarded, not repaired.~~ CLOSED**
   (Phase 4 Part 3). Discard is now the **policy** rather than the conservative
   floor pending one: holes are left absent and **nothing is ever forward-filled**,
   because a forward-filled bar is a fabricated print that every indicator
   downstream would consume as real.

   Two things had to change for that policy to mean anything. The hub's
   `mark_feed_gap` was **never called in the deployed runtime** — `ReconnectingFeed`
   had no constructor call outside tests, so `on_feed_gap` was never supplied. It is
   wired now. And `CandleBuilder`, which the engine uses for its own bars (**D23**),
   had no gap concept at all, so a twenty-minute hole produced one wide unmarked bar;
   it now marks `Candle.spans_gap`, and the engine declines to feed such a bar to
   indicators or trade on it (**D41**, **D42**).

   **What an indicator does with a hole** is answered using the scope Part 2 made
   real: `SESSION_LOCAL` indicators (VWAP) are reset because a session-cumulative
   value never recovers missing volume; `SESSION_SPANNING` ones are left alone
   because they are exponentially forgetting and self-correct. See
   `common.indicators.reset_session_local`.

5. **~~The paper fill model is minimal.~~ CLOSED** (Phase 4 Part 5, deviation D11
   closed with it). It had no bid/ask spread cost, which is the specific reason
   Phase 1 paper P&L was not a credible estimate of live P&L: a round trip on an
   unchanged book cost exactly zero, so every strategy was flattered by the full
   width of the book, twice per trade. Depth now reaches the fill through
   `QuoteBook`, a buy takes the ask and a sell the bid, the price is rounded
   adversely onto the tick grid, and the last-price fallback costs the conservative
   extra the spec always attached to it. Limit orders, partial fills and the nine
   rejection rules arrived with it.

   **Three residuals, each recorded as a deviation rather than folded away.**
   Simulated latency *selects* a quote rather than waiting for one, so on a live
   feed a market order still fills at its submission quote — per-fill observable
   via `Fill.latency_applied` (**D48**). A partial fill is refused at the gateway
   rather than sized into the engine's book (**D51**). And `max_quote_age_ms`
   defaults to off, so a live configuration must set it (**D53**).

   **The one gate item is closed.** The opt-in Full-mode capture against a real
   `NSE_FNO` option ran live on 6 August 2026 and passed clean, including the
   exchange-time trip-wire added closing limitation 20. See "What is asserted
   rather than proven" under Part 5. **Limitation 5 and deviation D11 are fully
   closed; nothing about the fill model remains asserted-not-proven.**
6. **Migration atomicity is by replay, not transactions** (deviation D6).
7. **~~Square-off is driven by the candle clock, not a wall clock.~~ CLOSED**
   (Phase 4 Part 3). The candle clock is still the primary trigger and still the
   right one — it is deterministic and a replay reaches the same decision. What it
   could not survive is the feed stopping *before* the square-off bar, which left a
   position open overnight with nothing to notice.

   A wall-clock net now runs on `HubTickFeed`'s poll loop — the only thing in a
   worker that runs on a timer — and asks the **same** `SquareOffAuthority`. One
   owner is preserved: the net supplies a clock reading, the authority decides, and
   the close goes through the existing **D18** request path, so there is no second
   square-off code path. `PersistedSquareOffAuthority` already refuses a `COMPLETED`
   day, so a restart cannot re-close and no new state was needed.

   **The wall clock speaks only for its own day.** `trigger_at` is a time-of-day
   decision with no notion of *which* day, so a worker replaying a historical tape
   would otherwise square off before its first tick — which is exactly what happened
   on first implementation, in 25 tests. Off its trading date the net stays silent
   and the candle clock remains the only decider.

   Proven on real threads with a real database:
   `tests/integration/test_wall_clock_square_off_threads.py`.

8. **One instrument, one runtime group, one strategy shape.** Multi-strategy and
   mixed-mode supervision are Phase 5.
9. **Pattern-based log redaction remains heuristic.** It masks `key=value` shapes
    with sensitive-looking keys and every literal value from `.env`, and Phase 2
    closed two real gaps in it (section 3.1) — both confirmed holding against real
    auth-log output in Block 2. But a secret arriving from a third party as a bare
    token with no key beside it can only be caught by literal redaction, which
    requires knowing the value. The bootstrap therefore registers each minted
    token with the redactor the moment it exists — before anything can log it —
    which closes the case we control.
10. **The rejection cooldown fails *open* on a corrupt file.** A malformed
    cooldown record is treated as absent, so a damaged file cannot permanently
    lock out authentication. That is deliberate: the protection it offers is
    bounded, whereas an unclearable block would be an outage. The
    one-attempt-per-invocation rule holds independently of it.
11. **Dhan's server-side lockout threshold is unknown.** It is not documented in
    the SDK source or the reference implementation, which treats lockout as a risk
    to avoid rather than a published budget. The design therefore minimises
    attempts rather than relying on a safe number — one request per rejection,
    zero during the cooldown — and no figure is quoted here because none has been
    verified.
12. **~~No LaunchAgents, no supervised launch entry point, no reconciliation.~~
    PARTIALLY CLOSED** (Phase 8). `orchestration/launchd/` has three
    generated, validated `.plist` files and `orchestration/process_control/
    supervised_launch.py` is the bounded-restart entry point every one of
    them points at. **Still open, deliberately**: no plist is loaded
    (Phase 9's job — see the Phase 8 writeup's own decisions), and
    reconciliation remains Phase 10's, unchanged.
13. **A live feed can go silent and unclosable, and nothing will force it shut.**
    Introduced by the Phase 3 Part 1 design, deliberately: the alternative is a
    shutdown path that can itself hang. A connected feed delivering *no frames*
    leaves its owning thread blocked in `recv()` with **no boundary at which to
    notice a stop request**, and the only mechanism that could interrupt it from
    outside is the cross-thread close that hangs. The supervisor waits
    `DEFAULT_SHUTDOWN_GRACE` (10 s) and then gives up on the connection. **There is
    no automatic escalation — no forced close, no retry, no kill.** The socket is
    left for process exit to reclaim; the feed thread is a daemon, so the process
    does still exit (proven by
    `test_a_feed_that_cannot_be_closed_raises_an_alarm_an_operator_would_see`,
    which asserts a clean exit code from a real process whose feed thread never
    returned).

    **When to expect it.** During market hours frames arrive continuously, so a
    stop normally lands within milliseconds. The grace period is actually consumed
    in two situations: out of hours, which is benign — and **a dead-but-open socket
    during the session, which is an incident**, because it means the runtime has
    been receiving nothing while believing itself connected. Those two look
    identical at the shutdown boundary, so this condition is always reported at
    `CRITICAL` and never downgraded by the time of day.

    **How you find out** — three channels, because a log line is not an alarm and
    nobody is tailing the file at 15:31:

    | Channel | What appears | Where |
    |---|---|---|
    | Dashboard health tile | group heartbeat goes `DEGRADED`, and **stays** `DEGRADED` — the run deliberately does not overwrite it with `STOPPED` on the way out, so the alarm survives the tidy exit that follows | `runtime_heartbeats` where `strategy_id IS NULL` |
    | Dashboard error text | a `CRITICAL` row, `component='feed'`, naming the grace period and saying the connection was not closed | `errors` |
    | Notification | `feed_shutdown_unclean` via the configured notifier (Telegram when one is wired), wrapped in `SafeNotifier` so a failed send cannot disturb the shutdown | `common/notifications/` |

    The run also ends with `SupervisorResult.clean_feed_shutdown=False` and
    `runtime_sessions.shutdown_reason='feed_did_not_stop'`, so a later reader can
    tell which runs ended this way without reconstructing it from logs.

    Both halves are asserted as behaviour, not left to comments:
    `test_a_silent_feed_cannot_be_closed_from_another_thread` for the refusal to
    escalate, and the two real-process tests above for the alarm and for the
    clean-run case that must **not** raise one. Revisit only with evidence about
    how the SDK behaves on a socket that has stopped delivering — which needs
    limitation 2 closed first.

    **The engine-level half of this is closed as of Part 2b-ii-B-2.** A worker's
    engine used to face the same shape one layer in: a request set a flag that only
    `on_tick` could act on, so with a silent feed and `idle_timeout_seconds=None` —
    which is what a live session runs — the wait was unbounded. `HubTickFeed` now
    asks a `should_stop` predicate on every poll wake, so the request is honoured
    within one poll interval and control reaches `TradingEngine.run()`'s `finally`,
    the second square-off boundary **D18** already names. Proven by
    `test_a_silent_feed_still_honours_a_square_off_request`, which fails to terminate
    without it. **The supervisor's half above is unchanged** and still open: that one
    is a blocked `recv()` inside the SDK, which this repository has no boundary
    inside. What remains for the engine is only the outcome check — if a square-off
    was requested and a position is still open when the run ends, the same three
    channels fire with `component='engine'`.
14. **A dropped tick silently corrupts one worker's bars, rather than removing one
    visibly.** Introduced by the Part 2b-ii-A tick channel, deliberately (**D23**).
    D9's guarantee is that every worker sees byte-identical bars because there is
    exactly one aggregator per instrument; a worker driving the ported engine off
    raw ticks builds its own, so under queue overflow its OHLC quietly differs from
    the hub's for that interval. This is *worse in kind* than a candle-channel drop,
    which loses a whole bar and is obvious.

    **Why it is accepted:** the alternative needs a candle→candle aggregator (the
    hub runs at 60 s, the engine wants `cfg.timeframe`) plus an engine entry point
    bypassing `_on_underlying_tick`, which the ported session-gating test pins
    attribute by attribute. It also moves toward spec section 6 — distributing
    normalised ticks is what the spec describes, and D9 was always the deviation.

    **What bounds it:** the depth is sized from a measured rate rather than a guess
    (`DEFAULT_TICK_MAX_DEPTH = 2048`, ~60-70 s of buffer at a generous peak, with
    `test_a_minute_at_the_measured_live_rate_drops_nothing` asserting zero drops at
    Block 2's observed 4 ticks/s), and every drop is counted in `queue_stats()` and
    logged at `WARNING`.

    **The entry block is closed as of Part 2b-ii-B-1.** A `TickDropNotice` now
    travels down the tick queue itself (**D28**) — the only parent→child channel
    there is, and the one the shutdown sentinel already uses — and the engine
    latches `_block_entries` for the rest of the day on receipt. Entry side only, so
    an open position keeps its exits and its square-off. Proven end to end by
    `tests/integration/test_tick_drop_blocks_entries.py`, including a real hub
    overflow, the same tape entering normally as a control, and an open position
    still exiting after a drop.

    Until 2b-ii-B-1 `hub.py`'s own docstring asserted this block as fact while no
    code performed it — `_block_entries` had exactly two callers, both in `_warm_up`.
    Worth recording as a defect in its own right: a claim in a module docstring is
    not a mitigation, and this one survived a review because it was written in the
    same change as the counting that *was* real.

    **Still open:** the block fires in the child that suffered the drop; a second
    worker holding the same instrument on its own queue is unaffected, which is
    correct (its bars are fine) but means "a drop occurred" is not a group-level
    alarm. And the notice itself costs a queue slot while overflowing — see D28.
15. **A runtime subscription needs a tick to be applied.** The hub applies pending
    subscriptions at the top of `on_tick`, on the thread that owns the connection
    (**D24**), so a feed delivering nothing never applies one — the same shape as
    limitation 13, and bounded by the same reasoning: during market hours frames
    arrive continuously, and `start()` drains once before the adapter loop for
    anything requested at startup. Asserted rather than assumed by
    `test_a_subscription_requested_while_no_ticks_flow_is_never_applied`.

    Consequence when it bites: the engine's pending entry never fills, because it
    deliberately waits for a *fresh* tick on the chosen contract rather than using a
    cached price. That is the safe direction — no entry rather than an entry at a
    stale price — but it is silent today.

    **No longer silent, as of Phase 4 Part 1.** The supervisor now watches how long
    the oldest unapplied subscription has been waiting
    (`SharedFeedHub.pending_subscription_age_seconds`) and, past
    `STUCK_SUBSCRIPTION_SECONDS` (30 s), raises through the same three channels
    limitation 13 uses — a forced `DEGRADED` heartbeat, a `CRITICAL` row in
    `errors` with `component='feed'`, and a `subscription_not_applied`
    notification. Once per run, not once per poll: the condition persists by
    nature, and a notification every second is noise rather than an alarm.

    **The condition itself is unchanged and this entry stays open.** The hub still
    applies a pending subscription only at a tick boundary (**D24**), because the
    alternative is a cross-thread call into the SDK's loop — the hang Phase 3
    Part 1 exists to prevent. The threshold is generous against the mechanism it
    watches: the hub applies pending subscriptions on *any* tick, so reaching 30 s
    means the group has received no tick at all for that long, which is already an
    incident. It matters more from Part 1 on: with real contracts, an unapplied
    subscription is the single thing standing between a resolved contract and a
    fill.
16. **~~An engine worker cold-starts its indicators.~~ CLOSED** (Phase 4 Part 4,
    5 August 2026). `engine_worker.py` now builds a real `WarmupManager`/
    `WarmupSource` pair when a strategy opts in via `warmup_source: dhan`; the
    default (`"none"`) keeps every existing configuration's prior behaviour
    unchanged.

    **The claim this entry made before Part 4 was too strong, and building
    Part 4 found the gap that made it so — closed in the same part, not left
    corrected-in-wording.** It said a cold start "can only refuse to trade, or
    trade a strategy that declared it did not care" — true for a
    `warmup_spec()` that raises, and true for a strategy whose spec is `None`,
    but **not** true for the third case: a continuity-required strategy with
    no manager injected (every config's default absent an explicit opt-in)
    logged a `WARNING` and **traded anyway** on the cold-started indicator.
    `entry_blocked_by()` was only consulted on the `warmup_manager` path; the
    fallback (Phase 3 Part 2b-ii-B-2) only logged. Found while building Part
    4's end-to-end test, not assumed — and once found, fixed rather than
    merely documented, because it sat behind the *default* configuration and
    reproduces exactly the failure shape this subsystem exists to prevent
    (the reference's own 2026-07-17 manufactured-signal incident).
    `validate_warmup_config(strategy, cfg, warmup_manager)` (**D47**) now
    refuses construction for a continuity-required strategy whenever
    `warmup_from_history` is false **or** no manager is supplied — collapsing
    what were two independently-reasoned mechanisms with a gap between them
    into one. `test_no_manager_or_source_now_refuses_construction` pins the
    refusal.

    **Proven end to end** — not just at `WarmupManager`'s own unit level — by
    `tests/integration/test_engine_warmup_end_to_end.py`: a fetch failure
    degrading to `COLD_START` actually blocks the live entry that would
    otherwise fire; a successful `WARMED` replay does not; and a
    continuity-required strategy with no manager at all now fails at
    construction rather than reaching a runtime warning.

    **Still open:** the underlying-only scope (no per-option-leg warm-up,
    D43), the cross-worker rate-limit collision risk (no coordinator — Phase
    5), and the unverified partial-candle response shape (documentation and
    an opt-in smoke test only, no captured fixture). None of these three
    permit a continuity-required strategy to trade on an unwarmed indicator
    without an explicit `InvalidWarmupConfig` at startup somewhere in the
    chain — they bound what "warm" can mean, not whether the refusal fires.
17. **~~The engine trades synthetic contracts, not real ones.~~ CLOSED**
    (Phase 4 Part 1, 1 August 2026). The engine now resolves real Dhan contracts
    through `DhanOptionChainResolver` over the daily instrument master
    (`common/market_data/scrip_master.py`), so the `security_id`, the strike, the
    expiry **and the lot size** come from the exchange rather than from
    configuration. `SimulatedOptionChainResolver` remains and remains the
    **default**, so every recorded/offline path is unchanged; `contract_resolver:
    dhan` opts a strategy into real contracts.

    **The fix this entry originally specified was wrong**, and the correction is
    recorded rather than quietly applied. It said an `OptionChainResolver` backed
    by `OptionChainService` — but Dhan's `/v2/optionchain` response is keyed by
    strike and carries no per-strike `security_id`, so it cannot name a tradable
    contract. The instrument master can, works outside market hours, and needs no
    per-trade API call. See **D33**.

    **It was also larger than this entry described.** A real id still could not be
    subscribed: `DhanMarketFeedAdapter` held one `exchange_segment` for every
    instrument, and an options runtime needs `IDX_I` for the underlying and
    `NSE_FNO` for its contracts at the same time. A wrong segment does not raise —
    it delivers silence — so this is fixed and covered by
    `tests/unit/test_feed_exchange_segments.py`.

    **Proven against the real instrument master on 1 August 2026.** The offline
    rehearsal was run: NIFTY resolved to real numeric ids (`65697` CE / `65698`
    PE, "NIFTY 04 AUG 24100"), 225 strikes for expiry 2026-08-04, and — the point
    of the whole exercise — an exchange lot size of **65** against the **50** the
    configuration defaulted to. Full numbers in the Part 1 gate evidence
    (section 4).

    **~~What is still asserted rather than proven: that those ids actually
    stream.~~ Proven, 6 August 2026.**
    `test_a_real_option_contract_delivers_ticks_on_the_fno_segment` ran live
    (`ALGO_LIVE_SMOKE=1 pytest -k test_a_real_option_contract_delivers_ticks_on_the_fno_segment`
    → 1 passed in 2.62s), on the default cache-reusing auth path — no fresh
    login. A real resolved contract delivered a live tick on `NSE_FNO`, with
    `tick.last_price > 0` and `tick.exchange_time <= tick.received_at` (the
    latter meaningful now that known limitation 20 is fixed; this run is
    additional live confirmation of that fix, not just of resolution).
    Resolution and delivery are now both proven. **Limitation 17 has no
    remaining residual.**

18. **~~Generating a fresh Dhan access token for the shared client ID invalidates
    whichever token was previously active — for both systems.~~ FIXED** (6 August
    2026, same day as the incident). Full incident writeup and sequence remain
    under [Operational risk noted during the audit](#operational-risk-noted-during-the-audit),
    "Incident, 6 August 2026" — that record is kept as-is, since it is what
    happened. The fix: `ALGO_LIVE_SMOKE=1` (run the live smoke tests) and
    `ALGO_SMOKE_ALLOW_FRESH_LOGIN=1` (permit minting a new token) are now two
    separate gates. `tests/smoke/test_live_feed_smoke.py`'s `_bootstrap()`
    defaults to this repo's real `data/cache/token_cache.json` and never puts
    `DHAN_PIN`/`DHAN_TOTP_SECRET` into `AuthCredentials` unless
    `allow_fresh_login=True` is passed explicitly — so `AuthCredentials.can_generate`
    stays False by default and `AuthBootstrap` never builds a login object,
    regardless of what is exported. A test without a usable cached or
    environment token now fails closed with `MissingCredentialsError` rather
    than minting one. Exactly one test
    (`test_the_token_cache_is_written_atomically_and_privately`) still needs a
    real generation to verify the cache-write path itself; it is gated behind a
    new `needs_fresh_login` marker and isolated to its own `tmp_path`, never
    touching the real cache file. **Verified by a mocked dry run** (fabricated
    client id, `DhanTotpLogin.generate` monkeypatched to raise if called, no
    network reachable): default-with-no-cache fails closed without attempting a
    login; default-with-a-cached-token reuses it without attempting a login;
    `allow_fresh_login=True` does reach the login call, proving the opt-in path
    is real. Full suite unchanged at 1242 passed, 11 skipped, both before and
    after. Not yet re-run against a live account under the new gates.

19. **~~`dhanClientId` sent as an HTTP header instead of a JSON-body field, on
    every POST call this repo makes with a hand-rolled `httpx` request.~~
    FIXED** (6 August 2026). Found while investigating limitation 18.

    **The bug.** `tests/smoke/test_live_feed_smoke.py`'s `_index_last_price`
    (backs the ATM-strike lookup in two live rehearsal tests) and
    `test_the_option_chain_throttle_holds_against_the_real_endpoint`'s `fetch()`
    both send `headers={"access-token": token, "dhanClientId": client_id}` to a
    `POST` endpoint (`/v2/marketfeed/ltp`, `/v2/optionchain`) and never put
    `dhanClientId` in the JSON body at all. The installed `dhanhq==2.2.0` SDK's
    own `dhan_http.py` shows the real contract for a POST:
    `header = {"access-token": ..., "client-id": ..., ...}` (`client-id`, not
    `dhanClientId`), and separately, unconditionally,
    `payload["dhanClientId"] = self.client_id` injected into the JSON body
    itself before every POST is sent (`_send_request`, `dhan_http.py:53-56`).
    Both call sites here are missing the body field, and neither sends
    `client-id` as a header.

    **Not confined to the smoke-test helpers — the same pattern is in
    production code.** `common/market_data/dhan_historical.py`, the module that
    performs the real `POST /v2/charts/intraday` warm-up-candle fetch for Phase
    4 Part 4, builds its request the identical way:
    `headers = {"access-token": ..., "dhanClientId": ...}`, payload never
    touched. This is not a test-only bug if the same defect applies there too.
    That endpoint has itself never been exercised against a real live call —
    its own module docstring and `test_the_intraday_endpoint_returns_a_success_shape_during_market_hours`'s
    docstring both say so — so this may already be a live defect in a shipped
    production code path that simply has not been observed yet, only because
    nothing has forced it to run.

    **Contrast, and why the theory holds together.** The one call proven to
    work live is `GET /v2/profile` (`common/authentication/dhan_login.py`'s
    `validate_token`), which also sends `dhanClientId` as a header — but it is
    a bodyless GET, so there is no payload for the SDK's contract to require
    the field in. That is consistent with the theory that this only bites
    POST/JSON-body calls, which is every endpoint listed above except
    `/profile`.

    **Not proven live in isolation, and here is why honestly.** An attempt to
    correct just the header name (`client-id` instead of `dhanClientId`) was
    made live during the limitation-18 incident and still returned 401 — but by
    that point the token itself had already been invalidated by the same
    incident, so that attempt cannot distinguish "still wrong" from "token is
    dead" and is not evidence either way. The stronger evidence is earlier in
    the same incident: the *first* `_index_last_price` failure happened using a
    token minted moments earlier in the very same call (via the then-unfixed
    fresh-login-every-run behaviour) — a token that had every reason to be
    good — and it still came back 401 "Client ID or Token invalid". That is
    consistent with a request that never told Dhan which client it was.

    **Could this be silently causing failures? Yes, plausibly, on every future
    attempt, independent of token validity** — a structural request-shape bug
    fails the same way whether the token is perfect or dead, which is exactly
    what makes it easy to misdiagnose as a token/auth problem (as very nearly
    happened here) rather than a malformed request.

    **Worth checking before the next live rehearsal? Yes** — specifically
    before trusting either: (a) the option-chain smoke test or the ATM-strike
    lookup that gates the Part 1 and Part 5 live rehearsal tests, or (b) Part
    4's warm-up feature, which reads real pre-market history through the same
    pattern in production code.

    **Fixed, 6 August 2026.** `common/market_data/dhan_historical.py` and both
    smoke-test call sites now send `client-id` as the header and inject
    `dhanClientId` into the JSON body, matching the SDK's contract exactly.
    Verified fail-first: reverted the fix and confirmed
    `test_fetch_intraday_builds_the_documented_request_shape`,
    `test_fetch_intraday_sends_the_documented_auth_headers` (which previously
    *asserted the bug itself* — it expected `headers["dhanClientId"]`, exactly
    what the old code sent) and the new
    `test_index_last_price_builds_the_documented_request_shape` (new
    injectable-`token`/`http_post` coverage added specifically so the smoke
    helper's request shape could be asserted without credentials or a network
    call) all failed against the old shape and passed against the corrected
    one. **One flagged gap, left as-is**: the option-chain test's `fetch()`
    closure got the same shape fix but no dedicated shape-asserting unit test
    of its own — a local closure inside one opt-in live test, judged
    lower-value than the other two call sites since nothing else depends on
    it. Full suite unaffected: 1243 passed, 11 skipped at the time of this
    fix.

20. **~~`reconstruct_exchange_time` relabelled IST as UTC instead of converting
    it, producing a live exchange timestamp 5:30 ahead of every real one.~~
    FIXED** (6 August 2026, same day, found running the Part 5 gate item live
    for the first time).

    **The bug.** `common/market_data/dhan.py`'s docstring and code both
    asserted "the SDK renders the exchange epoch as `strftime('%H:%M:%S')`
    against UTC." A real captured tick disproved it: at a genuine
    2026-08-06 05:38:49 UTC, `LTT` read `"11:08:48"` — the IST wall clock, not
    UTC. The old code combined those digits with a UTC-labelled date, which
    does not convert anything — it relabels an IST instant as UTC, producing
    an `exchange_time` 5:30 in the future on every live tick. It surfaced as
    the last assertion of an unrelated live test: `tick.exchange_time <=
    tick.received_at` failed with `exchange_time=2026-08-06 11:08:48 UTC`
    against `received_at=2026-08-06 05:38:49 UTC` — the ~5.5-hour gap matching
    the IST offset exactly, which is what pointed at the root cause.

    **Severity, worked through concretely, not just asserted.** Every
    session/candle predicate converts `exchange_time` to IST via
    `.astimezone(Asia/Kolkata)` before comparing it to session bounds. Feeding
    that conversion an already-mislabelled value adds *another* +5:30 on top,
    so the computed "local" instant is the real IST time plus 5:30, not the
    real IST time:
    - `CandleAggregator.add`'s session-window check (`09:15 ≤ local < 15:30`)
      only holds when `real_IST + 5:30` falls in that range, i.e. real IST
      `03:45–10:00`. **From 10:00 IST onward — the great majority of every
      trading day — every live tick would have been silently rejected as
      "out of session," and zero candles built from live data.** In the
      narrow 09:15–10:00 IST window ticks would still be accepted but bucketed
      into the wrong candle (a tick at real 09:20 IST computed as 14:50 IST,
      landing in the 14:50 bar).
    - `SessionSquareOffAuthority.due()` compares the same mislabelled local
      time against the square-off time (~15:15–15:20 IST). `due()` would read
      true once `real_IST + 5:30 ≥ square_off`, i.e. from roughly **09:45 IST
      onward** — a real position would have been squared off within half an
      hour of the session opening, almost regardless of the strategy.
    - `MarketSession.is_open`/`can_enter` fail the same way — the engine would
      have treated the market as closed for most of the day and refused
      entries throughout.

    **Zero downstream consumers were ever exercised with a live tick, so none
    of the above actually happened.** Traced every production consumer of
    `Tick.exchange_time` — `CandleAggregator`, `CandleBuilder` (both index and
    option/premium candles), `MarketSession.is_open`/`can_enter`,
    `SessionSquareOffAuthority.due`, gap detection in both `engine.py` and
    `HubTickFeed`, position open/close timestamps, `QuoteBook`/`quoted_at` for
    `PaperBroker` fills, and the hub/`ReconnectingFeed` last-tick health
    tracking — and confirmed, by sweeping every test and script that
    constructs a real `DhanMarketFeedAdapter`, that **none of them ever
    routed a live tick into any of those consumers**. Every opt-in smoke test
    that touches a live tick (`test_one_live_tick_reaches_the_hub`,
    `test_the_live_payload_matches_the_ratified_shape`,
    `test_a_real_option_contract_delivers_ticks_on_the_fno_segment`, and the
    Part 5 gate item itself) uses the raw adapter with a bare Python callback
    that only inspects `Tick` fields directly — never a `MarketDataHub`,
    `CandleAggregator`, or `TradingEngine`. `scripts/capture_live_tape.py`
    does the same. Every place that *does* wire a real `CandleAggregator`/
    `TradingEngine`/`Hub`/`ReconnectingFeed` to a tick stream in the test
    suite feeds it via `RecordedFeed` or hand-built `Tick(...)` fixtures with
    directly-specified, already-correct `exchange_time` values — never through
    `reconstruct_exchange_time`. No LaunchAgent or supervised runtime has ever
    been started (Phase 7/8 not shipped), so there is no other live-tick path.
    **No corruption occurred anywhere; the gate item's own trip-wire assertion
    caught this before anything downstream ever saw a live tick.**

    **Fixed.** `reconstruct_exchange_time` now parses the wall-clock time as
    IST, combines it with an IST calendar date (the day-picking logic is
    unchanged in shape, just re-anchored on IST midnight instead of UTC
    midnight), and converts to UTC via `.astimezone(UTC)` — a real conversion,
    not a relabel. The numeric-epoch branch is untouched (a true Unix epoch is
    UTC by definition regardless of exchange timezone); the speculative
    ISO-8601 fallback branch (never observed live) is also untouched rather
    than guessed at.

    **Verified fail-first**, not assumed: reverted the fix and reran — six
    failures, exactly the tests touching this logic, including a **new** test
    using the actual values captured live,
    `test_a_real_captured_ist_ltt_converts_correctly_to_utc`
    (`LTT="11:08:48"`, `received_at=2026-08-06 05:38:49.473969 UTC` →
    asserts `2026-08-06 05:38:48 UTC`). Restored the fix — all green. Three
    *existing* tests had encoded the wrong premise and needed correcting, not
    just the code:
    - `test_the_time_only_ltt_is_reconstructed_and_not_a_fallback` compared
      `exchange_time` directly against the raw `LTT` string — only ever true
      because the old code never converted; now compares via
      `.astimezone(IST)`.
    - `test_ltt_just_before/after_utc_midnight_resolves_to_...` re-anchored on
      IST midnight (the boundary that is actually relevant) with recomputed
      expected values.
    - `test_the_adapter_really_does_produce_utc_ticks`
      (`tests/unit/test_session_timezone_rule.py`) is the more serious one —
      its docstring called the UTC assumption **"the premise the whole defect
      rests on"** and asserted it as fact. Rewritten to state and test the
      corrected premise: a real 10:00 IST tick's `LTT` reads `"10:00:00"`, and
      reconstruction must convert that to `04:30:00` UTC, not relabel it.

    **New standing regression guard**, not just the assertion that happened to
    catch this once:
    `tests/smoke/test_live_feed_smoke.py::test_every_live_tick_has_a_sane_exchange_time`,
    deliberately its own test rather than another assertion riding along
    inside a test about something else (which is exactly how this one was
    only caught by accident). Subscribes to the index alone in Ticker mode —
    the invariant must hold for any live tick — and asserts both directions:
    `exchange_time` not after `received_at`, and not implausibly far before it
    either, so a wrong-day pick would also be caught, not just a wrong-zone
    one.

    **Documentation corrected at the source**, not just in tests: the module
    docstring's payload-shape comment, the `NEVER_TRADED_LTT` sentinel comment
    (same wrong "midnight UTC" reasoning), and the function's own docstring —
    swept the tree afterward and confirmed no stray uncorrected copies of the
    "SDK renders against UTC" claim remain.

    Full suite: 1244 passed, 12 skipped (both counts up by one for the two new
    tests) — no regressions.

21. **`discover_enabled_strategies` cannot scope strategy files to one
    runtime.** `common.config.discover_enabled_strategies(config_root,
    runtime_id)` resolves every enabled file under `config/strategies/`
    against whatever `runtime_id` it is called with, because `StrategyConfig`
    has no `runtime_id` field to filter on — nor does the spec's own "required
    resolved strategy fields" list (section 9). A strategy's membership in a
    runtime group today is only a filename convention (the spec's own example
    is `io_supertrend_fast_v1`, `io_` implying `intraday_options`), never
    validated. **Not a live risk today**: `intraday_options` is the only
    runtime that exists, so "every enabled strategy" and "every enabled
    strategy belonging to this runtime" are the same set. **Becomes a real
    risk the day a second runtime is added** (`positional_options` or
    `intraday_stocks`, per **D56**): two supervisors calling this function
    against the same `config/strategies/` directory would each discover the
    other's strategies too, and try to build a `WorkerConfig` for a strategy
    shaped for a different instrument class. The duplicate-worker lock would
    still prevent both from actually running the same `strategy_id` at once
    (whichever supervisor starts second is refused, per **D55**'s proof), so
    this is a startup-time correctness gap, not a double-order risk — but it
    would still mean the wrong supervisor sometimes wins the race, or an
    operator sees a confusing refusal for a strategy their runtime was never
    meant to own. **Fix needed before a second runtime ships**: either an
    explicit strategy-to-runtime list in the runtime's own YAML, or a
    validated `strategy_id` prefix convention enforced at load time rather
    than assumed. Not built now because there is exactly one runtime to test
    either mechanism against — see **D57**.
22. **~~`strategy_state.daily_realised_pnl` silently held only the last-touched
    contract's own P&L, not the day's total.~~ FIXED** (Phase 6 Part 1,
    6 August 2026, found while building the column's first reader). See
    **D58** for the mechanism. Bounded in practice before the fix: every
    strategy shipped so far trades at most one contract per day (the
    fixture path and every existing worker config), and a single-contract
    day cannot distinguish "accumulate" from "overwrite" — there is only
    ever one value to keep — so this was never observed live. It would have
    bitten the first strategy to close one contract and open a different
    one on the same trading day, which Phase 9's real strategies are
    expected to do routinely (rolling, re-entry into a different strike).
23. **`DailyRiskGuard`'s `max_trades`, `daily_profit_target` and
    `kill_switch` have no configuration surface.** `EngineWorkerConfig` (and
    the `EngineConfig` it builds) exposes only `max_daily_loss_percent` and
    `starting_capital` — `TradingEngine._build_daily_guard` constructs
    `DailyRiskConfig(daily_max_loss=...)` and nothing else, so the other
    three fields are permanently at their disabled defaults on every worker
    this repository can actually run today. Not a Phase 6 Part 1 gap
    specifically — this predates Part 1 and Part 1 did not need to touch
    it, since `DailyRiskGuard.restore` accepts a `DailyRiskRecovery`
    regardless of which limits are configured. Recorded now because Part 1
    is the first code to depend on `DailyRiskGuard` behaving correctly
    across its full configuration surface (`tests/unit/test_daily_guard.py`
    exercises `max_trades` and `kill_switch` directly against the guard,
    since no worker configuration can reach them end to end to prove it
    there). Closes when a real strategy's configuration needs one of the
    other three limits — most likely Phase 9.
24. **The Phase 6 Part 2 per-candle exit-state write's multi-worker
    contention cost is unmeasured.** `TradingEngine._persist_exit_state`
    (see **D61**) writes to the shared group `strategy_state` row once per
    completed candle per open position — not once per tick, and the same
    write shape every other `strategy_state` write already uses, so it is
    not a new *kind* of load. What is genuinely new and unverified: candle
    boundaries are wall-clock aligned, so multiple strategies in one group
    sharing a timeframe tend to complete candles in the same second — a
    synchronised write burst against one SQLite file, not
    uniformly-distributed load. `journal_mode=WAL` /
    `busy_timeout=5000ms` (`common/persistence/database.py`) already
    governs every write across every worker in a group, unchanged by this
    part, but **no test or benchmark in this repository measures write
    latency or lock-wait time under concurrent multi-worker load at all** —
    for this write or any other that already existed. Found while answering
    a direct question about it, not during Part 2's own build — worth
    recording precisely because it would have been easy to let the
    deviation entry's mention of "a real, measurable behaviour change"
    quietly stand in for having measured it. Revisit before Phase 7
    operations work, or before any runtime group grows past a handful of
    concurrently-timeframed strategies, whichever comes first.
25. **Candle-idempotency protection (`last_candle_end_at`) only applies
    while a position is open — a flat day's candles are not idempotently
    resumable.** Phase 6 Part 3, **D65**, decided directly with the user:
    gating the watermark write on "position open" (reusing Part 2's
    checkpoint) rather than writing it unconditionally on every candle
    avoids adding new write frequency on top of limitation 24's already-
    unmeasured contention question. The cost is real: a worker that
    crashes and restarts on a day with no open position (before its first
    entry, or after a clean close) has no watermark at all, so a replayed
    tape's underlying candles are reprocessed from scratch — indicator
    state (a session-cumulative one, in particular) could double-count on
    a genuine replay of that portion of the day.
    `test_a_flat_restart_does_not_carry_the_candle_guard_over` proves the
    current behaviour (a fresh entry is not silently skipped) but does not
    close this gap. Not believed to matter for live operation — a live
    feed does not re-deliver ticks already received, so genuine
    reprocessing only arises from an offline/recorded-tape replay — but
    worth closing before this repository's own recorded-tape tooling
    (`scripts/capture_live_tape.py` and any future replay-based testing)
    is used to validate a day that both starts flat and later opens a
    position. Closes if a future part decides the write-frequency cost
    (limitation 24) is acceptable and lifts the "position open" gate, or a
    cheaper day-level heartbeat write is found.

26. **`square_off_before_expiry_days` counts calendar days, not trading
    days.** Phase 6 Part 4. A holiday sitting inside the configured lead
    window shortens it in trading-day terms — e.g. a two-day lead with one
    holiday in between only buys one trading day of actual pre-expiry
    warning. Harmless at the shipped default (`0`: the lead is zero days
    either way), and `SquareOffPolicy` has no holiday calendar to consult
    even if it wanted to (`MarketSession._holidays` lives one layer up, at
    the session, not the policy). Closes when the lead is configured
    non-zero for the first time, or when spec section 11's own "expiry
    calendar and last-trading-day handling" item of the settlement policy
    (still entirely unbuilt — see limitation 27) lands and can be reused
    here instead of duplicated.

27. **Exchange-settlement simulation (`expiry_policy:
    simulate_exchange_settlement`) is not implemented, and is refused at
    config load, not merely undocumented.** Phase 6 Part 4 delivers only
    the safer half of spec section 11: `force_square_off_before_expiry`.
    None of the eight items section 11 requires before the alternative may
    be used exist in this repository — expiry calendar and last-trading-day
    handling, final settlement price capture, ITM/OTM determination,
    index-option cash settlement, exercise/assignment event recording,
    exercise-related STT and other charges effective-dated to the
    settlement date, T+1 settlement timing, and stock-option
    physical-settlement obligations/delivery margin/assignment risk. A
    `StrategyConfig` naming `simulate_exchange_settlement` is rejected by a
    `field_validator` at load, naming this precondition in the raised
    `ConfigError`, and `SquareOffPolicy.__post_init__` refuses the same
    value again as defence in depth against direct construction bypassing
    the loader. No configuration in this repository can enable it today.
    Closes when a versioned settlement policy covering all eight items
    ships and passes its own settlement tests — spec section 11's explicit
    precondition — not before.

28. **The Phase 1 fixture path's expiry is structurally unknowable, so
    Part 4's expiry-lead rule is permanently inert there.** The fixture
    strategy (`FixtureSignalStrategy`, `runtimes/intraday_options/
    worker.py`) trades a bare `security_id`, never an `OptionContract` —
    there is no expiry field anywhere on its path to know. `_maybe_square_off`
    now calls `trigger_at(..., expiry=None)` explicitly, with a comment
    recording that this is by construction rather than an oversight. Not a
    gap to close: the fixture exists only to exercise Phase 1's plumbing
    end to end and is explicitly documented as never a template for a real
    strategy (see its own file's opening comment). Closes only if the
    fixture path is ever extended to carry a real contract, which is not
    planned.

29. **Adding `StrategyConfig.expiry_policy`/`.square_off_before_expiry_days`
    moved `fingerprint(cfg)` for every strategy, including ones whose YAML
    is untouched.** Verified before accepting this cost rather than assumed
    harmless: `config_fingerprint` is write-only across this repository —
    audited every consumer in `common/`, `runtimes/`, `scripts/`,
    `dashboards/` and found no `SELECT`, comparison or lookup keyed on it
    anywhere; it is written into three columns (`sessions`, `order_intents`,
    `orders`, migration `0001`) and never read back. No `.db`/`.sqlite` is
    git-tracked and `logs/algo_trading.log` contains zero occurrences of
    "fingerprint" as of this phase, so no operational record depends on a
    stable value across this change. `MarketSession.fingerprint` is a
    *different* fingerprint (session timing/calendar only) and is
    unaffected — `SessionConfig.from_square_off_policy` reads only
    `entry_cutoff`/`square_off_at`/`timezone` off the policy. The residual
    cost: two runs with byte-identical strategy YAML, one either side of
    this change, now carry different fingerprints, which reads as "the
    configuration changed" to the human the fingerprint exists to serve.
    Deliberately not mitigated by excluding the new fields from the
    canonical digest — a safety-relevant field that does not move the
    fingerprint is the worse failure, and doing so would contradict
    `test_fingerprint_changes_when_any_value_changes`'s own premise. Spec
    ARCH:3003 scopes live approval to "that strategy instance and
    configuration fingerprint"; that binding is unimplemented (`live_approved`
    is a plain bool with no fingerprint tie) and Phase 10 does not exist yet,
    so nothing is invalidated today, but Phase 10 must know the fingerprint
    domain moved here, and any paper-acceptance record predating this phase
    needs re-deriving before binding it to a fingerprint. Closes if a
    fingerprint *version* or schema-generation marker is ever introduced.

30. **CLOSED (2026-08-15, `strategy-weekly-delta-neutral` branch).** ~~A
    position or strategy-state row still cannot survive across a
    `trading_date` boundary — D56's gap, still open by design, now with a
    written candidate direction rather than none.~~ `weekly_delta_neutral`
    is the real positional strategy D69 said this needed before the
    candidate direction could be validated or replaced — it validated it.
    Migration `0010` adds a durable `cycle_id` identity
    (`strategy_cycles`/`strategy_cycle_legs`/`strategy_cycle_adjustments`/
    `strategy_cycle_events`) plus `cycle_position_bindings`, exactly as
    D69 sketched: `trading_date` on `positions`/`order_intents`/`orders`/
    `fills`/`trade_ledger` is untouched and still records the *event*
    date; what changed is that `ExecutionRepository._read_position`/
    `_upsert_position`/`apply_fill`/`reserve_intent` now accept an
    **optional** `cycle_id` that resolves the mutable position row through
    `cycle_position_bindings` instead of `(trading_date, security_id)` —
    every existing intraday call site, which never passes one, is
    byte-identical to before. A binding write shares the exact transaction
    the position mutation it guards is written in
    (`ExecutionRepository.apply_fill`), so a binding failure rolls back the
    position write and vice versa — proven, not asserted, by
    `tests/unit/test_cycle_position_bindings_are_atomic.py` and by
    `tests/integration/test_weekly_delta_neutral_restart.py`, which enters
    a real four-leg cycle, closes the process, reopens a second
    `ExecutionRepository` against the same database file, and confirms the
    same cycle/legs/positions are adopted with no duplicate row and
    `positions.trading_date` still the *opening* date after a restart on a
    later trading date. See section 3's positional-options entry and
    `common/engine/positional/`'s own module docstrings for the full
    design (sequential hedge-first entry, the expiry-day lifecycle ladder,
    restart reconciliation). D56/D69 stay in section 2.3 as the historical
    record of the decision to defer, not rewritten.

31. **A pre-database authentication failure is not persisted to
    `auth_events`.** `IntradayOptionsSupervisor.set_startup_auth_outcome`
    (Phase 7 Part 1) records a *successful* `AuthBootstrap.get_token()`
    outcome once `run()` opens this runtime's database — but `get_token()`
    runs in `__main__.py`, before that database exists (before the runtime is
    even confirmed enabled to start). A rejected credential, a rate limit, or
    a cooldown-suppressed attempt at that point stays `print()`+log only, the
    same as before Part 1. Persisting it would mean opening (and therefore
    creating) the operational database before knowing whether the runtime
    should start at all — a larger design question than one part's scope.
    Not a safety gap: `AuthBootstrap` fails closed regardless (module
    docstring: "There is no degraded mode"), so a failed auth still stops the
    runtime from starting; only the *audit trail* of that failure is
    incomplete. Revisit if operators need to query historical auth failures
    from the database rather than the log.

32. **The engine's own `entry`/`exit`/`eod_summary` notifications carry no
    `correlation_id`, even though `NotificationEvent` now has the field.**
    `common.engine.positions.FillOutcome`, `OpenPosition` and `Trade` do not
    persist a correlation ID today — `PositionManager.open()`/`close()` read
    only `fill_price`/`charges`/`charges_breakdown` off the gateway's
    `FillOutcome` and discard the rest. `worker.py`'s own notifications
    (`order_filled`, `square_off_completed`, both on the **fixture** path)
    already carry one, read straight off `ExecutionResult.correlation_id` — a
    field that already exists and is already available to `LifecycleGateway.
    _outcome()` inside `common/engine/gateway.py`, which is what makes this a
    contained, deliberately-deferred follow-up rather than an unknown one:
    add `correlation_id: str | None = None` to `FillOutcome`, thread it
    through `OpenPosition.entry_correlation_id` and `Trade.entry_correlation_
    id`/`.exit_correlation_id` (both dataclasses already carry several
    optional trailing fields in exactly this shape — `entry_regime`,
    `session_tags` — so this is additive, not a redesign), and read it back
    in `TradingEngine._open`/`_close`. Scoped out of Phase 7 Part 2
    specifically to keep that part's already-large diff (`SafeNotifier`'s
    redesign, the entrypoint wiring, two real bugs found and fixed) from
    growing into a fourth change to the engine's core position-management
    seam under the same time budget. Revisit as a small, separate, well-
    contained follow-up whenever correlation-tagged engine-path alerts
    become a real operational need.

33. **`EXIT_SAFETY_SHUTDOWN` covers the operator-stop path only — a tripped
    kill switch does not end the supervisor run.** Introduced by Phase 8,
    deliberately scoped: `main()` returns the new exit code when
    `SupervisorResult.stopped_by_signal` is true, so `supervised_launch.py`
    treats a genuine `SIGTERM` as terminal rather than retryable. The daily
    loss halt and kill switch (`common/engine/daily_guard.py`) are a
    different mechanism entirely — they latch new entries off *inside the
    engine, per worker* (`DailyRiskGuard.halted`) and the supervisor keeps
    running to session end regardless, returning its ordinary `EXIT_OK`.
    Under a loaded `KeepAlive`-style plist that would mean a halted worker's
    process still restarts clean the next scheduled window, which is
    probably the right behaviour for a *daily* halt (a new trading date
    should get a fresh chance) but is untested and unexamined for the
    all-strategies emergency kill switch, which is meant to stay down.
    Deliberately not built this phase: making a tripped kill switch end the
    whole supervisor run is a behaviour change to the trading path, not a
    LaunchAgent-validation concern, and belongs with Phase 9 once a real
    strategy gives the question a concrete shape.

34. **~~`launchd`'s own `stdout`/`stderr` streams (`logs/launchd/*.log`) sit
    outside the log retention sweep.~~ CLOSED**, same-phase — reconsidered
    once the actual volume was traced rather than assumed negligible: every
    production `setup_logging()` call site leaves `console=True` (its own
    default), so these files are not a handful of incidental `print()` lines
    but a full second copy of the entire application log stream, growing
    forever since `launchd` never rotates or truncates what it appends to.
    `common.retention.logs.rotate_launchd_logs` is the missing rename step —
    `sweep_logs` itself is deliberately untouched, since it must never treat
    an unrotated, potentially-still-open file as a safe-to-compress backup
    (its own docstring's rule). `run_retention` now takes an optional
    `launchd_log_dir`, rotates it first, then sweeps it with the same
    `log_max_age_days`/`log_compress_after_days` policy as the main log
    directory; `runtimes/intraday_options/__main__.py:main` passes
    `paths.log_root / "launchd"` at the one existing controlled-startup call
    site, no new one. See **D83** for why renaming a file `launchd` may still
    hold open is safe (verified on this project's actual target platform,
    not assumed from POSIX documentation) and for the day-granularity
    argument that a same-run sweep can never touch what this step just
    rotated. `newsyslog`/`/etc/newsyslog.d` remains out of scope, as before —
    this is a userspace fix within the repository's own writable directory,
    not a system-level configuration change.

35. **No `EgressIpProvider` implementation is chosen or shipped.** Phase 10's
    static-IP preflight (`common.broker.live_preflight.check_static_ip`) needs
    an independently-observed current egress IP as one of its three checked
    facts (configured expected IP, Dhan-registered IP, observed current IP) —
    deliberately, so a stale or wrong Dhan whitelist entry cannot be trusted
    alone. `EgressIpProvider` is a `Protocol`; `FakeEgressIpProvider` exists
    for tests only. The shipped default is `egress_provider=None`, which
    fails the check closed (`BLOCKED`) rather than skip it — a real
    implementation (an external IP-echo HTTPS service, or an operator-infra
    local source) is an open decision requiring separate approval before
    Phase 10 gates can ever open for real capital.
36. **CLOSED — Dhan trade-book integration.** The installed
    `dhanhq==2.2.0` source confirms `get_trade_book(order_id=None)`.
    `DhanLiveBroker.fetch_trades()` now uses it, joins `orderId` through the
    broker order book to recover correlation identity, and blocks on malformed,
    failed or unattributable rows rather than reporting an incomplete snapshot.
37. **CLOSED IN CODE — audited live-confirmation workflow.**
    `scripts.live_confirmation` issues or revokes an exact account/runtime/
    strategy/config-fingerprint confirmation with a maximum 30-minute TTL,
    requires an explicit action phrase, and appends an immutable event through
    account migration `0003`. This does not activate live mode; the remaining
    independent gates and approvals still block it.
38. **Code fix complete; live operational revalidation remains outstanding.**
    Known limitation 18's accidental fresh-login path was fixed on 6 August:
    smoke execution and permission to mint a fresh token are separate flags.
    No real-capital activation is allowed until an operator revalidates that
    fixed path alongside every system sharing the Dhan client ID. This is an
    evidence gate, not an unfixed code path.
39. **CLOSED — production static-IP preflight and worker revalidation.**
    The earlier finding was correct for commit `3de9602`: static-IP preflight
    had zero production call sites. It is now wired through
    `runtimes.intraday_options.__main__.main` for parent admission and
    `runtimes.intraday_options.live_runtime.prepare_live_runtime` for an
    independently forced child check. `GuardedLiveCall` enforces TTL freshness
    and account-shared rate capacity before every Dhan call. The configured
    provider is loaded from a strict `module:attribute` seam; no production
    provider was selected without approval, so missing provider configuration
    still blocks operational activation.


### Operational risk noted during the audit

The **legacy `Trading_Automation` system was running during the audit** — its
`portfolio.db`, `weekly_strategies.db` and strategy log files were being written
live. The spec requires preventing simultaneous execution of the old and new
systems. Nothing in this repository goes near it (verified again this phase: its
newest source+config mtime is unchanged across 1010 files — see the recorded
baseline below).

**Settled in Phase 8 (D81), not merely re-checked.** The read-only mtime
comparison above proves this repository never *writes* near the legacy
system; it says nothing about whether the two could ever *run*
simultaneously, which is the spec's actual requirement and Phase 8's own
gate test 6. `common.process.legacy_guard.legacy_system_status()` checks
that directly — and, run for real during this phase, found the legacy
LaunchAgent (`com.soundarraj.tradingautomation.starttrading`) genuinely
loaded and its `weekly_strategies` component genuinely running. Both
`scripts/validate_environment.py` and `runtimes/intraday_options/__main__.py`
now refuse to start while either signal is positive, naming the exact
`launchctl bootout` command. **Still open**: the legacy agent itself was not
unloaded from inside this session — that is the operator's own action on
their own machine, by this phase's own decision (see the Phase 8 writeup
above), and remains a precondition for Phase 9 loading any plist.

#### Recorded baseline: `Trading_Automation` newest source+config mtime

Until Phase 3 Part 2a this document asserted "mtime unchanged" without ever
recording the value, which made the claim unfalsifiable — a later phase could
only repeat the assertion, never test it. The baseline is now stored.

**Widened at Part 2b-ii-A from `.py` only to source *and config*.** The `.py`-only
check was sound but narrow: it could not have caught a stray write to a `config.yaml`,
a `pyproject.toml`, a launchd `.plist` or a startup `.sh`. Widening it to *every*
file type was rejected as dishonest in the other direction — the legacy system is
still running and legitimately writes `.log`, `.db`, `.csv` and `.parquet`
continuously, so a whole-tree check would fail every time it was run and be
switched off within a phase. The set below is the largest one that stays quiet
while the reference system runs normally.

**Baseline — unchanged in value by the widening, which is itself the evidence
that the 106 newly-covered config files were not touched either:**

```
2026-07-28 10:29:14  option_strategies/Trading_Strategies_Automation_v2/tests/test_warmup_coordinator.py

covered: 1010 files  =  904 .py  +  73 .yaml  +  14 .sh  +  7 .json
                        +  4 .toml  +  4 .sql  +  4 .plist
```

Reproduce with:

```bash
find /Volumes/Trading/Trading_Automation \
  \( -name '*.py' -o -name '*.yaml' -o -name '*.yml' -o -name '*.toml' \
     -o -name '*.json' -o -name '*.sql' -o -name '*.sh' -o -name '*.plist' \
     -o -name '*.ini' -o -name '*.cfg' \) \
  -not -path '*/.venv/*' -not -path '*/site-packages/*' -not -path '*/__pycache__/*' \
  -not -name 'access_token.json' \
  -exec stat -f '%Sm %N' -t '%Y-%m-%d %H:%M:%S' {} + | sort -r | head -1
```

Note the absolute path: per `CLAUDE.md` this must not depend on the shell's working
directory, because a `cd` here is exactly what caused a `git status` to run against
the reference repository in Part 2b-ii-A.

**Every exclusion, and why — the boundary is the guarantee, so it is stated rather
than left to the reader to infer from the command:**

| Excluded | Why |
|---|---|
| `.venv/`, `site-packages/`, `__pycache__/` | `pip` and the interpreter legitimately write these. Carried over from the original check |
| `access_token.json` | **The only file excluded by name.** It is a live Dhan token that the still-running legacy system rewrites on every authentication (observed moving at 09:03 on 31 July), so including it would guarantee a false positive daily. It is also a secret, and nothing in this repository would ever write one there |
| `.log`, `.db`, `.csv`, `.parquet`, `.pid`, `.lock`, `.out` | Runtime output of the running legacy system — the churn this check exists to see *past* |
| `.md` | Documentation, not source or config. A deliberate gap: this check would not catch a stray write to a reference `.md`. Narrow enough to accept, recorded so it is a known limit rather than an oversight |
| `.env` | Secrets. Never read, never stat'ed, never printed |

**This is a strict superset of the old check** — all 904 `.py` files remain covered,
including the nine under `src/stock_intraday_orb/data/`, which are source modules in
a package that happens to be named `data/` rather than runtime output. An earlier
draft of this command excluded `*/data/*` wholesale and silently dropped them, which
would have *weakened* the `.py` guarantee while appearing to strengthen it.

The timestamp predates this repository's first reference read (the Phase 0 audit,
29 July 2026), so any future value newer than it means something here wrote to
the reference tree and must be investigated before the phase is accepted.
**Re-verified unchanged at Phase 3 Part 2a and again at Part 2b-ii-A (31 July
2026), the latter both before and after the commit.**

**Phase 2 adds one more instance of the same concern.** Both systems authenticate
as the same Dhan client, and Dhan rate-limits token generation to roughly one
request every two minutes. If the legacy system is running its own auth
bootstrap, the two will contend for that budget — and our token cache and refresh
lock are local to this repository, so they cannot coordinate with it. Do not run
both bootstraps in the same window.

#### Incident, 6 August 2026: a fresh login from this repo invalidated `Trading_Automation`'s live token

The concern above understated the risk. It framed the shared client ID as a
**rate-limit contention** problem — two systems competing for one budget of
roughly one token generation per two minutes. Today's attempt to run the Part 5
Full-mode gate item (`tests/smoke/test_live_feed_smoke.py`) showed it is worse
than that: **generating a new access token for the shared client ID invalidates
whichever token was previously active, immediately, for both systems.** This is
not contention over a budget: it is one system's login silently logging the
other one out.

Sequence, reconstructed from the session:

1. `scripts.auth_bootstrap` was run in this repo — a fresh token was minted and
   validated OK against `GET /v2/profile`.
2. Minutes later, `tests/smoke/test_live_feed_smoke.py::
   test_a_real_option_in_full_mode_delivers_a_two_sided_book` was run with
   `ALGO_LIVE_SMOKE=1`. Its `_bootstrap()` helper builds a fresh
   `AuthBootstrap` against a throwaway `tmp_path` cache directory on every
   invocation — it has no notion of "a good token already exists", so with
   `DHAN_PIN`/`DHAN_TOTP_SECRET` exported it always attempts its own login
   rather than reusing this repo's already-valid cache.
3. That second login minted a new token for the same client ID.
4. Both this repo's just-minted token from step 1 **and**
   `Trading_Automation/common/access_token.json` (created earlier that morning,
   expiry the next day, actively in use by its running `start_trading.py`, two
   Streamlit dashboards and the weekly-strategies scheduler) were confirmed
   dead immediately after — both returned `DH-906 Invalid Token` against a
   direct, read-only `GET /v2/profile` check.
5. After the invalidation was discovered, a subsequent attempt exported
   `DHAN_ACCESS_TOKEN` only, with `DHAN_PIN`/`DHAN_TOTP_SECRET` unset. With no
   PIN/TOTP available, `AuthCredentials.can_generate` is `False`, so
   `_bootstrap()` cannot fall back to a fresh login even from a throwaway cache
   directory — it is limited to the token handed to it. That attempt did not
   trigger a second fresh login. **The safe path already exists today, without
   the code fix**: export a known-good `DHAN_ACCESS_TOKEN` and omit
   `DHAN_PIN`/`DHAN_TOTP_SECRET` for any run against a shared client ID, and no
   login call — fresh or otherwise — is possible.

**Confirmed today: no positions were affected.** Both systems are paper-only
right now, so a dead auth token meant a broken read, not a stuck live order or
an unmanaged position. **This will matter once either system holds real
capital** — an invalidated token on a live system means it cannot manage or
exit an open position until it notices and re-authenticates, and there is no
guarantee it notices promptly.

**Action required before Phase 10 (live trading) is ever enabled — tracked here
as a checklist item, not yet done:**

- `test_live_feed_smoke.py`'s module-scoped fresh-login pattern must default to
  **reusing a cached token** (this repo's own `data/cache/token_cache.json`, if
  valid) rather than minting a fresh one on every run. A fresh-login code path
  may still exist for the cases that genuinely need one, but it must be gated
  behind an explicit opt-in separate from `ALGO_LIVE_SMOKE=1` — the two are
  currently the same gate, which is how this ran unnoticed.
- This must be **verified fixed**, not just noted, before Phase 10. A live
  system silently logging out another live system's session is not acceptable
  once either is trading real capital.

---

## 7. Safety confirmations

Re-confirmed for **Phase 2 Block 1**, with the code and tests that back each
claim. Every statement below was re-run this phase, not carried forward.

**Outstanding Phase 10 precondition, not yet satisfied:** known limitation 18 —
a fresh Dhan login from this repo invalidates `Trading_Automation`'s live token
and vice versa, confirmed live on 6 August 2026. Harmless today because both
systems are paper-only; must be fixed and reverified before Phase 10 gates open
for real capital. See known limitation 18 and the incident writeup under
[Operational risk noted during the audit](#operational-risk-noted-during-the-audit).

**Re-confirmed again for Phase 3 Part 1** (30 July 2026), against the full suite:
546 passed, 6 skipped. Part 1 touched the feed's *shutdown* path only. It added no
network call, no endpoint, no credential handling and no order surface; the four
read-only calls listed below are still the complete set, `DhanLiveBroker` still
does not exist, all 12 broker-factory tests pass unchanged, and no file under
`Trading_Automation` was read for it, let alone written.

**Re-confirmed again for Phase 3 Part 2b-ii-B-2** (1 August 2026), against the full
suite: 815 passed, 6 skipped. This is the part that put a real engine into a real
worker process, so the claim is stronger than "no new call sites were added": an
engine worker configured `execution_mode=LIVE` was **run end to end** and produced
zero order intents and zero fills, because it reaches the broker only through the
same `build_broker()` gate the fixture path uses
(`test_live_mode_is_still_refused_on_the_engine_path`). `DhanLiveBroker` still does
not exist, all 12 broker-factory and all read-only-script tests pass unchanged, and
no file under `Trading_Automation` was written — its newest **source and config**
mtime is unchanged at the recorded baseline.

- Live order placement was **not implemented through Phase 9** — `DhanLiveBroker`
  did not exist, there was no class, no order method, no stub. **As of Phase 10
  (13 August 2026) `DhanLiveBroker` exists and is code-complete** — see the
  "Re-confirmed again for Phase 10" block below for the current, accurate
  statement. It has never made a real Dhan network or order-placement call;
  every test against it uses a mock/fake `DhanOrderClient`. Operational live
  activation remains **blocked**: every committed config value still keeps
  every live gate fail-closed, and `scripts/assert_no_live_config_committed.py`
  enforces that in CI.
- **`build_broker()` refuses live in every reachable *committed* configuration.**
  It consults `effective_live_gate()` first and raises `LiveExecutionBlocked`
  unless every one of the AND-chain's conditions is true — and every one of
  those conditions is false in every committed YAML file today. Proven by
  `tests/unit/test_broker_factory.py`, including a parametrised test that flips
  each gate individually, extended in Phase 10 to also prove a real
  `DhanLiveBroker` is constructed when every gate (including live preflight) is
  satisfied with a fake broker dependency — never with real credentials or a
  real network call.
- **No live-to-paper fallback** anywhere. The refusal message says so explicitly,
  and `test_a_blocked_live_strategy_is_never_rerouted_to_paper` asserts it.
- **The supervisor refuses to spawn a non-paper worker at all**, so a live
  strategy never reaches a process, let alone a broker.
- **`common/market_data/dhan.py` is still the only module importing the SDK.**
  Phase 2 added authentication that talks to Dhan over `httpx` precisely so this
  stays a one-file rule (deviation D14). `test_only_the_dhan_adapter_imports_the_sdk`
  now matches actual `import` statements rather than the bare word, so prose
  explaining the rule cannot trip it; `test_authentication_does_not_import_the_sdk`
  proves the auth package pulls in no SDK at runtime.
- **Every network call Phase 2 can make is read-only**, and there are exactly
  four: `POST auth.dhan.co/app/generateAccessToken` (credentials in, token out),
  `GET api.dhan.co/v2/profile` (validation), `POST /v2/optionchain` (a read
  expressed as a POST), and the market-feed WebSocket. **None can place, modify or
  cancel an order.** Order placement lives on `/orders`
  (`POST`/`PUT`/`DELETE`), which no file in this repository references —
  `tests/unit/test_scripts_are_read_only.py` asserts that structurally, along with
  the absence of any `put`/`delete`/`patch` call and of any broker import.
- **Dhan additionally gates order endpoints behind static-IP whitelisting** that
  has not been configured (spec section 13), so even a hypothetical order call
  would be refused at the broker.
- `scripts/auth_bootstrap.py --status` **makes no network call at all**, proven by
  a test that makes the socket layer raise.
- The opt-in smoke tests **place no order**; they authenticate, subscribe, assert
  a tick, and make one option-chain read.
- **No secret reaches SQLite.** Migration `0002`'s three new tables record events,
  reason codes and timings only; `test_no_phase_two_table_can_store_a_secret`
  checks every column name structurally. The token lives only in
  `data/cache/token_cache.json` (mode `0600`, gitignored twice) and in memory.
- **The token cache and cooldown files contain no credential beyond the token
  itself**, and the cooldown record holds only a timestamp and a redacted reason —
  asserted by `test_the_cooldown_file_holds_no_secret`.
- No real credential was printed, committed, copied or written to any file.
  `.env.example` holds empty placeholders only, and secrets never enter SQLite.
- No file under `Trading_Automation` was written or modified in this phase; no
  secret, database, token or log was copied from it. It remains a read-only
  reference with no runtime dependency.
- **No default test requires credentials or network access.** The 529 tests that
  run by default use recorded/synthesised data and fake values; the 6 that would
  touch the network are skipped unless explicitly enabled. This was *verified*,
  not assumed: the full suite passes three times over with every Dhan and Telegram
  variable unset and with IP socket creation, `getaddrinfo` and
  `create_connection` all raising.
- **The test fixture contains no credential.** `test_the_fixture_contains_no_credential_or_account_identifier`
  scans it, and the Block 2 capture script scrubs payloads field by field and then
  refuses to write a file containing a credential-shaped key or the client id.

**Re-confirmed again for Phase 10** (13 August 2026), against the full suite:
**1999 passed, 16 skipped, 0 failed** (`pytest -o addopts=""`, exact counts —
zero unexplained failures, both previously-known issues fixed this phase:
see the regression-fixes bullet in the Phase 10 subsection above); `ruff
check .` clean; the **exact literal** `mypy common strategies runtimes
dashboards scripts` clean (173 source files, zero errors — bare `mypy` was
not treated as a substitute); `scripts/assert_no_live_config_committed.py`
confirms zero live-enabling value anywhere in `config/`.

- **`DhanLiveBroker` now exists and is code-complete**, but has never made a
  real Dhan network or order-placement call. Every test against it
  (`tests/unit/test_dhan_live_broker.py` and everywhere else it is exercised)
  uses a mock/fake `DhanOrderClient` — `dhanhq`'s real HTTP transport is
  never invoked. `common/broker/dhan_live.py` depends only on
  `DhanOrderClient`, a structural `Protocol` — it does not import `dhanhq`
  itself. `common/market_data/dhan.py` therefore **remains the only module
  importing the SDK**, unchanged from Phase 2, still proven by the same
  `test_only_the_dhan_adapter_imports_the_sdk` (its assertion —
  `importers == {"common/market_data/dhan.py"}` — is a single-element set
  and was not relaxed for Phase 10; it still passes because nothing new
  imports `dhanhq` directly).
- **`build_broker()` constructs a real `DhanLiveBroker` only when every gate
  in the AND-chain is true, including a fresh, passed live preflight** —
  and every committed config value keeps at least one of those false, so
  this path is unreachable from any committed configuration today. Proven
  with a fake `LiveBrokerDependencies` (fake order client, fake egress IP
  provider, fake broker-connectivity check) — never real credentials.
- **The raw Dhan client ID is never persisted or logged anywhere in Phase
  10's new tables.** Every `account_key` column stores only the derived
  HMAC-SHA256 digest (`common.broker.live_preflight.derive_account_key`);
  `account_identity_pepper` is added to both `_SENSITIVE_KEYS` and
  `secrets_from_settings()`'s redaction list, defense-in-depth even though
  its own value is never echoed anywhere.
- **`reconciliation_mismatches.detail` and `audit_events.detail` are now
  redacted before every `INSERT`** (`common.logging.redact_for_persistence`),
  closing the one persistence path Phase 10's own audit found that a logging
  handler's `SecretRedactingFilter` cannot reach — proven by
  `tests/unit/test_logging_redaction.py`'s Phase 10 additions, which insert
  a deliberately secret-shaped string through both real call sites and read
  the row back redacted.
- **No committed configuration value was changed to enable live trading.**
  `global.yaml`, every `config/runtimes/*.yaml` and every
  `config/strategies/*.yaml` file are byte-identical in every live-enabling
  field to their Phase 9 state — confirmed by `git diff` showing no change
  to `config/` on this branch, and now additionally enforced structurally by
  `scripts/assert_no_live_config_committed.py`, which this session ran
  clean against the real tree.
- **`OPERATIONAL LIVE ACTIVATION ELIGIBLE: NO — BLOCKED.`** Static-IP
  resolution, an approved `EgressIpProvider`, and a `live_confirmations`
  operator workflow are still outstanding (known limitations 35 and 37),
  and the known-limitation-18
  auth-token-invalidation fix remains unresolved (limitation 38) — none of
  these block Phase 10 code-completeness, and all of them block operational
  activation.

**Correction, same day (13 August 2026).** The bullet above — "none of
these block Phase 10 code-completeness" — was wrong, and so was this
section's original `PHASE 10 CODE COMPLETE: PASS` verdict. A follow-up
review, prompted by a direct question about `check_static_ip`'s real call
sites, found that `run_live_preflight`/`LivePreflightGate.ensure_fresh`
are never called from `common/` or `runtimes/` outside tests — confirmed
by `grep` for `ensure_fresh(` and `run_live_preflight(` across both trees.
The real CLI entrypoint, `runtimes/intraday_options/__main__.py::main()`
(the one actually run via `if __name__ == "__main__":`), calls
`build_supervisor(...)` **without** `live_preflight_passed_for`, leaving
it at its `None` default. This is not the same claim as "the live path is
gated shut by config," which remains true and unaffected — it is a
separate, additional finding: **the gate itself has a hole that config
values cannot see or close**, because one of its required checks cannot
run at all, under any config. Recorded as known limitation 39, with the
two concrete design decisions that block closing it. `DhanLiveBroker`
existing, being fully mocked in its own tests, and being unreachable from
committed config all remain true and are not retracted — only the
"Phase 10 is fully code-complete" conclusion was at that commit. A subsequent
hardening review closed limitation 39 with both parent admission and forced
worker-local/TTL revalidation; see the current Phase 10 section below.

---

## 8. Next phase

Phase 2 is complete, both blocks. **Phase 3 is complete** — all five parts, with its
acceptance gate met in full. **Phase 4 is complete** — all five parts, its one live
gate item run and passed. **Phase 5 is complete** — see below. **Phase 6 is
complete** — all five parts, see below. **Phase 7 is complete — all five parts**,
see below. **Phase 8 is complete**, see below. **Phase 9 is complete** — the first
(and, per CLAUDE.md, only) real strategy, `ema_cross_9_21_buy`, is fully
integrated end to end through the ported engine, paper only — see below.
**Phase 10 controlled-live code is complete and remains fully disabled.** A
hardening review on 13 August 2026 closed the earlier production preflight
wiring gap: the parent performs admission preflight only when all static gates
would otherwise pass, the child independently forces a fresh check, and every
subsequent broker call crosses TTL preflight and the shared rate limiter. The
same review wired the Dhan trade-book method and live order-update transport,
shared account MTM, broker-authoritative startup reconstruction, disabled-
strategy mode-transition checks, restorable backup validation, and a lifetime
account lease enforcing Phase 10's one-live-strategy rollout. This remains
code-level fixture/mock evidence only. **Operational activation is separately
blocked** by the 30-day paper evidence, a second genuine paper strategy,
strategy-specific approval, static-IP/provider setup, live auth revalidation,
and an explicit decision to change any committed gate. **The read-only
dashboard was rewritten into a unified, tabbed application on 14 August
2026** — see below; this is a dashboard-scope addendum, not a live-gate
change, and every committed live gate remains exactly as disabled as
before it.

### Dashboard — unified Home/Intraday Options/Positional Options/Intraday Stocks/System Health (14 August 2026)

Phase 7 Part 3 shipped one Master tile, one per-strategy page and one
System Health page. This addendum grew that into the full information
architecture a later spec pass asked for — five top-level pages (Home,
Intraday Options, Positional Options, Intraday Stocks, System Health), each
category page tabbed rather than flat — without weakening any of Part 3's
guarantees and without touching `common/execution/`, `common/health/
snapshot.py`, any runtime/strategy code, or the schema.

**Architecture.** A new `dashboards/data/` package holds every typed
read-model query (`account.py` — reconciliation/account-wide/live-gate
matrix, moved out of `app.py` unchanged; `intraday_options.py` — overview,
live positions, orders/fills, closed trades (entry/exit price and side
derived structurally from fills via `positions.entry_correlation_id`, never
string-parsed), performance metrics, strategy comparison, signals;
`calendar_stats.py` — trading-day/30-day-rollup math; `incidents.py` —
active-vs-resolved classification; `positional.py`/`stocks.py` — typed
dataclass shells for runtimes that do not exist yet). Every function takes
an already-open `connect_readonly` connection, mirroring
`common.health.snapshot.read_snapshot`'s own convention; `dashboards/
_shared.py` gained `run_bounded()` to generalise the missing/locked/pre-
migration handling every query needs. Pages stay thin: `load_*` returns a
dataclass, `render(streamlit, data)` takes the module as a parameter (every
test drives a fake one), `main()` imports Streamlit lazily. The existing
`pages/` multipage convention was kept (a working, tested mechanism);
`st.page_link` gives Home its three clickable category cards — each
resolves its own `config/runtimes/<id>.yaml` rather than a hardcoded
runtime id, so a future real `positional_options`/`intraday_stocks` runtime
lights its card up with no dashboard code change.

**Two deliberate non-changes, called out rather than papered over.** (1) No
schema migration and no runtime instrumentation were added. Current
mark/unrealised P&L for paper positions and structured option contract
metadata (strike/expiry/CE-PE) are not persisted anywhere; `ema_cross_9_21_buy`
trades the NIFTY index directly (`security_id: "13"`), not an option
contract, so contract metadata is inapplicable to it, not merely
unpersisted. Both render as "—" with an explanation rather than being
derived from a stale entry price. (2) `positions` holds one row per
(strategy, mode, trading date, security) identity with no separate
append-only trade ledger: if a strategy round-trips the same security
twice on the same day, reopening overwrites the earlier closure's `status`
back to `OPEN` and Closed Trades/Performance for that day shows only the
currently-closed state, not every historical round trip. Confirmed against
a real seeded fixture during manual verification (a BUY→SELL→BUY sequence
on one trading date left `positions` with a single OPEN row, `realised_pnl`
still correctly carrying the first round trip's P&L into the daily
realised-P&L total, but Closed Trades reporting zero rows for that day) —
a genuine persistence-model characteristic, not a dashboard defect, and out
of scope to change here (no runtime/schema edits without separate
approval).

**Active vs. resolved incidents** (the spec's named complaint —
`common.health.snapshot.RecentError`'s own docstring records the real
14 August 2026 incident this traces to) are now separated without new
schema: `dashboards/data/incidents.classify_incidents` marks an error
"active" only when it is the most recent recorded error for its
(strategy, component) pair *and* that component's current health signal
(`broker.healthy`, `database.integrity_ok`, feed subscriptions-match/
last-event, a strategy's current `health_state`) is still unhealthy;
System Health shows the two lists separately, both always timestamped.

**Read-only/live-safety proof unchanged in kind, extended in scope.** The
three AST-based import-boundary checks in `tests/unit/test_dashboard.py`
(no broker/feed import, no top-level `streamlit` import outside `pages/`,
no write-capable `Database` import) now walk `dashboards/**/*.py`
recursively rather than the five original files, so the new `data/`
package and `formatting.py` are covered by the same guarantee. A new
`AppTest`-based smoke suite (`tests/unit/test_dashboard_apptest.py`) runs
every page through the real Streamlit runtime end to end against a
throwaway `PROJECT_ROOT` and found one real bug no fake-streamlit test
could have: `st.page_link` resolves its path relative to the entrypoint
script's own directory, not the repository root — `dashboards/app.py`'s
category links were wrong (`dashboards/pages/...` instead of `pages/...`)
until this suite caught it. The same suite proves no page writes to the
database (row counts identical before/after loading every page for real).
Manual verification: a throwaway project root was seeded with a realistic
paper round trip via the real `OrderLifecycle`/`PaperBroker` write path,
`streamlit run dashboards/app.py` was started on a non-default port
(never 8501/8511 — a pre-existing, independently-started dashboard process
on 8511 was left untouched throughout), every page returned HTTP 200 with
an empty server log, then the verification process was stopped; no order
was placed and no runtime was started or stopped.

**Tests added**: `test_dashboard.py` (safety suite, recursive), `test_
dashboard_read_models.py` (pure calendar/metrics/incident/formatting
calculations), `test_dashboard_intraday_options_data.py` (DB-backed, real
`OrderLifecycle` fixtures), `test_dashboard_intraday_options_page.py`
(render tests, all eight tabs), `test_dashboard_home.py` (multi-runtime
aggregation, a missing-database card degrading only itself, paper/live P&L
never combined), `test_dashboard_system_health.py` (the active/resolved
incident scenario directly), `test_dashboard_positional_and_stocks.py`,
`test_dashboard_apptest.py`. `test_dashboard_account_wide.py`/`test_
dashboard_reconciliation.py` (Phase 10's own tests) were updated to import
from their new home in `dashboards/data/account.py`, logic unchanged.
`tests/end_to_end/test_walking_skeleton.py`'s dashboard gate test was
updated the same way (`load_master`/`RuntimeCard` retired in favour of the
snapshot read every page now shares).

### Dashboard — strategy drill-down and durable trade ledger (14 August 2026, second corrective pass)

A follow-up request the same day: the unified dashboard above had a
Strategy Comparison table but no way to scope any other tab to one
strategy, and — surfaced during that pass's own manual verification — its
Closed Trades/Performance numbers were re-derived from `positions`+`fills`
at read time, a model that silently loses a round trip's own detail the
moment its `(strategy, mode, day, security)` identity reopens after
closing (a real BUY→SELL→BUY sequence left `positions` back in `OPEN`
status, so Closed Trades reported zero rows for a day that had already
booked a real ₹930 profit — `positions.realised_pnl` kept accumulating
correctly throughout; only the per-trade *record* was lost). Fixing that
required this project's first new runtime instrumentation since Phase 7:
migration `0008_trade_ledger.sql` adds `trade_ledger`, an append-only
durable record — one row per realising fill — written by
`ExecutionRepository._upsert_position` inside the exact same transaction
that already computes each closing fill's realised P&L. Alongside it, a
long-standing latent bug in that same method was fixed: `positions.
opened_at`/`entry_correlation_id` were never reset when a fully-closed
identity reopened, so a second round trip silently inherited the first
entry's timestamp and correlation id forever — bookkeeping only, no
entry/exit/risk rule changed, verified by
`tests/integration/test_trade_ledger.py`'s reopen-regression tests and
confirmed visually against a real seeded reopen scenario (two independent,
correctly-priced Closed Trades rows) during manual verification. `trade_
ledger` joined `common.retention.policy.NEVER_PURGED_TABLES` — a trading
record, never age-purged, like `orders`/`fills`/`positions`/
`order_intents`. `dashboards/data/intraday_options.py`'s `load_closed_
trades` now reads `trade_ledger` directly (one query, exact prices,
`ClosedTradeRow`'s shape unchanged) instead of re-deriving from fills;
`ComparisonRow` gained `execution_mode` so a strategy that has ever run in
both modes gets two independent rows, never blended.

**The reusable strategy selector** the request asked for lives in the new
`dashboards/data/strategy_scope.py`: `discover_strategy_options` unions
config strategies (reusing `dashboards.data.account._raw_strategy_files`),
current `runtime_heartbeats` strategy ids, and every historically-active
strategy id across `trade_ledger`/`signals`/`order_intents`/`positions` —
so a strategy is selectable the moment it is configured, running,
disabled, or merely has history, labelled Running/Stopped/Disabled/
Historical only. `render_strategy_selector` is one `st.selectbox` over raw
strategy ids with a `format_func` label, so `st.session_state[key]` always
holds exactly the resolved id — both Streamlit's own natural persistence
across tab switches and reruns, and the same key the Strategy Comparison
tab's row click (`st.dataframe(..., on_select="rerun", selection_mode=
"single-row")`, supported natively by the pinned Streamlit 1.60) writes to
directly to change the active strategy. `dashboards/intraday_options.py`
places one selector below the title and above the tabs, threading the
resolved strategy id through Overview, Live Positions, Orders & Fills (new
Mode filter), Closed Trades/Performance (existing date range, new Mode
filter), Signals & Events and Health (via `dataclasses.replace` on
`SystemHealthView`'s `strategy_pids`/incident lists — `system_health.py`
itself untouched, so the standalone System Health page still shows every
strategy). Strategy Comparison keeps its own independent "Compare
strategies" multiselect, defaulting to every available strategy. Overview
gained a Configuration summary section for the selected strategy (`config/
strategies/<id>.yaml`, any key matching `secret|token|password|pin|api_key`
replaced with `"REDACTED"` — defence in depth; no committed strategy YAML
has ever held a real secret). Two more real "avoid clipped values" bugs
were found and fixed during this pass's own manual verification against
seeded multi-strategy data: Overview's "Health" metric (`RUNNING_PAPER`
clipped to `RUNNING_P...` in a quarter-width column — now a full-width
colored `health_badge()` markdown line instead of a boxed metric) and, in
the prior pass, Home's "Last refresh" and Overview's "Square-off" metrics
(already fixed then, confirmed still correct here).

`dashboards/positional_options.py`/`intraday_stocks.py` gained the same
selector, deliberately wired with `conn=None, config_root=None` — showing
nothing today — because `config/strategies/*.yaml` carries no real
per-runtime membership mechanism (the same single-runtime limitation
`common.config.discover_enabled_strategies` already documents), so
attributing `intraday_options`'s own strategies to a stub runtime would be
fabricated, not prepared, capability.

**Tests**: `tests/integration/test_trade_ledger.py` (full-close row
correctness, idempotent replay, the reopen regression, paper/live
separation, entry/exit charge reconciliation against `positions.charges`
for the non-scaling case); `tests/unit/test_dashboard_strategy_scope.py`
(all four status labels, filter composition, CSV-scope proof);
`tests/unit/test_dashboard_apptest.py` gained real-Streamlit coverage of
the selector filtering Overview, surviving an unrelated rerun, and the
comparison multiselect's default; `test_migrations.py`/`test_retention.py`
updated for migration `0008`/the widened `NEVER_PURGED_TABLES` set. Full
test suite, ruff and mypy (strict) pass; every committed live gate remains
disabled and `scripts.assert_no_live_config_committed` still passes — this
pass touched `common/execution/repository.py`'s write path but changed no
entry/exit/risk decision, only what gets persisted for later reading.

### Phase 10 — Controlled live readiness — **CODE COMPLETE; operational activation blocked**

Built on branch `phase-10-controlled-live`, under an explicit two-stage
approval (Stage 1: read-only inspection and a written design, rejected twice
with detailed numbered corrections before approval; Stage 2: "APPROVED —
PROCEED WITH STAGE 2" on 13 August 2026, with explicit constraints: manual
edit approvals, every committed live gate stays disabled, mocks/fakes only,
no real Dhan network or order-placement call, no push, no merge). Scope is
**infrastructure**, not an operational go-live: `ema_cross_9_21_buy` (Phase
9's only real strategy) and its rules/acceptance matrix are unchanged; there
is no placeholder production strategy and no strategy-specific branch in
`TradingEngine`.

**What exists now, all tested with mocks/fakes, never a real Dhan call:**

- **`DhanLiveBroker`** (`common/broker/dhan_live.py`) — the full operation
  set (submit, modify, cancel, order/correlation lookup, order book, Dhan
  trade book and positions). Submission-failure
  classification is evidence-gated: only a genuine `success` with an
  `orderId` resolves definitively; every `failure` becomes `UNKNOWN` until a
  positive `get_order_by_correlationID` confirmation — never inferred from
  response shape (a malformed HTTP error body and a bare transport exception
  produce the same string-shaped `remarks`, confirmed by reading the SDK
  transport source directly). `OrderStatus.EXPIRED` is represented
  explicitly (confirmed real via Dhan's own docs), via a reviewed-destructive
  SQLite rebuild migration (`versions/0006`).
- **Account identity and risk** (`common/broker/live_preflight.py`,
  `common/risk/account_reservations.py`, `common/risk/account_risk.py`) — a
  stable, non-secret `account_key` derived via HMAC-SHA256 from the
  authenticated Dhan client ID and a per-installation pepper
  (`Settings.account_identity_pepper`, `.env`-only) — never an operator-typed
  alias, and the raw client ID is never persisted or logged. Account risk is
  **reserve-before-submit**: one atomic `BEGIN IMMEDIATE` transaction checks
  existing reservations plus open positions plus the proposed order *before*
  any broker call, across a directed state-machine graph (`RESERVED →
  SUBMITTED → ACKNOWLEDGED → ... `) where `UNKNOWN` has no legal exit except
  `RECONCILED` — enforced in Python, not merely by a `CHECK` constraint.
  Realised P&L is an idempotent append-only event ledger; unrealised MTM is
  overwritten per mark, never appended (the double-counting bug the first
  rejected design had). Entry-only trust failures never trap an existing live
  position: a same-security, opposite-side order no larger than the locally
  tracked open quantity is marked risk-reducing, receives an auditable
  zero-capital reservation, and may cross the guarded call path even while
  provenance/MTM/confirmation readiness blocks new exposure. It still crosses
  the shared rate limiter, and any ambiguous outcome still becomes `UNKNOWN`
  and requires reconciliation.
- **Live order rate limit — real number, real margin, cited.**
  `RateLimitRule.limit`/`.window_seconds` are operator-configured with no
  baked-in default (an unconfigured call class stays zero-permit, never
  unlimited), but were previously undocumented against Dhan's own actual
  limit — closed this pass. **Dhan's current documented Order API limit is
  10 requests/second**: `https://dhanhq.co/docs/v2/releases/`, Version 2.3
  (Monday 08 Sep 2025) — verbatim: *"Along with this, we are changing rate
  limit for Order APIs to 10 order per second, in accordance with
  regulations."* Cross-confirmed by
  `https://dhan.co/support/platforms/dhanhq-api/what-are-the-api-rate-limits-for-dhan/`:
  *"Order APIs: Up to 10 requests per second."* An older `dhanhq.co/docs/v1`
  Freshdesk support article (dated 6 Feb 2024, pre-dating the Version 2.3
  regulatory change) states 25 requests/second for the v1 API — superseded,
  not used. The recommended configured value, documented on
  `RateLimitRule` itself and used by the mixed-mode fixture
  (`tests/end_to_end/test_mixed_mode_live_readiness.py`), is `limit=5,
  window_seconds=1` — a 50% margin below the documented 10/second, sized
  specifically against this limiter's own fixed-window mechanic
  (`common.broker.live_rate_limiter._floor_to_window`): a fixed window can
  legally admit `limit` requests at the tail of one window and another
  `limit` at the head of the next, so the true worst-case burst near a
  boundary is `2 × limit` within a short real span. At `limit=5` that
  worst case is exactly 10 — Dhan's own ceiling, not over it — leaving
  nothing assumed for clock skew between this process's `datetime.now()`
  and Dhan's own enforcement clock. `window_seconds=1` (matching Dhan's own
  per-second granularity) is load-bearing here: a larger window at a
  proportionally larger limit (e.g. `limit=300, window_seconds=60` for the
  same 5/s average) widens the real-time span that `2 × limit` worst case
  can land within and does **not** carry the same guarantee.
- **Shared account database** (`data/operational/dhan_account_shared.db`,
  `common/persistence/account_shared.py`, migrated by the same
  `MigrationRunner` machinery, its own `versions_dir`) — one database shared
  by every live worker across every runtime group authenticated to the same
  account, so a shared limit cannot be bypassed by using a different
  `runtime_id`. A missing/recreated/empty database is never interpreted as
  zero exposure: `live_account_state_provenance` defaults every unseen
  `account_key` to `never_reconciled`, and only a full rebuild across every
  configured runtime group (`common/reconciliation/account_rebuild.py`, its
  own `filelock`) flips it to `reconciled`; every worker's preflight and
  every reservation attempt re-checks this row and blocks account-wide, not
  per-worker, on anything else.
- **Reconciliation** (`common/reconciliation/`) — the full mismatch taxonomy
  (`MATCHED` through `DUPLICATE_CORRELATION`) and a narrow, spec-bounded set
  of permitted automated resolutions (adopt-by-correlation-ID, update traded
  quantity from a confirmed fill, mark rejected/closed only on positive
  broker evidence — never an automatic flatten). `ReconciliationRunner`
  persists every run to `reconciliation_runs`/`reconciliation_mismatches`
  (migration `0007`) and never silently deletes a local record.
- **Mode-transition safety** (`common/execution/mode_transition.py`) —
  `paper → live` checks open paper positions across **all trading dates**,
  not just today; `live → paper/disabled` requires a broker and a
  reconciliation runner and runs a **fresh** reconciliation (never cached),
  refusing if either dependency is missing or the run finds a critical
  mismatch or an `UNKNOWN` order.
- **Static-IP preflight** (`common/broker/live_preflight.py`) — three
  independent facts (configured expected IP, Dhan-registered IP,
  independently observed current egress IP via an injectable
  `EgressIpProvider`), not a whitelist-only check. Shipped default
  `egress_provider=None` fails **closed** (see known limitation 35).
- **Production preflight and update wiring**
  (`common/broker/dhan_preflight.py`, `common/broker/live_call_guard.py`,
  `common/broker/order_update_consumer.py`,
  `runtimes/intraday_options/live_runtime.py`) — the real CLI supplies the
  parent admission callback; the worker forces its own fresh check after
  taking its process/account locks and revalidates on every guarded Dhan call.
  A reconnecting, stoppable Dhan order-update websocket is the primary
  settlement signal; one correlation lookup is the bounded fallback, and an
  ambiguous submission is never resubmitted.
- **Worker wiring** — `runtimes/intraday_options/worker.py`'s
  `resolved_config_stub` (which hard-coded `GlobalConfig(live_trading_
  enabled=False)` regardless of real config, found during Stage 1) is
  replaced by `resolved_config_from_worker`, which builds a real
  `ResolvedConfig` from the worker's actual fields — the fix that lets a
  genuinely-`true` config value actually reach `effective_live_gate`, proven
  by `tests/unit/test_worker_resolved_config.py`. `__main__.py` calls
  `check_mode_transition_safety` for enabled and newly-disabled strategies
  before adding a worker. A per-account lifetime lease enforces the approved
  one-live-strategy rollout until multi-live startup enumeration is separately
  designed; shared cross-process limit/risk machinery remains generic.
- **Dashboards** — `dashboards/app.py`'s Master page reads real
  `reconciliation_runs` status (replacing the old "Not implemented"
  placeholder) and an account-wide risk/rate-limit section reading
  `dhan_account_shared.db` read-only — reconciliation status, daily P&L,
  open-position capital, reserved capital and current-window order-rate
  count, per `account_key`. Both stay within the existing AST-enforced
  import boundary (no broker, no feed, no write-capable `Database`).
- **Fixture-based mixed-mode acceptance**
  (`tests/end_to_end/test_mixed_mode_live_readiness.py`) — a paper-designated
  and a live-designated fixture strategy (never `ema_cross_9_21_buy`, never a
  placeholder production strategy) in the same runtime group, through the
  real `build_supervisor`/`discover_strategies` config path: with
  global live disabled, the live strategy is blocked outright, never
  rerouted to paper, and leaves no trace in any trading table; the paper
  strategy trades normally with its own `p_`-namespaced correlation IDs.
- **Cross-process, cross-runtime-group proof** — real
  `multiprocessing.get_context("spawn")` processes (not threads, not
  in-process objects), two simulated runtime groups sharing one account,
  prove the order-rate limiter and the risk reservation cap hold at the
  *account* total, not per-group
  (`tests/integration/test_account_wide_coordination_across_runtime_groups.py`).
- **Persist-time redaction** — `reconciliation_mismatches.detail` and
  `audit_events.detail` are free-text columns a logging handler never sees;
  `common.logging.redact_for_persistence` (backed by the same
  `SecretRedactingFilter` a worker's logging setup already installs) runs on
  both before the `INSERT`. Today's content is entirely computer-generated
  (no secret has ever flowed through either column) — this is deliberate
  defense-in-depth against a future call site, not a fix for a
  currently-exploitable leak. The confirmation writer now applies the same
  rule to operator/actor/reason evidence (known limitation 37 closed).
- **`scripts/assert_no_live_config_committed.py`** — a read-only CI/commit-
  time guard, independent of the runtime gate chain, that refuses a
  live-enabling value anywhere in `config/`: `global.live_trading_enabled`,
  `runtime_defaults.live_execution_allowed`/a runtime file's own
  `live_execution_allowed`, `strategy_defaults.mode`/`.live_approved`, or any
  individual strategy file's `mode: live`/`live_approved: true` — checked
  regardless of that strategy's `enabled` flag, since the rule (CLAUDE.md,
  verbatim) forbids `mode: live` in committed YAML outright.
- **Regression fixes found and closed along the way**: a date-sensitive test
  pinned wall-clock time via monkeypatch instead of assuming today stays
  before a fixture's hardcoded expiries forever
  (`test_engine_worker_contract_resolution.py`); a flaky comparative-timing
  retention test replaced with a direct `EXPLAIN QUERY PLAN` assertion
  against the two specific databases under test, keeping only an absolute
  wall-clock ceiling as a secondary guard; `dashboards/__init__.py` and
  `scripts/__init__.py` (both empty) close a real mypy "found twice under
  different module names" collision between the exact `mypy common
  strategies runtimes dashboards scripts` command and this project's own
  `import dashboards.intraday_options`/`import scripts.auth_bootstrap` test
  patterns — confirmed not to break Streamlit's own file-path-based `pages/`
  discovery (a live `streamlit run` smoke test still served the page and
  passed its health check with `dashboards/__init__.py` present), and not to
  break either directory's own AST-enforced "what files actually live here"
  guard test (both new files are explicitly excluded from those globs, the
  same way `dashboards/_shared.py`'s own directory-contents test already
  excluded `__init__.py`).

**What Phase 10 deliberately did NOT deliver** (operational preconditions,
not code-path omissions): a chosen production `EgressIpProvider`, a second
specified paper strategy, 30-day paper evidence, or live revalidation of the
known-limitation-18 fix. Missing inputs all block closed.

**Correction closed by the hardening review:** the earlier report's static-IP
production-call-site criticism was valid at commit `3de9602`. The real CLI now
supplies parent admission preflight and the worker independently revalidates at
startup and by TTL before guarded Dhan calls. No provider was guessed: a strict
configured `module:attribute` seam loads an operator-approved implementation,
and absent/malformed/raising providers block closed.

**No committed configuration value was changed to enable live trading** —
every gate remains exactly as fail-closed as it was at the end of Phase 9,
now with a CI-time guard enforcing it; limitation 39 is closed independently.
`OPERATIONAL LIVE ACTIVATION ELIGIBLE: NO — BLOCKED.`

**Post-completion fix (14 August 2026): `dashboards/app.py` failed every real
`streamlit run` with `ImportError: attempted relative import with no known
parent package`.** Reported via a screenshot of the Master page. Root cause:
`dashboards/app.py` is the one module in the `dashboards` package that
Streamlit itself executes directly as the entry point — its ScriptRunner
installs a fake `__main__` module and `exec()`s the compiled script into it,
never importing `dashboards.app` as a package member — so its module-level
`from ._shared import ...` (present unchanged since Phase 7 Part 3,
`9a88cdae`) had no parent package to resolve against. `dashboards/pages/*.py`
already carries the fix for exactly this constraint (a `sys.path` fix-up
walking up to `pyproject.toml`, then an absolute `dashboards.*` import) —
`app.py` was the one entry point that never got it, because it is imported
normally (as `dashboards.app`) by every test in `tests/unit/test_dashboard.py`,
which masked the bug from the whole suite (45 tests, all green) and from the
AST-only "no broker/feed" checks (which parse the file, never execute it).
The Phase 7 Part 3 runbook entry's live verification (`streamlit run
dashboards/app.py`, HTTP 200, empty server log) did not catch it either:
Streamlit serves the static app shell with a 200 regardless of a script
error — the exception is pushed to the browser over the websocket after a
session connects and run, exactly the in-page red traceback box the
screenshot showed, not a startup failure a bare `curl` against `/` or a
server-log grep would ever see. Fixed the same way the `pages/*.py` shims
already do it: `app.py` now carries the identical `sys.path` fix-up ahead of
one `from dashboards._shared import ...` absolute import (`# noqa: E402`,
matching the shims' own exception). Verified three ways: `runpy.run_path(...,
run_name="__main__")` — the same execution shape Streamlit's own
`ScriptRunner` uses — now runs past the import into the page itself instead
of raising; a real `streamlit run dashboards/app.py` server was started and
its log grepped for `error`/`traceback`/`exception` (none, where the
pre-fix log would show the exception once a session actually connected);
`tests/unit/test_dashboard.py` (45 passed, 4 pre-existing skips), ruff, and
mypy all still pass unmodified. No behavior change for every other page —
they were never affected, since Streamlit only ever imports them (via the
`pages/*.py` shims), never `exec()`s them as `__main__`.

**Post-completion fix (14 August 2026, same session): System Health's
"Recent errors" panel carried no timestamp, so a resolved incident read as
a live one.** Reported via a second screenshot, taken right after the fix
above, of System Health showing two red error rows ("feed did not finish
within 10.0s...", "a runtime subscription has been waiting 30.2s..."). Both
were real — but a `grep` of `logs/algo_trading.log` timestamped them at
10:19:18 / 10:23:20 IST, and the running worker (PID 20696, started 14:02:58,
after both of that morning's feed fixes — `a36b606`, `bcd2d5a`) had been
evaluating 5-minute bars cleanly with zero new errors straight through to
14:15. `common.health.snapshot._recent_errors` selected only `message`, never
`occurred_at`, from the `errors` table (indexed on exactly that column,
`idx_errors_recent`, but the column was never read) — so the panel had no way
to distinguish a four-hour-old, already-fixed incident from one happening
now. `HealthSnapshot.recent_errors` changes type from `tuple[str, ...]` to a
new `tuple[RecentError, ...]` (`message` + `occurred_at`, exported from
`common.health` alongside every other read-model dataclass this layer
already returns); `_recent_errors` now selects both columns. Every consumer
— `dashboards/system_health.py`, `dashboards/app.py`'s Master page, and
`scripts/status.py` — renders `f"{occurred_at} — {message}"` instead of the
bare message; `scripts/status.py --json`'s `dataclasses.asdict(snapshot)`
serializes the nested dataclass with no code change. Verified directly
against the real, running database
(`read_snapshot(connect_readonly(...), runtime_id="intraday_options",
trading_date="2026-08-14"))`: both rows now carry `occurred_at` timestamps
(`2026-08-14T04:49:18`/`04:53:20+00:00`, i.e. the same 10:19/10:23 IST
incident), confirming an operator looking at the dashboard right now would
see the age and not mistake it for a live emergency. `tests/unit/
test_dashboard.py` and `tests/unit/test_health_snapshot.py` updated for the
new type (`test_recent_errors_respects_the_limit_and_most_recent_first` now
asserts `occurred_at` is populated, not just message order); full suite,
ruff, and mypy all pass.

### Phase 9 — Real strategies — **Complete**

Scope, exactly as approved: implement `ema_cross_9_21_buy` — NIFTY 5-minute
EMA(9)/EMA(21) crossover, ATM weekly CE/PE, BUY-only, intraday — as the single
Phase 9 acceptance strategy, reusing the preserved engine end to end, PAPER
only. It is the **first and only** real strategy this phase delivers; no other
strategy exists in this repository. Full functional/design spec:
`strategies/intraday_options/ema_cross_9_21_buy/ema_cross_9_21_buy_spec.md`.

**Blocking decision resolved with the operator (spec section 12.1).** The 3%
daily-loss cap's capital base was not derivable from the repository (no
existing authoritative value; `EngineConfig.starting_capital`'s ₹1,00,000 is a
generic engine default, not a strategy-specific one) — asked rather than
guessed, per CLAUDE.md and the approved brief. Answer: **₹10,00,000**, so
`daily_max_loss` = 3% × ₹10,00,000 = **₹30,000**, evaluated on live MTM
(realised so far + the open position's unrealised P&L) on every option tick.
`config/strategies/ema_cross_9_21_buy.yaml`'s `parameters.capital_base` /
`.daily_max_loss_pct` carry this; the %→₹ conversion and the per-tick
evaluation are not strategy code at all — both already lived in
`TradingEngine._build_daily_guard` / `_on_option_tick`
(`common.engine.daily_guard.DailyRiskGuard`) before this phase.

**Architecture reused without duplication** — confirmed by inspection before
writing anything, per the approved brief's own gate: `common.engine.strategy.
BaseStrategy` + `@register_strategy`; `common.indicators.ema.EMA` +
`ConfirmedCrossover`; `common.exit.combined_candle_exit.CombinedCandleExit`
(`momentum_low_or_highest_close`); `common.engine.daily_guard.DailyRiskGuard`
(fully engine-wired already — the strategy touches none of it);
`common.engine.selection.OptionSelector` + `common.market_data.scrip_master`
(lot size and weekly expiry resolved from the exchange at runtime — never
hardcoded); `common.engine.session.MarketSession` /
`common.engine.square_off` (entry window, cutoff, mandatory 15:15 square-off
— all engine-owned; the strategy never checks a clock); the Phase 6 Part 2
restart-recoverable exit-state snapshot seam
(`exit_state_snapshot`/`restore_exit_state`).

**New common-layer components — small, generic, none EMA-specific:**

- `BaseStrategy.on_warmup_complete()` (`common/engine/strategy.py`) — a
  no-op-by-default lifecycle hook, called once by `TradingEngine.run()`
  immediately after `_warm_up()` and before the first live tick. Needed
  because `_start_day()` calls `strategy.reset()` *before* warm-up replay,
  and replay drives indicator updates through the same `on_candle()` a live
  candle uses — so a day-scoped detector built on an indicator (here,
  `ConfirmedCrossover`, which latches "confirmed side") would otherwise come
  out of warm-up already primed with yesterday's trend. This strategy's
  `on_warmup_complete()` resets *only* the crossover detector, leaving the
  (session-spanning, intentionally warmed) EMAs untouched — the one invariant
  the spec's "fresh intraday crossover" requirement reduces to. No
  EMA-specific branch exists in the engine; the hook is generic.
- `CombinedCandleExit.on_gap()` + `.snapshot()`/`.restore()`
  (`common/exit/combined_candle_exit.py`) — the exit engine had no gap hook
  and no restart snapshot at all before this phase (verified against
  `tests/unit/test_exit_engines.py`, which covers `trailing`/`highest_close`/
  `consecutive_reversal` but not this one). `on_gap()` suppresses the
  momentum leg for exactly the next `should_exit()` call (the first premium
  candle after a skipped bucket cannot compare across the hole) while leaving
  the best-close trail's extreme/activation untouched; `.snapshot()`/
  `.restore()` persist that trail state (delegating to the child
  `HighestCloseExit`'s own snapshot) for restart recovery, exactly mirroring
  every other exit engine's Phase 6 Part 2 contract.
- `common.engine.risk_managers.HardStopRiskManager` (`hard_stop`) — the first
  concrete `RiskManager` this repository ships (the ABC + registry existed
  since Phase 3 Part 2b-i with "arrives with Phase 9" as its own docstring's
  words). Minimal by design (spec section 7): a disabled-by-default
  catastrophic backstop behind the premium-candle exit, nothing else — no
  `sl_lock_trail`, no target/lock/trail framework. **Deviation from the
  spec's own config sketch, recorded rather than silently diverging**: the
  spec names this threshold `catastrophic_stop_pct` (a percentage of
  premium). That cannot be computed honestly under the existing `RiskManager`
  contract — `on_pnl(pnl)` receives only absolute rupee P&L, and
  `new_position(lots, entry_price=...)` deliberately withholds `lot_size`/
  `quantity` (its own docstring: "lots scales per-lot thresholds"), because
  `lot_size` is exchange-resolved at runtime and CLAUDE.md forbids
  hardcoding it. Recovering a price percentage from a rupee P&L needs
  quantity; inventing one would either hardcode a lot size or require
  widening `RiskManager.new_position`'s signature for every registered *and*
  every test-double risk manager. So `HardStopRiskManager` instead follows
  the contract's own documented idiom: `catastrophic_stop_rupees_per_lot`, an
  absolute rupee floor scaled by the position's own `lots`. Shipped disabled
  (`none`) per the approved brief ("catastrophic stop remains disabled unless
  explicitly configured"); §12.5 (whether/at what level to enable it) was an
  explicitly open spec decision, not a blocking one, and stays open.

**Runtime wiring — the "Phase 9 boundary" `config_adapter.py` recorded for
itself is now closed.** Before this phase, `runtimes/intraday_options/
config_adapter.py::build_worker_config` always left `WorkerConfig.engine`
`None` — its own docstring: "no real strategy exists yet to supply
[engine parameters]". It now builds a real `EngineWorkerConfig` whenever
`parameters.strategy_ref` (a dotted `"package.module:ClassName"`) is present
— not `StrategyConfig.engine` (`EngineKind`), which already defaults to
`TRADING_ENGINE` on *every* strategy, fixture included, and so cannot
distinguish "wants the ported engine" from "carries the single-leg engine's
default label". Absent `strategy_ref`, the Phase 1 fixture path is
byte-for-byte unchanged (`skeleton_fixture.yaml` and every other existing
config keep behaving exactly as before — proven by the untouched
`test_config_adapter.py` suite still passing, plus its one updated
docstring). `lots_per_trade` has exactly one configured home
(`parameters.strategy_kwargs.lots_per_trade`) — it drives both the
strategy's own `quantity_lots` property *and* `EngineWorkerConfig.lots`,
which is what `PositionManager` (and therefore
`OpenPosition.quantity = lots * contract.lot_size`) actually sizes every
order from. **Worth recording for whoever writes the next real strategy**:
`BaseStrategy.quantity_lots` is declared abstract and every engine strategy
must implement it, but nothing in `TradingEngine` itself ever reads it —
`PositionManager._lots` (set once, at construction, from
`EngineWorkerConfig.lots`) is the actual sizing source. That split predates
this phase; `config_adapter.py`'s single `strategy_kwargs.lots_per_trade`
source keeps the two from drifting apart for this strategy, but it is a
pre-existing architecture quirk, not something this phase changed.

**Files created:** `strategies/intraday_options/ema_cross_9_21_buy/
{__init__.py,strategy.py}` (the `EmaCross9x21BuyStrategy` `BaseStrategy`
subclass); `common/engine/risk_managers.py`; `config/strategies/
ema_cross_9_21_buy.yaml`; `tests/unit/test_ema_cross_9_21_buy_strategy.py`
(35 tests); `tests/integration/test_ema_cross_9_21_buy_engine.py` (17 tests,
a real `TradingEngine` over a real `SimulatedFeed`, no monkeypatching);
`tests/unit/test_config_adapter_engine_branch.py` (14 tests).

**Files modified:** `common/engine/strategy.py` (`on_warmup_complete` hook);
`common/engine/engine.py` (one call site); `common/engine/__init__.py`
(exports `HardStopRiskManager`/`get_risk_manager`, registers `hard_stop` on
import); `common/exit/combined_candle_exit.py` (gap-notify + snapshot/
restore); `runtimes/intraday_options/config_adapter.py` (the engine branch);
`tests/unit/test_config_adapter.py` (one docstring updated to describe the
real discriminator — its assertion is unchanged and still passes).

**Fresh-crossover verification (spec section 4.3, the headline requirement)
— all PASS**, proven with hand-verified `EMA`/`ConfirmedCrossover` sequences
(not asserted from memory — computed against the real classes before being
hard-coded): previous-day bullish continuation → NO entry (the EMAs carry
their exact value across `on_warmup_complete()` unchanged — asserted by
equality — while the detector resets); previous-day bearish continuation →
NO entry; a genuine intraday flip after either → CE or PE BUY respectively;
same-direction continuation never re-signals.

**Exit verification — all PASS:** momentum break fires pre-+4% (isolated:
trail never arms); +4% move activates the best-close trail (exact boundary,
`move_pct >= 4.0`); an 8% retracement fires the trail in isolation (momentum
does not co-fire, by construction of the candle wick); the first premium
candle after a gap cannot fire momentum from before the hole
(`CombinedCandleExit.on_gap()`), while the best-close extreme and activation
both survive the gap untouched, and momentum resumes normally on the next
candle; every actual close clears all premium-exit state
(`exit_state_snapshot() == {}`) and a second trade never inherits the
first's extreme/activation; a stray option tick for a closed contract cannot
book a second exit; a restart of the *same* open trade restores the exact
persisted extreme/activation via `restore_exit_state`.

**Risk verification — all PASS:** live-MTM daily cap trips mid-trade from an
*open* position's unrealised loss alone (`check_open_mtm`, before any
realised close), latches off every later entry for the day (proven against
the same tape that would otherwise reverse PE→CE→PE); a restarted process
carries forward a previously-realised loss (`DailyRiskRecovery`) rather than
re-zeroing, and trips from the correct remaining headroom; one position at a
time is structurally enforced (`PositionManager.open` raises on a second
open — proven live, not just asserted); the entry cutoff and the mandatory
15:15 square-off both hold; a reversal signal arriving after 14:45 closes
the open leg but never opens the opposite one (`can_enter()` gates the
re-entry half only — proven with an exact hand-computed tick timestamp,
14:56, past the 14:45 cutoff).

**Quality gates:** `pytest` — full suite green (one unrelated, pre-existing,
environment-load-sensitive timing test,
`test_retention.py::test_purge_is_dramatically_faster_with_the_index_than_
without`, occasionally falls just under its 5×-speedup assertion when run
concurrently with heavy CPU load elsewhere on the machine; confirmed to pass
cleanly in isolation both before and after this phase's changes, and
`common/retention/` is untouched by this phase — not a regression, not
modified to make it pass). `ruff check` — clean on every file this phase
touched or added. `mypy` (bare `python -m mypy`, honouring
`pyproject.toml`'s `packages =` — not path arguments, which conflict with it)
— `Success: no issues found in 157 source files`.

**Safety, explicitly confirmed:** paper execution only
(`InMemoryGateway`/`PaperBroker` — no network client exists on this path);
`mode: paper`, `live_approved: false` in the shipped config, and
`effective_live_gate()` refuses it directly (proven by test); no real Dhan
order was submitted at any point building or testing this phase; no
LaunchAgent was loaded (`config/runtimes/intraday_options.yaml` stays
`enabled: false`, unchanged by this phase — the strategy config itself is
`enabled: true`, ready for an operator to turn the runtime on, but nothing in
this phase does that); the legacy `Trading_Automation` system was not
started, read, or written to; every Phase 0–8 test still passes unchanged.

**PHASE 9 COMPLETE. READY FOR PAPER FORWARD TESTING. PHASE 10 NOT STARTED.**

### Phase 8 — LaunchAgent validation — **Complete**

Approved plan, verbatim scope: spec section 12's LaunchAgent design (absolute
paths, the project `.venv` interpreter, an explicit working directory, an
environment-file path, independent `stdout`/`stderr` logs, a bounded restart
policy, no restart loop after a deliberate safety shutdown, start only when
the runtime is enabled, never run the legacy and new systems together) —
gated on the phase's own one sentence: "Enable LaunchAgents only after manual
supervisor/worker start, stop, crash, restart, duplicate-worker and
old-system exclusion tests pass." Three decisions were made before building
anything (recorded in the plan, not re-litigated here): author and validate
every plist but load none of them (`intraday_options.yaml` stays
`enabled: false`; Phase 9 loads them); guard the legacy system in code and
document the unload command, but do not run it — the operator's own machine,
the operator's own call; no per-worker respawn inside the supervisor —
`launchd`'s own bounded restart of the whole process is the recovery path
for a crashed worker, not new concurrency logic on the trading path.

**`common/process/legacy_guard.py`** is the "old-system exclusion" gate.
Two independent signals, because either alone can miss it: `launchctl list
<label>` for whether the legacy LaunchAgent is *loaded* (queued to run, even
with nothing currently alive), and a `psutil.process_iter` scan for any live
process whose executable or command line sits under the legacy project root
(`/Volumes/Trading/Trading_Automation`, matched by `Path.is_relative_to` —
never the shared `/Volumes/Trading` mount root this repository also lives
on, which would flag this codebase's own processes as the thing it is
detecting). This was not a theoretical check written against a description:
running it for real, on this machine, during this phase, found the legacy
agent's label (`com.soundarraj.tradingautomation.starttrading` — note the
filename of its plist, `...controller.plist`, does not match its own
`Label`; `launchctl` keys on the label) genuinely loaded, and its
`weekly_strategies` component genuinely running as a live process, both
confirmed independently by `launchctl` and by `ps`. Wired into
`scripts/validate_environment.py` (a new problem, not a warning) and into
`runtimes/intraday_options/__main__.py:main` — checked immediately after the
`enabled: false` gate, before backup, migration or any network call, and
before `--strategy-id` validation, returning a new `EXIT_LEGACY_SYSTEM_
ACTIVE = 5` (**D81**).

**`orchestration/process_control/supervised_launch.py`** is what every
runtime LaunchAgent's `ProgramArguments` actually points at, never
`runtimes.intraday_options.__main__` directly — `launchd`'s own `KeepAlive`
has no attempt cap, only a pacing `ThrottleInterval`. It runs
`scripts.validate_environment` once as a preflight, then the real supervisor
entrypoint, and classifies every exit code: `EXIT_OK`, `EXIT_RUNTIME_
DISABLED`, `EXIT_STRATEGY_NOT_FOUND`, `EXIT_LEGACY_SYSTEM_ACTIVE` and the new
`EXIT_SAFETY_SHUTDOWN` are terminal — stop immediately, whatever the code;
`EXIT_FAILED`/`EXIT_NO_CREDENTIALS` are retried up to `--max-attempts` (default
3) with a fixed backoff, then this process itself exits non-zero. Every
attempt is written to `errors` (`component="supervised_launch"`), not
`audit_events` — that table's `action` column is a closed vocabulary
enforced by a `CHECK` baked into `CREATE TABLE` (migration 0004), and the
additive-only migration runner cannot widen it without a real schema
migration, since it rejects `DROP` and any SQLite `CHECK`-widening rebuild
needs one; a launch attempt is also an automated lifecycle event, not an
operator-issued command, which is exactly what `errors` already models for
`IntradayOptionsSupervisor`'s own lifecycle events (**D82**).

**`runtimes/intraday_options/__main__.py`** gains `EXIT_SAFETY_SHUTDOWN = 6`:
`main()` now returns it when `SupervisorResult.stopped_by_signal` is true —
an operator's `SIGTERM` ended the run deliberately, and `supervised_launch.py`
treats this as terminal, satisfying spec section 12's "no restart loop after
a deliberate safety shutdown." **Scope, stated rather than implied**: this
covers the operator-stop path only. The daily-loss halt and kill switch
(`common/engine/daily_guard.py`) latch new entries off *per worker, inside
the engine* — they do not end the supervisor run, so a tripped kill switch
is not currently reachable through this exit code. Making it so is a
behaviour change to the trading path that belongs with Phase 9's real
strategies, not LaunchAgent validation; see limitation 33.

**`orchestration/launchd/generate_plists.py`** is the one place that knows
the project root, the `.venv` interpreter path and the log directory — the
three committed `.plist` files under the same directory are its output,
never hand-edited, and `tests/unit/test_launchd_plists.py`'s drift guard
fails if they and a fresh generation ever disagree. Only the trading-runtime
plist wraps its program in `caffeinate -i -s` (spec section 13's sleep
prevention), scoped to that process's own lifetime — the auth bootstrap
runs for seconds and the dashboard has no runtime-hours requirement of its
own. `scripts/start_dashboard.py` (`algo-dashboard` console alias) gives the
dashboard plist something to point at — `streamlit run dashboards/app.py`
had no non-interactive entrypoint before this phase.

**`common/retention/logs.py::rotate_launchd_logs`** — a same-phase addendum,
not deferred. `logs/launchd/*.log`'s volume was initially assumed small
enough to leave for a later pass; reconsidered once actually traced —
`console=True` is every production `setup_logging()` call site's own
default, so these files duplicate the entire application log stream, and
`launchd` never rotates or truncates what it appends to. `sweep_logs` cannot
help unmodified — it only ever manages a *rotated* backup, by design, never
an unrotated file a live process might still hold open — so this adds the
missing rename step, run once per controlled startup immediately before
`sweep_logs` is pointed at the same directory, folded into `run_retention`
via a new optional `launchd_log_dir` parameter (`RetentionReport` gains a
matching `launchd_logs` field; omitting the parameter is a no-op, so every
pre-existing caller is unaffected). The one property that had to be checked
rather than assumed — renaming a path out from under a file descriptor
`launchd` (or the very process performing the rename) may still be actively
writing to — was verified directly on this project's own Darwin target
before any code relied on it, and the fail-first pair
(`test_a_launchd_style_file_accumulates_unbounded_without_rotation` /
`test_rotation_then_sweep_makes_the_launchd_log_visible_to_retention`)
demonstrates the gap and its closure with the same file. See **D83**.

**The six manual gate tests — run by hand, real processes, real signals.**
The spec sanctions a fake/recorded feed for exactly this kind of test (§3,
"Use a fake/recorded feed in normal automated tests and an opt-in live-feed
smoke test during market hours"), so tests 2–5 reused the real-process/
real-signal harness Phase 3 Part 1 already built for its own SIGTERM proof
(`tests/end_to_end/supervisor_signal_child.py`) rather than inventing a
second one, run directly from a shell rather than through `pytest`:

| # | Test | How | Result |
|---|---|---|---|
| 1 | **start** | `python -m runtimes.intraday_options` against the real repo, real `config/runtimes/intraday_options.yaml` (`enabled: false`) | `runtimes/intraday_options.yaml has enabled: false — nothing to start.` → exit `3` |
| 2 | **stop** | Real supervisor process (`supervisor_signal_child.py`) reached `READY`, then a real `kill -TERM` from a second shell | `RESULT {"stopped_by_signal": true, "clean_feed_shutdown": true, "worker_exit_codes": {"skelfix": 0}, ...}` — orderly, in order, on the right thread |
| 3 | **crash** | Same, but `kill -9` (uncatchable) instead | Process gone instantly; its PID file was left behind on disk, genuinely orphaned — the exact hazard **D76** hardened `common/process/locks.py` against |
| 4 | **restart** | A fresh process pointed at the same lock/PID directory, once after test 2's clean stop and once after test 3's crash | Both reached `READY` — no stale-lock refusal either way; the crash case specifically proves `clear_stale_pid_file()`'s pre-acquire sweep against a real orphaned file, not just a fixture |
| 5 | **duplicate-worker** | A second real process started against the *same* lock identity while the first (from test 2) was still alive | Refused immediately: `DuplicateProcessError: Refusing to start: another process already holds 'intraday_options.supervisor' held by pid ... since ...` — the first process untouched |
| 6 | **old-system exclusion** | `scripts.validate_environment` and `runtimes.intraday_options.__main__:main`, both against the real, currently-active legacy system | Both refused: `PROBLEM: the legacy Trading_Automation system appears active (...)` / exit `5` (`EXIT_LEGACY_SYSTEM_ACTIVE`), naming the exact `launchctl bootout` command |

No plist was loaded to produce this evidence — every row above is a direct
process invocation. `launchctl list | grep algotrading` stays empty at the
end of this phase, by design (see the decisions above).

**Real Telegram messages with no `data/operational/` or `logs/` trail are
expected from this harness, not a bug.** `supervisor_signal_child.py` takes
a `work_dir` argument and roots its lock/PID/log directory *and* its
`SupervisorConfig.database_path` there (`main()`, near the top) — never
under the project's real `data/operational/` or `logs/`. Run it by hand with
`RecordingNotifier()` swapped for a real one (`build_notifier(settings)` /
`NOTIFIER_FROM_SETTINGS`, same as production resolves it), and every
`worker_started`/`order_filled`/`worker_stopped` event still reaches
Telegram for real — `common.notifications.factory` only cares whether
`.env` has credentials, not where `work_dir` points — while the SQLite
database and per-worker log land wherever `work_dir` was pointed, which a
scratch invocation typically never checks in and often deletes afterward.
Contrast this with `runtimes.intraday_options.__main__.main()` (and
`supervised_launch.py`, which calls that same function): its fixed order is
`setup_logging` → `enabled`/legacy gates → **open `Database(database_path)`,
back it up, migrate it, retain it** → authenticate → *only then*
`build_notifier(settings)` inside `build_supervisor()`. `Database.connect()`
(`common/persistence/database.py`) creates the `.db` file on `sqlite3.connect`
itself, before a single row is written, so by the time that entrypoint could
ever reach code capable of sending a Telegram message, the durable database
trail already exists at the real project path — there is no ordering under
which the production entrypoint sends a real notification without one. So:
a real Telegram message with a real durable trail under `data/operational/`
means the production entrypoint ran; a real Telegram message with **no**
trail there means a test-shaped harness ran by hand with a real notifier and
a scratch `work_dir` — both are legitimate, and the absence of a trail is
diagnostic of *which one happened*, not evidence that anything malfunctioned.
(Traced in full against a real incident of exactly this shape, 8 August
2026 — see the operator's own investigation notes; no code change resulted,
since both entrypoints behaved exactly as designed.)

**Deviation from spec section 12's five-plist list.** Only three plists
exist: `positional_options` has no supervisor to point at (a placeholder
package, D56) and `intraday_stocks` does not exist at all (D34). A plist
naming an absent entrypoint would fail on its very first load, which is a
worse failure mode than a documented, deliberate absence — both are one-line
additions to `orchestration/launchd/generate_plists.py`'s `PLIST_SPECS` the
day either runtime gets a real entrypoint.

**What Phase 8 deliberately did NOT deliver.** No plist loaded or
`launchctl bootstrap`'d — Phase 9. `intraday_options.yaml` stays
`enabled: false`. The legacy LaunchAgent was found, not unloaded — the
operator's own action, with the exact command surfaced in every refusal
message this phase adds. Per-worker respawn inside the supervisor — a
crashed worker is still detected and its exit code still recorded
(`Result.worker_exit_codes`, unchanged since Phase 3), but nothing restarts
it mid-session; recovery is the whole-process restart `launchd` (via
`supervised_launch.py`) already provides. Static public IP validation stays
Phase 10 (spec's own placement, spec:2954). A tripped kill switch does not
end the supervisor run — see `EXIT_SAFETY_SHUTDOWN`'s scope note above and
limitation 33.

**Second same-phase addendum: two defects found by independent verification
of Phase 8's own work, both confirmed against the actual code before being
fixed, not assumed either way (D84, D85).** Two claims were raised for
review after this phase was first written up as complete: that
`supervised_launch.py` only classified returned exit codes with no exception
handling around the supervisor call, and that `legacy_guard.py` treated a
`launchctl` that could not be queried the same as one confirmed not loaded,
contradicting its own call site's "Fail-closed" comment. Both were checked
directly against the code rather than accepted or dismissed on the claim
alone, and both were real:

- **D84.** `run()`'s only call to `supervisor_main.main()` had no
  `try`/`except` anywhere near it. A fail-first test run against the
  unmodified module proved the consequence, not just the gap: a `ValueError`
  raised from a stubbed supervisor propagated straight out of `sl.run(...)`
  uncaught, writing no `errors` row and triggering no retry — precisely the
  unbounded-restart failure mode (`launchd`'s own `KeepAlive` has no attempt
  cap) this module's own docstring says it exists to prevent, just reached
  through a bug in the supervisor rather than one of its five classified
  exit codes. Fixed with `except Exception` (never `except BaseException`,
  so a deliberate `SystemExit`/`KeyboardInterrupt` still propagates
  untouched) around the call, folding an unexpected exception into the
  existing `max_attempts`/`backoff_seconds` retry path, logging its full
  traceback (`_log.exception`), and recording its type and message in the
  `errors` row `_record_attempt` already writes. All five
  `TERMINAL_EXIT_CODES` and both `RETRYABLE_EXIT_CODES` are byte-for-byte
  unchanged.
- **D85.** `_launchd_label_loaded`'s `except (OSError,
  subprocess.TimeoutExpired)` branch returned the same `False` that a
  confirmed `returncode != 0` did — `LegacySystemStatus.active` was a plain
  `launchd_label_loaded or process_running` with no way to tell "confirmed
  absent" apart from "could not check." `runtimes/intraday_options/__main__.py`
  already carried a comment directly above the call site claiming
  "Fail-closed — a legacy system that cannot be determined either way is not
  'not detected'"; the code did not match it, and
  `tests/unit/test_legacy_guard.py` had pinned the collapsed value as
  expected behaviour. Fixed with a new `LaunchdLabelState(StrEnum)`
  (`ACTIVE`/`INACTIVE`/`UNKNOWN`) replacing the boolean:
  `LegacySystemStatus.active` now refuses on `ACTIVE` *or* `UNKNOWN`, only
  proceeding on a confirmed `INACTIVE` plus no independently-detected
  process. A new `undetermined` property lets both call sites
  (`runtimes/intraday_options/__main__.py`,
  `scripts/validate_environment.py::_check_legacy_system`) tell the operator
  "state could not be determined" apart from "confirmed active" — and stops
  `validate_environment` printing "OK: ... not detected" for a check that
  never actually ran. Fail-first here was structural: every test file
  importing the new `LaunchdLabelState` symbol failed to even collect
  against the unmodified module.

Full details, including the fail-first evidence for each, are recorded as
**D84** and **D85** in the decisions table above. Neither reopens an
existing "known limitation" entry — both defects were introduced when D81
and D82 first built these modules earlier in this same phase, and are
closed within it, never having been recorded as an open gap at a phase
boundary. `pytest` (1616 passed, 16 skipped), `ruff check .`, and
`.venv/bin/mypy` (154 source files, no arguments — the configured
`packages` list, per the note at D-notes above on why bare `mypy .` fails
on an unrelated pre-existing `dashboards` dual-module-name error) were all
re-run clean after this addendum. No plist was loaded, `intraday_options.yaml`
still has `enabled: false`, all three plists still have `KeepAlive: false`,
and the `Trading_Automation` mtime baseline is unchanged from the value
recorded above (`2026-07-28 10:29:14`).

### Phase 7 — Operations — **Part 5 of 5 complete** (retention and backups)

Approved plan, verbatim scope: `common/retention/` with a policy module and a single
entry point invoked at controlled startup (not a cron, not a trading-path thread); log
compression and age-based retention on top of the existing size cap; bounded age-based
deletion for `runtime_heartbeats`/`notifications`/`errors`/`feed_events`/`auth_events`,
in one transaction, never touching `orders`/`fills`/`positions`/`order_intents`; a
pre-migration database backup with a configurable retained-backup count (backup only,
not the Phase 10 rollback machinery); `ScripMasterCache.prune()` given a real caller;
and an audit-found fix to `Settings.algo_log_level`.

**`common/retention/policy.py`** is the reviewable rule, deliberately separate from
how it is enforced: `RETAINED_TABLES` (a fixed `dict[str, str]` of table → its
timestamp column — `runtime_heartbeats.beat_at`, `notifications.created_at`,
`errors.occurred_at`, `feed_events.occurred_at`, `auth_events.occurred_at`) and
`NEVER_PURGED_TABLES` (`orders`/`fills`/`positions`/`order_intents`, stated explicitly
rather than left as "everything not in the allowlist" so a reviewer — and
`tests/unit/test_retention.py`'s own disjointness check — can see the boundary without
reading SQL). `common/retention/database.py`'s `purge_old_rows()` deletes past a
configured age from every table in the allowlist, in one `Database.transaction()` —
a crash mid-purge must not leave `notifications` trimmed and `errors` untouched,
silently disagreeing about how far back each table's history goes — and bounds each
table's delete with a `LIMIT`ed subquery (`DELETE FROM t WHERE id IN (SELECT id FROM t
WHERE col < ? ORDER BY col LIMIT ?)`) rather than `DELETE ... LIMIT` directly, since
the stdlib `sqlite3` build this project uses does not enable
`SQLITE_ENABLE_UPDATE_DELETE_LIMIT`. A database with months of backlog on its first
run does not hold the write lock for one unbounded delete; each controlled startup
chips away at the rest.

**`common/retention/logs.py`**'s `sweep_logs()` adds the two axes the existing
`RotatingFileHandler` size cap (10 MB × 10 backups per log name) does not cover: it
gzips a rotated backup once it is older than a configurable "compress after" age, and
deletes anything — compressed or not — past a configurable "max age", using each
file's mtime (which is exactly when `RotatingFileHandler` stopped writing to it). Only
rotated backups (`name.log.N` / `name.log.N.gz`) are ever touched; the active file
(`algo_trading.log`, `io_alpha.log`) is never compressed or deleted, since it is open
for writing by a live process.

**`common/retention/backup.py`**'s `backup_database()` runs before migration, from the
same call site retention runs after — see the ordering note below. It uses
`sqlite3.Connection.backup()`, SQLite's own online backup API, rather than a raw file
copy, so a WAL-mode database still produces one consistent snapshot file instead of a
copy that misses whatever is still sitting in a separate `-wal` file. A database that
does not exist yet (a fresh install, before the first migration ever runs) backs up to
`None` rather than failing startup. Old snapshots beyond the configured
`retain_count` are pruned by filename, the same "sort chronologically, drop the
oldest" trick `ScripMasterCache.prune()` already used.

**Ordering, and why it is one call site rather than one function.** Migration itself
happens deep inside `IntradayOptionsSupervisor.run()`, which then blocks for the whole
trading session — driving the feed until it exhausts or a shutdown signal arrives — so
"backup before migration, sweep after" cannot be one function called once partway
through a multi-hour blocking call. Both steps instead run explicitly in
`runtimes/intraday_options/__main__.py:main()`, back to back, strictly before
authentication and before `build_supervisor()`/`.run()` are ever reached:
`backup_database()`, then `MigrationRunner(database).run_pending()`, then
`run_retention()`. `Supervisor.run()` still performs its own (idempotent, replay-safe)
migration call afterward, unchanged — this was already true before Part 5 for every
worker process too, and duplicating a no-op replay across process boundaries is an
existing, accepted property of this migration runner, not a new one. Keeping both new
calls at this one site, rather than also wiring them into `Supervisor.run()` or
`worker.py`, is what keeps every existing supervisor/worker test — none of which
expects a backup file or a retention sweep as a side effect of constructing a
`Supervisor` directly — unmodified.
`test_main_backs_up_before_migrating_and_retains_after`
(`tests/unit/test_intraday_options_main.py`) proves the ordering directly: a database
seeded with one marker table before `main()` runs backs up to a snapshot containing
only that table, then the live database ends up with the real migrated schema —
backup genuinely ran first.

**`ScripMasterCache.prune()`** existed since Phase 4 Part 1 with no caller anywhere
outside its own tests. `run_retention()` is that caller — it constructs a
`ScripMasterCache(cache_dir)` and prunes it to `scrip_cache_retain_count` on every
controlled startup, alongside the database and log sweeps.

**Configuration.** `RuntimeConfig.retention: RetentionConfig` (`common/config/models.py`)
follows the exact precedent `HealthConfig`/`heartbeat_interval_seconds` set in Part 1:
six `Field(gt=0)` knobs (`log_max_age_days=30`, `log_compress_after_days=1`,
`db_row_max_age_days=90`, `db_delete_batch_limit=5000`, `backup_retain_count=7`,
`scrip_cache_retain_count=3`), each literal duplicated from — and tested against —
`common/retention/policy.py`'s own `DEFAULT_*` constants, the same "must never
silently drift apart" discipline `test_config_loader.py` already enforced for the
heartbeat default. `ProjectPaths.backup_root` (`data/backups/`) is new, added to
`ensure_writable_dirs()`.

**D80** (see the deviation log): `common/persistence/migrations.py` carried two
comments claiming that a genuinely destructive migration "arrives with backup and
rollback machinery" once one is needed. That is no longer accurate on the backup half
and needs stating precisely rather than left to imply more than Part 5 built: a
pre-migration backup now exists and runs on every controlled startup, but there is
still no rollback machinery — nothing restores from a snapshot, checks it against a
running schema, or replays writes made since it was taken. Both comments, and the
`MigrationError` message `_reject_destructive` raises, are corrected to say exactly
that.

**D79** (see the deviation log): the Phase 7 audit found `Settings.algo_log_level` —
read from the environment since Phase 0, exposed on every `Settings` instance — was
never once passed to `setup_logging(level=...)` at any of its four production call
sites (`runtimes/intraday_options/__main__.py`, `runtimes/intraday_options/worker.py`,
`scripts/auth_bootstrap.py`, `scripts/capture_live_tape.py`). Every process has been
running at `setup_logging`'s own hardcoded `"INFO"` default regardless of what
`ALGO_LOG_LEVEL` was actually set to. Fixed at all four call sites.

**Deliberately not done in Part 5:**

- No restore/rollback tooling for a pre-migration backup — backup only, explicitly;
  see D80 and the migrations-runner docstring. Phase 10's job, if a genuinely
  destructive migration ever needs it.
- No cross-runtime coordination for the shared `logs/` directory. `positional_options`
  has no supervisor yet (D56) — `intraday_options` is the only caller of
  `run_retention()` today — so two runtime groups sweeping the same log directory with
  different configured ages is a real but currently unreachable scenario, not one this
  part had a second runtime to test against.
- No retention for `option_chain_snapshots` or `paper_fill_quotes` (migration `0003`)
  or for `audit_events` (migration `0004`) — the plan named five tables, not seven; an
  operator audit trail being retained forever is the intended behaviour, not an
  oversight.

### Phase 7 — Operations — **Part 4 of 5 complete** (PID handling and operator commands)

Approved plan, verbatim scope: implement the PID ownership validation the module
docstring already claimed and did not do, discriminating on process start time
rather than command path; add verified stale-PID-file cleanup; make the supervisor
act on `EXIT_DUPLICATE`; restructure `tests/unit/test_scripts_are_read_only.py`'s
hard-pinned whitelist into a read-only tier and a control tier; build `scripts/
status`, `scripts/validate_environment`, `scripts/stop_runtime`/`stop_strategy`,
`scripts/square_off --confirm`, `scripts/start_runtime`/`start_strategy`, and a
`scripts/authenticate` alias; add an `audit_events` migration following 0002's
precedent. Marked in the plan as "the one property in the plan where a wrong
implementation causes a real operational incident" and built accordingly: fail-first,
with the defect demonstrated on today's unmodified code before any fix was written.

**The fail-first proof (D76).** `test_a_live_process_that_is_not_ours_is_not_an_owner`
was added to `tests/unit/test_process_locks.py` before `psutil` was imported anywhere
or `locks.py` was touched. It spawns a real, signalable `time.sleep(60)` subprocess —
deliberately not `pid: 1`, which `_process_is_alive`'s existing `PermissionError`
handling treats as "alive" without reproducing the actual hazard (`SIGTERM` to
`launchd` fails with `EPERM` and harms nothing) — writes its PID into a fixture
claiming the identity `intraday_options.supervisor`, and asserts `current_owner() is
None`. Run against unmodified code: **it failed**, exactly as the plan predicted —
`current_owner()` returned a populated `LockOwner` for the unrelated live process,
because liveness was the only thing checked. That observed failure is the evidence,
recorded in full as **D76** above. The paired over-strictness guard,
`test_a_genuinely_live_holder_is_still_recognised` (a lock this process actually
holds, plus a spawned child with a multiprocessing-bootstrap-shaped `command` string
in its fixture), passed both before and after the fix — confirming the tightened
check does not swing the other way and start refusing a supervisor that really is
ours.

**The fix.** `LockOwner` gained a required `create_time: float` field — the decision —
plus `executable`/`project_root`, diagnostic-only, surfaced in `DuplicateProcessError`
messages but never compared. `_verified_owner(pid, expected_create_time)` reads
`psutil.Process(pid).create_time()`, treats `psutil.NoSuchProcess` and
`psutil.AccessDenied` alike as "not verified" (fail-closed: a process this system
actually started runs at our own privilege level, so `AccessDenied` should never
legitimately fire against our own PID file), and compares by exact float equality —
deliberate, since any tolerance wide enough to matter would let a process
started-and-killed within that window slip through as a false match, precisely the
failure mode the check exists to close. `current_owner()` now calls it;
`clear_stale_pid_file()` is new (spec step 6's second clause, previously
unimplemented) and safe to call unconditionally, including right before `acquire()`
takes the lock, because a genuinely-held lock's PID file always verifies. The
supervisor now acts on `EXIT_DUPLICATE` — previously recorded at `supervisor.py:450`
and never inspected, so a refused duplicate worker was silently a zero-length run;
it now records a `CRITICAL` `errors` row (`component="supervisor.duplicate_worker"`)
and sends a `duplicate_worker_refused` notification.

**D77, found by the supervisor's own new test, not by inspection.**
`test_a_duplicate_worker_is_reported_not_silent` — written to prove the
`EXIT_DUPLICATE` wiring above against a real spawned duplicate — asserted
`result.worker_exit_codes["skelfix"] == EXIT_DUPLICATE` and got `0`.
`multiprocessing.Process(target=run_worker, ...)` discards `run_worker`'s return value
entirely; only `sys.exit()` or an uncaught exception changes a spawned child's real
exit code, so `EXIT_DUPLICATE`, an integrity-check failure, and the per-candle
exception handler's `exit_code = 1` had all been invisible to `worker_exit_codes`
through every real `spawn`ed worker since they existed, each one silently reporting
success. Fixed with a `run_worker_process()` wrapper (`run_worker()` then
`sys.exit(outcome.exit_code)`) used only as the `multiprocessing.Process` target —
`run_worker()` itself is unchanged, since dozens of existing tests call it directly
and would break under an unconditional `sys.exit()`. See **D77** above.

**D78, found only once D77 stopped masking it.** Re-running the suite after the D77
fix surfaced a second, previously-hidden failure: `test_two_workers_receive_
identical_bars` raised a `UNIQUE constraint failed: order_intents.correlation_id`.
`strategy_token()`'s 4-character truncation lets `"skelone"` and `"skeltwo"` collide
on the same token, and since sequence numbers are scoped by the full strategy id,
both strategies' first orders produced byte-identical correlation IDs. Fixed at
admission time — `IntradayOptionsSupervisor.add_worker()` now refuses the second
colliding strategy (an `errors` row plus a notification, the same individual-refusal
shape the live-gate check already uses), not by redesigning the correlation-ID format
itself. See **D78** above, and `tests/unit/test_correlation_ids.py`'s four new tests
for the newly-public `strategy_token()`.

**Operator commands.** `tests/unit/test_scripts_are_read_only.py`'s hard-pinned
`{"auth_bootstrap.py", "capture_live_tape.py"}` whitelist is now two tiers: a
**read-only tier** (`auth_bootstrap.py`/`authenticate.py`, `capture_live_tape.py`,
`status.py`, `validate_environment.py`) keeping all four original Dhan-API-safety
assertions unchanged, and a **control tier** (`stop_runtime.py`, `stop_strategy.py`,
`square_off.py`, `start_runtime.py`, `start_strategy.py`, `_operator_common.py`)
proving instead that no script imports a broker and no script writes directly to a
trading table (`signals`/`order_intents`/`orders`/`fills`/`positions`/
`strategy_state`) — every control-tier write goes through
`ExecutionRepository.record_audit_event`, the new `audit_events` sink (migration
`0004`, following 0002's precedent: purely additive, `IF NOT EXISTS`, no column that
can hold a secret).

* `scripts/status` — read-only, prints `HealthSnapshot` (`common.health.read_
  snapshot`) via `connect_readonly`; `--json` for machine-readable output.
* `scripts/validate_environment` — read-only preflight per spec §13: project root,
  writable directories, disk space, system time (reported, not verified against a
  network source — no such source exists in this offline-by-design script), Dhan/
  Telegram credentials, database integrity, and a path-drift check against
  `PROJECT_ROOT`.
* `scripts/stop_runtime` / `scripts/stop_strategy` — read the PID file, verify
  ownership with the D76 check, send `SIGTERM`, record an audit event either way
  (refused or signalled). Never guesses: no verified owner means nothing is
  signalled. Confirmed by hand against a real PID-reuse fixture (a spawned sleeper
  process, a PID file naming it with a wrong `create_time`) — refused, and the
  sleeper was still running afterwards; against a genuine owner — signalled, and the
  process exited.
* `scripts/square_off --strategy-id X --confirm` — writes a request file
  (`common/process/square_off_requests.py`, atomic temp-file-then-replace, the same
  pattern `locks.py`'s PID file uses) that the running worker itself polls and
  executes through its own square-off path; the script never opens a write
  connection to `positions`. `--confirm` is mandatory — without it, nothing is
  written and nothing is asked of any worker. Records `square_off_requested`; the
  worker records `square_off_completed` once it actually closes (or finds nothing
  to close), and clears the request file only then — a crash between "seen" and
  "acted" does not lose the request. Wired into both worker shapes: the fixture path
  checks the request once per candle (it needs a real candle to price a close
  against, so an idle gap between candles delays but never loses a request — the
  fixture path is Phase 1's deterministic test-only signal, not production);
  the ported engine path checks it on `HubTickFeed`'s existing poll timer, composed
  alongside the wall-clock square-off net, so it lands within one poll interval
  regardless of tick flow.
* `scripts/start_runtime` / `scripts/start_strategy` — thin wrappers translating the
  spec's positional-argument invocation into the real entrypoint's `--runtime-id`/
  `--strategy-id` flags. `runtimes/intraday_options/__main__.py`'s `build_supervisor`
  gained an additive `strategy_ids` filter so a per-strategy start still goes through
  a supervisor exactly as an unfiltered start does — the spec is explicit that a bare
  worker is never spawned outside one. An unknown `--strategy-id` is refused before
  authenticating, so a typo does not cost a Dhan auth request.
* `scripts/authenticate` — a pure alias for `scripts/auth_bootstrap.py` (re-exports
  its `main`/exit codes), satisfying the spec's naming without duplicating or
  renaming the original, whose own tests are untouched.

**Verification.** Full suite green (`.venv/bin/python -m pytest -q`), `ruff check .`
clean, `mypy` clean (146 source files). The PID-reuse drill from the plan's own
end-to-end verification list (start a throwaway sleeper, point a PID file at it, run
`stop_runtime`) was run by hand against the real script, not only its unit tests —
refused, with the sleeper still running afterwards; a genuine owner was signalled and
exited cleanly.

### Phase 7 — Operations — **Part 3 of 5 complete** (the Streamlit dashboard)

Approved plan, verbatim scope: Master, Intraday Options and System Health pages
reading exclusively from `common/health/snapshot.py` and `connect_readonly()`,
stub pages for Positional Options and Intraday Stocks, robust handling of a
missing/locked/pre-migration database (a message, not a traceback), and
`tests/unit/test_dashboard.py` driving `render()` with a fake streamlit module,
including a regression test that no page module imports a broker, a feed, or a
write connection.

**"Exclusively snapshot.py and connect_readonly" is stricter than the plan's own
Master-page bullet — flagged, then corrected once reviewed.** The plan's original
Part 3 text named `effective_live_gate` as a Master-page data source for "global and
runtime live-gate status", but `effective_live_gate` takes a `ResolvedConfig`, which
means reading `config/`, not the database. First shipped with live-gate status
**not shown**, flagged explicitly rather than silently dropped, precisely so it could
be revisited. Reviewed and corrected in the same part: config and the operational
database are different resources, and "exclusively snapshot.py and connect_readonly"
was never actually about config — it was about the database specifically (no write
connection, no second ad-hoc SQL path) plus no broker/feed import. Verified directly
before adding it back, not assumed: `common.config` and everything it imports
transitively (`common.risk.squareoff`, `pydantic`, `yaml`, the standard library)
touches neither `common.broker` nor `common.market_data` nor the write-capable
`common.persistence.Database`, and `tests/unit/test_dashboard.py`'s AST regression
tests pass unmodified for `app.py` with the new import present — they check for
broker/feed/write-`Database` imports specifically, which config reading is none of.
Live-gate status now ships on Master; see the addendum below.

**Two spec-asked-for fields are deliberately not shown, and the difference between
them is why one is a data-source limit and the other is an honesty decision:**

- **A "disabled strategy" count** — spec's Master page bullet. A strategy with
  `enabled: false` in config never starts a worker, so it never writes a heartbeat
  and is structurally invisible to `read_snapshot`; showing it would need reading
  `config/strategies/*.yaml`, a second read path the "exclusively" constraint above
  already rules out for this part. `RuntimeCard` has no `disabled_count` field, and
  `test_no_disabled_count_is_fabricated` pins that it stays absent rather than a
  silently-wrong zero.
- **Engine type, open legs/baskets, selected strikes/expiry, per-leg P&L, roll
  count** — spec's Intraday Options page bullet. Not a data-source limit: even
  reading every table `common.health.snapshot` could ever cover, none of this data
  exists, because `MultiLegEngine`/`FixedStrikeEngine` are not ported into this
  codebase (runbook D56/D34, unchanged since Phase 3's reuse audit). Building UI
  plumbing for an engine that produces no data would be exactly the "looks finished
  but isn't" pattern the runbook already declines elsewhere for the same two
  engines. `dashboards/intraday_options.py`'s `NOT_YET_AVAILABLE` constant states
  this on the page itself, not just in a docstring an operator will never read.

**What *is* shown on Intraday Options is real, not a reduced echo of Master.**
`StrategyHealth` (Part 1's own dataclass) gained three genuinely-persisted fields
this part — `pid` (`runtime_sessions`, the same column `ProcessHealth` already reads
for the group, just not previously read per-strategy), `square_off_state`/
`entries_blocked` (`strategy_state`, keyed on `(strategy_id, execution_mode,
trading_date)` — a strategy that changed mode across restarts reads its *current*
mode's row, not an earlier day's), and `open_positions` (a per-strategy `COUNT(*)`
against `positions`, scoped to its own `trading_date` so a stale contract from
another day can never inflate today's count). All four are additive to a dataclass
nothing outside `snapshot.py` constructs directly — checked before assuming it, not
after: `grep` for `StrategyHealth(` across `tests/` returned nothing, so widening it
could not silently break an existing direct construction anywhere.

**The "simulated" wording moved, not was lost.** Phase 1's `RuntimeTile.mode_label`
property — "PAPER (simulated)" / "LIVE", the guarantee an operator must never guess
whether a number is real money — is retired along with the rest of `RuntimeTile`,
but the property itself, word for word, now lives on `dashboards.intraday_
options.StrategyRow` (and the module-level `mode_label()` function it wraps), since
Master shows paper/live *counts* across strategies, not one strategy's own mode —
the per-strategy badge belongs on the page that actually has one strategy per row.
`tests/end_to_end/test_walking_skeleton.py`'s dashboard gate test (`'one dashboard
tile works'`) is updated to match: it now calls `dashboards.app.load_master` for the
group-level assertions and `dashboards.intraday_options.load_intraday_options` for
the mode-badge assertion, rather than the retired `load_tile`.

**Robustness.** `dashboards/_shared.py` is new: `load_snapshot(database_path,
runtime_id, trading_date)` is the one place that turns "no database file", "locked
past the read connection's busy timeout" and "pre-migration or corrupt (a queried
table does not exist)" into a `SnapshotUnavailable(reason)` value every page's
`render()` checks for, instead of each of five pages growing its own
`try/except`. The missing-file and pre-migration cases are exercised for real (an
absent path; a genuinely empty, migration-less SQLite file); the locked case is
exercised by monkeypatching `read_snapshot` to raise `sqlite3.OperationalError`
directly — a real concurrent lock needs a second connection holding a write
transaction past the busy timeout, which would make the test slow and
timing-dependent for no extra safety actually proven, since the code path under
test does not care *why* SQLite raised, only that the exception is caught.

**D75, found while running the new test suite, not written into it on purpose:**
`test_the_group_heartbeat_is_the_strategy_id_is_null_row` (Part 1) failed
intermittently on a full-suite run — `heartbeat_age_seconds == -0.0010058283805847168`
where `>= 0.0` was expected. Root cause: `common.health.snapshot._process_health`
computes age as `(julianday('now') - julianday(beat_at)) * 86400.0` — SQLite's own
clock — against a `beat_at` written a moment earlier from Python's `datetime.now(UTC)`.
The two clocks are each individually correct but sampled a fraction of a millisecond
apart, and on a read moments after the write the subtraction occasionally comes out
fractionally negative. Not a logic bug, and not worth chasing out of the query
(`MAX(0, ...)` in SQL would hide the same measurement, just one layer down) — fixed by
clamping in Python, a new `_non_negative_age()` helper applied at both computation
sites (`_process_health` and `_strategy_healths`), with the same judgement the
codebase already applies to MFE/MAE restarting at zero rather than a small negative
(`PositionManager.adopt`): a truly negative age is not a fact worth reporting as one.
`test_a_negative_age_from_clock_skew_between_sqlite_and_python_clamps_to_zero` pins
the fix directly, deterministically, rather than relying on re-running the flaky test
enough times to trust it.

**Verified two ways.** The unit suite (`tests/unit/test_dashboard.py`, 45 tests) drives
every page's `render()` with a fake streamlit module and checks the AST of every file
in `dashboards/` (including the four `pages/*.py` shims) for a broker, feed or
write-capable-`Database` import. Separately, `streamlit run dashboards/app.py` was
started for real (streamlit 1.60.0, already installed) and all five pages —
`/`, `/Intraday_Options`, `/Positional_Options`, `/Intraday_Stocks`, `/System_Health`
— were fetched over HTTP: 200 on every one, an empty server log (no traceback, no
`ModuleNotFoundError`), confirming the `pages/*.py` shims' defensive `sys.path`
fix-up (walking up to `pyproject.toml`, the same technique `common.config.paths.
_discover_root_from_source` uses) actually resolves `dashboards.*` imports under
Streamlit's own execution context — the one thing the unit suite cannot exercise,
since nothing pytest does replicates how Streamlit sets up `sys.path` for a page it
discovers by directory convention rather than by import.

#### Addendum: live-gate status added to Master

Requested directly after the part above shipped: add `effective_live_gate` status
back to Master, sourced from config, confirm the AST regression test still passes,
and confirm this does not reopen whatever risk "exclusively snapshot/connect_readonly"
was actually guarding against.

`RuntimeCard` gains one new field, `live_gate: LiveGateStatus | ConfigUnavailable |
None = None` — defaulted so every call site and every test written against
`load_master`/`RuntimeCard` before this addendum keeps working completely unchanged;
`config_root` is a new *optional* keyword on `load_master`, and only supplying it
computes the section at all. `LiveGateStatus` carries the two account/runtime-level
booleans (`global_live_trading_enabled`, `runtime_live_execution_allowed` — spec's
own "global and runtime" framing, distinct from strategy-level) read directly off
`GlobalConfig`/`RuntimeConfig`, plus `effective_live_gate` evaluated for real —
not re-derived — against every *enabled*, *live-mode* strategy
(`discover_enabled_strategies` already resolves each one fully), so what Master shows
is exactly what the supervisor's own admission gate would decide for that strategy,
reasons included.

Isolated into its own failure type, `ConfigUnavailable`, deliberately not reusing
`SnapshotUnavailable`: a malformed or absent `config/runtimes/<id>.yaml` is now a real
possibility this page has to handle (it wasn't, before this addendum touched config at
all), and it must degrade *only* the live-gate section — a broken YAML file must never
blank the snapshot-backed rest of an otherwise-healthy page. `load_master` still
returns a plain `SnapshotUnavailable` when the *database* read fails, unchanged;
`ConfigUnavailable` only ever appears inside `RuntimeCard.live_gate`, never as
`load_master`'s own top-level return type.

Verified precisely, not assumed: `common.config`'s own import list and every module it
imports (`common/config/loader.py`, `common/config/models.py`,
`common/config/__init__.py`, and `common.risk.squareoff` transitively) were read
directly and confirmed to touch only `pydantic`/`yaml`/the standard library — no
`common.broker`, no `common.market_data`, no write-capable `common.persistence.
Database`. `tests/unit/test_dashboard.py::test_no_dashboard_module_imports_a_broker_or_a_feed[app.py]`
and `::test_no_dashboard_module_imports_the_write_capable_database_class[app.py]` both
still pass unmodified with the new `common.config` import present, run individually to
confirm rather than inferred from the full suite passing. Re-verified live against the
real repo: `streamlit run dashboards/app.py` against the actual `config/global.yaml`/
`config/runtimes/intraday_options.yaml` returned HTTP 200 with an empty server log, the
same two-part verification (unit suite + a real running instance) the part itself used.

9 new tests in `tests/unit/test_dashboard.py` cover `load_live_gate_status` directly
(global/runtime flags, a live-mode strategy correctly blocked by the real gate, a
paper-mode strategy correctly excluded, a missing runtime config returning
`ConfigUnavailable` rather than raising), `load_master`'s backward-compatible default
and its `config_root`-supplied path, and `render()` for all three states (no live-gate
requested, a real status, and an unavailable one that must not blank the rest of the
page).

**Deliberately not done in Part 3:**

- Engine type, legs/baskets, strikes/expiry, per-leg P&L, roll count — see above;
  `MultiLegEngine`/`FixedStrikeEngine` produce none of this data yet.
- A "disabled strategy" count — see above; invisible to runtime state alone.
- Lock-file status on System Health (PID is shown; whether the matching `.lock` is
  still held is filesystem state, Part 4's job, not this page's read path).
- No PID hardening, no operator commands — Part 4.
- No retention, no backups — Part 5.

### Phase 7 — Operations — **Part 2 of 5 complete** (Telegram in production)

Approved plan, verbatim scope: wire the real notifier at the entrypoint (with a
spawn-boundary comment explaining child notifier construction), move sends off the
tick thread via a bounded internal queue, add the three missing rendered fields
(timestamp, correlation/order ID, required action), add rate limiting and repeated-
error aggregation in `SafeNotifier` replacing the two hand-rolled latches, make
`record_notification` a real production caller so notification failures persist,
confirm the redaction guarantee survives every new rendered field, and fill the
spec's remaining event-category gaps.

**Wiring the real notifier surfaced a fact worth stating up front: this development
machine's own `.env` carries real, working Telegram credentials.** Every new test in
this part was written with that specifically in mind — `tests/unit/test_notifier_
factory.py` constructs `Settings` against an isolated, empty `tmp_path` `.env` with
explicit `None` overrides (constructor kwargs are pydantic-settings' highest-
precedence source, ahead of a stray exported shell variable too), and no test
anywhere calls `Settings()`/`load_settings()` unguarded. This is also *why* `None`
could not be overloaded to mean "build the production notifier" for a spawned
worker — see the sentinel/D72 entry below.

**Redaction.** `common/logging/redaction.py`'s own docstring already claimed secrets
"must never be printed, **persisted or notified**" and that this is "enforced once,
at the logging boundary" — checked directly rather than taken on faith, and the
"notified" half was not actually true: `SecretRedactingFilter` is a `logging.Filter`,
reachable only through a handler, and `TelegramNotifier.send()` builds its payload
from `NotificationEvent.rendered()` without ever passing through logging at all. The
existing guarantee for Telegram specifically was sound anyway — the token is read
from `SecretStr` at send time and never stored on `NotificationEvent`, so nothing
token-shaped could reach `rendered()` to begin with — but the *general* claim, now
that `rendered()` carries three new fields and feeds a real `notifications.message`
column too, was aspirational. `NotificationEvent.rendered()` now calls `common.
logging.active_redactor()` and runs its output through the same filter before
returning it — a real second layer where the docstring already claimed one existed,
not a fix to a live leak. Degrades to unredacted, not to a raise, when no `setup_
logging()` call has run in the process (most unit tests) — see `test_rendering_is_
unredacted_when_no_logging_has_been_configured`.

**The two hand-rolled latches, and what "replacing" them actually meant.** Read
closely before touching anything: `TradingEngine._block_entries`'s `self._entry_
blocked is not None: return` and the supervisor's `_stuck_subscription_alarmed`
both gate more than a notification — the first is the actual "entries are blocked"
state (its own docstring: *"the latch is set first, then announced, so a disabled or
throwing notifier cannot turn trading back on"*), the second also gates a `DEGRADED`
heartbeat beat and an `errors`/`feed_events` row, not only the Telegram send. Neither
could simply be deleted without changing behaviour unrelated to notifications.
`SafeNotifier` therefore gained its *own* generic rate limiter/aggregator, keyed on
`(runtime_id, strategy_id, event_type, message)` with a 60s default window — every
call site gets duplicate-suppression now, not just the two that happened to hand-roll
it — while both existing latches stay exactly as they were, for the state/DB-write
reason above. In practice they make `SafeNotifier`'s aggregation inert at those two
call sites (the caller never repeats the send), and active everywhere else, including
call sites that never had any latch at all. `test_repeated_failures_are_suppressed_
not_amplified` replaces `test_repeated_failures_are_counted_not_amplified`, whose old
body only asserted the counter incremented and never verified the "not amplified"
its own name promised (Phase 7 Part 1 audit finding, restated here because Part 2 is
what actually closes it).

**Deferred delivery, and why it is scoped to exactly one `SafeNotifier`.**
`TradingEngine`'s notifier is reached from `on_tick` — the feed's own callback
thread — so a synchronous 5s Telegram timeout there stalls tick processing itself.
`SafeNotifier` gained an opt-in `deferred=True` mode: a bounded `queue.Queue`, a
dedicated drain thread that performs the real send, and `close()` to drain and flush
at shutdown. `worker.py`'s own `SafeNotifier` — reached only between `candle_queue.
get()` calls, never from inside a feed's callback — stays synchronous by contrast,
which keeps every existing `RecordingNotifier`-based test's "assert on `.events`
right after the run returns" pattern deterministic and unchanged; the engine path
sets `deferred = config.engine is not None` at the one construction site both paths
share. A full deferred queue drops the oldest entry and counts the drop rather than
blocking the producer (spec 2554's "small internal queue... may" — not Celery, not
an external broker) — `test_a_full_deferred_queue_drops_the_oldest_and_counts_it_
never_blocks` proves it under a genuinely slow inner notifier, not a mock.

**Failure persistence without reintroducing Phase 7 Part 1's cross-thread bug.**
`SafeNotifier` takes an injected `on_failure` callback rather than a repository
reference directly — the same seam discipline `common.engine` already keeps from
`common.execution` (`recover_position`, `persist_exit_state`; see D62). For a
*synchronous* `SafeNotifier` this is safe to call inline (the caller's own thread
already owns whatever repository the callback touches). For a *deferred* one it is
not: the drain thread's `sqlite3` connection is not its own, and calling a
repository-touching callback from that thread would be exactly the bug D70 (Part 1)
found and fixed for `feed_events`, reintroduced one layer up. Deferred failures are
therefore appended to an in-memory list under a lock and *pumped* out through
`on_failure` only from `send()`/`close()` — i.e. only ever on a thread the caller
already trusts with that repository. `worker.py` and `supervisor.py` both wire
`on_failure=lambda event, reason: repository.record_notification(...)`; the
supervisor's, like Part 1's health-event sink, is late-bound via a new
`SafeNotifier.set_on_failure()` because its own repository does not exist at
`__init__` time either.

**Correlation IDs: wired where cheap, deliberately not where invasive.**
`NotificationEvent.correlation_id` is populated at every call site that already had
one in scope without new plumbing — `worker.py`'s `order_filled` and `square_off_
completed` (`ExecutionResult.correlation_id`, already a top-level field). The
*engine's own* `entry`/`exit` notifications (`TradingEngine._open`/`_close`) do not
carry one: `common.engine.positions.FillOutcome`/`OpenPosition`/`Trade` do not
persist it today, and threading it through would mean widening three dataclasses and
`PositionManager.open()`/`close()` — real, contained, additive work, but a second,
separate change deliberately left out of this part rather than folded in under time
pressure. `ExecutionResult.correlation_id` already exists and would make that
follow-up straightforward. See limitation 32.

**Event-category gaps filled**, each reusing an existing detection point rather than
inventing a new one: authentication (`authenticated`, alongside the `auth_events` row
Part 1 already writes), runtime-group lifecycle (`runtime_started`/`runtime_stopped`,
distinct from the existing strategy-level `worker_started`/`worker_stopped`), feed
lifecycle beyond the two existing alarms (`feed_disconnected`/`feed_recovered`/
`feed_reconnect_exhausted`, sent from the same thread-safe drain point Part 1 built
for `feed_events` — deliberately *not* every `connected`/`reconnect_attempted`/
`resubscribed`, which stay diagnostic-only to avoid exactly the noise spec 2539/2554
warn against), and a square-off *success* event on the engine path
(`TradingEngine._handle_square_off`, guarded by the same `_squared_off` latch that
already makes the method itself idempotent) — the fixture path has had one since
Phase 1; the engine path had only the failure alarm.

**Two real bugs, found by the fail-first test discipline before either shipped:**

- **D72** — the notifier sentinel (`NOTIFIER_FROM_SETTINGS`) is an `Enum` member, not a
  plain `object()`, because a plain sentinel does not survive the `spawn` pickle round
  trip with its identity intact: unpickling constructs a new instance, so `notifier is
  NOTIFIER_FROM_SETTINGS` silently evaluated `False` inside every spawned worker, and
  the un-recognised sentinel fell through to being used *as* a notifier —
  `AttributeError` the moment anything called `.send()` on it. Caught immediately: the
  existing supervisor end-to-end suite (which spawns real workers through the real
  code path) failed nine tests the moment this shipped, not later. `Enum` members are
  the standard library's own pickle-safe singleton. See `tests/unit/test_notifier_
  sentinel.py` for a direct, minimal regression test alongside that indirect coverage.
- **D73** — `TradingEngine.__init__` was unconditionally wrapping its `notifier`
  argument in a fresh `SafeNotifier`, even when `worker.py` had already handed it one
  (`engine_worker.run_engine` passes `notifier=safe_notifier` straight through). Type-
  valid — `SafeNotifier` structurally satisfies `Notifier` — but a silent double-wrap:
  two independent success/failure counters, two independent aggregation windows on the
  same event, and the *outer* `deferred=True`/`on_failure` this part's own design
  depends on would have lived on a `SafeNotifier` `TradingEngine` never touches
  directly. Found while designing the deferred-mode wiring, not by a failing test —
  `isinstance(notifier, SafeNotifier)` now reuses an already-built one as-is;
  a bare `Notifier` still gets the fallback wrap, defaulted `deferred=True` since that
  path is exactly the one reachable from `on_tick`.

**Deliberately not done in Part 2:**

- The dashboard still reads nothing new — Part 3.
- No PID hardening, no operator commands — Part 4.
- No retention, no backups — Part 5.
- `scripts/reconcile` and any reconciliation-category notification — Phase 10
  throughout the spec, unchanged from Part 1's own scoping note.
- Correlation IDs on the engine's own `entry`/`exit` notifications — see above and
  limitation 32.

### Phase 7 — Operations — **Part 1 of 5 complete** (health snapshot layer)

The spec's Phase 7 entry (`ALGO_TRADING_FORWARD_TESTING_ARCHITECTURE_FINAL.md:
2912-2914`) is one sentence: harden Streamlit, Telegram, health snapshots,
worker/supervisor PID handling, log retention and manual commands. A pre-work
audit checked each item against the codebase before any implementation, the
same discipline every phase since Phase 0 has used. The finding that shaped
delivery order: the *isolation* contracts were already solid and well tested
(`SafeNotifier` wraps Telegram at all three construction sites and swallows
any exception; `connect_readonly` enforces `mode=ro` at the SQLite driver
level; `common/process/locks.py`'s `flock`-based exclusion is real and tested)
— but almost nothing above those contracts existed. Sharpest single finding:
migration 0002 (Phase 2) shipped `auth_events` and `feed_events` with CHECK-
constrained vocabularies and useful columns (`reason_code`, `downtime_
seconds`, `expected_subscriptions`), and **nothing in the repository ever
wrote to either table** — `grep` for both names outside `tests/unit/test_
migrations.py` returned nothing. `TelegramNotifier` was never constructed
outside a test; `runtimes/intraday_options/__main__.py` built the supervisor
without a `notifier=` argument, so every one of the 17 existing notification
call sites delivered into a `NullNotifier` in production. The dashboard
(`dashboards/app.py`) built its own four inline `SELECT`s rather than sharing
a read model — every future page would have repeated that.

Decided with the user, three scope questions: (1) the dashboard ships three
real pages (Master, Intraday Options, System Health) plus honest stub pages
for Positional Options and Intraday Stocks, whose runtimes and tables do not
exist yet; (2) operator commands never open a second writer — reads are
read-only, `stop_*` signals a PID, `square_off` writes a request the running
worker executes through its own existing square-off path; (3) delivery is five
reviewed parts, mirroring Phase 6's pattern, because six loosely-related items
each needing new subsystems is the largest remaining scope of any phase so
far.

**Part 1 builds the layer everything else in the phase reads from**, so it
went first:

- **`common/health/snapshot.py`** — `HealthSnapshot` and `read_snapshot()`,
  covering the spec's six health sub-models (process, authentication, market
  data, broker, database, strategies) from a single already-open connection.
  Deliberately takes a connection rather than a path, the same discipline
  `dashboards/app.py` already used: the caller decides whether that connection
  is `connect_readonly` or a write connection open for another reason, and
  nothing in this module can mutate the database regardless. Broker health is
  *derived* (the latest `errors` row with `component='broker'`), not polled —
  there is no live broker connection to query from a read-only connection in
  another process, and a dashboard opening its own broker connection is
  exactly the side-effecting import the dashboard forbids.
- **`common/execution/health_events.py`** — the Python-side mirror of the two
  CHECK-constrained vocabularies, and `auth_event_for_source`, the one place
  `TokenOutcome.source` ("environment"/"cache"/"generated") maps onto the
  `auth_events` vocabulary (`token_reused_from_env`/etc.).
  `ExecutionRepository.record_auth_event`/`record_feed_event` validate against
  it before ever reaching SQLite.
- **Feed events wired at every real transition**, not invented: `connected`,
  `disconnected`, `reconnect_attempted` and `reconnect_exhausted` from
  `ReconnectingFeed`'s own state-transition points (a new `on_health_event`
  hook, late-bound via `set_health_event_sink` because the feed is built
  before the supervisor's repository exists); `resubscribed` from
  `_reconnect()`; `recovered` from the existing degraded→clear check inside
  the tick callback, so nothing new runs on the tick hot path. `degraded` is
  **not** a new tick-rate check — it reuses the two alarms that already fire
  rarely (`_check_stuck_subscription`, `_raise_silent_feed_alarm`), which now
  also write a `feed_events` row alongside the `errors` row and notification
  they already sent. `stale_instrument` is new, latched per instrument the
  same way the stuck-subscription alarm is latched per run, so a quiet far-OTM
  strike produces one row, not one per poll.
- **A startup broker-health check** (`worker.py:_check_broker_health`) calls
  the long-dead `Broker.is_healthy()` once, before the first candle, and
  records an `errors` row if it says no. `PaperBroker.is_healthy()` always
  returns `True` — there is nothing for it to be unhealthy about — so this
  never fires today; it exists so the pattern is in place before Phase 10's
  `DhanLiveBroker`, where a real connectivity failure is exactly the thing
  worth knowing before the first order rather than after.
- **The startup auth outcome** reaches `auth_events` through
  `IntradayOptionsSupervisor.set_startup_auth_outcome`, called from
  `__main__.py` right after `AuthBootstrap.get_token()` succeeds. It cannot be
  recorded any earlier: the token is obtained *before* this runtime's
  database exists. A pre-database auth **failure** is therefore not persisted
  — see limitation 31.
- **The heartbeat interval is configurable**, closing the literal gap spec
  line 2482 names ("every 5 to 15 seconds is enough, and the interval is
  configurable"): `HealthConfig.heartbeat_interval_seconds` on
  `RuntimeConfig`, read into both `SupervisorConfig` and `WorkerConfig` by
  their respective `__main__.py`/`config_adapter.py` wiring points.
  `HeartbeatWriter` also gained an injectable `clock` (matching
  `ReconnectingFeed`'s own `clock`/`sleep`/`rng` pattern), so its rate-limit
  gate — previously untested except incidentally through supervisor/worker
  integration tests — now has a direct unit test
  (`tests/unit/test_heartbeat.py`).

**A real bug, found by the fail-first test discipline before it shipped.**
The first version of the feed-events wiring called
`repository.record_feed_event(...)` directly from the sink `ReconnectingFeed`
invokes — but the feed runs on its own thread (the module's own documented
"thread ownership" rule), and the repository's `sqlite3` connection was opened
on the *supervisor's* thread. `sqlite3.ProgrammingError: SQLite objects
created in a thread can only be used in that same thread` — caught by
`test_the_feeds_health_events_reach_the_repository_once_run_opens_it` on the
first run, not discovered later. Fixed the same way the supervisor already
solves the opposite-direction problem (`_control_queues`/
`_drain_control_queues`): the sink only enqueues onto a plain
`queue.Queue[FeedHealthEvent]`; a new `_drain_feed_health_events`, called from
the same 1-second poll loop that already drains control queues and checks the
stuck-subscription alarm, is the only thing that ever calls
`repository.record_feed_event`. See **D70**.

**Deliberately not done in Part 1:**

- Telegram is still never constructed in production — Part 2.
- The dashboard still reads nothing from this new layer — Part 3. It remains
  the single Phase-1 tile today.
- No PID/lock hardening, no operator commands — Part 4.
- No retention, no backups — Part 5.
- A pre-database auth *failure* (a rejected credential, a rate limit, before
  this runtime's database exists) is not persisted to `auth_events` — see
  limitation 31. Persisting it would mean opening (and therefore creating) the
  operational database before knowing whether the runtime should start at
  all, which is a larger design question than Part 1's scope.

### Phase 6 — paper recovery and expiry handling — **Complete, all five parts**

A pre-work audit (spec's Phase 6 bullets read directly from
`ALGO_TRADING_FORWARD_TESTING_ARCHITECTURE_FINAL.md:2905-2910`, checked against
the codebase before any implementation) found Phase 6 does **not** follow Phase
5's "mostly wiring" pattern: bullet 1 (restore positions and strategy/risk state)
was roughly 60% already built and tested — `recover_position`, `PositionManager.
adopt`, persisted square-off state and mode-separated recovery all shipped in
Phases 3-5 — but bullets 3 and 4 (`force_square_off_before_expiry`, exchange-
settlement simulation) are entirely absent from the repository, and three of
bullet 2's four items (fixed strikes, basket legs, rolling counters) are blocked
on `FixedStrikeEngine`/`MultiLegEngine` not being ported, per D56/D34's "needs a
real consumer" reasoning.

Part 1 closes the audit's single highest-value finding, and it is not from the
spec's own bullet list: `DailyRiskGuard.reset()` zeroed realised P&L and trade
count on **every** restart, including one after a loss the day's cap should
already have stopped, and `strategy_state.daily_realised_pnl` — already written
by every closed trade — had no reader anywhere. A worker restarting after a loss
began the day's loss cap from zero. `DailyRiskGuard.restore()` seeds the guard
from what a previous process already booked, running the guard's own evaluation
so an already-past-cap restart halts immediately at the *remaining* headroom
(never a fresh full cap); `TradingEngine` gained an injected `recover_daily_risk`
provider mirroring `recover_position`'s shape, including fail-closed behaviour on
a raising provider; `engine_worker.recover_daily_risk` supplies it from the
existing column plus a `positions.status='CLOSED'` count (queried, not a new
stored counter — the same "auditable in SQL, no migration" reasoning `closed_
position_count` documents).

**Building the column's first reader found it was silently wrong.**
`_touch_strategy_state` overwrote `daily_realised_pnl` on every fill rather than
accumulating it, so a strategy that closed one contract and opened a *different*
one the same day lost the first contract's booked P&L the moment the second
contract's first fill landed — the same bug shape Phase 4 Part 5 already found
and fixed on the sibling `orders.filled_quantity`/`average_fill_price` columns,
missed here because nothing read this one back until now. Fixed at the source
(`ExecutionRepository._upsert_position` now returns the true per-fill delta) with
fail-first evidence (`test_daily_realised_pnl_accumulates_across_contracts_not_
just_the_last_one`, demonstrated failing against the pre-fix code). See **D58**
and limitation 22.

**Deliberately not done in Part 1, each for a stated reason** — the plan's own
standing rule (no persisted value or docstring claim describing a safety
behaviour that no code performs) ruled out the cheaper-looking alternative in
each case:

- `strategy_state.re_entry_count` stays unwritten. §7 lists it, but writing it
  "counted, not enforced" would be a queryable, persuasive half-claim next to
  D52's existing hardcoded `risk_decision=ALLOWED` — and it would duplicate the
  now-restored `max_trades` counter, which is actually enforced. Phase 9
  populates it alongside the real §13 cap.
- MFE/MAE, square-off attempts, state-version validation and last-processed-
  candle idempotency (the rest of §7's "restore at minimum" list) are Part 3.
- Exit-policy state (trailing peak, momentum streak) and stop/target
  persistence on the engine path are Part 2 — see below.

| Property | Status |
|---|---|
| Daily loss cap survives a restart, at remaining headroom | **Done** — `DailyRiskGuard.restore`, fail-first proven |
| Daily trade-count limit survives a restart | **Done** — same mechanism, unit-proven (no worker config exposes `max_trades` yet — limitation 23) |
| A restart on a fresh trading date starts the cap at zero | **Done** — no-leak control test |
| An unrestorable `daily_realised_pnl` fails closed, not silently | **Done** — mirrors `recover_position`'s CRITICAL-error pattern |
| `daily_realised_pnl` accumulates across contracts in one day | **Fixed** — was silently wrong before Part 1 (D58 / limitation 22) |
| Fixed strikes, basket legs, rolling counters | **Still blocked** — no `FixedStrikeEngine`/`MultiLegEngine` consumer |
| `force_square_off_before_expiry` | **Done** — Part 4, see below |
| Exchange-settlement simulation (`simulate_exchange_settlement`) | **Not implemented, refused at config load** — Part 4, see below and limitation 27 |

**Part 2 — position-management state snapshot/restore.** Pre-work found the
plan's own bullet ("add `snapshot()`/`restore()` to `BaseExit`/`RiskManager`,
populate `positions.stop_price`/`target_price`") understated four real gaps —
see **D59-D61** for the mechanisms, and the plan file's own record of the
pre-work for the reasoning behind each:

1. **No possible stop/target producer, at any depth** — `RiskManager.
   new_position()` received no entry price, so no manager (today or a
   hypothetical Phase 9 one built against the old interface) could ever
   compute an absolute price level. Put to the user directly — the one
   genuine either-way fork in the part — and decided: widen the interface
   now rather than add inert properties and defer the plumbing. See **D59**.
2. **Nothing exposed a strategy's exit engine to the engine layer.**
   `BaseStrategy` gained `exit_state_snapshot()` / `restore_exit_state()`,
   no-op defaults, mirroring the pair one layer down on `BaseExit`.
3. **No per-candle persistence hook existed on the engine path at all** —
   `strategy_state.payload` was touched only on a fill before this part.
   `TradingEngine` gained an injected `persist_exit_state` callback, called
   after every candle a position is open for. Genuinely new write-frequency
   behaviour, recorded rather than folded silently into "reuse
   `merge_payload`". Its multi-worker contention cost is unmeasured — see
   **D61** and **limitation 24**.
4. **`EngineFixtureStrategy` could only drive `MomentumCloseExit`'s own
   convenience method**, not the generic `BaseExit.should_exit()` contract
   `TrailingExit`/`HighestCloseExit`/`ConsecutiveReversalExit` implement —
   widened with a default-preserving `exit_engine_name` kwarg so the plan's
   own test scenario (a trailing stop's peak surviving a restart) could be
   proven through the real worker, not asserted at the unit level only.

**Exit-state recovery is deliberately fail-*open*** — log and continue with
the strategy's already-reset (empty) state — the only recovery mechanism in
Phase 6 that is, because a wrong or missing snapshot degrades exit timing
quality, never safety, unlike position or daily-risk recovery. See **D60**.

| Property | Status |
|---|---|
| `TrailingExit`/`HighestCloseExit`/`ConsecutiveReversalExit` snapshot/restore | **Done** — round-trip unit-proven, `CompositeExit` delegates by label |
| A trailing stop's peak survives a real worker restart and still exits | **Done** — fail-first proven through `run_worker`, with a same-shape negative control |
| A snapshot naming a different contract is ignored, not applied | **Done** — position still adopts; no `errors` row (fail-open, not fail-closed) |
| `stop_price`/`target_price` reach `positions` through the full path | **Done** — plumbing only; every value is `None` under today's `FixtureRiskManager` |
| Negative control: `stop_price`/`target_price` stay NULL under today's config | **Done** — will fail the day a real risk manager reports either, by design |
| `RiskManager.snapshot()`/`restore()` (internal state, distinct from stop/target) | **Done** — mechanism proven by a local stateful test double, not `FixtureRiskManager` |

**Part 3 — the remaining §7 "restore at minimum" gaps.** Same pattern as Parts
1-2: each item named a destination but not a write *path*, and three of the
five needed a real path invented, not just wired. See **D62-D65** for the
mechanisms.

1. **MFE/MAE had no write path at all**, at any point in a position's life —
   `highest_favourable`/`lowest_favourable` were read by `_row_to_position`
   and set by nothing, ever, including at close. A new
   `ExecutionRepository.update_position_marks` (direct `UPDATE`, outside the
   fill path — `_upsert_position` only runs on a fill) is called from the
   **same** per-candle-while-open checkpoint Part 2's `_persist_exit_state`
   already established, not a new one. Seeded on adopt via two new
   `AdoptedPosition` fields, applied to the constructed `OpenPosition` before
   any mark so a restored baseline (not zero) is what the first post-restart
   tick compares against — the `PositionManager.adopt` docstring's
   pre-Part-3 "MFE/MAE deliberately restart at zero" is corrected in the same
   change.
2. **Re-entry count — unchanged from Part 1's own reasoning.** Still
   deliberately unwritten; nothing found on review changes it.
3. **Square-off attempts needed `save_strategy_state` widened** —
   `square_off_attempts` was not a parameter and the SQL never referenced the
   column. `increment_square_off_attempts: bool = False`, accumulated in SQL
   (the same pattern D58 fixed `daily_realised_pnl` onto), incremented on
   *every* `PersistedSquareOffAuthority._save()` call — a normal day reaches
   exactly 2, a crash-forced retry reaches higher. See **D63**.
4. **State schema version validation needed three sub-decisions the plan's
   one-liner didn't make**: where `CURRENT_STATE_VERSION` lives (`common/
   models/trading.py`, keeping `common.execution` out of the `common.engine`
   runtime-import direction Parts 1-2 protected — **D62**); that
   `save_strategy_state` must stamp it explicitly, not rely on the schema
   default (**D62**); and that `recover_position`'s own unwrapped
   `read_payload` call needed a try/except to keep the `CRITICAL`-row
   precedent every other position-recovery failure gets (**D64**).
   `read_payload` now raises `UnsupportedStateVersion` on any
   `state_version != CURRENT_STATE_VERSION` — the one deliberate narrowing
   of its "never raises on bad data" rule.
5. **Last-candle idempotency, put to the user directly — the one genuine
   either-way fork in the part.** Write `last_candle_end_at` unconditionally
   on every candle (the spec's literal day-level framing) or gate it on
   "position open" like Part 2, avoiding new write frequency on top of
   limitation 24's already-unmeasured contention question. Decided: gate it.
   The residual — a flat day's candles are not idempotently resumable — is
   recorded as **limitation 25**, not left implicit. See **D65**.

**A planned test was found architecturally impossible while building it, not
before, and corrected rather than forced.** The draft expected exit-state
recovery's fail-open path (D60) to be independently provable through the same
`state_version` corruption that fails position recovery closed. It cannot be:
`state_version` gates the whole `strategy_state` row, not a key within it, and
`recover_exit_state` is only ever called from inside `_adopt_recovered_position`
— after `recover_position` has already succeeded. A version bad enough to
block one blocks both, and position recovery, which runs first, always
intercepts it. The test now asserts what is actually true (both fail
together); D60's fail-open mechanism remains real and provable for the
failures Part 2's own test already covers (a foreign `security_id`, or no
snapshot at all), which do not depend on `state_version`. See **D65**.

| Property | Status |
|---|---|
| MFE/MAE survives a restart rather than resetting to the first post-restart tick | **Done** — fail-first proven through `run_worker` |
| `PositionManager.adopt`'s "MFE/MAE restart at zero" docstring | **Corrected** — was permanent policy, is now the pre-Part-3/no-data default |
| Square-off attempts persist and accumulate | **Done** — a normal day reaches 2, a forced retry reaches more, unit-proven on `PersistedSquareOffAuthority` directly |
| An unrecognised `state_version` blocks position recovery | **Done** — `CRITICAL` `engine.recovery` row, same as every other recovery failure |
| The same corruption reaching exit-state recovery | **Architecturally unreachable when a position is open** — position recovery always intercepts it first; test corrected to assert this |
| A restored candle watermark blocks reprocessing | **Done, position-gated** — fail-first proven, with a same-tape no-watermark control |
| A flat restart does not inherit a stale watermark | **Done** — proven directly |
| Idempotent replay on a flat (no position) day | **Not covered** — limitation 25 |

**Part 4 — `force_square_off_before_expiry` and the settlement gate.** Pre-work
(exploration + a dedicated Plan pass, both read before any code) found the spec
underspecifies two things `grep` confirmed exist nowhere in the repository or
either doc: how `expiry_policy` fits the config hierarchy (shown as a bare YAML
key in spec section 11, absent from section 9's own "required resolved strategy
fields" list), and `square_off_before_expiry_days` itself, which names nothing
the spec or the runbook had used before. Both forks were put to the user
directly before writing any code, not decided unilaterally:

1. **When an overdue day fires.** Immediately, at the first `due()` — any time
   of day, not gated on the ordinary `square_off_at`. Firing only at the
   existing square-off time would make the whole trigger behaviourally
   identical to the clock it composes into, which is not testable and adds
   nothing observable. Decided with the user.
2. **The lead's default and unit.** `0`, calendar days. The last day a contract
   may be held is expiry day itself, so every existing intraday config and
   test stays behaviour-unchanged unless a run genuinely outlives its
   contract's expiry date. Trading-day counting was considered and rejected
   for now — it needs a holiday calendar `SquareOffPolicy` does not have, and
   is meaningless at a zero-day default anyway. Recorded as **limitation 26**
   rather than built ahead of a real need. Decided with the user.

**The composition, not a second decider.** `SquareOffPolicy.trigger_at` gained
one optional `expiry` keyword, checked after the persisted-state guard and
ahead of the existing time-of-day ladder — every call site that omits it (the
policy's own tests, the fixture path, `SessionSquareOffAuthority`, which is not
touched at all) is byte-identical to before. `PersistedSquareOffAuthority`
gained a keyword-only `expiry` constructor argument, resolved once by
`engine_worker._build` from whichever resolver `build_option_selector` chose —
`DhanOptionChainResolver.expiry` (a real ISO date) when one is wired,
`engine_config.expiry` (`None` by default) otherwise, so every simulated/fixture
run keeps today's behaviour without being told to. See **D66**.

**The unsafe alternative is refused, not stubbed.** `simulate_exchange_settlement`
is the spec's only other permitted value, gated by its own text ("only after
settlement tests pass"). None of spec section 11's eight settlement-policy
items exist in this repository — expiry calendar/last-trading-day handling,
final settlement price capture, ITM/OTM determination, index-option cash
settlement, exercise/assignment recording, effective-dated exercise STT and
other charges, T+1 timing, stock-option physical-settlement obligations —
so building a shell of it would be exactly the "untested code that merely
looks finished" D34 already declined to do elsewhere. A `field_validator` on
`StrategyConfig.expiry_policy` refuses the value at config load with the
precondition named in the raised `ConfigError`; `SquareOffPolicy.__post_init__`
refuses it again, independently, as defence in depth against direct
construction bypassing the loader. See **limitation 27**.

**Inert, not overdue, on a bad expiry — the opposite direction from the
persisted-state precedent.** An unreadable persisted `square_off_state` fails
*towards* squaring off, because nothing else will ever close the position if it
does not. The expiry rule cannot borrow that direction: `SimulatedOptionChainResolver`'s
placeholder `"WEEKLY"` expiry is the *common* case every existing
config carries, not an edge case, and failing it towards `SQUARE_OFF` would
force-close every fixture run on its first tick. Since the ordinary
time-of-day ladder still runs when the rule is inert, there is no unsafe
direction to fail towards here. Logged once, at authority construction, not
per `due()` call. See **D67**, **limitation 28**.

**`expiry_policy`/`square_off_before_expiry_days` are typed `StrategyConfig`
fields, not `risk:` keys** — `risk` is an untyped dict that `_StrictModel`'s
`extra="forbid"` cannot reach inside, so a typo there would be silently
ignored rather than refused, exactly the failure every other top-level safety
field is protected from. See **D68**.

**A fingerprint-stability audit was run before accepting the field additions'
cost, not assumed harmless.** Adding fields to `StrategyConfig` moves
`fingerprint(cfg)` for every strategy, including ones whose YAML did not
change. Checked rather than waved through: `config_fingerprint` is write-only
everywhere in this repository — no `SELECT`, comparison or lookup keyed on it
exists in `common/`, `runtimes/`, `scripts/` or `dashboards/` — so nothing
breaks. The residual cost (an old row now reads as "different configuration"
when it was not) is recorded as **limitation 29**, along with the one real
dependency found: spec ARCH:3003 scopes live approval to a configuration
fingerprint, which is unimplemented today but which Phase 10 must be told the
fingerprint domain moved here before relying on it.

An end-to-end integration test (`tests/integration/test_engine_square_off.py::
test_an_overdue_expiry_force_closes_an_open_position_end_to_end`) drives the
real `TradingEngine`, a real `PersistedSquareOffAuthority` and a real SQLite
database across two threads (the engine's own, since the test spawns it the
same way the cross-thread square-off tests in the same file already do):
entry happens on the contract's own expiry date (not yet overdue at the
zero-day default), the position stays open until a tick dated the next
calendar day arrives, and that single tick force-closes it with
`ExitReason.SQUARE_OFF` before any time-of-day check — proven failing first
against the trigger with the composition disabled, restored, then proven
passing.

| Property | Status |
|---|---|
| An overdue expiry force-closes before the entry cutoff, at any time of day | **Done** — fail-first proven at both the policy-unit and real-engine/real-database level |
| Persisted `COMPLETED`/`IN_PROGRESS` still suppress an overdue expiry, exactly as they suppress a post-`square_off_at` restart | **Done** — unit-proven against a real `ExecutionRepository` |
| The expiry-driven trigger writes `IN_PROGRESS` once, not per tick, and reaches exactly 2 attempts on a normal day | **Done** — same invariant Part 3 pinned for the time-of-day trigger, now proven for this one too |
| A restart on an already-overdue day reads `COMPLETED` and does not re-close | **Done** — unit-proven |
| Every existing simulated/fixture config is behaviour-unchanged (no expiry configured) | **Done** — full suite green unmodified elsewhere, plus an explicit byte-identical-trigger test |
| An unparseable or missing expiry is inert rather than overdue | **Done** — unit-proven, with a construction-time WARNING pinned via `caplog` |
| `simulate_exchange_settlement` is reachable by no configuration | **Done** — refused at config load and at direct `SquareOffPolicy` construction, independently |
| Exchange-settlement simulation itself (spec section 11's eight items) | **Not built** — limitation 27, the gate this part does not cross |
| `square_off_before_expiry_days` honours holidays | **Not built** — limitation 26, calendar days only |
| `config_fingerprint` stability across this change | **Audited, not assumed** — write-only everywhere today; residual cosmetic cost recorded as limitation 29 |

**Part 5 — the phase's record, not more code.** With bullets 1, 3 and (by
refusal) 4 done, the question this part actually answers is whether Phase 6
is substantively complete or whether something is being marked done that
is not. Re-read fresh rather than assumed:

1. **Bullet 1** ("restore open paper positions and strategy/risk state by
   strategy and mode") is done for every position this repository can
   produce — recovery is mode-separated and strategy-scoped, tested across
   Parts 1-3. D56 had glossed the bullet's "by strategy and mode" as also
   meaning "not by date," flagging the `trading_date`-scoped UNIQUE keys as
   an unmet part of the bullet. That gloss is a reasonable reading but not
   the bullet's literal text, and nothing today needs a position to survive
   a `trading_date` boundary — square-off, including Part 4's expiry
   trigger, always resolves within the trading date it started in.
2. **Bullet 2**'s fixed strikes, basket legs and rolling counters are
   unchanged: still blocked on `FixedStrikeEngine`/`MultiLegEngine` not
   being ported, the same D56/D34 "needs a real consumer" reasoning that
   has held since Phase 5. Not this phase's to close.
3. **Bullet 3** — done, Part 4.
4. **Bullet 4** ("add exchange-settlement simulation *before* any strategy
   intentionally holds through expiry") is a conditional, not an
   unconditional build order. Nothing in this repository can hold through
   expiry — `simulate_exchange_settlement` is refused outright — so the
   precondition is enforced, not merely unmet. Read this way, the bullet is
   satisfied by refusal, not left undone.

**D56's residual gap got one real question, checked before deciding what to
do about it.** Is the `trading_date`-scoped persistence identity worth
fixing now, given it is real and named as "a genuine structural blocker" in
D56's own words? The blast radius was checked, not assumed: `trading_date`
is a mandatory, exact-match parameter on 6+ `ExecutionRepository` methods,
every recovery function in `engine_worker.py`, `PersistedSquareOffAuthority`'s
own key, the fixture path, and `WorkerConfig.trading_date` itself — plus
dozens of existing tests asserting on that exact shape. Building a fix now
would mean guessing what "a cycle" operationally means (calendar days? an
explicit roll event? adjustment legs?) with no real positional strategy to
answer that question — the same "inventing Phase 6's answer out of order"
trap D56 named in Phase 5, just relocated to now instead of avoided. Put to
the user directly (implement now vs. document only); decided: document
only. **D69** records the candidate direction — an additional `cycle_id`
column, `trading_date` left untouched — as a starting point for whoever
eventually builds it, explicitly not a commitment. `runtimes/
positional_options/__init__.py` updated to cite it instead of gesturing at
"Phase 6" in the abstract. See **limitation 30**.

No code, no migration, no test changed in this part — the record is the
deliverable.

| Property | Status |
|---|---|
| Spec bullet 1 (restore positions/strategy state by strategy and mode) | **Done**, for every position this repository can produce |
| Spec bullet 2 (fixed strikes, basket legs, rolling counters) | **Still blocked** — no `FixedStrikeEngine`/`MultiLegEngine` consumer, unchanged since Phase 5 |
| Spec bullet 2 (custom exit state) | **Done** — Part 2 |
| Spec bullet 3 (`force_square_off_before_expiry`) | **Done** — Part 4 |
| Spec bullet 4 (exchange-settlement simulation before holding through expiry) | **Satisfied by refusal** — nothing can hold through expiry today |
| D56's persistence-identity question | **Given a written candidate direction, not implemented** — **D69**, limitation 30 |
| `runtimes/positional_options/__init__.py` | **Updated** to reflect Part 4's exit-timing answer and cite D69 |

### Phase 5 — mixed-mode supervisor and persistence — **COMPLETE**

A pre-work audit found most of the phase's machinery already built in Phases
1-4 but unwired — config discovery had no non-test caller, and there was no
CLI entrypoint anywhere in the repository. So this phase was predominantly
wiring plus proof, not new subsystems; the one real behaviour change is
`IntradayOptionsSupervisor.add_worker` refusing a live-mode strategy
individually instead of aborting the whole group. Duplicate-worker prevention
(spec bullet 4) turned out to already be satisfied by the existing lock's
mode-free identity — closed with two regression tests, not new code. Full
detail, including the positional-options rollout split (inert scaffolding
shipped, supervisor/worker/persistence deferred to Phase 6/9, **D56**) and
the one open mechanism gap (**limitation 21**), is in "What Phase 5
delivered" above.

| Property | Status |
|---|---|
| Config discovery + `WorkerConfig` adapter + CLI entrypoint | **Done** |
| Mixed-mode admission (blocked live strategy does not stop the group) | **Done** |
| Duplicate-worker prevention across mode | **Audited and proven — already closed since Phase 1** |
| Mode separation, proven against real persisted state | **Done** — schema-swept, `strategy_id`-keyed |
| Instrument-class rollout — positional options | **Split**: inert config/package scaffolding shipped; supervisor/worker/persistence deferred to Phase 6/9 — see D56 |
| Instrument-class rollout — intraday stocks | **Deliberately deferred, no scaffolding** — D34's "needs a real consumer" reasoning unchanged |
| Real risk gate | **Not this phase** — `RISK_BLOCKED` still has no producer (D52, unchanged) |

**Mixed-mode architecture gate (spec section 6), checked against what shipped:**

- Configuration supports one paper and one live-designated strategy in the
  same group — **yes**, proven with real `WorkerConfig`s in one supervisor.
- With global live disabled, the live-designated strategy is blocked rather
  than rerouted to paper — **yes**, `LiveExecutionBlocked`-style refusal,
  never a fallback broker.
- The paper strategy continues safely — **yes**,
  `test_a_blocked_live_worker_does_not_stop_the_paper_strategy`.
- Broker factory tests prove strategy-wise routing — **already true since
  Phase 1** (`common/broker/factory.py`, D5), unchanged by this phase.
- P&L, correlation IDs and positions remain mode-separated — **yes**, proven
  end to end rather than merely by schema, across every table that carries
  `strategy_id`.

### Phase 4 — candle, indicator and paper-execution foundation — **COMPLETE**

**All five parts are complete, including the live gate item.** The opt-in
Full-mode depth capture against a real `NSE_FNO` option — the item that stood
between Phase 4 and done — ran live on 6 August 2026 and passed clean. The
command is in section 5. Part 5's central claim — that a real option in mode 21
delivers a two-sided book — is now observed against the real socket, not just
inferred from the SDK's source. Along the way it surfaced and closed known
limitations 18, 19 and 20 — none of them Part 5 defects, all found only because
this was the first time the live path was actually exercised end to end.

Split into **five parts, in strict order**, using the rule Phase 3 used five times:
separate what is provable offline from what changes the deployed shape, and keep
independent concerns apart. **Stop for review after each part.**

| # | Part | Closes | Gated by | Status |
|---|---|---|---|---|
| 1 | Real contract resolution + the live rehearsal | 17; alarms 15 | — | **COMPLETE** (1 Aug 2026) |
| 2 | Indicator layer (EMA/RSI/VWAP/ATR/ADX) | D21 | — | **COMPLETE** (1 Aug 2026) |
| 3 | Candle continuity, session/timezone, wall-clock square-off | 4, 7, a live blocker | — | **COMPLETE** (1 Aug 2026) |
| 4 | Warm-up source and injection | 16 | 2, 3 | **COMPLETE** (5 Aug 2026) |
| 5 | `PaperBroker` realism | 5, D11 | 1 | **COMPLETE** (code 5 Aug 2026; live gate item 6 Aug 2026) |

**Why Part 1 came first**, since the ordering was not obvious and the reason
recorded when this list was first written was the weaker of the two: it is not
merely that limitation 17 blocked a live rehearsal. It is a **hard precondition
for Part 5**. The fill model needs bid/ask; indices carry none; the only source of
real option depth is a Full-mode subscription on a real `NSE_FNO` option
`security_id`. `paper.py`'s own docstring already said building the model against
a depth-free tape was the wrong move — Part 1 is what makes a depth-carrying tape
possible at all. **Part 5 bore that out**: the mode split it needed (index on
Ticker, contract on Full, one socket) is a direct extension of the segment split
Part 1 added, and reuses its per-security map, its `subscribe` keyword and its
control-queue tuple.

**Part 2 — the indicator layer — COMPLETE.** All five ported, `ConfirmedCrossover`
with them, `AdxAtrClassifier` registered (D21 closed), and the `pandas-ta-classic`
oracle behind a boundary test. Full record: "What Phase 4 Part 2 delivered"
(section 1), deviations D38-D39, and the Part 2 gate evidence in section 4.

**Read the three statements at the head of that section before relying on this
part.** In short: only 14 reference tests existed to port, RSI had none at all,
and the oracle is a cross-check rather than the live path. The port is sound and
its evidence is thinner than Part 2a's was — both are true and the second is the
one easily forgotten.

**Part 3 — continuity and the wall clock — COMPLETE.** Limitations 4 and 7 closed,
plus a live-blocking timezone defect the audit turned up: the engine treated every
real (UTC-aware) tick as out-of-session, so on a live feed it would have built no
candles and placed no orders, silently. Full record: "What Phase 4 Part 3
delivered" (section 1), deviations D40-D42, and the Part 3 gate evidence in
section 4.

**Limitation 2 is still open and Part 3 did not touch it.** Wiring
`ReconnectingFeed` into the supervisor put Phase 2's backoff and resubscription on
the live path for the first time — they had no production caller at all — but none
of it has been exercised against a real socket drop.

**Part 4 — the warm-up source — COMPLETE.** `WarmupManager`/`WarmupSource` ported;
`historical.py` ported with real adaptation (frozen `Candle`, D40-safe trading-day
walking) rather than verbatim, since a straight port would have broken this
repository's SDK-isolation boundary and reproduced a real request-format bug —
both found and fixed before anything was written, not after. `history_provider`
was deliberately left unwired (the manager path subsumes it), and only the
multi-session `fetch_warmup_candles_range` was ported, not the reference's
today-only convenience wrapper. `coordinator.py` stayed out, as planned — it
coordinates across strategies, which is still Phase 5. `warmup_manager`/
`warmup_source` are now injected at `engine_worker` behind an opt-in
`warmup_source: dhan` flag; their `TradingEngine` annotations are tightened from
`object | None` to real types. **A same-part amendment (D47) also closed a
pre-existing Phase 3 gap** found while writing the end-to-end test:
`validate_warmup_config` used to only check the `warmup_from_history` flag, so
a continuity-required strategy with the flag true (every config's default) but
no manager supplied sailed past construction and cold-started with only a
`WARNING` — it now refuses construction outright in that case too. Full
record: "What Phase 4 Part 4 delivered" (section 1), deviations D43-D47,
limitation 16 closed (with both the stated residual and this gap corrected
rather than left overstated), and the Part 4 gate evidence in section 4.

**Part 5 — `PaperBroker` realism — COMPLETE.** Code complete 5 August 2026; its
live gate item ran and passed 6 August 2026, closing known limitation 20 along
the way. Limitations 5 and D11 fully closed; new deviations **D48-D53** (D48,
D51, D53 remain open by design — documented scope boundaries, not gate items).
Full record: "What Phase 4 Part 5 delivered" (section 1).

**Two notes written here while planning turned out to be wrong, and are corrected
rather than deleted, because both were load-bearing:**

* `"Full Data"` in neither `_TICK_TYPES` nor `_NON_TICK_TYPES` would **not** have
  been "counted as malformed". `normalise` falls through to the unrecognised-type
  branch: `non_tick_frames` and a **debug** log. The real failure was a connected,
  silent feed producing zero ticks — worse, because a malformed count is at least
  visible.
* The plan's four-places-to-widen list (adapter, gateway protocol, `Signal`,
  `Quote`) was right about where depth dies and wrong about the fix. Widening
  `ExecutionGateway` and `Signal` means editing `PositionManager`, a Phase 3 port,
  for no gain; one shared `QuoteBook` serves both the lifecycle's quote and the
  broker's latency buffer, and the port is untouched.
* The plan also proposed adding `bid_quantity`/`ask_quantity` to `Quote`. **Dropped
  deliberately.** Nothing would consume them: slippage is not quantity-aware (spec
  section 6 defers that explicitly) and partial fills come from a policy hook
  rather than from depth. `Tick` carries no depth quantities either, so populating
  them would mean widening the IPC payload on every tick for a field with no
  reader — the same "declarations that lie about being supported" that
  `broker/base.py` has refused since Phase 1.

The comment claiming "Quote/Full add depth" *was* wrong as recorded, and is fixed
with the code: `process_quote` (mode 17) returns volume and session OHLC and no
book; only `process_full` (mode 21) carries one.

**~~Read "What is asserted rather than proven" before treating this part as
done.~~ Resolved, 6 August 2026** — see that section: the claim that a real
`NSE_FNO` option in mode 21 delivers a two-sided book is now proven live, not
just asserted offline.

**Struck from Phase 4 scope: "mode-namespaced correlation IDs."** The architecture
document lists it as a Phase 4 bullet, but it has been delivered since Phase 1 at
three layers — `common/execution/correlation.py` (`p_`/`l_` prefixes, `MAX_LENGTH`
25, round-trip parse), `OrderIntent.correlation_namespace`, and a
`CHECK`-constrained `order_intents.correlation_namespace` column — and is covered
by `tests/unit/test_correlation_ids.py`. Recorded here so it is not re-planned.

**Constraints, all parts:** paper mode only; `DhanLiveBroker` order placement
unimplemented and fail-closed (Part 5 widens the *simulator*, never the live
path); no `MultiLegEngine`/`FixedStrikeEngine`; no real strategies (Phase 9);
nothing written under `Trading_Automation`.


### Phase 3 — preserve custom engines and policies — **COMPLETE**

**Phase 3 has five parts, in strict order.** Part 1 was a dedicated fix — planned
and reviewed on its own, before the port began, not folded into it. Part 2 was
then split three times: the exit registry does not depend on `TradingEngine`, so it
was ported and proven on its own (Part 2a); the engine's port is separable from the
seams that wire it in, so the port landed first (Part 2b-i); and the two remaining
seams are independent of each other, so the feed seam landed on its own
(Part 2b-ii-A) and the execution seam followed (Part 2b-ii-B, itself split into B-1,
what is provable offline, and B-2, what changes the deployed process shape). Every
split used the same rule, and B-2's outcome is the evidence for it: the part that
finally put `common.engine` into a worker's process is the part where the import
boundary, the SQLite thread affinity and the missing tick sentinel all surfaced.

#### Part 1 — live-feed shutdown path — **COMPLETE** (30 July 2026)

Closed runbook limitation 1. All three requirements delivered, and the acceptance
gate met in full:

| Required | Status |
|---|---|
| Signal handling in the supervisor | **Done** — `SIGTERM`/`SIGINT` handlers, installed for the feed's lifetime and restored after; ordered shutdown; the feed moved to its own daemon thread so the main thread is free to receive them |
| Thread-safe stop coordination in `ReconnectingFeed` | **Done** — ownership rule promoted into the `MarketFeedAdapter` contract; `stop()` routes by ownership; `request_stop()` is the thread-safe half; `wait_until_stopped()` lets a caller join on the real thing |
| A real cross-thread start/stop test, **failing first** | **Done** — `tests/integration/test_feed_cross_thread_shutdown.py`, plus a real-signal end-to-end suite. Pre-fix and post-fix output recorded in section 1 |

What it changed, why, and the residual limitation: see "What Phase 3 Part 1
delivered" (section 1), the Part 1 gate evidence (section 4), and limitation 13.

#### Part 2a — port the exit-policy registry — **COMPLETE** (31 July 2026)

Delivered item 2 of the original Part 2 list: `framework/exit/` (all ten policies,
both wiring paths), plus the `SuperTrend`/`OHLC` and `WarmupRequirement`/
`parse_hhmm` subset the policies need, plus the three enums they read. 44 tests,
all passing; D2 and D3 confirmed against the ported code. Full record: "What
Phase 3 Part 2a delivered" (section 1). One carried-over mislabelled test recorded
there rather than fixed, to keep the ported suite diff-comparable.

**The registry has no caller yet.** The engines are exercised only by duck-typed
test doubles, exactly as the reference exercised them. This repository's own
`Position` model has none of the four attributes they read
(`.contract.option_type`, `.side`, `.entry_price`, `.last_price`) — closing that
gap is Part 2b's job, not a defect in the port.

#### Part 2b-i — signal ownership + the `TradingEngine` core port — **COMPLETE** (31 July 2026)

Delivered items 1, 4 and 5 of the original Part 2b list, and resolved the blocker
that stopped the part being started as a straight port. `TradingEngine` no longer
installs a signal handler; it is *told* to square off through an event, and acts on
it at boundaries owned by the thread already running it. Full record: "What Phase 3
Part 2b-i delivered" (section 1), deviations D18-D22, and the Part 2b-i gate
evidence in section 4.

The original blocker text is preserved in the git history of this file; the
resolution and the reason it took the shape it did are now in D18.

#### Part 2b-ii-A — the feed seam — **COMPLETE** (31 July 2026)

Delivered item 1 of the Part 2b-ii list below, the half of item 3 that belongs to
the feed (the sentinel → `request_square_off` mapping), and the **confirmation**
item 4 required before anything could be implemented. 39 new tests, suite 620 →
659, both walking-skeleton gates re-measured and green. Full record: "What Phase 3
Part 2b-ii-A delivered" (section 1), deviations D23-D25, limitations 14 and 15, and
the Part 2b-ii-A gate evidence in section 4.

It also **found and fixed a real hang**: undelivered ticks on a
`multiprocessing.Queue` blocked the supervisor's own process exit, with no error
and no exit code, at a measured ~65 KB. The candle channel never came close; the
tick channel reaches it in a few hundred ticks. That is a shutdown path that can
itself hang, which is the failure Part 1 exists to prevent — fixed at the cause
(D25) and pinned by a regression test.

**`worker.py` and `FixtureSignalStrategy` are still untouched**, so the gates keep
measuring what they measured before. The child does not yet receive the tick or
control queues; that is 2b-ii-B.

#### Part 2b-ii-B-1 — the execution seam — **COMPLETE** (31 July 2026)

Part 2b-ii-B was split in two, for the reason every previous split had: separate
what is provable offline from what changes the deployed process shape. B-1
delivered items 2 and 4 below plus the entry-block that closed limitation 14's open
half — 67 new tests, suite 659 → 726, both walking-skeleton gates re-run and green,
and the worker's import graph re-measured at **zero `common.engine` modules, 0.100 s
median**, confirming B-1 did not touch the risk that B-2 owns.

Full record: "What Phase 3 Part 2b-ii-B delivered — Part B-1" (section 1),
deviations D26-D29, limitation 14, and the B-1 gate evidence in section 4. It also
found a real cost it had not predicted: publishing the drop notice into an already
full queue evicts an item, doubling the loss rate while the overflow lasts (D28).

#### Part 2b-ii-B-2 — the wiring — **COMPLETE** (1 August 2026)

The last part of Phase 3. Items 3, 5, 6, 7 and 8 below are all delivered; items 2 and
4 were done in B-1. Full record: "What Phase 3 Part 2b-ii-B delivered — Part B-2"
(section 1), deviations D30-D32, limitation 13's engine half closed, new limitation 16,
and the B-2 gate evidence in section 4.

3. **Wire the engine into `worker.py`** — **DONE.** `WorkerConfig.engine`, an
   `EngineWorkerConfig` of primitives only, and one deferred import into
   `runtimes/intraday_options/engine_worker.py`. The import graph is unchanged at
   **0.110 s median, zero `common.engine` modules**, enforced by
   `tests/unit/test_worker_import_boundary.py` rather than by convention. The
   strategy travels as a dotted `module:Class` reference plus kwargs (**D30**), as
   this section predicted it would have to.

   One thing this section got wrong, and it is corrected rather than quietly
   dropped: it specified
   `with shutdown_signals(engine.request_square_off): engine.run()`. The handler also
   has to set a local event, because the worker needs to know a shutdown was
   requested in order to check afterwards whether the book is actually flat — the
   same reason `supervisor._on_signal` sets `signalled`.
5. **Deliver the queues from the supervisor** — **DONE.** Both queues reach the
   child, the `None` sentinel is published on the tick channel as well as the candle
   one, and tick drops are reported under `f"{strategy_id}:ticks"` rather than summed
   into the candle key.
6. **Restart recovery for the engine** — **DONE.** The contract record goes into
   `strategy_state.payload` on open and is removed on close; `PositionManager.adopt`
   seeds the book without calling the gateway; no migration. The open-vs-close
   judgement is read off the persisted `Position` rather than added to the gateway,
   which keeps the property that the gateway "cannot get that judgement wrong".
7. **Retain strategy-wise broker-factory routing** — **DONE.** The engine path calls
   the same `build_broker(resolved_config_stub(config), ...)` the fixture path does;
   a full engine worker in `LIVE` mode produces zero intents and zero fills.
8. **Bind the reporting protocols** — **DONE** (**D32**), and the alarm with them.
   The residual it was written for turned out to be **fixable** rather than merely
   reportable: `HubTickFeed` now asks `should_stop` on every poll wake, closing the
   engine half of limitation 13. The alarm remains for the sharper condition — a
   square-off was requested and a position is still open.

**The latent race in `test_duplicate_worker_startup_is_refused` was exercised and
held.** It was also measured with the boundary deliberately broken (0.31 s, 17 engine
modules, two red tests), so what it protects against is on record rather than
inferred.

**One design item in this section could not be built as specified**, and the reason is
recorded as **D31**: the engine cannot run on its own thread, because `Database`
opens SQLite with thread affinity and the first fill would raise `ProgrammingError`.

**Explicitly not in Phase 3:** `MultiLegEngine` and `FixedStrikeEngine`. The spec
schedules each for "when the first consumer is scheduled", and there is no
consumer yet. Porting 1,668 lines of engine with no strategy to exercise them
would produce untested code that merely looks finished.

**Acceptance gate (Part 2a) — met:** the ten exit policies pass the reference's
own regression suite unmodified (34 tests, names and assertion count identical to
source); both walking-skeleton gates still pass; live is still fail-closed. See
the Part 2a verification results in section 4.

**Acceptance gate (Part 2b-i) — met, except one item deferred by design:** the
ported engine's regression tests pass unmodified; the signal ownership rule is
decided, recorded (D18) and covered by a test that fails without it; both
walking-skeleton gates still pass; live is still fail-closed. The
real-`Position`-vs-real-exit-engine test is met for the engine's own `OpenPosition`
and deferred for the *persisted* `Position` to 2b-ii, which is where the bridge
between them is built. Detail in section 4.

**Acceptance gate (Part 2b-ii-A) — met:** the real engine is driven off the hub's
tick channel end to end, on the deployed two-thread topology, with a real Part 2a
exit policy closing a position (`tests/integration/test_engine_over_hub.py`); the
channel is sized and proven against a *measured* tick rate rather than candle-rate
assumptions; the supervisor's sentinel reaches the engine as a square-off request;
both walking-skeleton gates still pass, re-measured; live is still fail-closed. The
persisted-`Position` half is explicitly **not** claimed here — it needs
`LifecycleGateway`. Detail in section 4.

**Acceptance gate (Part 2b-ii-B-1) — met:** a real persisted `Position` is proven
against a real exit engine, reconciling between the engine's in-memory view and the
database after a full open→premium-walk→close cycle, with prices and charges read
back off the persisted fill rows rather than asserted twice
(`tests/integration/test_engine_lifecycle_gateway.py`); both hazards are covered by
tests, including a suppressed close that leaves the position **open** in the
database rather than phantom-closed; the square-off decision is implemented and a
restart no longer re-closes a completed day; limitation 14's entry block is closed
and proven end to end; both walking-skeleton gates still pass, with the worker's
import graph re-measured; live is still fail-closed. **Not claimed here:** restart
recovery with the engine wired, and the real-signal-to-a-real-child test — both
need the engine in a process, which is B-2.

**Acceptance gate (Part 2b-ii-B-2) — met in full:** restart recovery works with the
engine wired, not just the fixture strategy (`tests/integration/test_engine_worker_restart.py`,
including three fail-closed cases); a real `SIGTERM` **and** a real `SIGINT` to a real
worker child square off and exit 0, with the closing leg persisted through the audited
path (`tests/end_to_end/test_engine_worker_signal.py`); both walking-skeleton gates
still pass with the spawn import cost re-measured at 0.110 s median / zero
`common.engine` modules, and measured again with the boundary deliberately broken;
live is still fail-closed, including for a full engine worker configured `LIVE`.
Detail in section 4.

**Phase 3's own acceptance gate is therefore met in full.** Nothing from Parts 1
through 2b-ii-B remains deferred.

**Constraints unchanged, all parts:** paper mode only, live order placement
unimplemented and fail-closed, no real strategies (Phase 9), no second
architecture document.

---

## 9. Required Phase 1 report (spec section: Required Phase 1 report)

| Item | Where |
|---|---|
| Files created | Section 3 |
| Exact package versions tested | Section 4 (`dhanhq==2.2.0` as of Phase 2, Python 3.11.9, 78 pinned packages) |
| SDK feed/concurrency decision evidence | Section 4 — pin now ratified; payload shape ratified from SDK source; live connection is Block 2 |
| Walking-skeleton flow evidence | Section 3, gate evidence table |
| Existing-engine reuse inventory and test evidence | Section 2. `TradingEngine` ported in Phase 3 Part 2b-i (deviation D10 described Phase 1's deliberate deferral and is now superseded by that port); `MultiLegEngine`/`FixedStrikeEngine` still deferred to their first consumer |
| Per-strategy mode and broker-routing evidence | `tests/unit/test_broker_factory.py` — paper routes, live refuses, never reroutes |
| Supervisor/shared-feed/worker-process evidence | `tests/end_to_end/test_supervisor.py` — real spawned processes over real IPC queues |
| Test/lint/type-check results | Section 3 |
| Restart-recovery evidence | Gate evidence table |
| Known limitations | Section 6 |
| Live placement remains unimplemented | Section 7 |

---

## 10. Required Phase 2 report

Against the phase's own stated scope, both blocks. Every row below reflects
actual completion — nothing is deferred or marked not-performed.

| Item | Status | Where |
|---|---|---|
| Dhan authentication bootstrap, reusing the proven TOTP logic | **Delivered** | `common/authentication/`; ported from the reference's `dhan_auth.py` / `manager.py` / `token_store.py` / `jwt_utils.py`, translated to `httpx` + `filelock`; **exercised live** in Block 2 step 1 |
| Atomic token cache; crash cannot leave a partial file | **Delivered** | Section 4 gate table; `test_a_crash_between_write_and_replace_leaves_the_old_cache_intact`; real token cached live in Block 2 |
| Correct file permissions; never logged, never committed | **Delivered** | `0600` asserted; `token_cache*.json` and `data/cache/` gitignored; redaction tests; confirmed against real auth-log output in Block 2 |
| Fail closed on wrong PIN/TOTP, one attempt, no lockout risk from retries | **Delivered** | Section 3.1 defect 5; cooldown; four spawned processes → one attempt |
| `dhanhq` version spike, with a recommendation and evidence | **Delivered — pin changed to `2.2.0`** | Section 4 evidence table; deviation D12 |
| Exercise real auth + a real market-data call on the chosen version | **Delivered** | Block 2 steps 1–2: real TOTP login (one transient timeout, one successful retry), `GET /v2/profile` accepted, `POST /marketfeed/ltp` returned NIFTY 50 LTP `24274.2` |
| `DhanMarketFeedAdapter` payload shape ratified | **Delivered — ratified from source *and* confirmed live** | Section 3.1 defects 1–4 for the source ratification; Block 2 step 3's real capture matches it exactly, no divergence |
| Test coverage from a recorded/replayed fixture captured from the real response | **Delivered** | `tests/fixtures/dhan_ticker_payloads_real.json`, captured by `scripts/capture_live_tape.py`. Kept *alongside*, not in place of, `dhan_ticker_payloads_synthesised.json` — a real single-instrument ticker-mode capture cannot supply the Quote/OI/status/untraded-instrument frames the synthesised fixture exists to exercise, so replacing it outright would have silently dropped branch coverage. `source` field distinguishes the two in each file |
| Feed reconnect and resubscription to the same instruments | **Delivered against a scripted double; a real cross-thread hang was found and fixed in a related script, but the reconnect module itself remains unexercised this way** | `common/feed/reconnect.py`; the identical latent race found in `capture_live_tape.py` (limitation 1) was traced into `ReconnectingFeed` and left unfixed by deliberate decision — see limitation 1. **Phase 3 Part 1 fixed it and closed the test gap**: `ReconnectingFeed` is now driven by real threads against a blocking double. Reconnect against a *real socket drop* is still open (limitation 2) |
| Aggregator produces no corrupt or duplicate bar across the gap | **Delivered** | Three dedicated tests; `mark_feed_gap` discards rather than publishes |
| Option-chain 3s-per-underlying/expiry data throttle | **Delivered** | `common/market_data/option_chain.py`; burst tests including 8 real threads; **exercised live** in Block 2 step 4 (225-strike NIFTY chain; a second immediate call was served from cache, not a second API call) |
| Order-rate limiter stays out of scope | **Confirmed out of scope** | Spec section 14; `test_the_service_exposes_no_order_capability` |
| `build_broker()` still refuses live in every configuration | **Confirmed** | All 12 broker-factory tests pass unchanged |
| No live order placement, no stub | **Confirmed** | `DhanLiveBroker` does not exist; no order-capable endpoint was called at any point in Block 2 |
| Credentials never printed, logged, committed, or written to fixtures/runbook | **Confirmed** | Section 7; redaction gaps closed in section 3.1 and confirmed against real Block 2 log output |
| `Trading_Automation` untouched | **Confirmed** | Read-only reference; newest source+config mtime unchanged across 1010 files (widened from `.py`-only at Part 2b-ii-A). (A separate, still-running legacy component — `weekly_strategies` — was independently inspected read-only during Block 2 and found to be paper-mode only; the `option_strategies` legacy orchestrator was stopped by explicit user instruction mid-session, unrelated to this repository) |
| Test, lint, format, type-check output | **Delivered** | Section 4 verification table: **533 passed, 6 skipped**, ruff clean, mypy clean, after the fixture split (see "What Phase 2 Block 2 delivered") |
| New deviations recorded | **Delivered** | D12–D15 in section 2.3 |
| Real cross-thread shutdown hang found, diagnosed and fixed | **Delivered, scoped to one file** | `scripts/capture_live_tape.py` only; the identical untested flaw in `common/feed/reconnect.py`'s `ReconnectingFeed` and the complete absence of a live-feed shutdown path in `runtimes/intraday_options/supervisor.py` were found, deliberately **not** fixed this session, and recorded as limitation 1 — blocking live readiness until addressed. **Both closed in Phase 3 Part 1** (30 July 2026); see section 1 |

---

## 11. Weekly Delta-Neutral Positional Options (2026-08-15, `strategy-weekly-delta-neutral` branch)

Closes **D69**/limitation 30: `positional_options` is now a real,
single-process runtime driving `weekly_delta_neutral` — the one weekly
NIFTY defined-risk delta-neutral iron condor CLAUDE.md approves — in paper
mode only. Every committed live gate stays disabled; no live order API is
constructed anywhere in the new code. Full rule set:
`strategies/positional_options/weekly_delta_neutral/
WEEKLY_DELTA_NEUTRAL_ALGO_TRADING_SPEC.md`.

### 11.1 Architecture — a sibling, not a modification

`common/engine/positional/` (`positional_models.py`, `positional_engine.py`,
`positional_state.py`, `positional_strategy.py`, `lifecycle.py`) is a
sibling of `common/engine/multi_leg_engine.py`, exactly as that module is a
sibling of `common/engine/engine.py` — never a branch inside an existing
one. `MultiLegEngine` is structurally intraday (`_start_day()` rebuilds a
fresh `Basket` every start; `run()`'s `finally` forces square-off on every
stop), both wrong for a position meant to survive across trading days.
`PositionalMultiLegEngine` instead: adopts a durable `Cycle` on restart
(`_adopt_recovered_cycle`), never forces an exit on its own stop (only an
explicit trigger — strategy-signalled, operator square-off, or the
engine's own hard-expiry-deadline net — closes a leg), and drives a
sequential, hedge-first staged entry (`next_entry_role`/`entry_is_complete`/
`entry_has_blocking_leg` in `positional_models.py`) rather than
`MultiLegEngine`'s parallel one, because no short may exist without its
hedge already confirmed.

`LegRole` (`common/engine/multi_leg_models.py`) gained four additive
members — `SHORT_CALL`, `SHORT_PUT`, `HEDGE_CALL`, `HEDGE_PUT` — alongside
the unchanged `CE`/`PE`/`GENERIC`. `MultiLegEngine`'s own
`_ROLE_TO_OPTION_TYPE` mapping is untouched (`{CE: CE, PE: PE}`,
structurally unable to see the new members); the positional engine uses its
own separate mapping (`positional_state.ROLE_TO_OPTION_TYPE`). Proven, not
merely stated: `tests/unit/test_leg_role_extension_is_additive.py`.

`runtimes/positional_options/` is **deliberately single-process** —
`supervisor.py`/`worker.py`/`__main__.py`/`config_adapter.py`/
`positional_multi_leg_engine_worker.py`. `runtimes/intraday_options/`
spawns one child process per strategy because it must isolate *several
concurrent* strategies; CLAUDE.md and the spec restrict this runtime to
exactly one approved strategy today, so that isolation has nothing to
isolate from yet. `supervisor.select_one_enabled_strategy` refuses outright
(fail-closed, not "start the first one") if more than one strategy is ever
enabled under `config/runtimes/positional_options.yaml` — a real multi-
process split is the documented future path if a second positional
strategy is ever approved, and the on-disk config/database contract would
not need to change for it. The feed is `common.engine.adapter_feed.
AdapterFeed` — a direct, single-process `MarketDataFeed` over one
`MarketFeedAdapter`, sibling to `common/engine/hub_feed.py`'s `HubTickFeed`
(the multi-process/candle-aggregation feed `intraday_options` needs and
this runtime does not).

### 11.2 Greeks — chain-first, a vetted model second, never a third

`common/greeks/` is the one door every Greek requirement goes through
(`GreeksService`): Dhan chain Greeks first when complete/mapped/fresh;
`vollib.black_scholes_merton.greeks.analytical` second (the actively
maintained successor to the deprecated `py_vollib`, imported directly — no
handwritten `math.erf` Black-Scholes anywhere); `GreeksUnavailable` when
neither produces a usable value. The caller decides what "unusable" means
for its own action — blocks entry/normal-adjustment risk, **never** blocks
an exit (`WeeklyDeltaNeutralStrategy._evaluate_active` runs every stop/
target check before the `context.chain is None` early-return that would
otherwise block on missing Greeks). Every `GreekSnapshot` records its
source, source timestamp, received time, and — for a model-sourced
snapshot — every input the model was evaluated with (spot, strike, option
type, IV, risk-free rate, dividend yield, evaluation timestamp,
time-to-expiry), so a decision is always fully reconstructable regardless
of which source answered it. Verified against golden Black-Scholes
reference values, put-call parity, and a finite-difference delta
cross-check: `tests/unit/test_greeks_model.py`,
`tests/unit/test_greeks_service.py`. `common/market_data/chain_view.py`
parses the raw Dhan option-chain payload; `common/market_data/
dhan_option_chain.py` is the real, read-only `ChainFetcher` — no order
capability, added to `test_scripts_are_read_only.py`'s scope.

### 11.3 Persistence and recovery — see limitation 30 (closed) above

Migration `0010` (`strategy_cycles`, `strategy_cycle_legs`,
`strategy_cycle_adjustments`, `strategy_cycle_events`,
`cycle_position_bindings`, `cycle_decision_snapshots`), and the optional
`cycle_id` parameter now threaded through `ExecutionRepository._read_
position`/`_upsert_position`/`apply_fill`/`reserve_intent`. Full detail
moved to limitation 30's own entry (section 6) rather than duplicated here.

### 11.4 Strategy rules implemented (spec cross-reference)

- **Entry** (§3): Wednesday-only, 09:25–09:40 window; opening-stability
  filter (skip if the underlying has moved more than
  `opening_filter.maximum_move_percent` since the reference tick, up to
  `skip_after`); a runtime that first observes the market at/after 09:40
  never enters that cycle (no reference ever captured); nearest weekly
  expiry strictly *after* entry (`ScripMaster.nearest_expiry`, bumped by
  one day only in the rare same-day-expiry edge case) — never weekday
  arithmetic; the deterministic four-leg search
  (`strategies/.../selection.py`: fresh complete quote+Greeks → liquidity/
  spread → delta distance → hedge-width validity → the post-hoc
  `maximum_entry_delta_per_lot` portfolio-delta gate) with no relaxation —
  no qualifying candidate anywhere in the chain means no entry, full stop.
- **Adjustment** (§7): three consecutive over-threshold confirmations
  before rolling; only the untested short rolls, in the direction that
  reduces projected absolute portfolio delta; 1/day, 3/cycle, 90 minutes
  apart; a naked short (hedge missing/closed while its short is open) is
  repaired before any adjustment trigger is even considered
  (`_hedge_repair_needed`, checked first in `_evaluate_active`).
- **Exits** (§6): fill-based P&L including every closed adjustment leg and
  all charges; the original net credit is captured once, at entry
  confirmation, and never rebased by a later adjustment or config edit;
  the full priority ladder (emergency/hard/capital stop → expiry-day
  planned exit → soft stop → profit target → margin-utilization backstop,
  margin itself deliberately unmodelled and documented as a no-op, never
  fabricated).
- **Expiry day** (§8): the strategy signals its own planned exit at 15:05;
  the engine's own `PositionalLifecycle Policy`/`_evaluate`'s hard-exit net
  fires at 15:15 **regardless of what the strategy would have signalled**,
  and cannot be defeated by `execution.market_fallback_enabled: false` —
  that setting only removes the market-order fallback from the paper
  broker's own fill model (`config/strategies/weekly_delta_neutral.yaml`'s
  `paper_execution.allow_ltp_fallback: false` makes a depth-less fill
  structurally impossible, not merely discouraged).

### 11.5 Operational commands

```bash
# Paper start (refuses while runtimes/positional_options.yaml stays
# enabled: false, per CLAUDE.md's outstanding 30-day-evaluation gate):
.venv/bin/python -m scripts.start_runtime positional_options
.venv/bin/python -m scripts.start_strategy weekly_delta_neutral \
    --runtime-id positional_options

# Stop / status / square-off / environment check — all four already
# generic over --runtime-id and needed zero code changes: paths.
# database_path("positional_options") already matches config/runtimes/
# positional_options.yaml's own `database:` override.
.venv/bin/python -m scripts.stop_runtime --runtime-id positional_options
.venv/bin/python -m scripts.status --runtime-id positional_options
.venv/bin/python -m scripts.square_off --runtime-id positional_options \
    --strategy-id weekly_delta_neutral --confirm
.venv/bin/python -m scripts.validate_environment --runtime-id positional_options
```

`scripts/start_runtime.py`/`scripts/start_strategy.py` previously always
imported `runtimes.intraday_options.__main__.main` regardless of
`--runtime-id` — harmless while `intraday_options` was the only runtime
with a real composition root, wrong the moment `positional_options` got
one (a positional strategy would have been driven through intraday's
worker/engine wiring). Fixed via `scripts/_runtimes.py`, a one-dict
registry; `start_strategy.py` also verifies a `positional_options`
strategy id against real discovery before delegating, since that runtime
has no `--strategy-id` admission flag of its own to filter with (it drives
exactly one strategy per process by design — see 11.1).

**Restart / carried positions**: an overnight stop leaves any open cycle
exactly as it is — `run_worker`'s shutdown-signal handler stops the feed,
never calls `request_square_off`. The next start's `_adopt_recovered_cycle`
reconciles the cycle's projected leg states against
`order_intents`/`orders`/`positions`/`cycle_position_bindings`
(`positional_multi_leg_engine_worker.recover_cycle`/`_reconcile_cycle`),
correcting in place wherever authoritative data can establish the truth,
and raises `UnmanageableCycleState` (blocks the worker from starting) on
anything it cannot safely interpret — unknown, duplicate, orphaned, or
contradictory exposure or binding, exactly the same fail-closed posture
`multi_leg_engine_worker._reconcile_basket` already has. Proven end-to-end,
not merely by code inspection:
`tests/integration/test_weekly_delta_neutral_restart.py` enters a real
cycle, closes the process, reopens a second `ExecutionRepository` against
the same database file on a later trading date, and confirms exactly one
cycle row, the same four leg rows, the same four position rows (same
`entry_correlation_id`, `trading_date` still the opening date), and no
re-entry.

**Unresolved exposure**: `CycleState.CRITICAL_UNRESOLVED` blocks new
entries and requires operator/reconciliation action — never silently
retried, never silently ignored, surfaced through `record_incident` ->
`repository.record_error` and the Health tab (11.6).

### 11.6 Dashboard

`dashboards/data/positional.py` and `dashboards/positional_options.py`
went from inert placeholders (dataclasses/messages describing a runtime
that did not exist) to a real page reading migration `0010`'s tables
through the same `run_bounded`/typed-read-model/no-inline-SQL discipline
every other page in this package follows. Eight tabs (unchanged shape from
the placeholder): Overview, Active Cycles, Legs, Adjustments, Orders &
Fills, History, Performance, Health. `render(streamlit)` called with no
arguments still renders the identical eight-tab "not configured" stub
(disabled selector, no fabricated table) — the *only* thing that changed
is that `main()` now passes a real `config_root`/`database_path`, at which
point every tab queries real data.
`tests/unit/test_dashboard_positional_real_data.py` drives the page
against a real migrated database (built through the exact same
`runtimes.positional_options.worker.build_engine` production path) and
proves real cycle/leg data renders, while `tests/unit/
test_dashboard_positional_and_stocks.py`'s original five stub-behaviour
tests still pass unchanged (`render(st)` with no arguments is unaffected).

### 11.7 Real bugs found and fixed by this work

Both found only once real integration tests drove the actual production
wiring rather than inspecting the code — recorded because a future change
to either module should re-run
`tests/integration/test_weekly_delta_neutral_entry.py` before trusting a
refactor:

1. **`next_entry_role` could never attempt a role's first order.**
   `PENDING_ORDER` is both "no order has been attempted yet for this role"
   (the state `_enter_cycle` creates every leg row in) and "an order is
   mid-flight, retry on the next evaluation" — the original logic treated
   any `PENDING_LEG_STATES` leg as the latter and returned `None`,
   which meant `_drive_entry`'s loop broke on its very first iteration,
   every time, and no cycle could ever actually place an order. Fixed by
   returning the role instead of `None` when it is pending — `_open_leg_now`
   is designed to be retried against the same pending leg exactly this
   way.
2. **`select_iron_condor`'s net-delta formula used a sign convention
   inconsistent with `strategy.py`'s own `_signed_delta`.** A perfectly
   symmetric, genuinely delta-neutral condor computed a large, spurious
   non-zero net delta at the entry gate (would have rejected valid entries
   under real chain data, or admitted lopsided ones by coincidence) while
   `_net_delta_per_lot` — used for every *post*-entry adjustment decision
   on the very same legs — computed the correct near-zero figure. Fixed to
   match `_signed_delta`'s convention (long position => +raw delta, short
   position => −raw delta) exactly.

### 11.8 Known limitations (this branch)

- **Chain payload shape unverified against a live response.**
  `common/market_data/chain_view.py`'s own docstring already says so;
  carried forward, not newly introduced — the same category of gap
  `verify_vix_security_id.py` exists to close for a different field, and
  the same bounded, read-only, no-order-capability pattern should close
  this one before the first live paper session.
- **No genuine broker-side `LIMIT` order.** `OrderLifecycle.handle_signal`
  always builds `OrderType.MARKET` — a structural fact of shared code
  every strategy routes through, not something this branch could change
  without touching every existing strategy. `execution.order_type: LIMIT`
  in the strategy's own config is honoured through `PaperBroker`'s
  bid/ask-crossing adverse-fill model instead (`allow_ltp_fallback: false`
  makes a depth-less fill structurally impossible) — a deliberate,
  documented scope boundary, not a silently dropped requirement.
- **Margin utilization is unmodelled.** `is_margin_breach` always receives
  `estimated_margin=None` from this strategy — a documented no-op, never a
  fabricated pass, pending a real margin feed.
- **Selection is not a full combinatorial search.** Each role's
  nearest-delta, most-liquid candidate is used directly rather than
  searching every viable four-leg combination for the lowest absolute
  portfolio delta — documented in `selection.py`'s own module docstring as
  a deliberate scope boundary.
- **No auto-start.** `orchestration/launchd/generate_plists.py` gets no
  new plist for this runtime, matching the spec's own instruction; an
  operator starts it explicitly via 11.5's commands.

### 11.9 Safety confirmation

No live order API is constructed or called anywhere in this branch's new
code (`grep -rn "DhanLiveBroker\|build_dhan_order_client" common/engine/
positional runtimes/positional_options strategies/positional_options`
returns nothing). Every committed live gate stays independently disabled:
`config/runtimes/positional_options.yaml` (`enabled: false`,
`live_execution_allowed: false`), `config/strategies/
weekly_delta_neutral.yaml` (`enabled: false`, `mode: paper`,
`live_approved: false`) —
`python -m scripts.assert_no_live_config_committed` passes.
`runtimes/positional_options/config_adapter.py::build_worker_config`
refuses `mode: live` outright, independently of that assertion, before
any worker is ever constructed. **`OPERATIONAL LIVE ACTIVATION ELIGIBLE`
remains NO — BLOCKED**: this branch is paper-only infrastructure: it does
not start the 30-day paper evaluation clock, does not constitute the
"second real paper strategy" language elsewhere in this repository refers
to in a different context, and every other outstanding approval CLAUDE.md
lists remains outstanding.
