# Implementation status and runbook

Companion to `ALGO_TRADING_FORWARD_TESTING_ARCHITECTURE_FINAL.md` (the
architecture source of truth). This file records what is actually built, the
reference-repository reuse inventory, operating commands, known limitations and
the next phase. Updated after every phase.

| | |
|---|---|
| **Current phase** | Phase 0 — complete, awaiting review |
| **Next phase** | Phase 1 — walking skeleton (not started) |
| **Last updated** | 29 July 2026 |
| **Python** | 3.11.9 (arm64 macOS) |
| **`dhanhq` pin** | `2.1.0` — provisional, see [Package decisions](#package-decisions) |
| **Live order placement** | **Not implemented.** Fail-closed. Phase 10 only. |

---

## 1. Phase status

| Phase | Scope | Status |
|---|---|---|
| 0 | Reference audit + minimal bootstrap | **Complete** |
| 1 | Walking skeleton | Not started |
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
orchestration, LaunchAgents — and no second architecture document. The
`migrations/versions/` directory is **empty by design**: the walking-skeleton
tables arrive in Phase 1, with their first consumer.

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
  migrations/versions/   EMPTY BY DESIGN — Phase 1 adds 0001

config/
  global.yaml                     live_trading_enabled: false
  runtimes/intraday_options.yaml  enabled: false

tests/unit/   94 tests
```

### Verification results (29 July 2026)

| Check | Result |
|---|---|
| `pytest` | **94 passed** |
| `ruff check .` | **All checks passed** |
| `ruff format --check .` | **22 files already formatted** |
| `mypy` (strict) | **Success: no issues found in 13 source files** |

All four run clean. Nothing was skipped, weakened or marked `xfail`.

One test was corrected during the phase: it asserted that a migration inserting a
dangling foreign key would be caught by the post-batch `foreign_key_check`. With
`foreign_keys=ON` the orphan is rejected at insert time instead — earlier and
better. The test now asserts the real behaviour, and a separate test covers the
post-batch check using rows written while enforcement was off.

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

Start/stop/recovery commands do not exist yet — there is nothing to start. They
arrive in Phase 1 (`runtimes/`) and Phase 7 (`orchestration/scripts/`).

---

## 6. Known limitations

1. **No runnable trading path.** No engine, strategy, broker, market data or
   supervisor exists. This is Phase 0's intended end state.
2. **`dhanhq` pin unratified** — see above. Blocking for Phase 2, not Phase 1.
3. **No trading tables.** `migrations/versions/` is empty; Phase 1 adds `0001`.
4. **`effective_live_gate()` has no consumer** and can only return blocked
   (deviation D8).
5. **Migration atomicity is by replay, not transactions** (deviation D6).
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

- Live order placement is **not implemented**. `DhanLiveBroker` does not exist.
- The live path is **fail-closed**: `effective_live_gate()` defaults
  `preflight_passed=False`, and shipped `config/global.yaml` has
  `live_trading_enabled: false`.
- **No live-to-paper fallback** anywhere. A blocked live strategy refuses to run.
- No real credential was printed, committed, copied or written to any file.
  `.env.example` holds empty placeholders only.
- No file under `Trading_Automation` was written or modified; no secret,
  database, token or log was copied from it.
- No test requires credentials or network access.

---

## 8. Next phase — Phase 1, walking skeleton

One diagonal slice, proven end to end, then stop for review:

```
start one intraday_options supervisor
→ shared feed hub (recorded fixture in tests; opt-in live smoke test)
→ one paper strategy worker over a bounded IPC queue
→ one completed, validated candle
→ deterministic test-only signal fixture
→ order intent with a paper-namespaced correlation ID
→ PaperBroker fill
→ SQLite persistence with strategy_id and execution_mode=paper
→ one Streamlit tile
→ one Telegram event
→ restart worker and recover the open paper position
→ square off cleanly
```

Phase 1 also adds migration `0001`: `runtime_sessions`, `runtime_heartbeats`,
`signals`, `order_intents`, `orders`, `fills`, `positions`, `strategy_state`,
`notifications`, `errors`.

**Acceptance gate:** one feed event reaches a worker through the shared adapter;
one completed candle creates one deterministic paper order; fill and position
appear in SQLite with `execution_mode=paper`; one dashboard tile and one Telegram
event work; restart restores the open paper position; duplicate worker startup is
refused.

**Constraints:** no real strategy (deterministic fixture only), no live order
placement, no `MultiLegEngine` or `FixedStrikeEngine` port, one runtime, one
instrument, one signal fixture.
