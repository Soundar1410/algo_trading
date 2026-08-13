Warning: truncated output (original token count: 140608)
Total output lines: 6844

# Implementation status and runbook

Companion to `ALGO_TRADING_FORWARD_TESTING_ARCHITECTURE_FINAL.md` (the
architecture source of truth). This file records what is actually built, the
reference-repository reuse inventory, operating commands, known limitations and
the next phase. Updated after every phase.

| | |
|---|---|
| **Current phase** | **Phase 10 — Controlled live readiness: CODE HARDENED, fully disabled.** Production parent/worker preflight wiring, Dhan order/update handling, restart-safe account-loss emergency square-off, account-wide reserve-before-submit risk plus live MTM, shared rate limiting, broker-authoritative startup/mode-transition/session-end reconciliation, strict migration history, and restore validation exist and are tested with mocks/fakes only. `ema_cross_9_21_buy` and its Rev 3.1 matrix are unchanged. **Every committed live gate remains fail-closed** (`global.live_trading_enabled: false`, `live_execution_allowed: false`, `live_approved: false`, no `mode: live` in `config/`), enforced by `scripts.assert_no_live_config_committed`. No real Dhan order/network call was made. |
| **Next phase** | Operational evidence and explicit human decisions, not more live-enabling code: complete/review the 30-day paper run, build a second genuine strategy and keep it paper, choose/configure an approved egress-IP provider and static IP, resolve the confirmation/auth operational items, then separately decide whether to approve minimum-quantity live activation. |
| **Last updated** | 13 August 2026 — end-to-end hardening added continuous account-loss exits, production transition reads, complete reconciliation identity/history, final broker-flat shutdown checks, CI/package validation and strict migration history; all committed live gates remain disabled |
| **Python** | 3.11.9 (arm64 macOS) |
| **`dhanhq` pin** | `2.2.0` — **ratified**, see [Package decisions](#4-package-decisions) |
| **Live order placement** | Code path exists but is deliberately unreachable from committed configuration. Parent and child preflight both fail closed without approved operational inputs; `OPERATIONAL LIVE ACTIVATION ELIGIBLE: NO — BLOCKED`. |

### Phase 10 end-to-end hardening addendum — 13 August 2026

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
| 7 | Operations | **Complete — all five parts.** **Part 1** (health snapshot layer — `common/health/snapshot.py`, `auth_events`/`feed_events` writers and producers, configurable heartbeat interval). **Part 2** (Telegram in production — real notifier construction at both entrypoints, deferred delivery, rate limiting/aggregation, `notifications`-table persistence, redacted rendering — D71-D74). **Part 3** (the Streamlit dashboard — Master/Intraday Options/System Health pages plus two honest stubs, reading through `common.health.snapshot` for operational state — D75; addendum adds `effective_live_gate` status to Master, config-sourced, the one deliberate exception to database-only reads). **Part 4** (PID ownership hardened onto `create_time()`, fail-first proven — D76; two previously-hidden bugs found and fixed along the way — D77/D78; seven operator scripts plus `authenticate`, `audit_events` migration `0004`, file-based square-off request channel). **Part 5** (`common/retention/` — bounded age-based DB purge in one transaction, log compression/deletion, pre-migration backup with retained-backup count, one entry point at controlled startup — D80; `ScripMasterCache.prune()` given its first caller; `Settings.algo_log_level` wiring bug found and fixed — D79) |
| 8 | LaunchAgent validation | **Complete** |
| 9 | Real strategies | **Complete** |
| 10 | Controlled live readiness | **CODE HARDENED, fully disabled.** Generic infrastructure is production-wired and fake-tested; operational activation remains blocked by paper evidence, a separately specified second real strategy, provider/static-IP, confirmation/auth and explicit-approval gates. |

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
`fetch_warmup_candles_range` was ported (see the deviation ledger…90608 tokens truncated…ompressed or deleted, since it is open
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
