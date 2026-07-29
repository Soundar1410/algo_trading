# Implementation status and runbook

Companion to `ALGO_TRADING_FORWARD_TESTING_ARCHITECTURE_FINAL.md` (the
architecture source of truth). This file records what is actually built, the
reference-repository reuse inventory, operating commands, known limitations and
the next phase. Updated after every phase.

| | |
|---|---|
| **Current phase** | Phase 1 — complete, awaiting review |
| **Next phase** | Phase 2 — Dhan and shared-feed hardening (not started) |
| **Last updated** | 29 July 2026 |
| **Python** | 3.11.9 (arm64 macOS) |
| **`dhanhq` pin** | `2.1.0` — provisional, see [Package decisions](#package-decisions) |
| **Live order placement** | **Not implemented.** Fail-closed. Phase 10 only. |

---

## 1. Phase status

| Phase | Scope | Status |
|---|---|---|
| 0 | Reference audit + minimal bootstrap | **Complete** |
| 1 | Walking skeleton | **Complete** |
| 2 | Dhan and shared-feed hardening | Not started |
| 3 | Preserve custom engines and policies | Not started |
| 4 | Candle, indicator and paper-execution foundation | Not started |
| 5 | Mixed-mode supervisor and persistence | Not started |
| 6 | Paper recovery and expiry handling | Not started |
| 7 | Operations | Not started |
| 8 | LaunchAgent validation | Not started |
| 9 | Real strategies | Not started |
| 10 | Controlled live readiness | Not started |

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
| **D2** | Exit registry has **10 policies, not the 9** the spec lists | The extra is `trailing` (`framework/exit/trailing_exit.py`). It is a real, registered, config-selectable policy and will be preserved along with the other nine. |
| **D3** | `momentum_low_or_highest_close` is **not** in `CompositeExit._KEY_TO_ENGINE` | Deliberate in the reference repo: it evaluates on the traded option's *own premium candle stream*, not the underlying's, so strategies instantiate it directly via `get_exit_engine()`. Porting the composite map verbatim would silently drop it. Both wiring paths must be preserved. |
| **D4** | `PaperBroker` **rewritten**, not ported | The existing one fills at `ref_price ± fixed slippage` with none of the spec's required realism. Only `ChargesCalculator` carries over. |
| **D5** | Broker factory **gains a safety gate** | The existing factory builds a live broker from `mode: live` alone. The new one must consult `effective_live_gate()` and refuse to start when blocked. This is a behavioural change, made deliberately, for safety. |
| **D6** | Migrations are **replay-safe rather than transactional** | `sqlite3.executescript()` issues an implicit COMMIT before running, so a migration cannot be applied and recorded in one transaction. Safety comes from enforced idempotency (`CREATE ... IF NOT EXISTS` only, destructive statements rejected) plus recording last: a crash between the two leaves the next startup replaying a no-op. |
| **D7** | Env overrides can **only disable** live trading | `ALGO_LIVE_TRADING_ENABLED` is honoured when it parses false and ignored when true. An operator needs a fast kill switch; nobody needs to enable real money from an environment variable, where a stale export is indistinguishable from a decision. |
| **D8** | `effective_live_gate()` exists in Phase 0 with no consumer | It can only return *blocked* in this phase (`preflight_passed` defaults False and no preflight exists). Included now because a config model that accepts `mode: live` without a fail-closed evaluator beside it is a footgun. |
| **D9** | Feed hub fans out **completed candles, not ticks** | Spec section 6 (and core principle 6) describe distributing *normalised ticks* to workers. Aggregating once, centrally, guarantees every worker sees byte-identical bars and makes "prevent duplicate candle publication" structural rather than conventional. Cost: a worker cannot pick its own timeframe off the raw stream; it aggregates further from completed bars. A tick channel can be added in Phase 2 without reshaping the queues. |
| **D10** | **No engine port in Phase 1** | The slice runs on a minimal `Strategy` protocol with a deterministic fixture implementation, shaped like `TradingEngine`'s signal interface but not derived from it. Porting the real engines is Phase 3, and doing it early would have meant porting them against a skeleton with no exit policies to receive them. |
| **D11** | `PaperBroker` is **deliberately minimal here** | Bid/ask depth, latency-selected quotes, limit orders, partial fills and the full nine-rule rejection matrix are Phase 4. Phase 1 implements a fill at the submission-time quote plus adverse slippage, a recorded latency value, and exactly one rejection rule (duplicate correlation ID) — that one because it is a correctness property of idempotent submission, not a realism feature. |

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
  paper.py         PaperBroker — adverse slippage, idempotent on correlation ID
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

tests/    223 unit, 38 integration, 20 end-to-end, 2 smoke (skipped)
  fixtures/nifty_tick_tape.json    24 ticks, 6 one-minute buckets
```

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

## 4. Package decisions

### `dhanhq` — pinned `2.1.0`, provisional

- **Pinned exactly**, never a range, per project rules.
- `2.1.0` installs cleanly on Python 3.11.9 / arm64 macOS.
- **Unresolved:** the reference repo's virtualenv runs **`dhanhq 2.2.0`**. PyPI
  lists 2.2.0 as released with documented breaking changes; 2.1 introduced the
  `DhanContext` credential pattern.
- **Action, Phase 2:** run the compatibility spike the spec requires — validate
  authentication, `DhanContext`, `MarketFeed`, subscription, reconnect and
  shutdown on this Mac/Python — then confirm or change this pin and record the
  evidence here. **The current pin is not yet ratified by a spike.**

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
.venv/bin/python -m pip install -e ".[dev]"     # or: -r requirements.lock
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
ALGO_LIVE_SMOKE=1 DHAN_CLIENT_ID=... DHAN_ACCESS_TOKEN=... \
  .venv/bin/python -m pytest tests/smoke -v
```

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

1. **The live feed adapter is unratified.** `DhanMarketFeedAdapter` was written
   against the API surface of the installed `dhanhq==2.1.0`
   (`MarketFeed(dhan_context, instruments, version)` with `run_forever` /
   `get_data` / `subscribe_symbols` / `disconnect`, all verified by inspection),
   but **the payload shape returned by `get_data` has not been observed against a
   live connection.** Normalisation is defensive and counts unparseable frames
   rather than guessing. No automated test exercises it. This is the single
   largest piece of unproven code in the repository, and ratifying it is Phase 2.
2. **`dhanhq` pin still unratified** — `2.1.0` per `CLAUDE.md`; the reference
   repo runs `2.2.0`. The compatibility spike is Phase 2.
3. **No authentication bootstrap.** The smoke test requires a manually supplied
   `DHAN_ACCESS_TOKEN`. Token generation and the atomic cache are Phase 2.
4. **No feed reconnection, backoff, resubscription or staleness detection.** The
   recorded adapter never disconnects, so none of it is exercised. Phase 2.
5. **The paper fill model is minimal** (deviation D11). Paper P&L from Phase 1 is
   not yet a credible estimate of live P&L — it has no bid/ask spread cost.
6. **Migration atomicity is by replay, not transactions** (deviation D6).
7. **Square-off is driven by the candle clock, not a wall clock.** If the feed
   stops before the square-off bar, square-off never triggers. A wall-clock
   safety net belongs with the real session handling in Phase 4.
8. **One instrument, one runtime group, one strategy shape.** Multi-strategy and
   mixed-mode supervision are Phase 5.
6. **Pattern-based log redaction is heuristic.** It masks `key=value` shapes with
   sensitive-looking keys, but cannot recognise a secret that appears as a bare
   token with no key. Literal redaction of known `.env` values covers that case
   for our own credentials; a secret from a third party in an unusual shape could
   still slip through. Revisit when the Dhan auth adapter lands in Phase 2.
7. **No dashboard, notifications, health or PID/locks.** Later phases.

### Operational risk noted during the audit

The **legacy `Trading_Automation` system was running during the audit** — its
`portfolio.db`, `weekly_strategies.db` and strategy log files were being written
live. The spec requires preventing simultaneous execution of the old and new
systems. Nothing in Phase 0 goes near it, but this must be settled **before any
new runtime is started** in Phase 1 and again before LaunchAgents in Phase 8.

---

## 7. Safety confirmations

Re-confirmed for Phase 1, with the code that now exists to back each claim:

- Live order placement is **not implemented**. `DhanLiveBroker` does not exist —
  there is no class, no order method, no stub.
- **`build_broker()` refuses live in every reachable configuration.** It consults
  `effective_live_gate()` first and raises `LiveExecutionBlocked`; even with every
  gate open and preflight forced true, it still raises, naming Phase 10. Proven
  by `tests/unit/test_broker_factory.py`, including one parametrised test that
  flips each of the five gates individually.
- **No live-to-paper fallback** anywhere. The refusal message says so explicitly,
  and `test_a_blocked_live_strategy_is_never_rerouted_to_paper` asserts it.
- **The supervisor refuses to spawn a non-paper worker at all**, so a live
  strategy never reaches a process, let alone a broker.
- **The only network-capable code is `common/market_data/dhan.py`**, it is
  read-only (subscribe and receive), and it is reached by no default test.
  `test_only_the_dhan_adapter_imports_the_sdk` enforces by `grep` that no other
  module imports the SDK; `test_the_sdk_is_not_imported_at_package_import_time`
  enforces the import stays lazy.
- The opt-in smoke test **places no order**; it subscribes and asserts a tick.
- No real credential was printed, committed, copied or written to any file.
  `.env.example` holds empty placeholders only, and secrets never enter SQLite.
- No file under `Trading_Automation` was written or modified in this phase; no
  secret, database, token or log was copied from it. It remains a read-only
  reference with no runtime dependency.
- **No default test requires credentials or network access.** The 279 tests that
  run by default use the recorded tape and fake values; the 2 that would touch
  the network are skipped unless explicitly enabled.

---

## 8. Next phase — Phase 2, Dhan and shared-feed hardening

Phase 2 makes the live data path trustworthy. Everything it covers is currently
either unproven or absent, and it is the gate before any real strategy work.

1. **Authentication bootstrap and atomic token cache.** One `auth_bootstrap`
   process generates or refreshes the access token and writes an atomic cache
   that every supervisor and worker reads. Nothing in Phase 1 does this.
2. **Ratify the `dhanhq` version.** Resolve `2.1.0` (per `CLAUDE.md`) against the
   reference repo's `2.2.0` with a real compatibility spike, and record the
   decision. This unblocks the pin, which has been provisional since Phase 0.
3. **Ratify `DhanMarketFeedAdapter` against a live connection.** Observe the
   actual `get_data` payload shape and replace the defensive normalisation with
   tested code. Record a real tape from the live feed and add it to
   `tests/fixtures/`, so future phases test against observed data rather than
   synthetic prices.
4. **Reconnection with bounded exponential backoff, and resubscription** without
   duplicate subscriptions after reconnect.
5. **Stale-instrument and session-boundary detection**, with the four-clock
   observability the spec requires (tick, receipt, candle close, evaluation).
6. **Bounded callback-to-IPC bridge and per-worker lag/overflow health**,
   surfaced in the heartbeat and the dashboard rather than only counted.
7. **Option Chain service, cache and three-second throttle.**

**Acceptance gate:** the feed survives a forced disconnect and resubscribes
without duplicates; a lagging worker is detected and reported rather than
blocking the feed; the token cache survives a restart and a concurrent read; the
Option Chain throttle holds under burst.

**Constraints unchanged:** paper mode only, live order placement still
unimplemented and fail-closed, no real strategies, no engine port.

---

## 9. Required Phase 1 report (spec section: Required Phase 1 report)

| Item | Where |
|---|---|
| Files created | Section 3 |
| Exact package versions tested | Section 4 (`dhanhq==2.1.0`, Python 3.11.9, 78 pinned packages) |
| SDK feed/concurrency decision evidence | Section 4 and limitation 1 — API surface verified by inspection; payload shape unratified, Phase 2 |
| Walking-skeleton flow evidence | Section 3, gate evidence table |
| Existing-engine reuse inventory and test evidence | Section 2; no engine ported yet (deviation D10) |
| Per-strategy mode and broker-routing evidence | `tests/unit/test_broker_factory.py` — paper routes, live refuses, never reroutes |
| Supervisor/shared-feed/worker-process evidence | `tests/end_to_end/test_supervisor.py` — real spawned processes over real IPC queues |
| Test/lint/type-check results | Section 3 |
| Restart-recovery evidence | Gate evidence table |
| Known limitations | Section 6 |
| Live placement remains unimplemented | Section 7 |
