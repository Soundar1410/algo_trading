# WSR1 Weekly Stoch RSI — `algo_trading` Implementation Specification

**Strategy ID:** `wsr1_weekly_stochrsi` (correlation token `wsr1` — unique against every committed id)
**Runtime ID:** `positional_stocks` (new)
**Engine kind:** `stock_portfolio_engine` (the existing, reserved `EngineKind.STOCK_PORTFOLIO_ENGINE`; no new enum value)
**Execution shape:** a run-to-completion weekly job — no tick feed, no long-lived worker, no intraday decisions
**Initial mode:** paper only
**Status:** implementation specification v1.2g — Phase 3 review fixes (pending orders, boundaries, brakes, 1-share partial)
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
| 5 | Persistence decided: new `positional_stocks.db`, new tables in migrations 0016+, options cycle tables untouched | `strategy_cycles` cannot hold several open equity positions per strategy (`idx_one_open_cycle`), and its expiry key has no equity meaning |
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
- **Committed with EVENT_RISK (v1.2g):** a position whose row turns EVENT_RISK after T3 has filled still commits its full A; T3 is disabled only while it is unfilled.

### 4.13 Order of evaluation within one weekly run

1. Execute pending orders from the previous decision week at this week's first-session open (section 8).
2. Mark to market at this week's close; update equity, peak and brake state.
3. Evaluate exits and adds for open positions (4.10, 4.9).
4. Evaluate new entries (4.5–4.8) with the slots, cash and limits left after step 3's decisions. (v1.2f) A decided SELL_ALL frees its slot, sector, group and committed amount for this week's entries; a decided SELL_HALF recomputes committed as the cost of the shares that will remain. **Sale proceeds are not counted as cash until filled**, and each planned buy reserves its amount plus buy costs.
5. Persist decisions as pending orders for the next session; write the report.

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
- Any symbol whose adjusted series shows an unexplained overnight gap of 30% or more is flagged and skipped for new entries until the operator acknowledges it.
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
| Costs | Configurable: `cost_bps_buy` (default 12), `cost_bps_sell` (default 11), `fixed_cost_per_sell_rs` (default 15). Defaults approximate STT + stamp duty + exchange charges + DP charge for equity delivery at a zero-brokerage broker. **Operator to verify against Dhan's current charges** |
| Taxes | Not modelled |
| Gaps through the stop | No special handling. The exit fills at the open, whatever the price, and the report shows the planned vs actual loss |

---

## 9. State and persistence

- Database: `data/operational/positional_stocks.db`, created by the existing `Database` / `MigrationRunner`, with **additive, replay-safe migrations** (`CREATE ... IF NOT EXISTS`), following the repository pattern.
- **Do not alter the options cycle tables** (`strategy_cycles` has options-only semantics, such as `resolved_expiry_date NOT NULL`).
- **Decision (v1.1): new equity tables, not a generalised cycle model.** `strategy_cycles` allows at most one open cycle per strategy (`idx_one_open_cycle`), keys identity on an expiry, and restricts `strategy_cycle_legs.leg_role` to option roles; widening any of these means rebuilding tables `weekly_delta_neutral` depends on. Phase 0 confirms this against the code and records it as a D-entry.
- **Naming rule: every new table is prefixed `stock_`.** The shared `MigrationRunner` applies every file in `common/persistence/migrations/versions/` to every runtime database, so:
    - `positional_stocks.db` also receives migrations 0001–0015, including the existing `positions`, `fills` and `signals` tables;
    - an unprefixed `CREATE TABLE IF NOT EXISTS positions` would silently keep the options-shaped table and the equity code would write to the wrong schema;
    - the new 0016+ tables will also appear (empty) in `intraday_options.db` and `positional_options.db`. That is the existing model and is accepted; the prefix keeps them unambiguous.
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
2. Determine the weeks to process: every completed week after the last completed run, in order, up to `--as-of`. If there is no prior run, process only the latest completed week (no backfill of trades).
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
| 4 | Runtime: `weekly_run.py` with both modes, migrations 0016+ (all `stock_`-prefixed), repository module, report writer, Telegram, catch-up | Idempotency, catch-up, crash-resume, offline-decide, cold-cache and 12-week replay tests pass, under `TZ=UTC` and `TZ=America/New_York`; first real preview report reviewed by the operator | Scheduling, plists, dashboard |
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
