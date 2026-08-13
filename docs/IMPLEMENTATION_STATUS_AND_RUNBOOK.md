Warning: truncated output (original token count: 140990)
Total output lines: 6866

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
  and every offline run is unch…120990 tokens truncated…ble. Decided with the user.
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
