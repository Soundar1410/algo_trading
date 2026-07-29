# Algo Trading Forward-Testing Architecture — Complete Specification

**Version:** 1.2 — Final  
**Date:** 29 July 2026  
**Target environment:** Local Apple Mac, Python, VS Code with Claude Code, DhanHQ API  
**Primary objective:** Run individual strategies in paper or live mode under shared strategy-group infrastructure, beginning with realistic paper forward testing  
**Reference implementation:** `Soundar1410/Trading_Automation` — read-only source for proven custom engines, broker routing, shared-feed orchestration and exit policies

**Final revision summary:** Execution mode is configured per strategy. A strategy-group supervisor shares authentication, market data and persistence while each enabled strategy runs in its own worker process. The existing `TradingEngine`, `MultiLegEngine`, `FixedStrikeEngine`, broker-factory pattern and custom exit policies must be preserved and reused where compatible.

---

## How to use this document

This is the single source of truth for the new independently deployable implementation. Build a thin end-to-end walking skeleton first, prove that it works, and then harden the paper platform. Do not spend weeks completing every horizontal subsystem before the first executable path works.

The design is deliberately sized for fewer than 20 options strategies and fewer than 10 stock strategies, with strategy-wise execution modes and shared group services. It avoids enterprise infrastructure, a historical backtesting platform and AWS-specific components during the initial local implementation.

## Contents

1. Scope, objectives and non-goals
2. Architecture overview
3. Repository, runtime and process architecture
4. Dhan authentication, market data and broker integration
5. Paper and live execution architecture
6. Persistence, reconciliation and restart recovery
7. Risk, health, dashboards, Telegram and Mac operations
8. Packages, testing, implementation phases and acceptance
9. Claude Code implementation prompt
10. Appendix: changes from the original specification

---


## Scope, objectives and non-goals

### Purpose

This document defines a practical architecture for building a new independently deployable `algo_trading` repository. It selectively reuses proven custom engine, broker-routing, exit-policy and orchestration logic from `Trading_Automation` without carrying forward unnecessary production or backtesting complexity.

The intended scale is:

- Fewer than 20 options strategies.
- Fewer than 10 stock strategies.
- Real Dhan live market data in both paper and live modes.
- Simulated order execution during paper forward testing.
- Real Dhan order execution only after explicit live-readiness approval.
- Independent strategy-group supervisors with isolated strategy worker processes.
- Local execution on one Mac initially.
- Possible AWS migration later, without designing for AWS now.

The first implementation must first prove one thin end-to-end path. After that walking skeleton is green, harden the paper foundation and add the first real strategy without waiting for live-only infrastructure.

---


### Core design principles

1. **Forward testing, not backtesting, is the current goal.**
2. **Paper mode uses real Dhan live data.** Only order execution is simulated.
3. **Execution mode is selected for each individual strategy**, allowing paper and live strategies to run simultaneously under `intraday_options`.
4. **Global and runtime live permissions are safety gates, not execution modes.** They can block live execution but cannot silently convert a live strategy into paper mode.
5. **Each active strategy runs in its own worker process** for crash isolation, while a strategy-group supervisor shares authentication, market data, operational storage and monitoring.
6. **Do not open one Dhan WebSocket per strategy.** Use a shared feed hub and distribute normalised ticks to strategy workers through bounded IPC queues.
7. **The broker factory is the strategy-level switch:** `paper` selects `PaperBroker`; `live` selects `DhanLiveBroker` only after all live gates pass.
8. **Preserve proven custom engines and policies from `Trading_Automation`.** Do not replace them with one universal engine or rewrite their behaviour merely to fit a new folder structure.
9. **A failed paper strategy must not stop a live strategy**, and a failed strategy worker must not stop other workers in the same group.
10. **A failed strategy-group supervisor affects only that group.** Other strategy groups remain independent.
11. **Live mode is fail-closed and disabled globally by default.** No live-to-paper fallback is permitted.
12. **Paper and live orders, positions, risk and P&L must remain explicitly separated.**
13. **The dashboard is read-only and must never control the trading loop directly.**
14. **The first milestone is a working vertical slice, not a completed horizontal platform.**
15. **Live-only controls are deferred until the controlled-live phase.**
16. **Useful packages are retained, but multiple indicator backends and enterprise infrastructure are omitted.**
17. **The initial feed implementation uses the official Dhan SDK behind an adapter; the runtime is not asyncio-first.**
18. **Local Mac operation is the current design target.** AWS compatibility is preserved through clean interfaces, not premature cloud components.

---

### Current strategy groups

Use these logical groups:

```text
intraday_options
positional_options
intraday_stocks
positional_stocks
```

Recommended implementation order:

```text
Build now:
1. intraday_options runtime foundation
2. positional_options runtime foundation
3. intraday_stocks runtime foundation

Placeholder only:
4. positional_stocks
```

The placeholder preserves the repository structure without forcing unused implementation.

---

### Explicitly out of scope for the first implementation

- Full historical backtesting engine.
- Historical data lake.
- Backtrader, Zipline, VectorBT or another backtesting framework.
- Redis, Kafka, Celery or a message broker.
- Docker/Kubernetes orchestration.
- Microservices.
- Web application control plane.
- Multiple indicator implementations running in shadow mode.
- TA-Lib integration.
- Machine-learning models.
- AWS deployment scripts.
- Bulk implementation or migration of real trading strategies before the platform walking skeleton passes.
- Live order placement enablement.
- Bulk automated migration of all existing strategies. Selected proven engines and policies are explicitly reused.
- Legacy database/history migration.
- Rewriting proven custom engines only to conform to a new folder layout.

These items may be considered later only when a concrete requirement appears.

---

### Existing `Trading_Automation` reuse policy

The connected repository `Soundar1410/Trading_Automation` is a mandatory read-only implementation reference. Claude Code must inspect it before designing replacement components.

The following proven components must be preserved or ported with minimal behavioural change:

| Existing component | Required treatment |
|---|---|
| `TradingEngine` | Reuse for single-leg, underlying-driven intraday strategies |
| `MultiLegEngine` | Reuse for straddles, strangles, delta-neutral baskets, rolling and combined basket risk |
| `FixedStrikeEngine` | Reuse for fixed CE/PE contracts with independent option-price candles |
| `broker_factory.build_broker(cfg)` pattern | Retain strategy-wise routing to `PaperBroker` or `DhanBroker`/`DhanLiveBroker` |
| Process-per-strategy supervisor | Retain crash isolation while sharing the Dhan feed |
| Shared market-data feed/hub | Retain one shared connection per strategy group rather than one connection per strategy |
| Registered exit policies | Preserve `momentum_close`, `momentum_low`, `highest_close`, `momentum_low_or_highest_close`, `consecutive_reversal`, `fixed_target`, `stoploss`, `supertrend` and `time_exit` |
| Existing regression tests | Port or retain before refactoring engine behaviour |

Reuse rules:

1. Inspect the existing implementation and tests before writing a replacement.
2. Preserve externally observable strategy behaviour unless a documented defect is proven.
3. Refactor interfaces, paths and dependency injection only where the new architecture requires it.
4. Do not create a runtime dependency on the old repository; the new project must remain independently deployable.
5. Do not copy credentials, databases, tokens, logs or generated runtime state.
6. Record every reused component and any intentional behavioural change in the implementation runbook.

---

### Walking-skeleton milestone

Before expanding the architecture horizontally, the repository must prove this single diagonal slice:

```text
start one strategy-group supervisor
→ authenticate with Dhan
→ connect one shared live market-data feed
→ launch one paper strategy worker
→ build validated candles
→ run a test-only signal fixture
→ create an order intent
→ simulate a realistic paper fill
→ persist order and position state
→ publish heartbeat and dashboard state
→ send Telegram events
→ restart and recover the paper position
→ square off cleanly
```

Use one deterministic test-only signal fixture. Stop after this milestone, run it end to end, demonstrate restart recovery, and record the evidence before adding broader engines, reconciliation machinery or more runtime groups.

---

### Recommended reading and execution order

1. Read this document from the scope through the acceptance criteria.
2. Review the detailed architecture sections before implementation.
3. Give the **Claude Code implementation prompt** section at the end of this document to Claude Code together with the complete document.
4. Ask Claude Code to implement only the phases explicitly included in the initial scope.
5. Review architecture-validation evidence before adding any real strategy.

---

### Safety statement

This architecture can reduce software and operational risk, but it cannot eliminate trading, broker, network, market-data, execution or financial risk. Paper performance does not guarantee live performance. Live mode must start with the minimum practical quantity and one approved strategy only.

## Architecture overview

### 1. Objective

Build a modest, reliable algo-trading platform for real-time paper forward testing using Dhan live market data. After the platform and individual strategies pass defined acceptance checks, the same system may place real orders through a controlled live broker adapter on the local Mac.

The architecture must support:

- Fewer than 20 options strategies.
- Fewer than 10 stock strategies.
- Intraday and positional strategy lifecycles.
- Live Dhan market data.
- Dhan TOTP and access-token generation.
- Realistic paper execution.
- Controlled live execution.
- Telegram notifications.
- Streamlit dashboards.
- Runtime health monitoring.
- PID and lock files.
- macOS LaunchAgent auto-start.
- Independent runtime processes.
- Broker-position reconciliation.
- Restart recovery for open positions.

The design must remain understandable and maintainable by one developer working locally.

---

### 2. Primary runtime flow

```text
Dhan live market-data WebSocket
              ↓
      Tick normalisation
              ↓
 Candle construction and validation
              ↓
     Strategy signal evaluation
              ↓
       Common risk validation
              ↓
          Order intent
              ↓
        Broker interface
       ┌──────┴────────┐
       ↓               ↓
  PaperBroker      DhanLiveBroker
 simulated fill     real order API
       └──────┬────────┘
              ↓
     Order and position state
              ↓
       SQLite persistence
              ↓
 Heartbeat + Telegram + Streamlit
```

Paper and live execution must share everything above the broker interface.

---

### 3. Mode model

Do not use global conflicting execution flags such as:

```text
PAPER_TRADING=true
LIVE_TRADING=true
```

Execution mode belongs to the **individual strategy**, while global and runtime values act only as live safety permissions.

Recommended configuration:

```yaml
global:
  live_trading_enabled: false

runtimes:
  intraday_options:
    enabled: true
    live_execution_allowed: false
    shared_market_feed: true

strategies:
  supertrend_fast:
    enabled: true
    mode: paper
    live_approved: false
    engine: trading_engine

  vwap_rolling_straddle:
    enabled: true
    mode: paper
    live_approved: false
    engine: multi_leg_engine

  delta_neutral:
    enabled: false
    mode: paper
    live_approved: false
    engine: multi_leg_engine
```

After controlled-live approval, mixed execution is allowed:

```yaml
global:
  live_trading_enabled: true

runtimes:
  intraday_options:
    enabled: true
    live_execution_allowed: true
    shared_market_feed: true

strategies:
  supertrend_fast:
    enabled: true
    mode: live
    live_approved: true
    engine: trading_engine

  vwap_rolling_straddle:
    enabled: true
    mode: paper
    live_approved: false
    engine: multi_leg_engine

  delta_neutral:
    enabled: false
    mode: paper
    live_approved: false
    engine: multi_leg_engine
```

Allowed strategy execution modes:

```text
paper
live
```

A disabled strategy is represented by `enabled: false`; its configured mode is not executed. This mirrors the proven design already used in `Trading_Automation` and avoids two competing ways to disable a strategy.

Also separate market-data source from strategy execution mode:

```yaml
market_data:
  source: dhan_live
```

Both paper and live strategies consume the same live market data.

#### Effective live gate

A strategy may route an order to `DhanLiveBroker` only when all are true:

```text
global.live_trading_enabled
AND runtime.enabled
AND runtime.live_execution_allowed
AND strategy.enabled
AND strategy.mode == live
AND strategy.live_approved
AND all live preflight checks pass
```

A failed live gate must block that strategy. It must never fall back to `PaperBroker` automatically.

#### Mode-change rules

- Mode changes become effective only after the strategy worker restarts.
- Reject `paper → live` while an open paper position exists.
- Reject `live → paper` or `live → disabled` while broker positions or pending live orders exist.
- Persist the effective execution mode with every session, signal, order, fill, position and trade.
- Use separate paper/live correlation-ID namespaces.
- Keep paper and live P&L, limits and dashboard totals separate.

### 4. Runtime and process model

Use one **strategy-group supervisor** plus one worker process for each enabled strategy.

```text
intraday_options_supervisor
    ├── shared Dhan authentication/token cache
    ├── shared Dhan market-feed hub
    ├── shared subscription registry
    ├── shared intraday_options.db
    ├── worker: supertrend_fast          [LIVE]
    ├── worker: vwap_rolling_straddle    [PAPER]
    └── worker: delta_neutral             [PAPER]

positional_options_supervisor
    ├── shared feed/state services
    └── one worker per enabled positional strategy

intraday_stocks_supervisor
    ├── shared scanner/feed services
    └── one worker per enabled stock strategy

streamlit_dashboard
    └── read-only view across runtime databases and heartbeats
```

The supervisor is responsible for lifecycle and shared services. Each worker owns exactly one strategy instance, one execution mode and one broker adapter.

Do not create one WebSocket or one authentication flow per strategy. The supervisor/feed hub distributes normalised ticks to workers through bounded inter-process queues. A slow worker must not block the feed hub or other strategies.

#### Failure boundaries

- A strategy exception terminates or quarantines only that strategy worker.
- The supervisor may restart a failed paper worker according to bounded policy.
- A failed live worker enters `RECOVERY_REQUIRED`; automatic restart is allowed only after live-state checks prove it safe.
- A paper strategy failure must not stop a live strategy.
- A supervisor-level failure stops only its strategy group.
- Telegram or dashboard failure must not stop a trading worker.
- Database failure blocks new orders for every worker using the affected group database because safe state cannot be guaranteed.
- Broker reconciliation failure blocks only live entries; paper workers may continue if their own persisted state is healthy.

### 5. Strategy isolation inside a runtime

Each strategy worker must have:

- Stable strategy instance ID.
- Independent configuration.
- Independent `enabled` flag.
- Independent `mode: paper | live`.
- Independent `live_approved` flag.
- Exactly one selected custom engine.
- Exactly one broker adapter created at worker startup.
- Independent state, P&L and risk limits.
- Independent error count and health state.
- Independent subscription requirements.
- Independent order-correlation sequence and mode namespace.
- Independent PID and worker heartbeat.

Example instance IDs:

```text
io_supertrend_fast_v1
io_vwap_straddle_v1
po_weekly_delta_neutral_v1
is_orb_ranked_v1
```

Parameter-only variants should share Python code but have different configurations and stable instance IDs.

The engine selected for one worker must not change dynamically during the session. Execution mode must also remain immutable for that worker session.

### 6. Market-data architecture

Each active strategy-group supervisor may hold one shared Dhan live market-feed WebSocket. Strategy workers consume normalised ticks from the shared feed hub; they must not open their own Dhan feed connections.

Each market-data component must implement:

- Connection and authorisation.
- Binary response decoding.
- Subscription batching.
- Dynamic subscribe/unsubscribe.
- Ping/pong health.
- Reconnection with bounded exponential backoff.
- Resubscription after reconnect.
- Duplicate tick suppression.
- Out-of-order tick detection.
- Stale instrument detection.
- Session boundary handling.
- Feed sequence and timestamp observability.

The dashboard must not create a separate live market-data connection. It reads persisted state.

---

### 7. Candle architecture

Candle generation is part of the production path and must be deterministic.

Required rules:

- Use `Asia/Kolkata` for exchange-session logic.
- Prefer exchange timestamps where available.
- Publish signals on completed candles unless a strategy specification explicitly requires tick-level behaviour.
- Never use a future tick to modify an already published candle.
- Reject or mark invalid candles when continuity requirements are not met.
- Prevent duplicate candle publication.
- Record the exact candle snapshot used to produce a signal.
- Reset intraday indicators at the configured session boundary.
- Preserve positional indicator continuity when specified.
- Record tick time, receipt time, candle-close time and signal-evaluation time separately.

For combined option premiums, build synthetic OHLC from synchronised CE and PE samples. Do not calculate combined high or low by simply adding independent leg highs or lows unless they occurred at the same timestamp.

---

### 8. Broker abstraction

Provide one broker contract with two adapters:

```text
Broker
├── PaperBroker
└── DhanLiveBroker
```

The strategy must never call the Dhan SDK directly.

The common broker interface should support:

- Submit order.
- Modify order.
- Cancel order.
- Fetch order status.
- Fetch order book.
- Fetch trades.
- Fetch positions.
- Exit position or basket.
- Broker connectivity health.
- Correlation-ID lookup.

PaperBroker and DhanLiveBroker must return the same internal order and fill models.

---

### 9. Realistic paper-forward testing

PaperBroker must not fill every order immediately at LTP.

Minimum paper-fill behaviour:

- Buy market order fills from current ask plus configured slippage.
- Sell market order fills from current bid minus configured slippage.
- Apply configurable submission latency.
- Enforce tick size and lot size.
- Reject stale or unavailable prices.
- Support limit-order trigger conditions.
- Support partial-fill states in the model.
- Simulate rejected orders through configurable test hooks.
- Calculate brokerage and statutory charges through a separate cost component.
- Preserve order lifecycle events.
- Prevent fills outside configured trading sessions.

Paper execution is an approximation. Its purpose is to test strategy and runtime behaviour under live timing, not to reproduce the exchange matching engine perfectly.

---

### 10. Live trading safety boundary

A strategy configured with `mode: live` must require all of the following:

- Strategy is enabled.
- Strategy mode is explicitly `live`.
- Strategy `live_approved` is true.
- Global live trading is enabled.
- Strategy-group runtime is enabled.
- Per-runtime live permission is enabled.
- Valid Dhan credentials and current access token.
- Static-IP preflight validation where required by Dhan.
- Broker connectivity.
- Market session validation.
- Live market data not stale.
- Database integrity and writable state.
- Supervisor and worker locks acquired.
- No duplicate strategy worker.
- Broker/local position reconciliation complete.
- Strategy and account risk configurations present and valid.
- Explicit live confirmation token or local approval file.

Any failure must block live execution for that strategy. Never redirect the order to paper mode.

A paper strategy in the same group may continue only when its own market data, database and paper state remain healthy. Shared infrastructure failures must fail closed for all affected workers.

### 11. Persistence model

Use one SQLite database per runtime group:

```text
data/operational/intraday_options.db
data/operational/positional_options.db
data/operational/intraday_stocks.db
data/operational/positional_stocks.db   # create later
```

Use SQLite WAL mode and foreign keys. Keep dashboards read-only.

Persist the walking-skeleton minimum first:

- Runtime sessions and heartbeats.
- Signals and order intents.
- Orders and fills.
- Positions and strategy state.
- Notifications and errors.

After the walking skeleton is stable, add strategy registry, event-history and risk tables as needed. Reconciliation runs and mismatch tables are controlled-live additions, not paper-foundation prerequisites.

Do not persist every raw tick indefinitely. Store operational candles, signal evidence and selected market-data snapshots required for audit and debugging.

---

### 12. Restart recovery

#### Paper mode

On restart:

```text
acquire lock
→ load open paper positions
→ restore strategy state
→ restore SL/target/trailing/rolling state
→ subscribe to required instruments
→ validate fresh prices
→ mark positions to market
→ resume management
→ permit new entries
```

#### Live mode

On restart:

```text
acquire lock
→ load local open state
→ authenticate
→ fetch Dhan order book
→ fetch Dhan trades
→ fetch Dhan positions
→ reconcile broker and local state
→ block new entries on critical mismatch
→ restore strategy and risk state
→ subscribe to required instruments
→ resume position management
→ permit new entries only after successful reconciliation
```

Broker state is authoritative for live positions, but all mismatches must be persisted and reported rather than silently overwritten.

---

### 13. Risk hierarchy

#### Account-level

- Maximum daily loss.
- Maximum total open positions.
- Maximum deployed capital or margin.
- Maximum order rate.
- Global kill switch.
- No new entries during unresolved reconciliation.
- No new entries on stale data or broker disconnection.

#### Runtime-level

- Runtime daily loss cap.
- Maximum open strategies.
- Maximum open legs or stocks.
- Intraday square-off time.
- Maximum feed disconnection duration.
- Maximum restart attempts.

#### Strategy-level

- Maximum quantity/lots.
- Maximum loss.
- Entry cutoff.
- Exit time.
- Maximum re-entries.
- Maximum rolls/adjustments.
- Cooldown after stop loss.

#### Multi-leg options

- Maximum unhedged duration.
- Hedge-first or defined leg sequencing.
- Partial-fill recovery.
- Basket-level stop and profit rules.
- Maximum permissible combined slippage.

---

### 14. Dashboard architecture

Use one Streamlit multipage application:

```text
dashboards/
├── app.py
└── pages/
    ├── 01_master.py
    ├── 02_intraday_options.py
    ├── 03_positional_options.py
    ├── 04_intraday_stocks.py
    └── 05_system_health.py
```

The dashboard must be read-only and display:

- Runtime status and heartbeat age.
- Effective execution mode (`paper` or `live`).
- Mode-specific correlation-ID namespace.
- Authentication status.
- Market-data status and last tick time.
- Open orders and positions.
- Realised and unrealised P&L.
- Strategy health.
- Risk-limit usage.
- Reconnect count.
- Reconciliation status.
- Square-off status.
- Last error.

The dashboard must not import runtime-private strategy code or own trading state.

---

### 15. Telegram architecture

Telegram is an operational notification channel, not a control dependency.

Send notifications for meaningful state changes:

- Authentication success/failure.
- Runtime start/stop/crash.
- Feed disconnect/reconnect.
- Strategy enable/disable/quarantine.
- Order submitted/filled/rejected/partially filled.
- Position opened/closed.
- Stop, target or roll event.
- Daily loss limit.
- Square-off success/failure.
- Reconciliation mismatch.
- Stale data.
- Database write failure.

Notification failure must be logged and counted but must not stop trading.

---

### 16. macOS operation

Use LaunchAgent only after manual runtime validation.

Recommended LaunchAgents:

- Authentication bootstrap.
- Intraday options runtime.
- Positional options runtime.
- Intraday stocks runtime.
- Optional dashboard.

Each LaunchAgent must use absolute paths, the correct virtual-environment interpreter, validated working directories, independent logs and bounded restart behaviour.

Prevent simultaneous execution of the old and new systems. Validate Mac sleep, power, clock synchronisation, network stability and disk capacity before market use.

---

### 17. Packages and custom boundaries

Keep package-based calculations and infrastructure where appropriate:

- `dhanhq`
- `pandas`
- `numpy`
- `pandas-ta-classic`
- Dhan Option Chain API for the default Greeks/IV snapshot source.
- `pyotp`
- `pydantic`
- `pydantic-settings`
- `python-dotenv`
- `PyYAML`
- `httpx`
- `tenacity`
- `streamlit`
- `filelock`
- `pytest`
- `ruff`
- `mypy`

Optional only when a concrete strategy proves the need:

- `py_vollib`, installed through an optional dependency group only for between-snapshot Greek estimation or validation of Dhan values. Do not install it in the default environment.

Keep custom project code for:

- Strategy definitions.
- Candle policy and session behaviour.
- Option strike/expiry selection.
- Basket construction.
- Paper fills.
- Dhan order lifecycle integration.
- Order idempotency.
- Multi-leg sequencing.
- Rolling and adjustments.
- Risk permissions.
- Persistence and recovery.
- Reconciliation.

---

### 18. Implementation boundary before strategies

Use two gates rather than one large platform-completion gate.

#### Gate A — walking skeleton

Prove:

```text
live Dhan tick
→ completed candle
→ deterministic fake signal
→ PaperBroker fill
→ SQLite persistence
→ one dashboard tile
→ one Telegram event
→ process restart
→ recovered paper position
```

This gate intentionally uses minimal tables, one runtime, one instrument and one signal fixture.

#### Gate B — paper-foundation readiness

Before adding more than the first real strategy, prove the mixed-mode-capable foundation:

- Stable authentication and token cache.
- Feed reconnect and resubscription.
- Deterministic candles.
- Credible paper fills.
- Essential risk gates.
- Paper restart recovery.
- Read-only dashboard and failure-isolated notifications.
- Manual start/stop and duplicate-process protection.

Do **not** make live-only work a prerequisite for the first strategy. Cross-process order throttling, full broker mismatch taxonomy, migration checksums and live-order failure injection belong to the controlled-live phase.

### 19. Future AWS migration

Do not add AWS-specific code now. Preserve portability through:

- Environment-based configuration.
- No hard-coded local paths.
- Broker and market-data interfaces.
- Repository-based persistence access.
- Process-independent runtime entry points.
- Structured logs.
- Explicit health endpoints/files.

When AWS is genuinely required, LaunchAgent can be replaced with a cloud process supervisor and SQLite can be reconsidered. No redesign is necessary at the strategy interface level.

## Repository, runtime and process architecture

### 1. Recommended repository structure

```text
algo_trading/
├── README.md
├── pyproject.toml
├── uv.lock or requirements.lock
├── .env.example
├── .gitignore
│
├── common/
│   ├── config/
│   ├── authentication/
│   ├── broker/
│   ├── market_data/
│   ├── candles/
│   ├── indicators/
│   ├── option_analytics/
│   ├── execution/
│   ├── risk/
│   ├── persistence/
│   ├── notifications/
│   ├── health/
│   ├── models/
│   └── utils/
│
├── strategies/
│   ├── intraday_options/
│   ├── positional_options/
│   ├── intraday_stocks/
│   └── positional_stocks/
│
├── runtimes/
│   ├── intraday_options/
│   ├── positional_options/
│   ├── intraday_stocks/
│   └── positional_stocks/
│
├── dashboards/
│   ├── app.py
│   └── pages/
│
├── orchestration/
│   ├── launchd/
│   ├── process_control/
│   └── scripts/
│
├── data/
│   ├── reference/
│   ├── operational/
│   ├── runtime/
│   └── cache/
│
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── end_to_end/
│   └── fixtures/
│
└── docs/
```

Do not initially create separate top-level backtesting, historical-data, research, reports or backups hierarchies.

---

### 2. Common modules

#### `common/config/`

Responsibilities:

- Load `.env` and YAML configuration.
- Validate typed configuration.
- Resolve runtime enablement, runtime live permission and strategy execution mode.
- Resolve paths.
- Validate safety gates.
- Calculate configuration fingerprints.

#### `common/authentication/`

Responsibilities:

- Generate TOTP.
- Generate or refresh Dhan access token.
- Validate token.
- Write token cache atomically.
- Read token cache safely.
- Prevent concurrent token refresh.

#### `common/broker/`

```text
base.py
paper_broker.py
dhan_live_broker.py
order_mapper.py
broker_factory.py
```

No strategy may import the Dhan SDK directly.

#### `common/market_data/`

Responsibilities:

- Official Dhan SDK `MarketFeed` adapter for the initial implementation.
- Callback-to-bounded-queue bridge.
- Dhan payload-to-internal-tick mapping.
- Subscription registry.
- Reconnect and resubscribe.
- Option Chain snapshot cache and three-second throttle.
- Normalised tick publication.
- Feed and chain staleness detection.

Custom binary parsing belongs only in a future replacement adapter if the SDK compatibility spike proves it necessary.

#### `common/candles/`

Responsibilities:

- Tick-to-candle aggregation.
- Timeframe rules.
- Session boundaries.
- Candle continuity.
- Completed-candle publication.
- Synthetic basket candle construction.

#### `common/indicators/`

Use one adapter layer over the approved package backend. Do not create multiple production/shadow backends in the first version.

#### `common/execution/`

Preserve the proven custom engine boundaries from `Trading_Automation`:

```text
engine.py                 # TradingEngine — single-leg, underlying-driven
multi_leg_engine.py       # MultiLegEngine — baskets, rolling, combined risk
fixed_strike_engine.py    # FixedStrikeEngine — independent CE/PE option charts
stock_portfolio_engine.py # add only when the first scanner-driven stock strategy needs it
broker_factory.py         # strategy-wise PaperBroker/DhanLiveBroker routing
```

Do not replace these with one universal engine. Single-leg, multi-leg and fixed-strike strategies have genuinely different candle, position and risk models.

Port or retain the existing engine regression tests before changing internals. Add adapters around the engines where necessary for the new market-data, persistence and broker contracts.

#### `common/exit/`

Preserve the registered custom exit-policy architecture and existing policies:

```text
momentum_close
momentum_low
highest_close
momentum_low_or_highest_close
consecutive_reversal
fixed_target
stoploss
supertrend
time_exit
```

Exit, rolling, confirmation and risk policies are reusable trading rules; they are not full execution engines.

#### `common/risk/`

Separate:

- Account risk.
- Runtime risk.
- Strategy risk.
- Basket/multi-leg risk.

#### `common/persistence/`

Responsibilities:

- SQLite connection management.
- Sequential SQL migrations.
- Repositories.
- Transactions.
- Integrity checks.
- Read-only dashboard access.

#### `common/notifications/`

Telegram notifier with timeout, retry and isolation.

#### `common/health/`

Responsibilities:

- Heartbeat publishing.
- Feed health.
- Broker health.
- Database health.
- Strategy health.
- Health snapshots for dashboard consumption.

---

### 3. Strategy folder standard

Use a small strategy folder:

```text
strategy_name/
├── __init__.py
├── strategy.py
├── config/
│   └── config.yaml       # includes mode, live approval, engine and strategy parameters
├── tests/
│   ├── test_signals.py
│   ├── test_state.py
│   └── test_risk.py
└── README.md
```

Do not create a separate broker, database or dashboard inside every strategy folder. A thin worker entry point may be generated by the supervisor, but shared lifecycle and infrastructure remain framework responsibilities.

#### Strategy responsibilities

- Interpret indicators and market context.
- Generate entry/exit/adjustment decisions.
- Maintain strategy-specific state.
- Declare market-data subscriptions.
- Declare risk requirements.

#### Strategy must not

- Connect to Dhan.
- Write directly to SQLite.
- Send Telegram messages directly.
- Manage PID files.
- Start Streamlit.
- Call package indicators directly without the common adapter.
- Place orders outside the execution interface.

---

### 4. Runtime folder standard

```text
runtimes/intraday_options/
├── supervisor.py
├── shared_feed_hub.py
├── worker_launcher.py
├── strategy_loader.py
├── startup.py
├── shutdown.py
└── config/
    └── runtime.yaml
```

A strategy-group supervisor owns:

- Group process lifecycle.
- Authentication/token consumption.
- Shared market-data connection and subscription union.
- Strategy discovery and worker spawning.
- Shared runtime-level and live-account safety gates.
- Group persistence and health publication.
- Graceful group shutdown.
- Coordination of intraday square-off or positional carry-forward.

Each strategy worker owns:

- One stable strategy instance.
- One selected custom engine.
- One immutable execution mode for the worker session.
- One broker adapter selected by the broker factory.
- Strategy state, risk, order lifecycle and worker heartbeat.

---

### 5. Dependency direction

Allowed direction:

```text
strategies
    ↓
common interfaces

runtimes
    ↓
strategies + common services

dashboards
    ↓
read-only repositories and health snapshots

orchestration
    ↓
runtime entry points
```

Prohibited direction:

```text
common → strategy
strategy → runtime
strategy → Dhan SDK
strategy → Streamlit
runtime A → private implementation of runtime B
dashboard → live broker order methods
```

---

### 6. Process model

#### Strategy-group supervisors and workers

| Process | Scope | Database | Market-data connection |
|---|---|---|---|
| `intraday_options_supervisor` | Shared services and lifecycle for intraday options | `intraday_options.db` | One shared Dhan feed |
| `intraday_options:<strategy_id>` | One enabled strategy in `paper` or `live` mode | `intraday_options.db` | IPC from shared feed |
| `positional_options_supervisor` | Shared services and lifecycle for positional options | `positional_options.db` | One shared Dhan feed |
| `positional_options:<strategy_id>` | One enabled positional strategy | `positional_options.db` | IPC from shared feed |
| `intraday_stocks_supervisor` | Shared scanner/feed lifecycle | `intraday_stocks.db` | One shared Dhan feed |
| `intraday_stocks:<strategy_id>` | One enabled stock strategy | `intraday_stocks.db` | IPC/shared scanner output |
| `streamlit_dashboard` | Read-only monitoring | All, read-only | None |

The supervisor starts only enabled strategies. Each worker loads its own resolved configuration, selects one custom engine, and calls the broker factory once at startup.

#### Support process

`auth_bootstrap` generates or refreshes the access token once and writes an atomic cache for all supervisors and workers.

#### Shared-feed requirement

The shared feed hub must:

- Subscribe once for the union of active strategy requirements.
- Distribute normalised events through bounded queues.
- Drop or quarantine a lagging paper worker according to policy rather than blocking the feed.
- Block a lagging live worker from new entries and raise a critical alert.
- Track per-worker queue age, depth and dropped-event count.
- Resubscribe after reconnect without starting duplicate subscriptions.

### 7. Strategy worker isolation

The supervisor maintains a worker registry:

```text
strategy-group supervisor
  ├── shared feed hub
  ├── worker A: TradingEngine + DhanLiveBroker
  ├── worker B: MultiLegEngine + PaperBroker
  └── worker C: FixedStrikeEngine + PaperBroker
```

For each worker:

- Validate strategy configuration before spawn.
- Catch and record worker termination.
- Maintain independent PID, mode, heartbeat and restart count.
- Quarantine a repeatedly failing paper worker.
- Require reconciliation and explicit safe-restart policy for a failed live worker.
- Continue other workers when shared state remains safe.
- Never hide broker, execution or persistence errors that may compromise account state.

Recommended health states:

```text
DISABLED
STARTING
WARMING_UP
RUNNING_PAPER
RUNNING_LIVE
DEGRADED
BLOCK_NEW_ENTRIES
RECOVERY_REQUIRED
QUARANTINED
STOPPING
STOPPED
FAILED
```

### 8. Scanner-driven stocks

Do not let every stock strategy scan the full universe independently.

```text
Nifty 200 reference universe
        ↓
shared data-quality and liquidity filters
        ↓
shared ranking/scanner service
        ↓
selected candidate set
        ↓
strategy-specific entry logic
        ↓
portfolio execution
```

The scanner and the execution strategy remain logically separate, but both can run inside `intraday_stocks_runtime`.

---

### 9. Configuration organisation

```text
config/
├── global.yaml
├── runtimes/
│   ├── intraday_options.yaml
│   ├── positional_options.yaml
│   └── intraday_stocks.yaml
└── strategies/
    └── <strategy_instance_id>.yaml
```

Secrets belong only in ignored local files such as `.env`.

Use layered resolution:

```text
defaults
→ global YAML
→ runtime YAML
→ strategy YAML
→ permitted environment overrides
```

Required resolved strategy fields:

```yaml
strategy_id: io_supertrend_fast_v1
enabled: true
mode: paper                 # paper | live
live_approved: false
engine: trading_engine      # trading_engine | multi_leg_engine | fixed_strike_engine | stock_portfolio_engine
risk: {}
parameters: {}
```

Runtime configuration contains group lifecycle and live permission, not a shared execution mode:

```yaml
runtime_id: intraday_options
enabled: true
live_execution_allowed: false
shared_market_feed: true
database: data/operational/intraday_options.db
```

Persist a fingerprint of the resolved configuration with each worker session and order intent. Also persist the effective execution mode separately so historical records remain unambiguous after configuration changes.

### 10. Paths and local portability

All paths must be resolved from one configured project root. Do not hard-code the user home directory or old repository path.

Example:

```yaml
paths:
  project_root: /Volumes/Trading/algo_trading
  data_root: ${paths.project_root}/data
  log_root: ${paths.project_root}/logs
  runtime_root: ${paths.project_root}/data/runtime
```

The actual local path remains outside Git and may be overridden through `.env`.

---

### 11. Files excluded from Git

At minimum:

```gitignore
.env
*.db
*.db-wal
*.db-shm
logs/
data/runtime/
data/cache/
token_cache*.json
*.pid
*.lock
.DS_Store
.venv/
__pycache__/
.pytest_cache/
.mypy_cache/
.ruff_cache/
```

Do not commit generated reports containing account identifiers, order IDs or sensitive trading details unless explicitly sanitised.

---

### 12. First implementation restriction

Start with one strategy-group supervisor, one paper worker, one instrument and one deterministic fixture under `tests/fixtures/`. Prove the walking skeleton before creating all groups. Reuse the existing `TradingEngine` path first; port `MultiLegEngine` and `FixedStrikeEngine` when their first consumers are introduced. Implement the first real strategy after the relevant paper-foundation gate; do not wait for controlled-live machinery.

## Dhan authentication, market data and broker integration

### 1. Scope

This section defines how the local platform integrates with Dhan while keeping authentication, market data, option-chain snapshots, paper execution and future live execution cleanly separated.

All external Dhan payloads must be converted into internal typed models at adapter boundaries.

---

### 2. Authentication design

```text
pre-market authentication bootstrap
        ↓
load client ID, PIN and TOTP secret from ignored local environment
        ↓
generate TOTP
        ↓
generate or refresh access token
        ↓
validate token through a safe read request
        ↓
write token cache atomically
        ↓
notify success or failure
        ↓
trading runtimes read the same validated cache
```

Do not let every runtime independently generate a token.

Real secrets must never be printed, persisted in SQLite, returned to Streamlit, copied into tests or committed to Git.

`.env.example` contains empty placeholders only:

```dotenv
DHAN_CLIENT_ID=
DHAN_PIN=
DHAN_TOTP_SECRET=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
PROJECT_ROOT=
```

The token cache must use local user-only permissions, atomic replacement, a refresh lock, client-identity validation and secret redaction. Treat Dhan's official documentation as the source of truth for token lifetime.

---

### 3. Authentication failure behaviour

A runtime fails closed when the token cache is missing, expired, malformed, belongs to another client identity or cannot be validated. Multiple runtimes may wait briefly for the bootstrap, but they must not race to generate TOTP tokens.

---

### 4. Market-feed and concurrency decision

#### Initial decision

Use the official `dhanhq` SDK `MarketFeed` implementation for the walking skeleton and paper foundation, wrapped behind a project-owned `MarketDataFeed` adapter.

Reasons:

- It is the fastest route to a working vertical slice.
- Broker-maintained parsing reduces custom binary-protocol code.
- The adapter preserves the option to replace the SDK later without changing strategies, candles or runtimes.

#### Concurrency model

Do not make the entire application asyncio-first.

Recommended boundary:

```text
Dhan SDK feed thread/callback
        ↓
bounded thread-safe queue
        ↓
runtime consumer loop
        ↓
tick normalisation and candle processing
```

Rules:

- No strategy logic inside the SDK callback.
- The callback only validates minimally and publishes into a bounded queue.
- Queue overflow is a health event; do not silently drop without metrics.
- Use `asyncio` only inside an adapter if the validated SDK version requires it. Do not expose an event loop as a project-wide architectural dependency.
- Use one owner for reconnect and subscription state.

#### Custom WebSocket fallback

Do not implement a custom binary WebSocket client in parallel. Replace the SDK adapter only if a focused compatibility spike proves that the SDK cannot meet required reconnect, dynamic subscription, timestamp or stability needs. Record that decision before changing implementations.

---

### 5. `dhanhq` version policy

As verified on 29 July 2026, PyPI lists `dhanhq 2.2.0` as a released version and explicitly notes breaking changes. Version 2.1 introduced the `DhanContext` credential pattern. Therefore:

1. Do not assume that `2.1.0` is the latest stable release.
2. Run a small compatibility spike against the current released package.
3. Validate authentication, `DhanContext`, `MarketFeed`, subscription, reconnect and shutdown on the target Mac/Python version.
4. Pin the exact version that passes; do not use a loose range.
5. Record the chosen version and any SDK workarounds in the implementation status document.

Official package page: `https://pypi.org/project/dhanhq/`

---

### 6. Dhan live market-data connection

Current Dhan documentation states that a user may establish up to five market-feed WebSocket connections with up to 5,000 instruments per connection, and that one subscription message may contain up to 100 instruments. Responses are binary.

Initial process allocation:

```text
intraday_options_runtime   → one feed connection
positional_options_runtime → one feed connection when implemented
intraday_stocks_runtime    → one feed connection when implemented
positional_stocks_runtime  → future placeholder
streamlit_dashboard        → no feed connection
```

The walking skeleton starts with one runtime and one connection. Add other runtime connections only after the first slice is stable.

Verify broker limits immediately before implementation and live deployment because they can change.

---

### 7. Option-chain snapshot service and 3-second throttle

Dhan's Option Chain API already returns strike-level delta, theta, gamma, vega, implied volatility, OI, volume, LTP and top bid/ask. It is the default source for Greeks and IV used in delta-neutral, delta-selection and delta-rolling decisions.

Dhan documents a limit of one unique Option Chain request every three seconds. Implement this constraint from the first options runtime:

```text
strategy requests chain snapshot
        ↓
central option-chain service
        ↓
cache key = underlying + expiry
        ↓
return fresh cached snapshot when within TTL
        ↓
otherwise schedule one compliant API request
        ↓
fan out the result to all consumers
```

Rules:

- Strategies must never call the Option Chain endpoint directly.
- Deduplicate requests across strategies.
- Store snapshot time, receive time and freshness.
- Use the WebSocket feed—not repeated chain calls—for fast leg LTP, bid/ask and position marking.
- Block or degrade delta-dependent entries when the chain snapshot is stale beyond the strategy's configured tolerance.
- `py_vollib` is not a default dependency. Add it only if a specific strategy needs between-snapshot Greek estimation or independent validation of Dhan values.

Official reference: `https://dhanhq.co/docs/v2/option-chain/`

---

### 8. Market-data internal model

Normalise every feed event into a typed internal tick containing at least instrument identity, exchange segment, event time, receive time, LTP, volume/OI where available, best bid/ask, quantities, source and optional sequence hint.

Use `Decimal` or integer tick units for money-sensitive calculations where floating-point ambiguity matters.

---

### 9. Subscription registry

Each runtime maintains one authoritative, reference-counted registry:

```text
instrument
→ consumers
→ requested feed mode
→ subscribed status
→ last tick time
→ stale status
```

Dynamic option roll flow:

```text
request new contract
→ validate instrument master
→ subscribe new contract
→ wait for fresh quote
→ perform paper/live roll policy
→ retain old contract until safely closed
→ unsubscribe only when no consumer remains
```

---

### 10. Reconnection, resubscription and staleness

On feed loss:

1. Mark feed unhealthy.
2. Block new entries after the grace period.
3. Reconnect with bounded backoff and jitter.
4. Reauthorise and resubscribe in batches.
5. Require fresh ticks for critical instruments.
6. Clear degraded state only after validation.

Track reconnect count, disconnect reason, queue depth, expected versus active subscriptions, resubscription time and stale instruments.

A stale price is never converted to zero or treated as a fresh unchanged price.

---

### 11. Broker interface

Paper and live adapters share a typed broker protocol for submit, modify, cancel, order lookup, order/trade/position listing and health. Strategies never call the Dhan SDK directly.

Dhan statuses must be mapped into internal states without spreading raw dictionaries across the codebase.

---

### 12. Future `DhanLiveBroker` responsibilities

This adapter is defined now but implemented only in the controlled-live phase. It will:

- Map internal orders to Dhan requests.
- Validate segment, product, lot, tick and quantity.
- Generate/persist correlation IDs before submission.
- Handle uncertain results without blind resubmission.
- Query by correlation ID when necessary.
- Consume live order updates and use polling only as fallback.
- Convert broker errors into typed retryable/non-retryable errors.

Paper mode does not call Dhan order placement APIs.

---

### 13. Static-IP requirement

Dhan's current order documentation states that placement, modification and cancellation require static-IP whitelisting. Resolve and test this only before controlled live trading. It is not a blocker for live-data paper forward testing.

---

### 14. Deferred live-only account order limiter

A shared cross-process order limiter is not part of the paper foundation because paper mode sends no orders to Dhan.

Implement it in the controlled-live phase, when multiple live runtimes can share one Dhan account. It must distinguish new orders, modifications, cancellations and read-only calls, and must not blindly retry uncertain submissions.

---

### 15. Official references

Verify before coding and again before live deployment:

- Package release: `https://pypi.org/project/dhanhq/`
- Dhan API and rate limits: `https://dhanhq.co/docs/v2/`
- Live market feed: `https://dhanhq.co/docs/v2/live-market-feed/`
- Option Chain: `https://dhanhq.co/docs/v2/option-chain/`
- Market quote/depth: `https://dhanhq.co/docs/v2/market-quote/`
- Orders/correlation ID: `https://dhanhq.co/docs/v2/orders/`
- Order updates: `https://dhanhq.co/docs/v2/order-update/`
- Portfolio/positions: `https://dhanhq.co/docs/v2/portfolio/`
- Releases/authentication changes: `https://dhanhq.co/docs/v2/releases/`

## Paper and live execution architecture

### 1. Objective

The same strategy and execution lifecycle must operate in paper and live modes. Only the broker adapter and fill source change.

```text
strategy decision
→ order intent
→ common risk gates
→ common execution coordinator
→ PaperBroker or DhanLiveBroker
→ common order/fill/position state
```

---

### 2. Internal order lifecycle

Use a complete internal state machine:

```text
CREATED
→ VALIDATED
→ SUBMISSION_RESERVED
→ SUBMITTED
→ ACKNOWLEDGED
→ PARTIALLY_FILLED
→ FILLED
```

Terminal alternatives:

```text
REJECTED
CANCELLED
EXPIRED
UNKNOWN
```

`UNKNOWN` is important when a network timeout occurs and broker acceptance is not yet known. It must trigger correlation-ID reconciliation, not blind resubmission.

---

### 3. Order intent

An order intent must be persisted before external submission.

Minimum fields:

- Correlation ID.
- Runtime ID.
- Strategy instance ID.
- Trading date and sequence number.
- Instrument and security ID.
- Side.
- Quantity.
- Order type.
- Limit/trigger price.
- Product type.
- Effective execution mode (`paper` or `live`).
- Mode-specific correlation-ID namespace.
- Signal ID.
- Basket ID and leg ID where relevant.
- Resolved configuration fingerprint.
- Creation time.
- Risk-decision result.

Example correlation ID:

```text
io_st01_20260729_0001
```

Respect the current broker length and character constraints when constructing correlation IDs.

---

### 4. Idempotent submission

Before any submission:

```text
persist intent with unique correlation ID
→ verify no existing submitted/acknowledged order
→ reserve submission
→ call broker
→ persist broker response
```

On timeout:

```text
mark UNKNOWN
→ query broker by correlation ID
→ if found, adopt broker order
→ if not found after bounded verification, classify for operator review or safe retry policy
```

Never create a second correlation ID for the same logical order simply because the first API call timed out.

---

### 5. PaperBroker fill model

#### 5.1 Market orders

Default model:

```text
BUY  → best ask after simulated latency + configured adverse slippage
SELL → best bid after simulated latency - configured adverse slippage
```

Fallback to LTP is allowed only when:

- The strategy explicitly permits it.
- The quote is fresh.
- A conservative additional slippage rule is applied.
- The fallback is clearly recorded.

#### 5.2 Latency

Support:

```yaml
paper_execution:
  submission_latency_ms: 250
  modification_latency_ms: 200
  cancellation_latency_ms: 150
```

The simulator should use the quote available after the simulated latency, not the signal-time quote.

#### 5.3 Limit orders

A limit buy fills only when an eligible ask/trade reaches or improves the limit after submission. A limit sell fills only when an eligible bid/trade reaches or improves the limit.

#### 5.4 Partial fills

Implement model support even when the first default policy fills the full allowed quantity. Test hooks must be able to generate partial fills.

#### 5.5 Rejection rules

PaperBroker should reject orders for:

- Invalid instrument.
- Invalid lot size or quantity.
- Invalid tick-size price.
- Stale quote.
- Missing bid/ask when required.
- Market closed.
- Risk-gate failure.
- Duplicate correlation ID.
- Configured failure injection.

#### 5.6 Costs

Use a separate cost calculator for:

- Brokerage.
- Exchange charges.
- Regulatory/statutory charges.
- Taxes.
- Stamp duty where applicable.

Do not hard-code current rates into strategy code. Rates may change and should be configuration-driven or maintained in a dedicated module.

---

### 6. Slippage model

Start simple and observable:

```yaml
slippage:
  options:
    mode: ticks
    market_order_ticks: 1
  stocks:
    mode: basis_points
    market_order_bps: 2
```

Later, allow optional quantity/depth-aware slippage. Do not build a complex exchange simulator initially.

Record:

- Signal reference price.
- Submission-time quote.
- Simulated fill price.
- Slippage amount.
- Latency.
- Fallback method if bid/ask was unavailable.

---

### 7. Custom execution engines

Implement and preserve the engine boundaries below.

#### 7.1 `TradingEngine` — single-leg, underlying-driven

Reuse the existing engine for strategies such as Supertrend, EMA crossover and opening-range breakout.

```text
completed underlying candle/tick decision
→ validate option contract or stock instrument
→ risk check
→ submit entry through the strategy broker
→ track fill
→ create position
→ manage stop/target/trail/custom exit policy
→ exit
→ mandatory square-off
```

#### 7.2 `MultiLegEngine` — basket and rolling options

Reuse the existing engine for straddles, strangles, delta-neutral baskets, rolling and combined-premium strategies.

Responsibilities:

- Basket ID and leg IDs.
- Ordered or parallel leg entry policy.
- Partial-fill handling.
- Maximum unhedged duration.
- Per-leg and basket P&L.
- Combined-premium and timestamp-aligned candle handling.
- Roll sequencing and re-entry.
- Basket exit and restart recovery.

#### 7.3 `FixedStrikeEngine` — independent CE/PE option charts

Reuse the existing engine when strikes are selected and retained for the session while CE and PE are evaluated on their own option-price candles.

Responsibilities:

- Select and lock session contracts.
- Maintain separate CE and PE candle/indicator state.
- Prevent state leakage between legs.
- Manage independent entries/exits under common strategy risk.
- Restore selected strikes and leg state after restart.

#### 7.4 Scanner-driven stock portfolio engine

Add this engine only when the first scanner-driven stock strategy is implemented:

```text
candidate list
→ capital allocation
→ per-stock validation
→ order intents
→ position tracking
→ portfolio risk
→ intraday square-off
```

#### Engine selection rule

Each strategy configuration selects exactly one engine. The broker factory then selects exactly one broker for that worker based on the strategy mode.

```text
strategy config
    ├── engine → TradingEngine | MultiLegEngine | FixedStrikeEngine | StockPortfolioEngine
    └── mode   → PaperBroker | DhanLiveBroker after safety gates
```

Do not combine the three options engines into one generic engine. Preserve the existing custom exit-policy registry rather than duplicating exit logic in strategies.

### 8. Multi-leg execution policy

Each strategy must explicitly define:

- Entry order sequence.
- Whether hedge legs are placed first.
- Whether the basket requires all legs before becoming active.
- Maximum time between first and final leg.
- Behaviour when one leg rejects.
- Behaviour when one leg partially fills.
- Maximum acceptable combined slippage.
- Roll close/open sequence.
- Emergency flattening behaviour.

Do not assume multi-leg atomicity. Standard broker orders may execute independently.

---

### 9. Position model

Position records must include:

- Strategy instance ID.
- Runtime session ID.
- Instrument/security ID.
- Side and quantity.
- Average entry price.
- Realised and unrealised P&L.
- Effective execution mode (`paper` or `live`).
- Mode-specific correlation-ID namespace.
- Entry and exit order IDs.
- Basket and leg identity.
- Stop, target and trailing state.
- Roll/adjustment counters.
- Last mark time and source.
- Open/closing/closed/recovery-required status.

Do not derive critical live position state only from strategy memory.

---

### 10. Intraday square-off

Square-off is a runtime responsibility.

Recommended staged behaviour:

```text
entry cutoff reached
→ block new entries

pre-square-off warning time
→ notify and validate open positions

square-off time
→ submit exits
→ monitor acknowledgements and fills

retry window
→ reconcile and retry only known-safe actions

final cutoff
→ emergency alert and broker-state verification
```

Persist square-off attempts and final status. A process restart must not reset the square-off state or allow new entries after the cutoff.

---

### 11. Positional lifecycle

Positional options require persistent cycle identity, overnight state, expiry awareness, adjustment history, daily marks and explicit next-action times.

A positional runtime must not infer that there are no positions merely because the intraday order book is empty.

#### Expiry and settlement simulation

An option position must never simply disappear from the paper database on expiry.

Before any strategy is allowed to hold through expiry, implement a versioned settlement policy that supports:

- Expiry calendar and last-trading-day handling.
- Final settlement price capture.
- ITM/OTM determination.
- Index-option cash settlement.
- Exercise/assignment event recording.
- Exercise-related STT and other charges using rules effective on the settlement date.
- T+1 settlement timing where applicable.
- Stock-option physical-settlement obligations, delivery margin and assignment risk.

For the initial positional paper runtime, use the safer default:

```yaml
expiry_policy: force_square_off_before_expiry
```

A strategy may use `simulate_exchange_settlement` only after settlement tests pass. Stock options must remain force-square-off by default unless the simulator explicitly models physical delivery and margin obligations.

Do not hard-code tax rates indefinitely. Store effective-dated charge rules and verify them against official NSE/broker schedules. Official references include:

- NSE settlement price: `https://www.nseindia.com/static/products-services/equity-derivatives-settlement-price`
- NSE STT schedule: `https://www.nseindia.com/static/products-services/equity-derivatives-securities-transaction-tax`

### 12. Future live order update handling

Use live order-update events where available. Treat events as authoritative updates but make processing idempotent because duplicate or repeated messages can occur.

```text
broker event
→ validate account and order identity
→ map to internal state
→ ignore already-applied event
→ append order event
→ update aggregate order
→ update fill/position when applicable
→ publish dashboard/notification event
```

Use read APIs as a reconciliation fallback.

---

### 13. Live mode initial rollout

When live mode is eventually approved:

1. Enable one runtime only.
2. Enable one strategy only.
3. Use minimum practical quantity.
4. Keep all other strategies in paper or disabled mode.
5. Observe authentication, feed, order update, reconciliation, square-off and restart behaviour.
6. Expand only after several clean sessions and a written review.

Paper and live positions must never share the same strategy instance ID without mode included in the position identity and reporting dimensions.

## Persistence, reconciliation and restart recovery

### 1. Objective

The runtime must survive process restarts without losing the ability to understand and manage open positions. In live mode, broker state must be reconciled before new entries are allowed.

---

### 2. Database selection

Use SQLite for the initial local Mac implementation.

Reasons:

- Simple local deployment.
- Reliable transactional storage.
- No separate database service.
- Adequate for the planned strategy count.
- Easy backup and integrity verification.

Use one database per runtime group to preserve failure isolation.

---

### 3. Database files

```text
data/operational/
├── intraday_options.db
├── positional_options.db
├── intraday_stocks.db
└── positional_stocks.db  # later
```

Enable:

- WAL mode.
- Foreign-key enforcement.
- Busy timeout.
- Short transactions.
- Read-only dashboard connections.

Do not allow dashboards to run migrations or write operational tables.

---

### 4. Migration approach

Use simple sequential SQL migrations with a `schema_migrations` table.

Walking-skeleton requirements:

1. Acquire a migration lock.
2. Validate the current schema version.
3. Apply each migration once.
4. Run foreign-key and integrity checks.
5. Record migration version, name and applied time.

Do not build migration checksums or destructive-migration automation during the walking skeleton. Add checksum enforcement and stronger backup/rollback validation in the controlled-live phase or when the first genuinely destructive migration appears.

Add SQLAlchemy/Alembic only if sequential SQL becomes a demonstrated maintenance problem.

### 5. Tables by delivery stage

#### Walking skeleton

Implement only what the first vertical slice needs:

- `schema_migrations`
- `runtime_sessions`
- `runtime_heartbeats`
- `signals`
- `order_intents`
- `orders`
- `fills`
- `positions`
- `strategy_state`
- `notifications`
- `errors`

#### Paper-foundation expansion

Add when the walking skeleton is green:

- `strategy_registry`
- `order_events`
- `position_events`
- `risk_events`
- Any option-chain snapshot metadata required for audit/freshness; avoid storing every full chain indefinitely.

All correlation IDs must be unique. Dashboard connections are read-only. No secrets belong in SQLite.

#### Deferred controlled-live tables

Add only in the pre-live phase:

- `reconciliation_runs`
- `reconciliation_mismatches`
- Shared live-order throttle reservations, if implemented in SQLite.

Do not make these live-only tables a prerequisite for paper forward testing.

### 6. Transaction boundaries

Critical examples:

#### Order submission preparation

In one transaction:

- Insert order intent.
- Reserve unique correlation ID.
- Record passed risk decision.
- Mark submission reservation.

External broker submission happens after commit. The response is then persisted in a new transaction.

#### Fill processing

In one transaction:

- Insert idempotent fill.
- Append order event.
- Update aggregate order.
- Update position quantity and average price.
- Persist relevant strategy state.

Avoid long-running database transactions around network calls.

---

### 7. Paper restart recovery

Startup flow:

```text
acquire PID/lock
→ open database and run integrity checks
→ load previous incomplete runtime session
→ load open paper orders
→ load open paper positions
→ load strategy state and risk state
→ validate state schema versions
→ subscribe required instruments
→ wait for fresh quotes
→ recalculate marks
→ resume exits/adjustments
→ permit new entries
```

Restore at minimum:

- Entry quantity and price.
- Stop, target and trailing values.
- Highest/lowest favourable excursion where used.
- Re-entry count.
- Rolling/adjustment count.
- Selected expiry and strikes.
- Last processed candle.
- Daily P&L and loss-limit state.
- Intraday cutoff/square-off state.

State must never leak between days or contracts.

---

### 8. Future controlled-live startup reconciliation

```text
acquire PID/lock
→ validate database
→ load local open state
→ validate Dhan token and connectivity
→ fetch broker orders
→ fetch broker trades
→ fetch broker positions
→ normalise broker snapshot
→ compare with local state
→ persist reconciliation run and mismatches
→ block new entries on critical mismatch
→ restore position-management state
→ subscribe required instruments
→ permit new entries only after successful reconciliation
```

#### Broker-authoritative principle

For live quantity and existence of positions, broker state is authoritative. Local state remains essential for strategy intent, stops, targets, roll history and audit.

Never silently delete local records. Append reconciliation events and update state with an explicit reason.

---

### 9. Future controlled-live mismatch classifications

The detailed taxonomy below is a target for Phase 10, not an initial paper-mode requirement:

```text
MATCHED
LOCAL_ONLY
BROKER_ONLY
QUANTITY_MISMATCH
SIDE_MISMATCH
PRODUCT_MISMATCH
PRICE_MISMATCH
LOCAL_OPEN_BROKER_CLOSED
LOCAL_CLOSED_BROKER_OPEN
UNKNOWN_ORDER
DUPLICATE_CORRELATION
```

#### Critical mismatches

Block new entries for:

- Broker-only open position.
- Quantity or side mismatch.
- Unknown pending order.
- Local closed but broker open.
- Duplicate correlation identity.

A price difference may be informational when it is caused by expected broker-average rounding, but the tolerance must be explicit.

---

### 10. Future controlled-live resolution policies

Permitted automated actions must be narrow and documented.

Examples:

- Adopt a broker order found by an existing correlation ID.
- Update local traded quantity from broker-confirmed fills.
- Mark a local order rejected when broker status confirms rejection.
- Mark a local position closed when broker trade/position evidence proves closure.

Do not automatically flatten a broker-only position without an explicit emergency policy. First alert and block entries unless the configured kill-switch policy authorises flattening.

---

### 11. Intraday day-boundary handling

At session start:

- Create a new runtime session.
- Reset intraday-only strategy state.
- Carry unresolved prior-day reconciliation issues explicitly.
- Verify no unexpected carry-forward intraday positions.

At session end:

- Confirm square-off.
- Reconcile final orders/trades/positions.
- Persist final daily P&L.
- Mark session complete only when required checks pass.

A new process start must not clear the daily loss limit or allow entries after the configured entry cutoff.

---

### 12. Backups and retention

Keep backups modest:

- Backup operational DB before schema migration.
- Optional daily copy after final reconciliation.
- Retain a configurable number of daily backups.
- Compress old logs.
- Do not back up raw caches or PID files.

Implement retention for:

- Logs.
- Heartbeats.
- Tick/candle debug samples.
- Notifications.
- Old database backups.

Do not allow storage growth to remain unbounded.

---

### 13. Integrity checks

At minimum:

- `PRAGMA integrity_check` during controlled startup/maintenance.
- `PRAGMA foreign_key_check` after migrations.
- Unique correlation-ID verification.
- Open-position consistency checks.
- No negative open quantity unless explicitly modelled by side.
- Order filled quantity not exceeding requested quantity.
- Strategy-state schema-version validation.

Database corruption or migration failure must stop the affected runtime before order activity begins.

## Risk, health, dashboards, Telegram and Mac operations

### 1. Risk architecture

Risk checks must run before every order intent and remain active while positions are open.

```text
strategy decision
→ strategy risk
→ runtime risk
→ account risk
→ execution permission
```

A risk rejection is a normal recorded outcome, not an unhandled exception.

---

### 2. Account-level controls

Mandatory before live mode:

- Maximum daily realised plus unrealised loss.
- Maximum total open positions.
- Maximum total options legs.
- Maximum deployed capital/margin.
- Maximum order requests within configured windows.
- Global new-entry kill switch.
- Global emergency exit policy.
- Block entries on stale market data.
- Block entries on broker disconnection.
- Block entries on unresolved critical reconciliation mismatch.
- Block entries when persistence is unavailable.

Account-level risk state must be shared across runtime processes through a lightweight SQLite/file-lock mechanism.

---

### 3. Runtime-level controls

Each strategy-group supervisor must define:

- Group daily loss cap, with paper and live values calculated separately.
- Maximum concurrently active strategy workers.
- Maximum live strategy workers.
- Maximum open positions/legs by execution mode.
- Entry start and cutoff.
- Intraday square-off schedule.
- Maximum feed-stale duration.
- Maximum worker restart attempts.
- Behaviour after authentication, feed or database degradation.
- Per-worker queue depth and lag limits.

A paper loss must not automatically trigger the live-account kill switch. Live account risk aggregates only live orders and positions, although a separate combined monitoring metric may be displayed.

### 4. Strategy-level controls

Each strategy configuration must include:

- `enabled`.
- `mode: paper | live`.
- `live_approved`.
- Engine identifier.
- Maximum quantity/lots.
- Maximum strategy daily loss.
- Maximum loss per trade.
- Maximum re-entry count.
- Maximum rolling/adjustment count.
- Cooldown.
- Entry cutoff.
- Exit time.
- Stop-loss and target requirements.
- Maximum simultaneous positions.

No strategy should infer a permissive default when required risk configuration is missing.

Paper and live strategy risk state must be persisted and queried separately. A strategy-mode change must not inherit an incompatible open-position or daily-risk state from the previous mode.

### 5. Multi-leg options safeguards

- Define leg-entry order.
- Define maximum unhedged duration.
- Define action on rejected or partial leg.
- Define maximum combined slippage.
- Define basket stop and per-leg emergency stop.
- Define roll close/open sequencing.
- Define maximum number of rolls.
- Define final square-off procedure.

Risk controls must not assume that all basket legs fill together.

---

### 6. Runtime health model

Health is not just “process running”. Track:

#### Process

- PID and lock ownership.
- Start time.
- Main runtime-loop heartbeat and feed-adapter heartbeat.
- Memory and CPU warning thresholds where useful.
- Last clean checkpoint.

#### Authentication

- Token present.
- Token validation time.
- Expiry time.
- Client identity match.

#### Market data

- WebSocket state.
- Last message time.
- Last tick per critical instrument.
- Expected vs active subscriptions.
- Reconnect count.
- Decode errors.
- Stale instruments.

#### Broker

- Connectivity.
- Last successful read.
- Last successful order operation.
- Live order-update connection state.
- Rate-limiter status.

#### Database

- Writable state.
- Last successful transaction.
- WAL size warning.
- Integrity-check result.
- Migration version.

#### Strategies

- Enabled state.
- Warm-up state.
- Last evaluation.
- Last signal.
- Error count.
- Quarantine status.

#### Operations

- Square-off state.
- Reconciliation state.
- Telegram status.
- Dashboard data freshness.

---

### 7. Heartbeats

Each runtime writes a compact heartbeat periodically to:

- Its operational database.
- Optionally an atomic JSON file for very simple external status checks.

Do not write one database row for every tick. A heartbeat every 5–15 seconds is normally sufficient; make it configurable.

Example health statuses:

```text
HEALTHY
DEGRADED
BLOCK_NEW_ENTRIES
RECOVERY_REQUIRED
STOPPING
FAILED
```

---

### 8. PID and lock files

Use one supervisor lock per strategy group and one PID/worker lock per enabled strategy.

```text
data/runtime/pid/intraday_options_supervisor.pid
data/runtime/locks/intraday_options_supervisor.lock

data/runtime/pid/io_supertrend_fast_v1.pid
data/runtime/locks/io_supertrend_fast_v1.lock
```

Startup flow:

1. Acquire the strategy-group supervisor lock.
2. Check whether the stored supervisor PID is alive and belongs to the expected command/project path.
3. Start the shared feed and worker registry.
4. For each enabled strategy, acquire its worker lock and reject duplicate execution.
5. Write supervisor and worker PIDs atomically.
6. Remove PID files only during controlled shutdown or verified stale cleanup.

A PID file alone is insufficient because PIDs can be reused. Validate process command/path or use lock ownership.

The supervisor must prevent a second worker for the same strategy ID even when one configuration says paper and another says live.

### 9. Telegram notifications

#### Event categories

- Authentication.
- Runtime lifecycle.
- Feed lifecycle.
- Strategy lifecycle.
- Orders and fills.
- Positions and P&L.
- Risk limits.
- Reconciliation.
- Square-off.
- System errors.

#### Message discipline

Do not send every tick, candle or heartbeat. Aggregate repeated errors and use rate limiting to avoid alert storms.

A useful message includes:

- Environment/mode.
- Runtime.
- Strategy ID.
- Event.
- Instrument/position summary where relevant.
- Timestamp.
- Correlation/order ID where safe.
- Required action.

Never include client ID, PIN, TOTP secret, access token or Telegram token.

#### Failure isolation

Telegram timeouts must be bounded. Notification sending may use a small internal queue, but do not introduce Celery or an external broker. When Telegram is unavailable, persist notification failure and continue safe runtime operation.

---

### 10. Streamlit dashboard

Use one multipage application.

#### Master page

- Strategy-group supervisor cards.
- Counts of paper, live, disabled and failed strategies.
- Global/runtime live-gate status.
- Last heartbeat.
- Feed/broker/database status.
- Total open positions.
- Group P&L split into paper and live totals.
- Critical alerts.
- Reconciliation status.

#### Intraday options page

- Strategy state and explicit `PAPER`/`LIVE` badge.
- Worker PID, engine type and last heartbeat.
- Paper and live P&L totals shown separately.
- Open legs and baskets.
- Selected strikes and expiry.
- Combined and per-leg P&L.
- Roll count.
- Entry cutoff and square-off status.

#### Positional options page

- Cycle identity.
- Overnight positions.
- Adjustments.
- Expiry.
- Daily and cycle P&L.
- Reconciliation state.

#### Intraday stocks page

- Universe status.
- Scanner rankings.
- Selected candidates.
- Rejection reasons.
- Allocations.
- Open trades.

#### System health page

- Authentication/token expiry.
- WebSocket state.
- Stale instruments.
- Reconnect counts.
- Database health.
- PID/lock status.
- Recent errors.

#### Read-only enforcement

- Open SQLite in read-only mode.
- No order buttons.
- No direct strategy mutation.
- No runtime imports that create side effects.
- A dashboard crash must not affect trading.

Operational controls should initially remain command-line scripts with explicit safety checks.

---

### 11. Command-line operations

Provide scripts such as:

```text
scripts/authenticate
scripts/start_runtime intraday_options
scripts/start_strategy io_supertrend_fast_v1
scripts/stop_strategy io_vwap_straddle_v1
scripts/stop_runtime intraday_options
scripts/status
scripts/reconcile --strategy io_supertrend_fast_v1 --read-only
scripts/square_off --strategy io_supertrend_fast_v1 --confirm
scripts/validate_environment
```

Live-impacting commands must require explicit confirmation and log an audit event.

---

### 12. LaunchAgent design

Do not enable LaunchAgent during early development.

After manual testing, create separate property-list files for:

- Authentication bootstrap.
- Intraday options.
- Positional options.
- Intraday stocks.
- Dashboard.

Requirements:

- Absolute paths only.
- Correct `.venv` interpreter.
- Explicit working directory.
- Environment file path.
- Independent stdout/stderr logs.
- Bounded restart policy.
- No restart loop after a deliberate safety shutdown.
- Start only when the strategy-group runtime is enabled; the supervisor starts only strategies with `enabled: true`.
- Do not start legacy and new trading systems together.

---

### 13. Mac operational considerations

Before market use:

- Prevent system sleep during runtime hours.
- Ensure network is stable.
- Use a stable power source.
- Maintain accurate system time.
- Ensure adequate free disk space.
- Configure log and backup retention.
- Validate the mounted drive/project path exists before startup.
- Detect path changes after moving the repository.
- Validate static public IP for future live order APIs.
- Document manual emergency procedures when the Mac or network fails.

Do not rely on the dashboard browser being open for runtimes to operate.

---

### 14. Operational severity

Recommended severity levels:

```text
INFO      normal lifecycle event
WARNING   degraded but controlled
ERROR     function failed; runtime may block new entries
CRITICAL  account/position safety at risk; immediate attention
```

Examples:

- Telegram unavailable: `WARNING`.
- One strategy quarantined with no position: `ERROR` for strategy, runtime may continue.
- Database unwritable: `CRITICAL`, block orders.
- Broker-only open position: `CRITICAL`, block new entries.
- Feed stale with open position: `CRITICAL` or defined emergency state.

## Packages, testing, implementation phases and acceptance

### 1. Package strategy

Keep one package per job and keep difficult optional packages out of the default environment.

#### Default runtime dependencies

| Package | Purpose |
|---|---|
| `dhanhq` | Official Dhan client and initial MarketFeed implementation |
| `pandas` | Candle tables, resampling and time-series operations |
| `numpy` | Numerical calculations |
| `pandas-ta-classic` | Standard indicators through one adapter |
| `pyotp` | TOTP generation |
| `pydantic` / `pydantic-settings` | Typed models and configuration |
| `python-dotenv` | Local ignored `.env` loading |
| `PyYAML` | Runtime and strategy configuration |
| `httpx` | Telegram and REST calls |
| `tenacity` | Bounded retries for safe idempotent operations |
| `streamlit` | Read-only multipage dashboard |
| `filelock` | Local process and token-cache locks |
| `psutil` | Optional robust PID validation |

#### Optional strategy dependency

| Package | When to install |
|---|---|
| `py_vollib` | Only when a strategy requires Greek estimates between Dhan Option Chain snapshots or independent validation of Dhan Greeks/IV |

Do not include `py_vollib` in the default dependency set. Keep it in an optional dependency group because `py_lets_be_rational` can complicate installation on some Python/macOS combinations.

#### Development dependencies

Use `pytest`, `pytest-cov`, `ruff` and `mypy`. Add `pytest-asyncio` only if the selected SDK adapter actually exposes async code. Add deterministic-time helpers only when tests need them.

#### Standard library preference

Prefer `sqlite3`, `logging`, `zoneinfo`, `decimal`, `queue`, `threading` and `pathlib`. Do not choose `asyncio` as the global concurrency model merely because the system consumes a WebSocket.

#### Omit initially

TA-Lib, multiple indicator backends, backtesting frameworks, Redis/Kafka/Celery, SQLAlchemy/Alembic, Docker/Kubernetes, ML stacks and cloud monitoring SDKs.

---

### 2. Indicator and Greeks policy

```text
strategy
→ common indicator interface
→ pandas-ta-classic adapter
```

Test EMA, ATR, RSI, ADX, VWAP and Supertrend using fixed fixtures. Do not run native and package implementations in parallel.

For options:

```text
Dhan Option Chain snapshot → default Greeks and IV
Dhan WebSocket             → fast LTP/bid/ask/OI updates available in feed
py_vollib (optional)       → interpolation/validation only
```

---

### 3. Testing layers

#### Walking-skeleton end-to-end test

Prove one complete path before broad test expansion:

```text
SDK/fake-safe feed event
→ normalised live tick
→ completed candle
→ deterministic fake signal
→ PaperBroker fill
→ SQLite
→ one dashboard tile
→ Telegram test event
→ restart
→ recovered position
```

#### Paper-foundation unit/integration tests

Cover configuration, token-cache sanitisation, SDK adapter mapping, queue behaviour, candle completion, indicator fixtures, Option Chain throttle/cache, paper fill rules, risk gates, persistence, dashboard reads and notification isolation.

Normal tests use fake credentials and recorded/synthetic events. Real connectivity tests are opt-in and read-only.

---

### 4. Failure tests by stage

#### Required for paper foundation

- Feed disconnect and successful resubscription.
- Queue overflow/degraded health handling.
- Duplicate or out-of-order tick without duplicate candle/action.
- Stale market data blocks new entries.
- Token-cache expiry.
- Telegram timeout does not stop trading.
- SQLite busy/restart recovery.
- Duplicate process start is rejected.
- Restart with an open paper position.
- Restart after entry cutoff or during paper square-off.
- Option Chain requests are deduplicated and respect the three-second throttle.

#### Deferred to controlled live phase

- Crash after real order submission but before response.
- Unknown real submission result.
- Live partial fill and modification limits.
- Broker/local position mismatch matrix.
- Shared cross-process order-rate limiter contention.
- Migration-checksum tampering.
- Emergency live flattening and static-IP/network change scenarios.

PaperBroker state transitions may still have focused unit tests, but the full live failure-injection matrix must not delay the first strategy.

---

### 5. Implementation phases

#### Phase 0 — Repository-reference audit and minimal bootstrap

Before coding replacements:

- Inspect `Soundar1410/Trading_Automation` read-only.
- Inventory `TradingEngine`, `MultiLegEngine`, `FixedStrikeEngine`, broker factory, shared feed supervisor and registered exit policies.
- Identify reusable tests and interfaces.
- Record reuse decisions and intentional deviations.
- Create the new repository, one `pyproject.toml`, lock file, `.gitignore`, `.env.example`, basic configuration/logging and a minimal SQLite schema.

Do not create eleven architecture documents.

#### Phase 1 — Walking skeleton

Implement one `intraday_options` supervisor and one paper strategy worker using the existing single-leg engine pattern:

```text
Dhan SDK MarketFeed adapter
→ shared feed hub
→ one worker IPC queue
→ one completed candle
→ deterministic fake signal
→ PaperBroker fill
→ minimal SQLite state with execution_mode=paper
→ one Streamlit tile
→ one Telegram event
→ restart worker and recover
```

Use a fake/recorded feed in normal automated tests and an opt-in live-feed smoke test during market hours. Stop and review after this phase.

#### Phase 2 — Dhan and shared-feed hardening

- Authentication bootstrap and atomic token cache.
- Exact `dhanhq` version compatibility decision.
- Shared feed supervisor, reconnect/resubscribe and subscription union.
- Bounded callback-to-IPC-queue bridge.
- Per-worker lag/overflow health.
- Option Chain service, cache and three-second throttle.

#### Phase 3 — Preserve custom engines and policies

- Port or adapt `TradingEngine` without changing signal/execution behaviour.
- Port `MultiLegEngine` and its basket models/tests when the first multi-leg consumer is scheduled.
- Port `FixedStrikeEngine` and independent CE/PE candle tests when the first fixed-strike consumer is scheduled.
- Preserve the existing registered exit policies and their regression tests.
- Retain strategy-wise broker-factory routing.

Do not build a universal engine.

#### Phase 4 — Candle, indicator and paper-execution foundation

- Session/timezone and candle-continuity policies.
- `pandas-ta-classic` adapter and fixtures.
- Timestamp-aligned synthetic option-premium samples.
- Bid/ask fill model, latency, slippage and costs.
- Limit orders and focused partial/rejection simulation.
- Mode-namespaced correlation IDs.
- Entry cutoff, daily loss limits and square-off.

#### Phase 5 — Mixed-mode supervisor and persistence

- Strategy-specific `enabled`, `mode`, `live_approved` and engine configuration.
- One worker per enabled strategy.
- Paper/live records and P&L separated in the shared group database.
- Duplicate-worker prevention.
- Paper workers allowed while live gates remain globally disabled.
- Add positional options and intraday stocks one at a time; keep positional stocks a placeholder.

#### Phase 6 — Paper recovery and expiry handling

- Restore open paper positions and strategy/risk state by strategy and mode.
- Restore selected fixed strikes, basket legs, rolling counters and custom exit state.
- Implement `force_square_off_before_expiry`.
- Add exchange-settlement simulation before any strategy intentionally holds through expiry.

#### Phase 7 — Operations

Harden Streamlit, Telegram, health snapshots, worker/supervisor PID handling, log retention and manual commands.

#### Phase 8 — LaunchAgent validation

Enable LaunchAgents only after manual supervisor/worker start, stop, crash, restart, duplicate-worker and old-system exclusion tests pass.

#### Phase 9 — Real strategies

Implement strategies one at a time using the preserved engine that matches each strategy shape. Paper-forward-test each strategy independently. The presence of one live-approved strategy later must not prevent other strategies from remaining in paper mode.

#### Phase 10 — Controlled live readiness

Only now implement:

- `DhanLiveBroker` order methods.
- Global, runtime and per-strategy live gates.
- Static-IP preflight.
- Shared cross-process live-order rate limiter.
- Full reconciliation tables and mismatch taxonomy.
- Live account-risk aggregation across live workers.
- Migration checksums/strong rollback validation.
- Live order-update handling.
- Full live failure-injection matrix.
- Minimum-quantity rollout with one live strategy while at least one other strategy remains paper for mixed-mode validation.

### 6. Acceptance gates

#### Walking-skeleton gate

- One live/recorded feed event reaches a strategy worker through the shared SDK adapter.
- One completed candle creates one deterministic paper order.
- Fill and position appear in SQLite with `execution_mode=paper`.
- One dashboard tile and Telegram event work.
- Restart restores the open paper position.
- Duplicate worker startup is refused.

#### Engine-reuse gate

- Existing engine behaviour and key regression tests are preserved.
- `TradingEngine`, `MultiLegEngine` and `FixedStrikeEngine` remain separate.
- Custom exit policies remain registered and independently testable.
- Strategy code does not call the Dhan SDK directly.

#### Paper-foundation gate

- Token cache and shared-feed reconnect are reliable.
- Option Chain throttle/cache works.
- Candles and indicator fixtures are deterministic.
- Paper fills use credible bid/ask, latency and slippage rules.
- Essential risk and square-off gates survive restart.
- Dashboard/Telegram failures do not stop workers.
- Supervisor and worker PID/locks prevent duplicates.
- Paper/live fields and queries cannot be mixed accidentally.

#### Mixed-mode architecture gate

Before live order placement is implemented:

- Configuration supports one paper and one live-designated strategy in the same group.
- With global live disabled, the live-designated strategy is blocked rather than rerouted to paper.
- The paper strategy continues safely.
- Broker factory tests prove strategy-wise routing.
- P&L, correlation IDs and positions remain mode-separated.

#### Controlled-live gate

- Static IP is resolved.
- Exact Dhan package/API behaviour is re-verified.
- Shared order limiter, correlation recovery and full reconciliation pass.
- Live account risk aggregates all live workers and excludes paper simulation.
- Live-only migrations and failure scenarios pass.
- One minimum-quantity strategy is separately approved.
- At least one other intraday-options strategy remains in paper mode during mixed-mode validation.

### 7. Strategy acceptance criteria

Each real strategy needs:

- Written rule specification.
- Stable strategy instance ID.
- `enabled`, `mode`, `live_approved` and engine selection.
- Typed strategy and risk configuration.
- Signal, state and risk tests.
- Tests against the selected custom engine.
- Restart and mode-transition tests.
- Paper-forward evidence.
- Slippage review.
- Separate live approval.

A strategy must pass paper acceptance independently. Live approval applies only to that strategy instance and configuration fingerprint; it must not approve other strategies or variants.

Historical backtesting remains optional when reliable data and a concrete need exist.

## Claude Code implementation prompt

### Role and source of truth

Act as the lead Python implementation engineer for this local forward-testing project. Read this complete document first. Prioritise, in order: safety, a working walking skeleton, simplicity for one Mac, paper-forward realism and clean future live boundaries.

### Objective

Build an independently deployable repository for fewer than 20 options strategies and fewer than 10 stock strategies. Use real Dhan market data in paper mode. Support strategy-wise `paper` and future `live` execution under the same intraday-options supervisor. Simulate execution through `PaperBroker` initially. Do not place live orders in the initial implementation.

### Existing-repository reuse requirement

Inspect `Soundar1410/Trading_Automation` read-only before implementing engine, broker-routing or orchestration replacements.

Preserve or port with minimal behavioural change:

- `TradingEngine`.
- `MultiLegEngine`.
- `FixedStrikeEngine`.
- `build_broker(cfg)` strategy-wise routing pattern.
- Process-per-strategy supervisor with shared Dhan feed.
- Registered custom exit policies and their tests.

Do not create a runtime dependency on the old repository. Do not copy secrets or runtime data. Document reused files, tests and intentional changes.

### Strategy-wise execution mode

Use this hierarchy:

```text
global live gate
→ runtime-group live permission
→ individual strategy enabled/mode/live approval
→ broker factory
```

Each enabled strategy worker selects exactly one mode and one broker at startup:

```text
mode: paper → PaperBroker
mode: live  → DhanLiveBroker only after every live gate passes
```

Never fall back from live to paper. Mode changes require worker restart and are blocked while incompatible open positions or pending orders exist.

### Process model

Use one strategy-group supervisor, one shared Dhan feed hub and one worker process per enabled strategy. Do not open one WebSocket per strategy. A paper worker failure must not stop a live worker.

### Delivery rule: vertical slice first

Do not build the full platform horizontally before anything runs.

First deliver this executable slice:

```text
Dhan SDK MarketFeed adapter
→ shared feed hub
→ one paper strategy worker
→ live or recorded tick
→ completed candle
→ deterministic fake signal
→ existing TradingEngine-compatible path
→ PaperBroker fill
→ SQLite with strategy_id and execution_mode
→ one Streamlit tile
→ one Telegram event
→ restart worker and recover
```

Stop after this slice, run its tests and provide evidence before expanding.

### Market-feed and concurrency decision

- Begin with the official `dhanhq` SDK `MarketFeed` behind `DhanSdkMarketFeedAdapter`.
- Run SDK feed handling behind a bounded thread-safe queue.
- Do not put strategy logic in callbacks.
- Do not design the whole runtime around `asyncio`.
- Implement a custom binary WebSocket client only if a focused spike proves the SDK inadequate; document the reason first.

### Dhan package version

As of 29 July 2026, PyPI lists `dhanhq 2.2.0` as released with breaking changes, while 2.1 introduced `DhanContext`. Run a compatibility spike on the target Mac/Python, then pin the exact version that passes. Do not assume `2.1.0` is the latest stable and do not use a loose version range.

### Packages

Default dependencies:

- `dhanhq`
- `pandas`
- `numpy`
- `pandas-ta-classic`
- `pyotp`
- `pydantic`
- `pydantic-settings`
- `python-dotenv`
- `PyYAML`
- `httpx`
- `tenacity`
- `streamlit`
- `filelock`
- testing/lint/type-check tools

`py_vollib` is optional and must not be installed by default. Add it only through an optional dependency group when a named strategy needs Greek estimation between Dhan Option Chain snapshots or validation of Dhan values.

Do not add TA-Lib, backtesting frameworks, Redis, Kafka, Celery, Docker, Kubernetes, SQLAlchemy/Alembic or ML packages without a proven blocker.

### Custom engines and policies

Keep these separate engine shapes:

```text
TradingEngine
MultiLegEngine
FixedStrikeEngine
StockPortfolioEngine   # only when stocks need it
```

Reuse existing tests and preserve the custom exit-policy registry. Do not merge the options engines into one generic engine. Each strategy config selects the engine, and the broker factory selects paper/live execution independently.

### Options data

- Use Dhan Option Chain as the default source for delta, theta, gamma, vega and IV.
- Implement one shared snapshot service and cache.
- Respect Dhan's documented one-unique-request-per-three-seconds limit.
- Deduplicate requests across strategies.
- Use the WebSocket feed for fast leg LTP/bid/ask tracking; never poll Option Chain for every price update.

### Walking-skeleton storage

Use minimal SQLite tables only: schema migrations, supervisor/worker sessions and heartbeats, signals, order intents, orders, fills, positions, strategy state, notifications and errors. Every trading record must include strategy ID and effective execution mode.

Use sequential migrations with version/name/applied time and integrity checks. Defer migration checksums until controlled-live readiness or the first destructive migration.

### PaperBroker

Implement credible bid/ask fills after configurable latency, slippage, tick/lot validation, limit eligibility, costs and focused partial/rejection state support. Do not fill every order immediately at LTP.

### Paper recovery

Persist enough state to restart and restore an open paper position, stop/target/trailing state, last processed candle, daily risk state and square-off status.

### Options expiry

Default positional policy:

```yaml
expiry_policy: force_square_off_before_expiry
```

Before enabling any strategy that can hold through expiry, implement tested exchange-settlement simulation for index options and explicit physical-settlement handling for stock options, including effective-dated exercise/STT/charge rules.

### What to defer to Phase 10

Do not build these before the first real strategy:

- Live Dhan order placement/modification/cancellation.
- Shared cross-process order-rate limiter.
- Full broker/local mismatch taxonomy and reconciliation tables.
- Migration checksum enforcement.
- Full live-order failure-injection matrix.
- Emergency live flattening automation.

### Implementation order

Follow Phases 0–8 in this document, beginning with the read-only repository audit in Phase 0, and stop for review immediately after Phase 1. Implement real strategies in Phase 9 as soon as the relevant paper foundation is accepted. Phase 10 is a separate controlled-live project.

### Testing

Normal tests use fake credentials and recorded/synthetic feed events. Add opt-in read-only Dhan smoke tests for authentication, feed subscription and Option Chain. No automated test may place a live order.

Paper-foundation mandatory failures are feed reconnect, per-worker queue overflow, duplicate/out-of-order ticks, stale data, token expiry, Telegram timeout, SQLite restart, duplicate supervisor/worker, worker crash isolation, open-position recovery, cutoff/square-off recovery and Option Chain throttling. Add tests proving that a blocked live-designated strategy is not silently rerouted to paper.

### Documentation output

Do not create eleven architecture documents. This master document remains the architecture source of truth. Create only:

```text
README.md
docs/IMPLEMENTATION_STATUS_AND_RUNBOOK.md
```

The runbook records the exact pinned package version, start/stop/recovery commands, completed milestone evidence, known limitations and next phase.

### Stop conditions

Stop and report clearly when official Dhan behaviour conflicts with this specification, the SDK compatibility spike fails, credentials cannot be handled safely, or a package is incompatible with the target Mac/Python environment. Do not hide failures or weaken tests.

### Required Phase 1 report

Report:

- Files created.
- Exact package versions tested.
- SDK feed/concurrency decision evidence.
- Walking-skeleton flow evidence.
- Existing-engine reuse inventory and test evidence.
- Per-strategy mode and broker-routing evidence.
- Supervisor/shared-feed/worker-process evidence.
- Test/lint/type-check results.
- Restart-recovery evidence.
- Known limitations.
- Explicit confirmation that live order placement and live-only plumbing remain unimplemented.

## Appendix: changes from the original specification

### 1. Purpose

This appendix records how the earlier greenfield architecture is adapted for the actual requirement: live-data forward testing in paper mode, followed by controlled local live trading.

---

### 2. Keep without major change

| Area | Decision |
|---|---|
| Four logical strategy categories | Keep |
| Stable strategy instance IDs | Keep |
| Shared broker/config/logging/risk code | Keep |
| Strategy isolation | Keep as one worker process per enabled strategy |
| Independent strategy groups and worker-process isolation | Keep and align with the existing supervisor/shared-feed model |
| Paper-first defaults | Keep, but use live market data |
| Dhan and Telegram secrets outside Git | Keep |
| Option strike/expiry/basket logic as custom code | Keep |
| Read-only dashboards | Keep |
| SQLite operational separation | Keep |
| PID/locks, heartbeat and LaunchAgent support | Keep |
| Restart recovery and broker reconciliation | Keep and strengthen |
| Tests for configuration, risk and lifecycle | Keep |

---

### 3. Keep but simplify

| Original area | Simplified decision |
|---|---|
| Four dashboard applications plus master | One Streamlit multipage application |
| Broad generic execution taxonomy | Preserve three proven options engines; add a stock portfolio engine only when needed |
| Extensive `common/` hierarchy | Create modules only when there is a real first consumer |
| Separate requirements files | One `pyproject.toml` plus lock file |
| Separate database for every strategy | One SQLite database per runtime group |
| Large strategy folder template | Strategy, config, tests and README only |
| Uncoordinated strategy apps | Group supervisor owns shared services; each enabled strategy runs in an isolated worker |
| Complex monitoring platform | Heartbeats, health snapshots, logs and Telegram |
| Research hierarchy | Omit initially; add a small folder only when needed |
| Backup hierarchy | Migration and optional daily DB backups with retention |

---

### 4. Rewrite

#### Backtesting objective

Replace:

```text
backtesting platform and historical replay as a primary architecture
```

with:

```text
forward-testing platform using Dhan live data and simulated execution
```

#### Paper feed

Replace:

```text
paper market-data adapter or simulation feed
```

with:

```text
Dhan live market-data adapter used in both paper and live modes
```

#### Mode flags

Replace runtime-wide paper/live selection with:

```text
runtime: enabled + live_execution_allowed
strategy: enabled + mode(paper|live) + live_approved
```

This allows paper and live strategies to run simultaneously within one strategy group.

#### Indicator design

Replace:

```text
native production backend + package shadow backend + optional TA-Lib
```

with:

```text
one package adapter over pandas-ta-classic + fixed regression tests
```

#### Strategy onboarding

Replace mandatory historical backtest with:

```text
unit tests
+ integration tests
+ restart/recovery tests
+ forward-paper validation
+ risk acceptance
```

Historical backtesting may be added later.

#### Runtime independence

Clarify that independence means one strategy-group supervisor plus one worker process per enabled strategy, using a shared Dhan feed rather than one feed per strategy.

---

### 5. Remove from the first implementation

- `backtesting/` hierarchy.
- Historical datasets and replay engine.
- Backtesting adapters and metrics framework.
- TA-Lib.
- Native-versus-package shadow comparison at runtime.
- Research notebooks and experiment hierarchy.
- Commodity/futures/monthly future-proof modules without current consumers.
- Cloud/AWS deployment code.
- Microservices, Redis, Kafka, Celery and Docker orchestration.
- Real strategy implementations.
- Live order-placement methods.

---

### 6. Add or strengthen

| Area | Required addition |
|---|---|
| Paper execution | Bid/ask fills, latency, slippage, limit eligibility and partial-fill model |
| Authentication | Single bootstrap, TOTP, atomic token cache and secret redaction |
| Live market feed | Reconnect, resubscribe, stale-data detection and binary normalisation |
| Idempotency | Persisted correlation ID before submission |
| Unknown order state | Reconcile by correlation ID after timeout |
| Shared live-order rate limiting | Account-wide cross-process limiter in Phase 10 only |
| Static IP | Explicit live prerequisite and preflight |
| Reconciliation | Orders, trades and positions before new live entries |
| Restart recovery | Restore stop/target/trailing/rolling and cutoff state |
| Square-off | Persisted staged state machine that survives restart |
| Runtime health | Feed, broker, database, strategy and reconciliation health |
| Mac operations | Sleep, power, path, network, disk and legacy-process controls |
| Dashboard | Read-only multipage master and group views |

---

### 7. Revised top-level structure

```text
algo_trading/
├── common/
├── strategies/
├── runtimes/
├── dashboards/
├── orchestration/
├── data/
├── tests/
└── docs/
```

Do not initially create:

```text
backtesting/
large research/
historical data lake/
reports hierarchy/
cloud deployment/
```

---

### 8. Revised implementation order

```text
read-only audit of existing engines and tests
→ walking skeleton with one paper worker
→ shared Dhan authentication and feed hub
→ preserve custom engines and exit policies
→ candle/indicator and PaperBroker foundation
→ mixed-mode supervisor/risk/persistence
→ recovery and expiry handling
→ Telegram/dashboard
→ LaunchAgent
→ strategies one at a time
→ controlled live orders later
```

---

### 9. Final decision

The earlier architecture remains a useful source of broad design ideas, but it must not be implemented unchanged. This final document is the implementation source of truth for the current forward-testing programme. It supersedes Version 1.1 and the separate errata note.
