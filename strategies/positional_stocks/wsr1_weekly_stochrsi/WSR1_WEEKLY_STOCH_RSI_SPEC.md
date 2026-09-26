# WSR1 Weekly Stoch RSI — `algo_trading` Implementation Specification

**Strategy ID:** `wsr1_weekly_stochrsi` (correlation token `wsr1` — unique against every committed id)
**Runtime ID:** `positional_stocks` (new)
**Engine kind:** `stock_portfolio_engine` (the existing, reserved `EngineKind.STOCK_PORTFOLIO_ENGINE`; no new enum value)
**Execution shape:** a run-to-completion weekly job — no tick feed, no long-lived worker, no intraday decisions
**Initial mode:** paper only
**Status:** implementation specification v1.2l — corporate-action checks after fills; unit factor per fill; stuck-freeze exit
**Scheduling:** two of its own LaunchAgents (a fetch/preview job and an offline decision job). **Not** registered with `auto_start`, and no change to shared auto-start code
**Rule source:** "Weekly Stoch RSI V1 Trading Plan" (operator's V1 rulebook, 20 Sep 2026), including its resolved ambiguities and the first-cross / K < 50 fix
**Target branch:** new `strategy-wsr1-weekly-stochrsi`, cut from `feature-paper-auto-start` at `701e030`

### Changes in v1.1 (20 Sep 2026, after the first, spec-less Phase 0 pass)

| # | Change | Reason |
|---|---|---|
| 1 | Engine kind `weekly_batch` dropped; use the reserved `stock_portfolio_engine` | `EngineKind` separates candle/position/risk models, not cadence (`common/config/models.py`); the reserved slot already exists for this instrument class |
| 2 | Execution shape stated explicitly: run-to-completion job, no tick feed | Decisions use weekly closes only; paper fills use the official daily open (section 8). A long-lived, tick-driven worker adds failure surface with no decision value |
| 3 | Launch mechanism reopened — **settled in v1.2, section 10.3** | `orchestration.auto_start` fires on trading days only and has no per-runtime schedule; a Saturday job needs either its own LaunchAgent or a different decision time |
| 4 | All new tables prefixed `stock_` (section 9) | `positions`, `fills` and `signals` already exist from the 0001-era migrations, and the shared `MigrationRunner` applies every migration to every runtime database, so `CREATE TABLE IF NOT EXISTS positions` would silently keep the options-shaped table |
| 5 | Persistence decided: new `positional_stocks.db`, new tables (own migration set since v1.2i), options cycle tables untouched | `strategy_cycles` cannot hold several open equity positions per strategy (`idx_one_open_cycle`), and its expiry key has no equity meaning |
| 6 | Runtime YAML and strategy YAML must land in the same commit (section 13) | `common/config/loader.py` `resolve_runtime_strategies` raises for a strategy whose runtime file is missing — it would break the two existing runtimes at startup |
| 7 | Indicators implemented from the section 4.4 formulas; any library used only as a test cross-check (section 7) | `pandas_ta_classic`'s RMA seeds one bar earlier than TradingView's `ta.rma`; harmless after warm-up, but the formulas, not a library, are the contract |

### Changes in v1.2 (20 Sep 2026 — Phase 0 answers, operator decisions)

Numbers in brackets are the Phase 0 question numbers.

| # | Decision | Reason |
|---|---|---|
| 1 | **Launch: neither option B nor C. The job runs under two of its own LaunchAgents — a fetch/preview job and an offline decision job (section 10.3).** `positional_stocks` gets **no** `scripts/_runtimes.py::RUNTIMES` entry and `auto_start` is not touched [2, 7] | `auto_start`'s launcher checks process exit before the supervisor lock (`runtime_launcher.py:209-216`), so a fast, idempotent no-op run is reported as a failed launch; and 201 fetches at 09:00 would share Dhan's 5 req/s Data-API budget with the five `warmup_source: dhan` strategies, the profile of the 2026-07-17 truncated-warm-up incident. Staying outside `auto_start` avoids both, and keeps shared code that the Phase 10 live path also runs on untouched |
| 2 | The `auto_start` handshake defect is **recorded, not fixed** here [7] | Nothing in this feature depends on it. The first runtime that does need `auto_start` to launch a completing job takes the `completes_and_exits` fix, with its own approval |
| 3 | `schedule:` moves under `parameters:` in section 12 [1] | Top-level extra keys are rejected by `_StrictModel`; `weekly_delta_neutral.yaml` already nests its own schedule |
| 4 | Reports move to `data/reports/positional_stocks/` [5] | `data/operational/` is database-only and is what `common/retention/` backs up and purges |
| 5 | Per-run deadlines and a fetch throttle (sections 6.1, 10.1) [3, 4] | `DhanHistoricalDataClient` has per-call timeouts but no overall deadline; 201 symbols × worst-case retries ≈ 2.7 h |
| 6 | Weekly bars stay out of `common/warmup/` and `common/candles/` [6] | `parse_timeframe_minutes` rejects `1W`/`1d`; the whole warm-up stack is intraday-session-bucketed |
| 7 | Sector source NSE Industry [8]; results calendar optional in paper [11]; dashboard deferred [12]; paper book 10 positions / 0% buffer, with 8 / 20% for any live version [13] | As recommended in the Phase 0 report |
| 8 | Paper costs stay at 12 / 11 bps + ₹15 until the operator verifies them; they affect reported P&L only, never a decision [9] | Phases 1–3 are not blocked by them |
| 9 | **Operator action, blocking Phase 1:** confirm the Dhan Data API subscription is active for `/v2/charts/historical` [10] | Phase 1 cannot fetch without it |
| 10 | Two defects found in shared code are **recorded as findings only, not fixed**: `token_cache.py`'s `expiry_time` is stored without a timezone marker while `created_at` has one; `runtimes/positional_options/__init__.py`'s docstring still says "placeholder package" | Both are outside this feature; changing shared auth code needs its own approval |

### Changes in v1.2b (21 Sep 2026 — Phase 1 review)

| # | Change | Reason |
|---|---|---|
| 1 | **Staleness reference comes from the verified holiday calendar, not from NIFTY's data (section 6.2)** | 21 Sep 2026 was a trading day (NSE's September list has only 14 Sep as a holiday), yet Dhan had not published its daily candle by 22:20 IST. With a data-derived reference, a Friday candle missing at fetch time makes Thursday the "last session", every series passes, and a Monday–Thursday bar is used as the week. Reproduced against the Phase 1 modules |
| 2 | Fetch times in 10.3 are **provisional** until a publication-lag probe establishes when Dhan publishes a session's daily candle, including a Friday's on a weekend | The schedule assumed same-evening publication; that assumption is now known to be unsafe |
| 3 | Gap acknowledgements keyed per (symbol, session, ratio); a monthly full refetch (section 6.1) | MOTHERSON showed Dhan's back-adjustment can stop partway: its 1:2 bonus (ex-date 18 Jul 2025, per raw NSE data) was applied back only to 30 Apr 2024, plus one stray adjusted bar on 16 Aug 2023. The 10-session overlap check cannot see older history change |

### Changes in v1.2c (23 Sep 2026 — TradingView parity readings)

| # | Change | Reason |
|---|---|---|
| 1 | **EMA is seeded with the SMA of the first n closes** (section 4.4), not the first value | ETERNAL's EMA200 on TradingView is 209.70; SMA seed gives 209.70, first-value seed 223.24. It matters for recently listed stocks, where EMA200 gates adds |
| 2 | Six parity readings recorded in section 7, with the operator's TradingView settings | Phase 2's acceptance data |
| 3 | ISO (Monday–Sunday) weekly grouping confirmed as TradingView's, including weekend special sessions (section 7) | Sat–Fri grouping moves RELIANCE's K by 1.1 because of the Sunday 1 Feb 2026 Budget session |

### Changes in v1.2d (24 Sep 2026 — Phase 2 pre-check)

| # | Change | Reason |
|---|---|---|
| 1 | **Fetch full available history, not 260 weeks (section 6.1)** | The Phase 2 pre-check found ETERNAL's EMA200 at 206.60 vs TradingView's 209.70: the cache held 260 weeks, so the SMA seed averaged a different window. Reproduced independently: last-260-bars 206.60, full history 209.70. The same window moves EMA200 by 1.5–2.5% for long-listed stocks (RELIANCE, INFY, LT), which would change add decisions |

### Changes in v1.2e (24 Sep 2026 — Phase 2 review)

| # | Change | Reason |
|---|---|---|
| 1 | **Gap blocking limited to the most recent 520 weekly bars (section 6.1)** | The full-history gap scan flagged 262 moves in 43 symbols; all but MOTHERSON's are more than five years old. Old breaks barely move any indicator; blocking on them would shut out RELIANCE and HDFCBANK |
| 2 | The V1 plan's golden-case text committed as `WSR1_V1_GOLDEN_CASES.md` (section 14) | Phase 3's tests are written from the plan's own tables, which were not in the repository |
| 3 | Section 6.1's 260-bar comparison labelled as independent data, not parity | Dhan's full history gives RELIANCE's EMA200 as 1295.07; the review's data gives 1313.91. Only a TradingView reading can settle it |
| 4 | 6-month performance counts 26 ISO weeks, not 26 bars (section 4.4) | A stock with a missing week (a suspension) would otherwise compare a longer window than NIFTY's, and RS would mix two different periods |

### Changes in v1.2f (24 Sep 2026 — Phase 3 plan answers)

| # | Decision | Section |
|---|---|---|
| 1 | Repeat signal compares the **most recent** earlier trigger (the V1 plan's wording) | 4.6 item 8 |
| 2 | Brake 1: 4-decision-week pause, then entries resume; re-fires only after equity recovers above 90% of peak | 4.12 |
| 3 | Brake 2: clearing resets the peak to equity at the clearance date | 4.12 |
| 4 | Undefined NIFTY EMA40 → regime Unknown → no entries; undefined K/D in the arm or cross window → no trigger | 4.3, 4.5 |
| 5 | Decided exits free slots, sector, group and committed amount for the same week's entries; sale proceeds not counted as cash until filled | 4.13 |
| 6 | Re-entry after a profit needs a fresh arm **and** trigger after the exit fill week; P&L is net of costs | 4.11 |
| 7 | Weeks counted in ISO weeks: time exit decided at F + 52, fills F + 53; cooling-off entries from X + 26 | 4.10, 4.11 |
| 8 | PASS → EVENT_RISK on a held position disables T3; expired FAIL still exits; governance event on a held symbol is recorded as FAIL (operator procedure) | 4.2 |
| 9 | A removed symbol's `on_exit` comes from its last-seen row, persisted by the runtime | 6.3 |
| 10 | "Close ≥ P1 clears the touch" (4.9 item 4) is a deliberate spec rule the V1 plan did not state; the spec's behaviour stands | 4.9 |

### Changes in v1.2g (24 Sep 2026 — Phase 3 review)

| # | Decision | Section |
|---|---|---|
| 1 | **Unfilled BUY orders hold their slot, sector, group, committed amount and cash** until filled or cancelled | 4.12 |
| 2 | EVENT_RISK after T3 has filled keeps the full A committed; T3 is disabled only while unfilled | 4.12 |
| 3 | Undefined K/D blocks a trigger only in bars the check reads (the 8-close window plus the bar before j0) | 4.5 |
| 4 | Repeat-signal lookback is the 25 bars before this one; a trigger exactly 26 bars back is outside | 4.6 item 8 |
| 5 | Clearing brake 2 also ends a running brake-1 pause; brake 1's firing week is the first of its 4 | 4.12 |
| 6 | A 1-share position's partial sale sells 0 and still switches the position to the trail | 4.10 |
| 7 | A buy that filled after its week's first session starts the touch window the following week | 4.9 |
| 8 | Accepted as implemented: clearing brake 2 resets the peak at the first weekly run on or after the date; cost of remaining shares = average cost × shares; undefined RS ranks last | 4.7, 4.12 |

### Changes in v1.2h (24 Sep 2026 — Phase 3b review)

| # | Decision | Section |
|---|---|---|
| 1 | An unfilled SELL keeps its position counted as held; a transient one-over-limit book after an unfilled exit is accepted and reported | 4.12 |
| 2 | The rules reject an inconsistent book (a filled order passed as pending); the runtime must clear filled orders before deciding | 4.12, 4.13 |
| 3 | Phase 4 split into **4a** (persistence and paper accounting) and **4b** (the weekly run), each with its own review stop; the Phase 4 items accumulated in Phases 1–3 are assigned to one or the other | 15 |

### Changes in v1.2i (24 Sep 2026 — Phase 4a plan review)

| # | Decision | Section |
|---|---|---|
| 1 | **`positional_stocks.db` uses its own migration directory**; nothing is added to the shared `common/persistence/migrations/versions/` | 9 |
| 2 | Reason: the paper runtimes run from this working tree and migrate at every start; `verify_checksums()` would refuse to start both of them if a shared stock migration were later edited or missing | 9 |
| 3 | Phases 4a and 4b add new files only: no existing module that the two paper runtimes import is modified | 13 |

### Changes in v1.2j (26 Sep 2026 — Phase 4a audit)

| # | Decision | Section |
|---|---|---|
| 1 | **Corporate actions on held positions: detect, freeze, operator confirms in `corporate_actions.csv`, then rescale** (bonus/split and demerger) | 4.14 (new) |
| 2 | A decision run for week W requires W − 1 COMPLETED (or no prior run) | 10.2 |
| 3 | Late-arriving candles: `late_fill` flag and touch-memory replay from the fill week | 8 |
| 4 | Gap thresholds compared on exact decimal ratios, not floats (a move of exactly 30% was missed) | 6.1 |
| 5 | Section 9's `stock_fills` late/catch-up flag and `stock_equity` drawdown % columns are required | 9 |

### Changes in v1.2k (26 Sep 2026 — Phase 4a-fix audit)

| # | Decision | Section |
|---|---|---|
| 1 | **An unadjusted bonus/split on a held stock freezes it**: an unacknowledged gap of 15% or more (either direction) after its first fill session; resolved by a gap acknowledgement (real move) or by Dhan's later back-adjustment plus a CSV row | 4.14 item 7 |
| 2 | BONUS_SPLIT accepts ratios below 1 (consolidations); a frozen position with f > 1 can now be resolved | 4.14 item 3 |
| 3 | New kind PRICE_CORRECTION: price-only restatement with no cash credit (Dhan data corrections) | 4.14 item 3 |
| 4 | A re-issued sell fills at its original execution session when cached (late_fill), not 1–2 weeks later | 4.14 item 6 |

### Changes in v1.2l (26 Sep 2026 — Phase 4a-fix2 audit)

| # | Decision | Section |
|---|---|---|
| 1 | **The item 1 and item 7 checks run again after the run's fills, before any decision**: a position opened this run, with a bonus going ex later that week, no longer takes a false stop | 4.14 items 1, 7 |
| 2 | A re-issued SELL_HALF that rounds to 0 shares follows the 1-share partial rule instead of crashing the run | 4.14 item 6 |
| 3 | **Unit factor per buy fill**: the same unit break (a gap where Dhan's back-adjustment stops, plus the restated fills after it) is never counted twice; mixed units are escalated only | 4.14 item 7 |
| 4 | A consolidation that floors a holding to 0 shares closes the position on cash in lieu (D101) | 4.14 item 4 |
| 5 | **Stuck-freeze exit (D102, revised)**: the row must be eligible by its ex date; a factor that includes a gap matches within 10%; the exit fills at open ÷ the row's factor; it is skipped if an acknowledgement lifts the freeze first | 4.14 item 8 |
| 6 | Operator rule: never acknowledge a gap you know is a bonus, split or consolidation | 4.14 item 7, 16 |

---

## 1. Purpose and authority

Paper-trade the operator's V1 weekly Stochastic RSI positional equity strategy on NIFTY 200 stocks, with every weekly decision made by deterministic code instead of by hand, so that months of forward-test evidence exist before any live decision.

This document is authoritative for the implementation. Where it is silent, follow the established generic patterns of the current branch rather than inventing a strategy-specific subsystem. Where following an existing pattern would force options-specific semantics onto equities (expiries, lots, legs, Greeks), stop and raise it in the phase plan instead of bending either side.

### 1.1 Scope

In scope:

- a new `positional_stocks` runtime that runs **once per week** as a batch job;
- one strategy, `wsr1_weekly_stochrsi`, implementing the V1 rules in section 4 exactly;
- a pure, I/O-free rules core (indicators, signals, sizing, adds, exits, limits) that a future live adapter can reuse unchanged;
- Dhan daily historical data for NIFTY 200 stocks and the NIFTY 50 index;
- equity security-id resolution from the Dhan instrument master (the D34 gap);
- a durable paper ledger: pending orders, fills at the next session's open, positions, tranches, exits, weekly equity;
- a weekly report file and a Telegram summary;
- operator-maintained input files (universe, quality gate, optional results calendar).

Out of scope (each needs separate approval and a spec revision):

- live order placement of any kind, and any change to Phase 10 gates;
- intraday execution, live quotes or the shared tick feed;
- automatic fundamentals or news checks (the quality gate stays an operator input);
- a backtesting framework (a replay of recent weeks for testing is allowed, section 14);
- a dashboard page (deferred by decision 7; needs its own approval).

---

## 2. Strategy idea

Buy quality NIFTY 200 stocks when weekly momentum turns up from oversold, inside an uptrend or with relative strength; add at most twice, only after a confirmed weekly reversal; exit on a weekly-close stop, take half the profit when momentum is overbought, and trail the rest.

---

## 3. Time and calendar contract

| Item | Rule |
|---|---|
| Timezone | All dates and times in `Asia/Kolkata` via `common.utils.timeutils`. Never the host clock's local date (see D88+) |
| Weekly bar | One bar per ISO week from that week's trading sessions: open = first session open, high/low = extremes, close = last session close. The bar's date = its last trading session |
| Completed week | A week is usable only after its last session has closed (after 15:30 IST on its last trading day). A run must never use an incomplete current week |
| Fetch and preview | Friday 18:00 IST, retried Saturday and Sunday 10:00 IST; idempotent (section 10.3) |
| Decision time | Monday 08:30 IST — the decision run, offline from the cache the fetch job wrote (section 10.3) |
| Execution time | Every order decided for a week fills at the **open of the first trading session that starts after the decision run** — normally Monday. A decision run that happens after a session has opened (late start, retry) fills at the next session's open, never at an open that has already passed. Catch-up of fully missed weeks (section 10.2) fills each week's orders at the first session after that week, and the report flags those fills as catch-up fills |
| Holidays | Trading sessions come from the data. A Monday holiday moves execution to the next session |
| Missed runs | The run catches up week by week, in order, from the last completed run (section 10.2) |

---

## 4. Fixed trading rules

All conditions use completed weekly bars. `K`, `D` = Stoch RSI lines at a weekly close. `P1` = tranche-1 fill price. `s` = spacing. `A` = allocation for one stock. `B` = base allocation.

### 4.1 Universe

| Rule | Value |
|---|---|
| Membership | Symbols in the operator universe file (`universe.csv`, section 6.3), which mirrors NIFTY 200 |
| History | ≥ 200 completed weekly bars |
| Liquidity | Average daily traded value over the last 30 sessions ≥ ₹20 crore (traded value = close × volume when the source gives no value) |
| Red-regime subset | Only rows flagged `nifty100 = true` |

### 4.2 Quality gate (operator input)

The gate is judged by the operator and recorded in `quality_gate.csv` (section 6.4). The code only reads it.

- A symbol may be entered only with a row whose `status` is `PASS` or `EVENT_RISK` and whose `valid_until` is on or after the execution date.
- Missing, expired or `FAIL` → no entry and no add. The report lists it under "needs quality check". **Fail closed.**
- `EVENT_RISK` → allocation × 0.5 and max 1 add (T3 disabled).
- A held position entered as `PASS` whose row later becomes `EVENT_RISK` keeps its sizing, but T3 is disabled from then on (v1.2f).
- A held position whose row turns `FAIL` → thesis exit (section 4.10). An **expired** `FAIL` row is still a thesis exit (fail closed); an expired `PASS` or `EVENT_RISK` row on a held position blocks adds but does not exit (v1.2f).
- **Operator procedure (v1.2f):** a new company- or promoter-group-level governance event on a **held** symbol is recorded as `FAIL`, which exits it. After the position is closed the operator may change the row to `EVENT_RISK`. One status per symbol cannot express "exit if held, event-risk if not"; this procedure does.

The operator's criteria, for reference only (not coded): PAT > 0 in each of 3 years; 3-year sales and profit CAGR > 0; 3-year average ROCE ≥ 12% (banks/NBFCs: ROE ≥ 12% and GNPA % not above 4 quarters ago); D/E ≤ 1 or interest cover ≥ 3× (not for banks/NBFCs); operating cash flow > 0 in ≥ 4 of 5 years (not for banks/NBFCs); pledge ≤ 10% and not rising; FII + DII not down > 3 pp in 2 quarters; NOT (profit and revenue both down YoY in each of the last 2 quarters); no company fraud allegation, regulatory enforcement, auditor resignation or qualified opinion in 12 months. Event-risk: such an event 12–36 months ago, or at promoter-group level within 36 months.

### 4.3 Market regime

- NIFTY 50 weekly bars; 40-week EMA of close.
- **Red** = close < EMA40 AND EMA40 < its value 4 weekly bars earlier. Otherwise **Normal**.
- Red: max 1 new entry this week, and only `nifty100` symbols. Normal: max 2 new entries.
- **Unknown** (v1.2f): NIFTY 50's EMA40, or its value 4 bars earlier, is undefined → no new entries (fail closed). An undefined input must never read as "not Red".
- The regime affects new entries only. Adds and exits are unaffected.

### 4.4 Indicators (exact formulas — must match TradingView, section 7)

| Indicator | Definition on weekly bars |
|---|---|
| RSI(14) | Wilder: RMA of gains and of losses, each seeded with the SMA of the first 14 values, as `ta.rsi` |
| Stoch of RSI | `100 × (RSI − min14(RSI)) / (max14(RSI) − min14(RSI))`. If max = min, the value is undefined → the symbol is skipped that week and flagged |
| K | SMA(3) of the Stoch series |
| D | SMA(3) of K |
| EMA(n) for n = 10, 40, 50, 200 | α = 2/(n + 1). **Seeded with the SMA of the first n closes** — the first EMA value exists at bar n; earlier bars are undefined. **Verified against TradingView (v1.2c):** ETERNAL (270 weekly bars), week ending 18 Sep 2026, EMA200 = 209.70 on TradingView; SMA seed gives 209.70, first-value seed gives 223.24. For young stocks the seed changes EMA200 by several percent, so this is not negligible |
| ATR(14) | Wilder RMA of weekly true range, seeded as RSI; ATR% = ATR ÷ close of the same week |
| 52-week high | Max weekly high over the last 52 bars, including the current one |
| 6-month performance | close ÷ the close of the ISO week **26 weeks earlier** − 1, for the stock and for NIFTY 50 (v1.2e: by calendar week, not by bar count). Undefined if the series has no bar for that earlier week |
| RS | stock 6M performance − NIFTY 6M performance (percentage points) |

### 4.5 Arm and trigger

```
ARMED    = K < 20 AND D < 20 on this week's close or any of the previous 7 weekly closes
j0       = the most recent weekly bar with K < 20 AND D < 20 (within that window)
TRIGGER  = ARMED
           AND K(this week) > D(this week) AND K(last week) ≤ D(last week)
           AND no such cross occurred in any bar from j0 up to last week
           AND K(this week) < 50
```

A cross in the arm week itself counts. Values are weekly closes only. **An undefined K or D in any bar the check actually reads → no trigger, reported (v1.2f).** Those bars are the arming window (this close and the previous 7) plus the bar before j0, which the no-earlier-cross check needs (v1.2g: a bar outside that set never blocks).

### 4.6 Entry filters (all required)

1. Not already held, and no active cooling-off (section 4.11).
2. `close > EMA50` OR `RS > 0`.
3. `close ≥ 0.60 × 52-week high`.
4. `ATR% ≤ 12%`.
5. Universe rules of 4.1 pass; quality row valid (4.2).
6. No row in `results_calendar.csv` for this symbol falling Monday–Friday of the execution week. If the calendar has no row for the symbol, proceed and flag "results date unknown" (paper mode only; a live mode must fail closed — section 12).
7. Sector limit and promoter-group limit have room (4.12).
8. **Repeat signal:** if the **most recent** earlier TRIGGER for this symbol (traded or not) occurred within the last 26 weekly bars and its week's close is higher than this week's close, this week's close must also be above the prior week's high. (v1.2f: the most recent earlier trigger, as the V1 plan says — not any earlier trigger.) "Within the last 26 weekly bars" means the 25 bars before this one (i − 25 … i − 1); a trigger exactly 26 bars back is outside, matching the 26-week cooling-off count (v1.2g).

### 4.7 Ranking and entry count

- Candidates passing 4.5 and 4.6 are ranked by RS, highest first.
- Take at most 1 (Red) or 2 (Normal), also capped by the free position slots and by the drawdown brake (4.12).
- Every candidate not taken is reported with the reason.

### 4.8 Sizing

```
s   = max(10%, 1.5 × ATR%)          (ATR% of the trigger week; fixed for the trade)
A   = B × 10% ÷ s                   (× 0.5 when EVENT_RISK)
T1  = 40% of A;  T2 = 30% of A;  T3 = 30% of A   (EVENT_RISK: T3 disabled)
shares = floor(tranche amount ÷ fill price)
After the T1 fill at P1:
L1   = P1 × (1 − s)
L2   = P1 × (1 − 2s)
Stop = P1 × (1 − 3s)
```

Levels are fixed at the T1 fill and never recalculated. If `floor(...)` gives 0 shares, the order is skipped and reported.

### 4.9 Averaging (adds)

An add of the next tranche is decided for a week only when ALL hold:

1. The position has no partial sale yet, and tranches used < max (3, or 2 for EVENT_RISK).
2. **Touch:** some weekly low ≤ the next level (L1 for T2, L2 for T3), in any week from the previous buy's fill week onward. (v1.2g) The previous buy's fill week counts only if that buy filled at the week's **first session**; if it filled later in the week (the symbol did not trade Monday), the touch window starts the following week, because a weekly low cannot show whether it came before or after the fill.
3. **Reversal:** this week's close > the prior week's high, and this week is later than the previous buy's fill week (the touch week may be this same week).
4. This week's close > Stop AND > EMA200 AND < P1. If the close is ≥ P1, the add is skipped and its touch is cleared; the level must be touched again.
5. The quality row is still valid; no results in the execution week (4.6 item 6).
6. At most one add per position per week.

The add fills at the next session's open for the tranche amount ÷ fill price (floor). Each add consumes its touch; T3 needs a new touch of L2 after T2 fills.

### 4.10 Exits

For each position not yet half-sold, evaluate in this order; the first match decides:

1. **Thesis exit:** the quality row is `FAIL` (or the symbol left the universe file with `on_exit: exit` — section 6.3) → sell all.
2. **Stop:** weekly close < Stop → sell all.
3. **Time exit:** 52 weeks since the T1 fill week with no partial sale → sell all. Counted in ISO weeks (v1.2f): with T1 filled in week F, the exit is decided at the close of week F + 52 and fills in week F + 53. The trail time (below) and the cooling-off (4.11) count the same way.
4. **Partial:** K > 90 AND D > 90 → sell `floor(shares ÷ 2)` (once per trade). From then on: no adds, unfilled tranches cancelled, Stop replaced by the trail. (v1.2g) If `floor(shares ÷ 2)` is 0 (a 1-share position), the partial event still happens with nothing sold: the position becomes half-sold, adds stop and the 10W EMA trail replaces the stop. The report flags it.
5. **Add** check (4.9).

For each half-sold position:

1. Thesis exit → sell the rest.
2. **Trail:** weekly close < EMA10 → sell the rest.
3. **Trail time:** 52 weeks since the partial-sale week → sell the rest.

Every exit fills at the next session's open. A position is **closed** when its share count reaches 0.

### 4.11 Re-entry

- Closed with total net P&L ≥ 0 → eligible again on the next **fresh arm and trigger**: both the arming close (K < 20 and D < 20) and the cross must fall in decision weeks after the exit fill week (v1.2f, as the V1 plan says). An arm left over from while the stock was held does not count.
- Closed with total net P&L < 0 → 26-week cooling-off from the exit fill week X (entry decisions allowed from week X + 26); after it, entry also requires a fresh arm and trigger as above **and** `close > EMA50` (RS alone is not enough). P&L is **net of costs**: a trade slightly positive before costs but negative after them is a loss.
- One open position per symbol.

### 4.12 Portfolio limits (paper book values — all in configuration)

| Limit | Paper book | V1 rulebook default |
|---|---|---|
| Strategy capital C | ₹10,00,000 | — |
| Base allocation B | ₹1,00,000 | 10% of C |
| Max open positions | 10 | 8 |
| Committed cap | 100% of C | 80% |
| Untouchable buffer | 0% | 20% |
| Max per sector | 2 open positions | 2 |
| Max per promoter group | 1 open position | 1 |
| Drawdown brake 1 | Equity ≤ 90% of peak → no new entries for 4 decision weeks, the firing week counting as the first (v1.2g). After the pause, entries resume even if equity is still ≤ 90%; brake 1 can fire again only after equity has closed above 90% of peak at least once (v1.2f). A deeper fall is brake 2's job | Same |
| Drawdown brake 2 | Equity ≤ 80% of peak → no new entries until the operator clears `brake_2_cleared_on` in config. **Clearing resets the peak** to the equity at the first weekly run on or after the clearance date (v1.2f), and **also ends any running brake-1 pause** (v1.2g) — the operator's review is the decision to resume | Same |

- **Committed** = A for each open position until its partial sale; afterwards the cost of the shares still held. EVENT_RISK positions commit T1 + T2 only.
- **Equity** = cash + Σ(shares × weekly close), measured at each weekly run. The peak is the running maximum of that series.
- Cash may not go negative. An entry or add that would breach the committed cap or cash is skipped and reported.
- **Pending orders count (v1.2g).** A BUY order still unfilled after step 1 (the symbol did not trade, or its data is missing) holds its slot, sector, promoter group, committed amount and cash (amount plus buy costs) exactly as if filled, until it fills or is cancelled.
- **Pending sells (v1.2h).** An unfilled SELL keeps its position counted as held — slot, sector, group and committed — until it fills. A decided SELL_ALL frees its slot in the decision week (4.13), so a sale that then fails to fill (the symbol did not trade) can leave the book one position over a limit until it fills. That is accepted and reported; no entry is taken while the book is over a limit.
- **Book consistency (v1.2h).** The runtime must never pass a filled order as pending. The rules reject an inconsistent book outright: a pending BUY_T1 for a symbol already held, or a pending add or sell for a position that is not held.
- **Committed with EVENT_RISK (v1.2g):** a position whose row turns EVENT_RISK after T3 has filled still commits its full A; T3 is disabled only while it is unfilled.

### 4.13 Order of evaluation within one weekly run

1. Execute pending orders from the previous decision week at this week's first-session open (section 8).
2. Mark to market at this week's close; update equity, peak and brake state.
3. Evaluate exits and adds for open positions (4.10, 4.9).
4. Evaluate new entries (4.5–4.8) with the slots, cash and limits left after step 3's decisions. (v1.2f) A decided SELL_ALL frees its slot, sector, group and committed amount for this week's entries; a decided SELL_HALF recomputes committed as the cost of the shares that will remain. **Sale proceeds are not counted as cash until filled**, and each planned buy reserves its amount plus buy costs.
5. Persist decisions as pending orders for the next session; write the report.

### 4.14 Corporate actions on held positions (v1.2j)

Dhan back-adjusts history for bonuses, splits and (sometimes) demergers, but a held position's stored P1, levels, share count and fill prices are in the units at the time of the fill. Left alone, a 1:1 bonus halves the adjusted price while the stop stays at the old level: a false stop, a false loss, a false cooling-off and a false drawdown (reproduced in the Phase 4a audit: −₹19,685 on a flat stock). The gap scan cannot see it, because a correctly adjusted series has no gap.

1. **Detect**, every run (both modes), for every fill of every open position — **including positions and fills created by this run's own fills: the checks in this item and in item 7 run again after the fills and before any decision (v1.2l)**: compare the stored fill price with the cached open of the fill session. A relative difference above 0.5% means the history was restated; the factor is `f = cached open ÷ stored fill price`.
2. **Freeze** the position while unresolved:
    - no exit, add or partial decision for it;
    - it still counts toward every limit;
    - it is marked to market at `stored shares × (close ÷ f)` — in its own units — so equity and the brakes see no false drop;
    - it is flagged in the report and in the Telegram summary. New entries are unaffected.
3. **Confirm**: the operator adds a row to `config/positional_stocks/corporate_actions.csv`, with columns `symbol, ex_session, kind, ratio, confirmed_on, note`:
    - `kind = BONUS_SPLIT`: `ratio` is new shares per old share (1:1 bonus → 2; 1:2 bonus → 1.5; 1:10 split → 10; **a 10:1 consolidation → 0.1**, v1.2k). Any positive ratio other than 1;
    - `kind = DEMERGER`: `ratio` is the price factor Dhan applied (for example 0.90). Shares are unchanged; the value removed is credited as cash.
    - `kind = PRICE_CORRECTION` (v1.2k): `ratio` is the price factor (any positive value). Shares are unchanged and **no cash is credited** — for a Dhan data correction, which is not an economic event.
4. **Rescale** at the next run, only if the confirmed row matches the detected factor within 0.5% (BONUS_SPLIT: 1 ÷ ratio ≈ f; DEMERGER and PRICE_CORRECTION: ratio ≈ f); otherwise stay frozen and flag the mismatch:
    - **BONUS_SPLIT:** shares × ratio, floored; the fractional share is credited as cash at the adjusted close ("cash in lieu"). P1, L1, L2, Stop and every stored fill price ÷ ratio. Total cost in ₹ is unchanged. **If floor(shares × ratio) is 0** (a consolidation of a small holding), the whole value, `shares × ratio × adjusted close`, is cash in lieu and the position closes as a normal exit at that price: exit week = the ISO week of the ex session, no sell costs, pending orders skipped; P&L, cooling-off and re-entry apply as for any exit (v1.2l, D101).
    - **DEMERGER:** P1, L1, L2, Stop and fill prices × ratio; shares unchanged. The value removed, `shares × pre-ex close × (1 − ratio)`, is credited as cash. This stands in for the demerged company's shares, which the paper book does not hold.
    - **PRICE_CORRECTION (v1.2k):** as DEMERGER, but no cash is credited.
    - The rescale is stored as its own record (position, ex session, kind, ratio) and applied whenever positions are rebuilt from fills. Original fill rows are never edited.
5. A frozen position whose freeze lasts more than 2 weekly runs is escalated in the report as an operator action.
6. **Pending orders at rescale.** A pending SELL decided before the restatement is **re-issued in the new units** — SELL_ALL for all rescaled shares, SELL_HALF for floor(rescaled shares ÷ 2) — not dropped: the exit was decided on valid data and must still happen. **It fills at its original execution session when that session's candle is cached (flagged `late_fill`), otherwise at the next session (v1.2k)** — the restated open of the original session is economically the same price, so waiting adds price risk for nothing. **If floor(rescaled shares ÷ 2) is 0, no sell is issued: the 1-share partial rule of 4.10 item 4 applies** (half-sold, adds stop, the trail replaces the stop), dated to the original partial decision (v1.2l). Pending buys are amount-based and proceed unchanged. The kind `DEMERGER` covers price-only restatements that carry value (a special dividend Dhan adjusts for, for example); `PRICE_CORRECTION` covers those that do not.
7. **Unadjusted actions (v1.2k).** Detection in item 1 only sees history Dhan has restated. A bonus or split Dhan has **not yet** back-adjusted shows as a raw price drop instead, and would still cause a false stop, a false trail exit or a false add touch. So a **held** symbol with an **unacknowledged gap of 15% or more, in either direction** (the section 6.1 report level, not the 30% entry-block level; a consolidation shows as a raw jump), on a session **after its first fill session**, is frozen exactly as in item 2, marked at `close ÷` its freeze factor — the gap's close ratio r for a single unadjusted action; see **Unit factor** below. A gap on or before the first fill session does not count: P1 and every level come from the T1 fill price, so a position opened on the ex session is already in the new units. It is resolved in one of three ways:
    - the operator acknowledges the gap in `gap_acknowledgements.csv` (a real move): the freeze lifts and decisions resume;
    - Dhan back-adjusts the history: the gap disappears, item 1 detects the restatement, and a `corporate_actions.csv` row confirms it (the row may be added in advance);
    - otherwise it escalates as in item 5, and item 8 may close it.
   **Unit factor (v1.2l).** Each buy fill has a unit factor: its item-1 factor f (1 if not restated) × the ratios of the unacknowledged gaps on sessions after its fill session. When the factors of all buy fills agree within 10%, they reflect one unit break — for example a gap where Dhan's back-adjustment stops, with the restated fills after it — and the position's freeze factor is the factor of its **latest restated fill** (exact, from item 1) or, when no fill is restated, of its first fill. The same break is never counted twice. Factors that disagree by more than 10% mean mixed units: the position is frozen at its first fill's factor and escalated, and item 8 does not apply.
   **Operator rule (v1.2l):** never acknowledge a gap you know is a bonus, split or consolidation. An acknowledgement means a real move and resumes decisions on mismatched units — the false stop again. Add a `corporate_actions.csv` row instead.
   **Trade-off, accepted:** a genuine crash of 15% or more on a held stock waits for the operator's acknowledgement before its stop or trail can fire. With the fetch/preview run before the decision run, the operator resolves it in between and there is no delay; otherwise the exit is one week late. A genuine rise of 15% or more likewise pauses that position's partial, add and trail decisions until acknowledged.
   **Known limitation:** a bonus smaller than about 1:6 (price factor above 0.85) is below the threshold; it is caught only once Dhan restates the history (item 1).
8. **Stuck-freeze exit (v1.2l, D102).** Dhan's back-adjustment can be missing or partial (MOTHERSON, section 6.1); then no restatement ever resolves the freeze. A position frozen in **3 or more consecutive runs** (any run in which it is not frozen resets the count) is closed when an **eligible** `corporate_actions.csv` row **matches** its freeze factor but cannot be applied under item 4:
    - **eligible:** the row's ex session is after the position's first fill session and on or before the last session of the run's week. A row for a future ex date never matches and never produces the "waiting for Dhan restatement" note;
    - **matches:** the row's price factor (BONUS_SPLIT: 1 ÷ ratio; DEMERGER and PRICE_CORRECTION: ratio) is within 0.5% of the freeze factor when no unacknowledged gap is involved (item 1 only), and within **10%** otherwise, because a gap ratio carries that day's market move;
    - **exit:** a SELL_ALL of the stored shares is queued for the next execution session, reason "exit: corporate action not adjusted by Dhan". It fills at that session's open ÷ **the row's price factor** (the position's own units), with normal sell costs and no separate DEMERGER cash credit. The position's other pending sells are superseded and its pending buys skipped. P&L, cooling-off and re-entry apply as for any exit;
    - if an acknowledgement lifts the freeze before the fill (the operator says it was a real move), the queued exit is skipped ("freeze lifted") and decisions resume in that run. If Dhan restates first and the position is rescaled, item 6 re-issues the exit in the new units, with no price factor;
    - no eligible matching row: the position stays frozen and escalated.
   **Known limitation:** two unconfirmed actions stacked on one position multiply their factors, so no single row matches; the position stays frozen and escalated.

---

## 5. Instruments and identity

- Resolve each universe symbol to its Dhan `security_id` (NSE, series EQ) from the Dhan instrument master. Port or extend the equity parsing that D34 left unported, behind the existing scrip-master cache. Never hard-code security ids.
- NIFTY 50 index id is resolved the same way (IDX_I segment).
- A symbol that cannot be resolved is skipped and reported. It never blocks the rest of the run.

---

## 6. Data contract

### 6.1 Daily history

- Source: Dhan `POST /v2/charts/historical` (daily candles since inception), through `common/market_data/dhan_historical.py` extended with a `fetch_daily()` method in the existing client's style (httpx, bounded retries, error classification).
- **Fetch each symbol's full available daily history (v1.2d)** — request from 2000-01-01; Dhan returns from its earliest record (from listing for younger stocks). Cache on disk; afterwards refetch only the tail, with the overlap check below.
    - **Why not 260 weeks:** EMA200 is SMA-seeded (section 4.4), so a 260-week window leaves only 60 bars of smoothing after the seed and the value depends on where the window starts. Measured at week ending 18 Sep 2026 on the review's independent data (not Dhan's): 260 bars vs full history gives ETERNAL 206.60 vs 209.70 (TradingView 209.70), RELIANCE 1338.50 vs 1313.91, INFY 1468.65 vs 1433.36, LT 3267.62 vs 3335.11. These full-history values are not parity readings — only ETERNAL's EMA200 is (section 7); Dhan's full history gives RELIANCE 1295.07, pending a TradingView reading — errors of 1.5–2.5% in the value that gates every add. K, D, EMA50 and ATR are unaffected.
    - **No chunking is needed:** one call for RELIANCE over 2000-01-01 → 2026-09-22 returned 6,145 sessions back to 2002-01-01 (D94).
    - The gap scan (≥ 30% flag, ≥ 15% report) runs over the full history. Gaps older than about five years barely move any indicator, but they are reported.
- **Only the `fetch` mode touches the network.** Its cache is keyed by **symbol**, never by instrument id or by run date, so the `decide` mode can read it without resolving anything or authenticating (section 10.3).
- **Throttle:** at most 3 requests per second, leaving headroom under Dhan's documented Data-API limit of 5 per second and 100,000 per day (external figure, not yet verified against this account).
- **Per-run deadline:** 20 minutes for `fetch`, 5 minutes for `decide`. On expiry the run fails closed and reports; it never decides on a partially refreshed universe. The existing client has per-call timeouts but no overall deadline, so this is enforced by the run, not by the client.
- **Corporate-action adjustment must be verified in Phase 1, not assumed.** Test known events, e.g. Reliance 1:1 bonus (Oct 2024) and HDFC Bank 1:1 bonus (Aug 2025).
    - If Dhan history is adjusted → record the evidence in the runbook.
    - If it is not → STOP and report. Do not build an adjustment engine without approval.
- Any symbol whose adjusted series shows an unexplained overnight gap of 30% or more is flagged and skipped for new entries until the operator acknowledges it. Thresholds are compared on the exact decimal close ratio, never a float difference (v1.2j).
    - **An acknowledgement is keyed to (symbol, gap session, ratio), never to the symbol alone (v1.2b).** A new unexplained gap re-blocks an acknowledged symbol.
    - **Only gaps in the most recent 520 weekly bars (~10 years) block (v1.2e).** Older gaps are reported, never blocking. A price break of factor r that is N weeks old moves EMA200 by about |1 − r| × 0.99005^N: a 1:1 bonus (r = 0.5) left unadjusted 520 weeks ago moves it by ~0.3%, inside parity tolerance, and every other indicator here looks back far less. Applied to full history without a window, the rule would block 43 symbols — RELIANCE and HDFCBANK among them — over breaks 15–20 years old.
    - **Monthly full refetch (v1.2b):** the first fetch of each calendar month refetches every symbol's full history instead of the tail. The overlap check sees only the last 10 sessions, so it cannot detect Dhan restating older history — for example correcting, or newly breaking, a partial back-adjustment like MOTHERSON's.

### 6.2 Staleness

A run for week W requires every used series to contain W's last session. Series that don't are skipped and reported. If NIFTY 50 is stale, the whole run stops (regime unknown). **Fail closed.**

**W's last session comes from the calendar, never from the data (v1.2b).** It is the last weekday (Monday–Friday) of the ISO week that is not in the verified NSE holiday list in `config/global.yaml` (`holidays`), read through the existing session/calendar code rather than a second copy.

- NIFTY 50 must contain that expected session. If it does not, the data is **not yet published** (or the calendar is wrong): the fetch reports it and fails closed, and the job's next scheduled attempt retries. A week is never built from the sessions that happen to be present.
- Deriving W's last session from NIFTY's own data is **forbidden**: when Dhan has not yet published Friday's candle, NIFTY's last session is Thursday, every series looks current, and a Monday–Thursday bar is silently used as the week (reproduced in the Phase 1 review).
- A session present in the data on a date the calendar lists as a holiday (for example the Diwali Muhurat session, 8 Nov 2026, a Sunday) is included in its ISO week and reported. It does not change the expected last session.
- The holiday list is annual. Without a 2027 list, every 2027 holiday Friday is "expected" and that week fails closed — the safe direction.

### 6.3 `universe.csv` (operator-maintained, committed)

Columns: `symbol, isin, company, industry, nifty100 (true/false), group (promoter group or blank), on_exit (hold|exit), as_of`.

- `industry` is NSE's Industry column from the NIFTY 200 constituent file and is the sector used for limits.
- `group` blank = its own group.
- When NIFTY 200 is reconstituted (end of March / September), the operator updates the file. A held symbol removed from the file follows `on_exit` (default `hold`: exits as normal, no further adds). The `on_exit` used is the one from the symbol's **last-seen row**, which the runtime persists (v1.2f).

### 6.4 `quality_gate.csv` (operator-maintained, committed)

Columns: `symbol, status (PASS|EVENT_RISK|FAIL), checked_on, valid_until, notes`.

### 6.5 `results_calendar.csv` (optional, operator-maintained)

Columns: `symbol, results_date`.

---

## 7. Indicator parity contract

- Indicators are implemented directly from the section 4.4 formulas in `indicators.py`. A library implementation (for example the vendored `pandas_ta_classic` `stochrsi`) may be used **only as a cross-check in tests**, never as the production source: its RMA seeds with the mean of the first `length` diff values starting one bar earlier than TradingView's `ta.rma`. The difference vanishes after warm-up, but the contract is the formula.
- Unit tests pin every formula in 4.4 against hand-computed fixtures.
- **Acceptance against TradingView:** the operator supplies K / D / EMA50 / ATR readings from TradingView for at least 5 symbols at one weekly close. Each must match within ±0.5 (K, D) and ±0.5% (EMA, ATR).
- The first reference value is already known: **ADANIENSOL, week ending 18 Sep 2026: K 4.64, D 4.51** (TradingView, 1W, Stoch RSI 3/3/14/14, close).
- **Parity readings (v1.2c)** — TradingView, 1W, NSE, "Adjust data for dividends" OFF, the bar TradingView labels "Tue 15 Sep '26" (week ending 18 Sep 2026; Mon 14 Sep was a holiday):

| symbol | close | K | D | EMA50 | EMA200 | ATR14 |
|---|---|---|---|---|---|---|
| ADANIENSOL | 1436.10 | 4.64 | 4.51 | 1285.70 | — | 116.00 |
| RELIANCE | 1226.40 | 34.68 | 49.60 | 1353.80 | — | 55.60 |
| HDFCBANK | 731.00 | 16.53 | 14.86 | 817.94 | — | 33.56 |
| INFY | 1051.40 | 58.64 | 70.51 | 1270.60 | — | 76.06 |
| LT | 3885.00 | 25.83 | 40.62 | 3909.70 | — | 168.23 |
| ETERNAL | 326.85 | 90.14 | 93.05 | 280.88 | 209.70 | 19.27 |

- **Pre-check on independent data (review, not Dhan):** the section 4.4 formulas over ISO (Monday–Sunday) weeks reproduce every value above exactly, or within 0.01% for EMA50. Grouping weeks as Saturday–Friday instead moves RELIANCE's K by 1.1 — outside tolerance — because of the Sunday 1 Feb 2026 Budget session, which TradingView counts in its Monday–Sunday week. **So a parity failure on Dhan's data points at the data (a missing or misdated weekend session, an adjustment gap), not the formulas.**

---

## 8. Paper execution model

| Item | Rule |
|---|---|
| Fill price | The official open of the execution session, from that session's daily candle |
| Fill time | Recorded at the next weekly run, which has the candle. A run never fills an order before its session exists in the data |
| Symbol not traded that session | Fill at the next session's open; flag it |
| Candle arrives late (v1.2j) | If an order fills at a session in an earlier week than the run's week (its data was missing at the earlier run), it is flagged `late_fill`, and the add-touch memory is replayed over the bars from that fill week (respecting `at_week_open`) before this week's decisions |
| Costs | Configurable: `cost_bps_buy` (default 12), `cost_bps_sell` (default 11), `fixed_cost_per_sell_rs` (default 15). Defaults approximate STT + stamp duty + exchange charges + DP charge for equity delivery at a zero-brokerage broker. **Operator to verify against Dhan's current charges** |
| Taxes | Not modelled |
| Gaps through the stop | No special handling. The exit fills at the open, whatever the price, and the report shows the planned vs actual loss |

---

## 9. State and persistence

- Database: `data/operational/positional_stocks.db`, created by the existing `Database` / `MigrationRunner`, with **additive, replay-safe migrations** (`CREATE ... IF NOT EXISTS`), following the repository pattern.
- **Do not alter the options cycle tables** (`strategy_cycles` has options-only semantics, such as `resolved_expiry_date NOT NULL`).
- **Decision (v1.1): new equity tables, not a generalised cycle model.** `strategy_cycles` allows at most one open cycle per strategy (`idx_one_open_cycle`), keys identity on an expiry, and restricts `strategy_cycle_legs.leg_role` to option roles; widening any of these means rebuilding tables `weekly_delta_neutral` depends on. Phase 0 confirms this against the code and records it as a D-entry.
- **Own migration set (v1.2i, supersedes the shared-directory plan).** `positional_stocks.db` is migrated from its **own** directory, `runtimes/positional_stocks/migrations/`, passed to the existing `MigrationRunner` through its `versions_dir` argument. **Nothing is added to `common/persistence/migrations/versions/`.** Reasons:
    - The two paper runtimes run from the same working tree and call `run_pending()` at every start, so a shared migration reaches their live databases the next morning.
    - `verify_checksums()` then refuses to start a runtime if an applied migration file is later **edited** (likely while this feature is still under review) or **missing** (after switching the tree back to a branch without it). Either would stop both paper runtimes.
    - A separate set keeps the live databases byte-identical and makes every stock migration disposable until Phase 5.
- **`positional_stocks.db` is disposable until Phase 5** (paper, no real history yet): if a stock migration is edited during development, delete that database rather than work around the checksum check.
- **Naming rule: every new table is still prefixed `stock_`** — no longer needed to avoid a collision, but it keeps the schema unambiguous in queries and backups.
- Minimum entities:
    - **stock_weekly_runs:** strategy_id, week_ending, status, input fingerprints (universe, quality, calendar, config), started/finished.
    - **stock_signals:** per week and symbol — stage reached (armed / trigger / filters / ranked / taken), reason.
    - **stock_positions:** position_id, symbol, state (OPEN / HALF_SOLD / CLOSED), P1, s, A, L1, L2, Stop, event_risk, T1 fill week, partial week, close week, tranches used, touch flags.
    - **stock_pending_orders:** created_week, execute_on_or_after, position_id / symbol, action (BUY_T1 / BUY_T2 / BUY_T3 / SELL_HALF / SELL_ALL), quantity or amount, reason; state PENDING → FILLED / SKIPPED.
    - **stock_fills:** order id, date, price, quantity, costs, catch-up flag.
    - **stock_equity:** week_ending, cash, positions value, equity, peak, drawdown %, regime, brake state.
    - **stock_cooling_off:** symbol, until_week.
- Repository access lives in a new module for these tables, not in new overloads on `ExecutionRepository`.
- **Idempotency:** a run is keyed by `(strategy_id, week_ending)`. Re-running a completed week changes nothing. A pending order is filled exactly once, by state transition inside one transaction.
- **Crash safety:** a run interrupted mid-way is resumable. Each week's writes commit in one transaction, or in clearly ordered transactions with a run status that the next run checks.

---

## 10. Weekly run

### 10.1 Command

```
python -m runtimes.positional_stocks.weekly_run --mode fetch  [--as-of auto|YYYY-MM-DD] [--dry-run]
python -m runtimes.positional_stocks.weekly_run --mode decide [--as-of auto|YYYY-MM-DD] [--dry-run]
```

| Mode | Network | What it does | Deadline |
|---|---|---|---|
| `fetch` | Dhan Data API + scrip master | Refreshes the on-disk cache for every universe symbol and NIFTY 50, verifies staleness and adjustment, then writes a **preview** report (same content as the decision report, marked PREVIEW). Persists no decisions, changes no position | 20 minutes; on expiry it fails closed and reports |
| `decide` | **No Dhan call of any kind** | Reads only the local cache, applies section 4.13, persists the week, writes the report | 5 minutes |

- `--dry-run` computes and writes the report but persists nothing. `fetch` always behaves this way.
- `auto` = the most recent completed week.
- Both modes take a process lock so two runs can never overlap; neither starts a supervisor, a worker or a market feed.
- Both modes run `scripts.validate_environment` and the paper-safety check first, both of which are offline-safe.

### 10.2 Algorithm

1. Load config; confirm `mode: paper` (anything else is refused in this spec version).
2. Determine the weeks to process: every completed week after the last completed run, in order, up to `--as-of`. If there is no prior run, process only the latest completed week (no backfill of trades). **A decision run for week W is refused unless W is the first run or W − 1 is COMPLETED (v1.2j)**: a skipped week is a skipped stop check.
3. For each week: in `fetch` mode refresh the cache and verify staleness (6.1, 6.2), then compute and report without persisting; in `decide` mode read the cache only, apply section 4.13 steps 1–5 and record the run.
4. Write the report and send the Telegram summary once, for the final week processed.

### 10.3 Schedule

**Decided (v1.2): two of the strategy's own LaunchAgents. No `auto_start` entry, no change to shared auto-start code.**

| Job | Times (IST) | Mode | Notes |
|---|---|---|---|
| Fetch + preview | **Friday 18:00**, retried **Saturday 10:00** and **Sunday 10:00** — **provisional (v1.2b):** Dhan had not published Monday 21 Sep 2026's daily candle by 22:20 IST that day, so Friday 18:00 may find Friday missing. The times are fixed in Phase 5 from the publication-lag probe; until then the calendar rule in 6.2 makes an early fetch fail closed, never silently wrong | `--mode fetch` | Idempotent: if the cache already covers the completed week and its preview report exists, it exits immediately. Market closed and the intraday runtimes stopped, so nothing else is drawing on Dhan's 5 req/s Data-API budget |
| Decision | **Monday 08:30** | `--mode decide` | Offline. If the cache does not cover the completed week, it makes **no trades**, reports the reason and alerts. The operator may re-run either mode by hand |

Between the two, the operator reads the preview report and fills `quality_gate.csv` for the candidates it lists. That weekend window is the point of splitting the job in two: a candidate with no valid quality row is refused (section 4.2), and the preview is what makes it reachable in time.

**Rules that keep the decision run offline** (each was a verified blocker):

1. It must not touch the scrip master. `ScripMasterCache` keys its file by the IST date, so a Monday call would download. The fetch job therefore writes the cache **keyed by symbol**, and the decision run resolves no instrument ids.
2. It must not call `AuthBootstrap.get_token()`. A Friday 09:00 token is expired by Saturday, so a cache miss would attempt a login. The decision run needs no token because it makes no Dhan call.
3. It must not construct a market-feed adapter or the historical client. It is a batch job, not a worker; it does not follow the `__main__.py` pattern of the existing runtimes.
4. Telegram is the one outbound call it may make. "No Dhan call" is not "no network"; a missing or failing notifier is non-fatal.

**Fetch job authentication.** It reuses the token cache. A Friday 18:00 run finds the day's 09:00 token with roughly 15 hours of life left and performs no login. A Saturday or Sunday fallback run will log in, which is permitted. Phase 1 asserts the remaining life explicitly rather than assuming Dhan keeps issuing 24-hour tokens.

**LaunchAgent constraints to handle in Phase 5** (all verified):

- `tests/unit/test_launchd_plists.py` asserts the exact set `{autostart, dashboard}` and that every spec has its `.plist` committed. Both new specs and both generated plists land in the same change as the test update.
- `scripts/install_launch_agents.py` installs and enables **every** spec unconditionally. Either `PlistSpec` gains a flag the installer honours, or the plists are generated and deliberately not installed. **Nothing is scheduled until the operator says so.**
- Both jobs use `wait_policy="elapsed"` (the dashboard's policy), not `"session_deadline"`: an 18:00 job whose volume is not yet mounted would otherwise give up on its first pass with a misleading 15:15-deadline message.
- Label namespace: `LABEL_PREFIX` plus a new `short_name` is automatically unique.

## 11. Outputs

- **Report:** `data/reports/positional_stocks/<week_ending>.md` — **not** under `data/operational/`, which holds databases and is what `common/retention/` backs up and purges. The preview report carries the same name with a `-preview` suffix. Phase 4 confirms retention ignores the new directory and that `ProjectPaths` creates it. It contains:
    - regime, equity, drawdown and brake state;
    - fills executed since the last run;
    - pending orders for the next session (with levels);
    - open positions (P1, s, L1, L2, Stop / trail, unrealised P&L, weeks held);
    - closed trades this week;
    - the trigger funnel (triggered → filters → ranked → taken, with reasons);
    - a watchlist (armed, K ≤ D, K < 50, filters pass), ranked by RS;
    - data and input warnings.
- **Telegram:** a short summary through the existing notifier (regime, orders for next session, exits, equity). No secrets, no full tables.
- **Journal export:** one CSV row per closed trade, with the V1 journal columns.

---

## 12. Configuration contract (example)

```yaml
# config/strategies/positional_stocks/wsr1_weekly_stochrsi.yaml
strategy_id: wsr1_weekly_stochrsi
enabled: false
mode: paper
live_approved: false
engine: stock_portfolio_engine   # reserved EngineKind; verified accepted by StrategyConfig
parameters:
  # schedule stays INSIDE parameters — a top-level key is rejected by the strict model
  schedule: {fetch_days: [FRIDAY, SATURDAY, SUNDAY], fetch_time: "18:00", fallback_fetch_time: "10:00", decide_day: MONDAY, decide_time: "08:30"}
  deadlines: {fetch_minutes: 20, decide_minutes: 5, fetch_requests_per_second: 3}
  paths: {reports: data/reports/positional_stocks}
  capital: 1000000
  base_allocation: 100000
  max_positions: 10
  committed_cap_pct: 100
  buffer_pct: 0
  max_per_sector: 2
  max_per_group: 1
  stoch: {rsi_length: 14, stoch_length: 14, k: 3, d: 3, arm_level: 20, arm_window_weeks: 8, max_k_at_cross: 50, overbought: 90}
  filters: {ema_trend: 50, rs_weeks: 26, min_pct_of_52w_high: 60, max_atr_pct: 12, min_history_weeks: 200, min_traded_value_cr: 20, repeat_lookback_weeks: 26}
  sizing: {spacing_floor_pct: 10, spacing_atr_mult: 1.5, tranches_pct: [40, 30, 30], stop_spacing_mult: 3, event_risk_alloc_mult: 0.5}
  adds: {require_close_above_prior_high: true, require_close_above_ema: 200}
  exits: {trail_ema: 10, time_exit_weeks: 52, trail_time_weeks: 52}
  reentry: {cooling_off_weeks: 26}
  regime: {index: NIFTY 50, ema: 40, slope_lookback_weeks: 4, red_max_entries: 1, normal_max_entries: 2}
  brakes: {dd1_pct: 10, dd1_pause_weeks: 4, dd2_pct: 20, brake_2_cleared_on: null}
  costs: {cost_bps_buy: 12, cost_bps_sell: 11, fixed_cost_per_sell_rs: 15}
  inputs: {universe: config/positional_stocks/universe.csv, quality_gate: config/positional_stocks/quality_gate.csv, results_calendar: config/positional_stocks/results_calendar.csv, require_results_calendar: false}
```

Committed configuration must keep every live gate disabled (the existing `assert_no_live_config_committed.py` must cover this file).

---

## 13. Architecture placement

```
strategies/positional_stocks/wsr1_weekly_stochrsi/
  WSR1_WEEKLY_STOCH_RSI_SPEC.md    # this document
  indicators.py                    # section 4.4 — pure
  rules.py                         # sections 4.3, 4.5–4.12 — pure, no I/O, no clock
  models.py                        # dataclasses for bars, signals, positions, orders
runtimes/positional_stocks/
  __init__.py
  weekly_run.py                    # CLI, orchestration, persistence, report, Telegram
config/runtimes/positional_stocks.yaml
config/strategies/positional_stocks/wsr1_weekly_stochrsi.yaml
config/positional_stocks/{universe,quality_gate,results_calendar}.csv
```

- The rules core imports nothing from `common.engine`, `runtimes`, the broker or the database. It takes bars and state, and returns decisions. That is what lets a future live adapter reuse it unchanged.
- Architecture doc amendment (Phase 0): replace "keep positional stocks a placeholder" with a scoped positional-stocks section. It covers the `stock_portfolio_engine` kind, the run-to-completion weekly job, paper only, and a pointer to this spec. No second architecture document.
- **Commit-ordering rule:** `config/runtimes/positional_stocks.yaml` and `config/strategies/positional_stocks/wsr1_weekly_stochrsi.yaml` land in the **same commit**. `common/config/loader.py` `resolve_runtime_strategies` scans all of `config/strategies/**` and raises for a strategy whose runtime file is missing, which would stop the two existing runtimes at startup.
- **The runtime is never registered in `scripts/_runtimes.py::RUNTIMES` and never routed through `orchestration.auto_start`** (superseding the v1.1 bullet that said otherwise). It is scheduled only by its own two LaunchAgents, which ship generated but uninstalled (section 10.3).
- Package `__init__.py` files are created by the first phase that adds Python code to a folder. Phase 0 creates only the folder holding this document.
- **Weekly bars stay out of `common/warmup/` and `common/candles/`.** `parse_timeframe_minutes` rejects `1W` and `1d`, and the whole warm-up stack is bucketed by intraday minutes within one session. The weekly bar builder lives with this strategy's data layer; the minutes vocabulary is not extended.
- Reports live in `data/reports/positional_stocks/`; the database lives in `data/operational/positional_stocks.db`.
- **The paper runtimes run from this working tree (v1.2i).** Until this feature is merged, any change to a module that `intraday_options` or `positional_options` imports reaches live paper trading at the next start. Phases 4a and 4b therefore add new files only; Phase 5's shared changes (`orchestration/launchd`, the installer) get their own review for that reason.

---

## 14. Tests and acceptance

Golden cases, taken from the operator's V1 plan (all must pass exactly):

| Case | Expected |
|---|---|
| ATR 6%, fill ₹1,000 | s 10%, A ₹1,00,000, T1 40 shares, L1 ₹900, L2 ₹800, Stop ₹700 |
| ATR 7.2%, close ₹1,000 | s 10.8%, A ₹92,593, T1 ₹37,037 → 37 shares; after a fill at ₹1,000: L1 ₹892, L2 ₹784, Stop ₹676 |
| T2 fill ₹930, T3 fill ₹820 (A ₹1L) | 32 and 36 shares; 108 total; cost ₹99,280; average ₹919.26; loss at ₹700 −₹23,680 |
| Stop hit with T1 only | −₹12,000 before costs |
| Worked trade: T1 40 @ ₹1,000, T2 32 @ ₹930, half sold 36 @ ₹1,120, rest 36 @ ₹1,180 | Gross P&L +₹13,040 |
| Event-risk, ATR 9%, B ₹1L | s 13.5%, A ₹37,037, T3 disabled |
| Odd shares at the partial sale | 73 held → sell 36 |
| Entry table | Every YES/NO row of the V1 plan's section 7, including the K = D, midweek-cross, window-expired, K = 28 and K ≥ 50 cases |
| Exit table | Every row of the V1 plan's section 8 |
| Averaging scenarios and the ₹10L example | Every row of the V1 plan's sections 5 and 6 that the spec's rules decide (the paper book's limits replace the plan's 8 / 80%) |

The V1 plan's sections 1 and 5–8 are committed beside this spec as `WSR1_V1_GOLDEN_CASES.md` (v1.2e), with the known intentional differences listed at its top. The spec wins on any conflict; any other difference is a finding to report.

Also:

- Indicator parity (section 7).
- **Offline decision run:** with the historical client, the scrip master and `AuthBootstrap` all monkeypatched to raise on use, a `--mode decide` run over a warm cache completes normally. This is the test that keeps the Monday job offline as the code changes.
- **Cold cache:** `--mode decide` with the completed week missing from the cache makes no trades, reports the reason and exits non-zero.
- Idempotency: running the same week twice changes nothing.
- A catch-up over 3 missed weeks equals 3 sequential runs.
- Replay: the last 12 completed weeks, on real data from an empty book, run end to end without error. This is a smoke test, not a performance study.
- Timezone: the full suite passes under `TZ=UTC` and `TZ=America/New_York`.
- The live-config guard covers the new config.

---

## 15. Phase plan (each phase stops for operator review)

| Phase | Deliverable | Acceptance | Explicitly out of scope |
|---|---|---|---|
| 0 | This spec (placed), architecture doc section, CLAUDE.md bullets, runbook D93 | Operator approves; the commit contains zero `.py`, `.yaml`, `.sql`, `.csv` files; nothing pushed | Any code, config, migration or test |
| 1 | `fetch_daily()`, equity scrip resolution (closes D34), weekly bar builder, symbol-keyed cache, CSV loaders, per-run deadline and throttle, token remaining-life assertion | 200 symbols + NIFTY 50 resolve and fetch; corporate-action evidence recorded, **or STOP** (Reliance 1:1 Oct 2024, HDFC Bank 1:1 Aug 2025) | Indicators, rules, persistence, any runtime |
| 2 | `indicators.py` + parity tests | Hand-computed fixtures exact; TradingView readings for ≥ 5 symbols within ±0.5 (K, D) and ±0.5% (EMA, ATR); ADANIENSOL 18 Sep 2026 K 4.64 / D 4.51 | Anything reading a database |
| 3 | `rules.py` + golden tests | Every section 14 golden case and every YES/NO row passes; a guard test proves `rules.py` imports no engine, runtime, broker, database or clock | I/O of any kind |
| 4a | **Persistence and paper accounting:** the runtime's own migration set in `runtimes/positional_stocks/migrations/` (all `stock_`-prefixed; nothing added to the shared directory, v1.2i), repository module, paper fill model (section 8: official open, costs, not-traded → next session + flag, `at_week_open` from the calendar for every buy), applying fills through the rules' fill function, clearing filled orders before deciding (v1.2h), carrying unfilled orders' sizing/sector/group across runs, persisting `half_sold_week` and each symbol's last-seen universe row, equity/peak/brake persistence, gap acknowledgements keyed per (symbol, session, ratio) with the 520-bar block window, idempotency per (strategy_id, week_ending), crash-safe transactions | Idempotency (same week twice = no change), crash-mid-run resume, fill-model and cost tests, a multi-week book simulated end to end through persistence, under `TZ=UTC` and `TZ=America/New_York` | CLI, fetching, report, Telegram, scheduling |
| 4b | **The weekly run:** `weekly_run.py` with `--mode fetch` and `--mode decide`, `--dry-run`, `--force-refetch` (limitation 40), catch-up, the offline-decide rules of 10.3, deadlines and throttle, the monthly full refetch, report writer (section 11, including the "partial sold 0" flag, held symbols with no bar this week, truncated-week warnings only for calendar-covered weeks), Telegram summary, journal CSV, `validate_environment` and paper-safety at start, a process lock | Offline-decide (Dhan client, scrip master and `AuthBootstrap` monkeypatched to raise), cold-cache, catch-up (3 missed weeks = 3 runs), a 12-week replay smoke on the real local cache, **first real preview report reviewed by the operator** | Scheduling, plists, dashboard |
| 5 | `config/runtimes/positional_stocks.yaml` **and** the strategy YAML in one commit; two `PlistSpec`s with `wait_policy="elapsed"` and their generated plists; installer handling so nothing is enabled silently; runbook; sweep of the stale "placeholder" lines in the architecture doc | Operator installs and enables the two agents | Dashboard page (needs its own approval); any `auto_start` / `RUNTIMES` change |

## 16. Decisions and remaining operator actions

All thirteen Phase 0 questions are answered in the v1.2 changes table at the top of this document. What is left:

| # | Item | Owner | When |
|---|---|---|---|
| 1 | Confirm the Dhan Data API subscription is active for `/v2/charts/historical` on this account | Operator | **Blocks Phase 1** |
| 2 | Verify the paper cost defaults (12 bps buy, 11 bps sell, ₹15 per sell) against Dhan's current equity delivery charges | Operator | Before Phase 4 |
| 3 | Supply TradingView K / D / EMA50 / ATR readings for ≥ 5 symbols at one weekly close | Operator | Phase 2 |
| 4 | Maintain `universe.csv` (NIFTY 200) and `quality_gate.csv` | Operator | From Phase 1, then weekly |
| 5 | Decide whether to fix `token_cache.py`'s timezone-less `expiry_time` and the stale `positional_options/__init__.py` docstring | Operator | Separate approval, outside this feature |
| 6 | When a held position is flagged FROZEN, resolve it (section 4.14): a corporate action → a row in `config/positional_stocks/corporate_actions.csv`; a real move of 15% or more → a line in `gap_acknowledgements.csv`. **Never acknowledge a gap you know is a corporate action** | Operator | Before the next decision run (the preview prints both lines); escalated after 2 weekly runs, exited under item 8 after 3 |
