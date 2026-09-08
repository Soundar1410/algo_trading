# Build Spec — `supertrend_buy_1_0p5`

**NIFTY 5-minute SuperTrend(1, 0.5) · ATM weekly options · BUY-only · intraday**
**An exact replica of `supertrend_buy_1_1p2`, changing only the SuperTrend multiplier (1.2 → 0.5) and identity.**

> **How to use this document (Claude Code):** This is a build order, not just a
> behaviour spec. The authoritative behaviour is the existing, reviewed strategy
> `supertrend_buy_1_1p2` and its spec at
> `strategies/intraday_options/supertrend_buy_1_1p2/SUPERTREND_BUY_1_1P2_ALGO_TRADING_SPEC.md`.
> Your job is to produce `supertrend_buy_1_0p5` as a faithful clone of that
> strategy with **only** the differences listed in §2, plus the wiring in §5.
> Work in Plan Mode; show the plan before writing any file. Do not invent new
> behaviour, do not "improve" the strategy, do not touch `supertrend_buy_1_1p2`'s
> logic. Read the 1p2 spec and `strategy.py` in full before starting.

---

## 0. Pre-flight — the blocking issue, read this first

### 0.1 The obvious strategy_id would collide on the correlation token — **BLOCKING**

`common/execution/correlation.py` derives each strategy's order-correlation token
from the **first 4 alphanumeric characters** of its `strategy_id`, and the
supervisor **refuses to admit** two strategies whose tokens collide (they would
build identical correlation IDs and the database rejects the duplicate).

The naive id fails:

```
supertrend_buy_1_1p2  ->  "supe"
supertrend_buy_1_0p5  ->  "supe"     <-- COLLISION, both refused
```

This is the exact failure that already blocked the `ema_cross_*` family and
forced the `c921_ema_cross_buy` / `c509_ema_cross_buy` / `c521_ema_cross_buy`
rename. **Do not repeat it.**

**Constraint:** `STRATEGY_TOKEN_LENGTH` is 4 and **cannot be raised enough to
help** — Dhan's correlation-ID limit is 25 characters and the format
(`p_io_<token>_YYYYMMDD_NNNN`) leaves a hard ceiling of 6, at which
`supert` still collides with `supert`. **Do not change
`common/execution/correlation.py`.** It is shared by every running strategy.

**Required approach — same pattern as the ema family:** put the distinguishing
part FIRST so the ids diverge inside 4 characters. Follow the existing
`c921_` / `c509_` / `c521_` convention:

```
supertrend_buy_1_1p2  ->  st12_supertrend_buy   ->  "st12"   (rename, see 0.2)
new strategy          ->  st05_supertrend_buy   ->  "st05"   distinct
```

Verify by actually calling `strategy_token()` on every candidate at
`STRATEGY_TOKEN_LENGTH=4` and asserting pairwise distinctness against **all**
currently-configured strategy ids (`c921_ema_cross_buy`, `c509_ema_cross_buy`,
`c521_ema_cross_buy`, `straddle_920`, `weekly_delta_neutral`, and the
supertrend pair). Report the resolved tokens before writing files.

### 0.2 Decision required from the operator — **ASK, DO NOT GUESS**

The collision means one of two paths. **Present both and stop for a decision:**

- **Path A — rename the existing strategy too.** Rename
  `supertrend_buy_1_1p2` → `st12_supertrend_buy` and create the new one as
  `st05_supertrend_buy`. Consistent, matches the ema family's convention, both
  can run together. **But** `supertrend_buy_1_1p2` is `enabled: true` and
  currently running in paper — this is a live-cutover rename touching 11 test
  files, the spec doc, the runbook, and CLAUDE.md. Same continuity questions as
  the ema rename: check whether anything queries by `strategy_id` (dashboard,
  persisted `strategy_state`, historical order lookups) and report findings
  before proceeding.
- **Path B — new strategy only, existing one untouched.** Name the new one so
  it diverges within 4 chars of `supe` (e.g. `st05_supertrend_buy`), leave
  `supertrend_buy_1_1p2` exactly as it is. Smaller blast radius, nothing running
  is disturbed. **Cost:** the two supertrend strategies are named
  inconsistently, and the `supe`/`st05` pair does not collide so both CAN run
  together — verify this explicitly rather than assuming.

Path B is the lower-risk default if the operator has no preference, but **the
operator decides, not you.**

### 0.3 Other pre-flight constraints

1. **Paper only.** `mode: paper`, `live_approved: false`. Live placement is
   fail-closed and out of scope. Do not touch any live gate.
2. **`enabled:` is an operator decision — see §6.1.** Do not copy 1p2's
   `enabled: true` blindly.
3. **Risk caps are per-strategy.** Enabling this adds another independent
   ₹30,000 daily-MTM cap; there is no combined cap. Do not build one.
4. **Follow repo discipline.** `ruff check`, `mypy` (strict), full `pytest`
   green. Every git command passes `-C /Volumes/Trading/algo_trading`.

---

## 1. Objective

Create a NIFTY 5-minute **SuperTrend(1, 0.5)** strategy that buys ATM weekly
options on trend flips, BUY-only, one position at a time, reversing on the
opposite flip, with the same premium exit, the same 3% daily live-MTM cap, the
same 09:15/15:15/15:20 timing, the same warm-up regime and the same trading
calendar as `supertrend_buy_1_1p2`. Everything is identical **except** the
SuperTrend multiplier and the strategy's identity.

---

## 2. The complete delta vs `supertrend_buy_1_1p2`

Exhaustive. Anything not listed is copied **verbatim** (allowing for the rename).

| Aspect | `supertrend_buy_1_1p2` | new strategy |
|---|---|---|
| SuperTrend period | 1 | **1 (UNCHANGED)** |
| SuperTrend multiplier | 1.2 | **0.5** |
| `strategy_kwargs.supertrend_period` | 1 | **1 (UNCHANGED)** |
| `strategy_kwargs.supertrend_multiplier` | 1.2 | **0.5** |
| Strategy id | `supertrend_buy_1_1p2` | **per §0.2 decision** (e.g. `st05_supertrend_buy`) |
| Class name | `SupertrendBuy1x1p2Strategy` | `SupertrendBuy1x0p5Strategy` |
| Registry decorator | `@register_strategy("supertrend_buy_1_1p2")` | matching new id |
| Strategy folder | `strategies/intraday_options/supertrend_buy_1_1p2/` | matching new id |
| Config file | `config/strategies/intraday_options/supertrend_buy_1_1p2.yaml` | matching new id |
| `parameters.strategy_ref` | `...supertrend_buy_1_1p2.strategy:SupertrendBuy1x1p2Strategy` | matching new module path + class |
| Spec doc | `SUPERTREND_BUY_1_1P2_ALGO_TRADING_SPEC.md` | `SUPERTREND_BUY_1_0P5_ALGO_TRADING_SPEC.md` |
| Test files (11) | `*_supertrend_buy_1_1p2_*` | matching new id |

**Everything else is identical — do not change any of these:**
`lots_per_trade: 10`; `trail_percentage: 8.0`;
`activation_minimum_favourable_move_percentage: 4.0`; `warmup_min_bars: 75`;
`risk_manager_name: hard_stop` with `catastrophic_stop_rupees_per_lot: none`;
timing `entry_start: "09:15"`, `entry_cutoff: "15:15"`, `square_off_at: "15:20"`;
`capital_base: 1000000`, `daily_max_loss_pct: 3.0`; `contract_resolver: dhan`,
`strike_step: 50`, **no `lot_size:` key** (resolved from the scrip master —
preserve this deliberate omission and the comment explaining it); `expiry: null`;
`security_id: "13"`; `timeframe: "5m"`; `engine: trading_engine`;
`style: buying`; `regime_enabled: false`; warm-up
`warmup_from_history: true`, `warmup_source: dhan`,
`warmup_max_lookback_sessions: 3`; the **entire `holidays:` list verbatim**
(see §3.3); the full `paper_execution` block.

---

## 3. Behavioural consequences of the multiplier change

### 3.1 Only the band width changes — no structural change

SuperTrend's multiplier scales the ATR band around the median price. Lowering
1.2 → 0.5 makes the bands **much tighter**, so the trend latch flips **more
often**. Nothing else about the algorithm changes: period stays 1, the
continuity requirement stays, the latch semantics stay.

### 3.2 Warm-up: `warmup_min_bars: 75` is UNCHANGED — do not recompute it

The 75-bucket floor is **not** derived from the multiplier. Per the 1p2 config's
own comment it is an operator-approved conservative trust floor for a latched,
path-dependent trend — explicitly *not* the indicator's own `min_bars = period
= 1`. The multiplier does not enter that reasoning. Keep 75, keep
`warmup_max_lookback_sessions: 3`, and keep the comment explaining the
ceil(75/73) = 2-prior-sessions arithmetic verbatim.

### 3.3 The `holidays:` list is strategy-scoped and must be copied verbatim

The config carries a full NSE 2026 calendar with an explicit ownership note: it
is **strategy-scoped**, duplicated deliberately from `config/global.yaml` so the
two stay line-for-line comparable, and it must be maintained annually. Copy all
20 entries and the entire ownership/maintenance comment block unchanged. An
undeclared holiday corrupts the warm-up walk-back and downgrades a good replay
to PARTIAL, blocking entries for the day.

### 3.4 Expected trading-frequency change — flag, do not "fix"

A 0.5 multiplier on a period-1 SuperTrend will produce **substantially more
flips**, therefore more entries, more reversals, and more round trips per day
than 1.2. This is the intended point of the variant. **Do not** add any
cooldown, confirmation, or de-bounce logic to compensate — that would make it
not-a-clone. If the increased trade rate matters operationally, that is an
operator tuning decision made later against paper results, not part of this
build.

> **Note for whoever hand-builds test candles:** SuperTrend(1, 0.5) flips on
> different (and more) candles than SuperTrend(1, 1.2). **Re-derive** every
> candle sequence in the cloned tests against the actual SuperTrend(1, 0.5)
> math. Do not copy 1p2's numerics and assume the same flips occur. Assert
> against computed values, never from memory.

---

## 4. Files to CREATE

Mirror the 1p2 package exactly (3 files) plus the config, plus 11 test files.

### 4.1 `strategies/intraday_options/<new_id>/__init__.py`

Clone 1p2's. Preserve any "deliberately not imported by the parent package"
docstring and its import-boundary reasoning verbatim
(`tests/unit/test_worker_import_boundary.py` enforces this). Update only the
identity references.

### 4.2 `strategies/intraday_options/<new_id>/strategy.py`

Clone 1p2's `strategy.py`. Apply the identity rename
(`SupertrendBuy1x0p5Strategy`, new `@register_strategy(...)`, new `name`, and a
`display_name` following 1p2's own pattern, e.g. `"SuperTrend (1, 0.5) Buy"`),
and change the multiplier default in the constructor from `1.2` → **`0.5`**.
Leave the period default at **1**. Every other line — the SuperTrend
construction, the exit wiring, `warmup_spec`, `reset`, `on_warmup_complete`,
`on_candle`, the option-candle hooks, snapshot/restore, and all properties —
byte-for-byte identical except docstring references to the multiplier/name.

### 4.3 `strategies/intraday_options/<new_id>/SUPERTREND_BUY_1_0P5_ALGO_TRADING_SPEC.md`

Clone the 1p2 spec and adapt: replace id/class/multiplier throughout; **keep**
the 75-bucket warm-up reasoning (§3.2 above) and the parity/provenance notes
about the legacy `supertrend_fast` source, adjusted to state that this variant
is a multiplier clone of the in-repo 1p2 strategy. Do not change any behavioural
section's meaning.

### 4.4 `config/strategies/intraday_options/<new_id>.yaml`

Clone 1p2's YAML. The **filename is the identity** (the loader enforces
`strategy_id == filename stem`). Set: `strategy_id`, `strategy_ref`,
`supertrend_multiplier: 0.5`, and `enabled` per §6.1. Update the header comment
block to describe this variant (and drop/adjust the "parity source" paragraph so
it accurately describes cloning from the in-repo 1p2 rather than re-deriving
from the legacy tree). **Keep every warning comment**: the PAPER-ONLY /
live-gate paragraph, the no-`lot_size`-key rationale including the documented
single-leg-vs-multi-leg adapter asymmetry, the expiry-resolution note, the
daily-cap explanation, and the full holidays ownership block.

### 4.5–4.6 Tests — clone all 11

`supertrend_buy_1_1p2` has a far larger test surface than the ema clones. Clone
**every** one, renamed, with candle sequences re-derived for multiplier 0.5:

- `tests/unit/test_<new_id>_strategy.py`
- `tests/unit/test_<new_id>_config.py`
- `tests/unit/test_<new_id>_warmup.py`
- `tests/unit/test_no_<new_id>_branches.py`
- `tests/integration/_<new_id>_fixtures.py`
- `tests/integration/test_<new_id>_engine.py`
- `tests/integration/test_<new_id>_recovery.py`
- `tests/integration/test_<new_id>_risk_and_gaps.py`
- `tests/integration/test_<new_id>_dashboard.py`
- `tests/integration/test_<new_id>_warmup_handoff.py`
- `tests/integration/test_<new_id>_supervisor_composition.py`

Read each before cloning — several encode 1p2-specific expectations (the config
test pins `contract_resolver: dhan`; the warm-up tests encode the 75-bucket
arithmetic; the supervisor-composition test may assert the set of admitted
strategies and could need updating for a second supertrend strategy). Report any
that need more than a mechanical rename.

**Add one new test** (wherever the config tests live): assert
`strategy_token()` is pairwise distinct at `STRATEGY_TOKEN_LENGTH=4` across all
configured strategy ids, written so it also catches a **future** similarly
prefixed strategy colliding — not just re-proving today's set.

---

## 5. Files to EDIT

Discovery and wiring are automatic — **do not** hunt for a central strategy
list, dashboard code, or a per-strategy launchd plist; none exist:

- `discover_enabled_strategies` rglobs `config/strategies/**/*.yaml`; the new
  file is found automatically once placed in `intraday_options/`.
- The dashboard renders per discovered strategy dynamically.
- The launchd plist runs the runtime, not a strategy.
- Registration is the `@register_strategy` decorator + dotted `strategy_ref`.

Edits actually needed:

1. `docs/IMPLEMENTATION_STATUS_AND_RUNBOOK.md` — add the new strategy (dated
   addendum, per the runbook's own discipline). If Path A was chosen, also
   update every `supertrend_buy_1_1p2` reference to the new id.
2. `CLAUDE.md` — the strategy list needs the new entry (and the rename, under
   Path A). **Flag the exact edit to the operator rather than rewriting the
   project-rules file unilaterally.**
3. Under **Path A only**: every reference to `supertrend_buy_1_1p2` across the
   11 test files, the spec doc, its folder/config/class names, and anywhere else
   grep finds it.

If any test reads the **real** repo config and asserts an exact set of enabled
strategy ids, update that expectation (only relevant if the new strategy ships
`enabled: true`).

---

## 6. Decisions to confirm before implementation

### 6.1 `enabled: true` or `false`? (blocking)

`supertrend_buy_1_1p2` currently runs `enabled: true`. Enabling this one adds a
second supertrend strategy on the shared intraday feed with its own ₹10,00,000
base and its own ₹30,000 daily cap. Note the **operational context**: the
intraday runtime recently suffered a SQLite `database is locked` crash under
concurrent load from multiple strategies writing to `intraday_options.db`, and
there is currently no worker auto-restart. Adding another concurrently-writing
strategy increases that contention.

- **`enabled: false`** — built, tested, discoverable, dormant until deliberately
  enabled. Safer default given the open DB-contention issue.
- **`enabled: true`** — runs on the next supervised session alongside 1p2.

**Confirm with the operator; do not guess.**

### 6.2 Naming path (blocking) — see §0.2

Path A (rename both) vs Path B (new only). **Operator decides.**

---

## 7. Definition of done

- [ ] Correlation tokens verified pairwise distinct at
      `STRATEGY_TOKEN_LENGTH=4` across all configured strategy ids, reported
      explicitly, with a regression test that also catches future collisions.
- [ ] `common/execution/correlation.py` **not modified**.
- [ ] Strategy package (3 files) created, mirroring 1p2 with only §2 deltas.
- [ ] Class registered under the new id; `supertrend_period: 1`,
      `supertrend_multiplier: 0.5`; `warmup_min_bars: 75` unchanged.
- [ ] Config created, `strategy_id` matches filename stem, lives under
      `config/strategies/intraday_options/`, all warning comments and the full
      holidays block preserved, `enabled` per §6.1.
- [ ] All 11 test files cloned, renamed, and candle sequences **re-derived** for
      multiplier 0.5 (not copied numerics).
- [ ] Runbook updated; CLAUDE.md edit flagged to the operator.
- [ ] `discover_enabled_strategies` picks it up; dashboard renders it; no change
      to discovery or dashboard code.
- [ ] `ruff check` clean, `mypy` (strict) clean, full `pytest` green — noting
      any pre-existing unrelated failures separately rather than fixing them.
- [ ] `scripts/assert_no_live_config_committed.py` clean.
- [ ] `supertrend_buy_1_1p2` behaviour unchanged (Path B: untouched entirely;
      Path A: rename only, zero behavioural diff).
- [ ] Only the planned files changed; commits use
      `git -C /Volumes/Trading/algo_trading ...`.

---

## 8. Explicitly out of scope

- Any change to `common/execution/correlation.py` or `STRATEGY_TOKEN_LENGTH`.
- Any behavioural change to `supertrend_buy_1_1p2` (Path A permits rename only).
- Live order placement / Phase 10 anything.
- Any cooldown/de-bounce to compensate for the higher flip rate (§3.4).
- A shared or combined risk cap across strategies.
- The SQLite `database is locked` contention and the missing worker
  auto-restart — a known, separate piece of work. Do not fix it here.
- Any pre-existing, date-dependent test failures (stale scrip-master fixture
  dates) in unrelated suites.
- Any new common-layer capability. A multiplier clone needs nothing new; if you
  find yourself adding shared infrastructure, stop and report.
