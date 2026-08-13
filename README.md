# algo_trading

Local forward-testing platform for individual trading strategies, running **paper
execution against real Dhan live market data** on one Mac.

Paper and live differ in exactly one place — the broker adapter. Everything above
it (market data, candles, signals, risk, persistence, health) is shared, so a
strategy that has been paper-forward-tested is the same strategy when it is later
approved for live.

> **Status: Phase 10 controlled-live code hardened; operational activation
> blocked.** The shared paper/live architecture, the real
> `ema_cross_9_21_buy` paper strategy, and generic controlled-live machinery are
> implemented, including continuous account-loss emergency control,
> broker-authoritative startup/mode-transition/session-end reconciliation, and
> account-wide risk/rate coordination. Every committed live gate remains disabled and tests use
> mocks/fakes only—no real order was placed.
>
> The `dhanhq` pin is now **ratified at `2.2.0`** (2.1.0 is yanked upstream and
> cannot resubscribe on `websockets>=14`), and the feed payload shape is ratified
> from SDK source — which corrected three real defects, including an `LTT` bug
> that silently bucketed every candle by arrival time instead of exchange time.
>
> Operational activation still requires the 30-day paper evaluation, a second
> genuine strategy remaining in paper, strategy-specific minimum-quantity
> approval, approved static-IP/provider setup, confirmation/auth readiness, and
> a separate human decision to enable the gates. See
> `docs/IMPLEMENTATION_STATUS_AND_RUNBOOK.md`.

> **Live order placement code exists but is fail-closed.** `DhanLiveBroker` and
> its preflight/risk/reconciliation/update path cannot be reached from committed
> configuration.

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
requires network access or a populated `.env` — verified by running the suite with
every credential unset and IP socket creation blocked.

## Authentication

```bash
.venv/bin/python -m scripts.auth_bootstrap --status   # local state, no network
.venv/bin/python -m scripts.auth_bootstrap            # pre-market bootstrap
```

The bootstrap derives a TOTP locally with `pyotp`, exchanges client id + PIN +
TOTP for a 24-hour access token, and caches it atomically at
`data/cache/token_cache.json` (mode `0600`). Every runtime reads that cache; none
generates its own token.

A rejected PIN or TOTP costs **exactly one** request to Dhan and then records a
cooldown, so neither a re-run nor eight simultaneous workers can turn one bad
credential into repeated rejected logins. Re-run with `--force` after fixing
`.env`.

## Layout

```
common/          config, logging, persistence, models, market_data, candles,
                 feed, broker, execution, risk, notifications, health, process,
                 authentication
strategies/      intraday_options/  (test-only fixture; real strategies: Phase 9)
runtimes/        intraday_options/  supervisor + spawn-safe worker
dashboards/      app.py — one read-only tile
scripts/         auth_bootstrap.py, capture_live_tape.py (read-only)
config/          global.yaml, runtimes/, strategies/
data/operational/  one SQLite database per runtime group (gitignored)
data/cache/      token_cache.json — mode 0600 (gitignored)
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
