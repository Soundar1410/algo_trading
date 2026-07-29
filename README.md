# algo_trading

Local forward-testing platform for individual trading strategies, running **paper
execution against real Dhan live market data** on one Mac.

Paper and live differ in exactly one place — the broker adapter. Everything above
it (market data, candles, signals, risk, persistence, health) is shared, so a
strategy that has been paper-forward-tested is the same strategy when it is later
approved for live.

> **Status: Phase 1 complete.** The walking skeleton runs end to end on a
> recorded feed: supervisor → shared feed hub → bounded IPC queue → spawned paper
> worker → deterministic fixture signal → `PaperBroker` fill → SQLite → dashboard
> tile → notification → restart recovery → square-off.
>
> There is still **no real strategy** (Phase 9), no engine port (Phase 3), and the
> live Dhan feed adapter is **written but unratified** against a live connection
> (Phase 2). See `docs/IMPLEMENTATION_STATUS_AND_RUNBOOK.md`.

> **Live order placement is not implemented and is fail-closed.** `DhanLiveBroker`
> order methods arrive in Phase 10 only, behind an explicit approval gate.

## Architecture source of truth

`docs/ALGO_TRADING_FORWARD_TESTING_ARCHITECTURE_FINAL.md` — the single
architecture document. It is not superseded by anything in this repository, and
no second architecture document is to be created.

The only other document is `docs/IMPLEMENTATION_STATUS_AND_RUNBOOK.md`, which
records what is built, the reuse inventory, commands and the next phase.

## Requirements

- macOS, Python 3.11 (3.11.9 validated)
- A Dhan account, for anything beyond the test suite

## Setup

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"     # or: -r requirements.lock

cp .env.example .env        # then fill in locally — .env is gitignored
```

`.env` is the only place secrets live. They are never written to YAML, never
persisted to SQLite, never shown in the dashboard, and are redacted from every
log record by `common.logging.SecretRedactingFilter`.

## Checks

```bash
.venv/bin/python -m pytest
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy
```

Tests use fake credentials and synthetic data. No test places an order, and none
requires network access or a populated `.env`.

## Layout

```
common/          config, logging, persistence, models, market_data, candles,
                 feed, broker, execution, risk, notifications, health, process
strategies/      intraday_options/  (test-only fixture; real strategies: Phase 9)
runtimes/        intraday_options/  supervisor + spawn-safe worker
dashboards/      app.py — one read-only tile
config/          global.yaml, runtimes/, strategies/
data/operational/  one SQLite database per runtime group (gitignored)
docs/            architecture spec + implementation runbook
tests/           unit/, integration/, end_to_end/, smoke/, fixtures/
```

The Dhan SDK is imported by exactly one module, `common/market_data/dhan.py`, and
a test enforces that. Strategy, execution and persistence code never touches it.

## Execution mode

Mode belongs to the **individual strategy**, not to a global flag:

```yaml
# config/strategies/<strategy_id>.yaml
enabled: true
mode: paper          # paper | live
live_approved: false
engine: trading_engine
```

Global and runtime flags are *permissions*. They can block live execution; they
can never turn a live strategy into a paper one. A blocked live strategy refuses
to start — it is never silently rerouted to paper.
