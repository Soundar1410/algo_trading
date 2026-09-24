# WSR1 V1 golden cases — source text for Phase 3 tests

**What this is:** sections of the operator's "Weekly Stoch RSI V1 Trading Plan" (20 Sep 2026), exported verbatim so the Phase 3 golden tests can be written from the plan's own examples. Spec §14 refers to "the V1 plan's section 7 / section 8" — those are sections 7 and 8 below.

**Authority:** `WSR1_WEEKLY_STOCH_RSI_SPEC.md` wins on any conflict. Known, intentional differences — test the spec's value, not the plan's:

| Topic | Plan says | Spec (authoritative) |
|---|---|---|
| Max positions / committed cap / buffer | 8 / 80% / 20% | Paper book: 10 / 100% / 0% (spec 4.12). The plan's 8 / 20% is for any live version |
| Sector source | TradingView "Sector" | NSE Industry from `universe.csv` (spec decision v1.2 #7) |
| Drawdown brake 2 | "until you review the rules" | Until the operator sets `brake_2_cleared_on` (spec 4.12) |
| Order timing | Market order in the first 15 minutes | Paper fill at the official open of the execution session (spec 8) |
| Quality gate | Checked by hand from Screener | Read from `quality_gate.csv`; the code never judges it (spec 4.2) |
| Add touch after a recovery | Silent | A weekly close ≥ P1 clears a level's touch; the level must be touched again (spec 4.9 item 4) |
| Re-entry P&L | "Total P&L" | Total **net** P&L, after costs (spec 4.11) |
| Governance event on a held stock | "New event while holding = exit" | Operator records it as `FAIL` in `quality_gate.csv`, which exits (spec 4.2, v1.2f) |

Any other difference found while writing tests is a finding: report it, do not resolve it silently.

---

## Fixes to the V1 draft (resolutions the rules below already use)
- Every rule below uses these resolutions. Rows marked "untested" differ from the version in the study.
| # | Issue in the draft | V1 resolution |
|---|---|---|
| 1 | When does the 8-week trigger window start — first or latest oversold close? | From the latest weekly close with K & D both < 20. Armed = such a close this week or in the previous 7 weeks. Untested (study used 26 weeks from the first close; 99% of crosses came within 7 weeks either way). |
| 2 | "K crosses above D" — intraweek? Or just K > D? | Weekly closing values only: K > D this week AND K ≤ D last week, first cross since the latest oversold close, K < 50. |
| 3 | Three regimes (Green / Amber / Red), but Green and Amber act the same | Two regimes: Normal and Red. The "why is it weak?" news check applies to every candidate. |
| 4 | Time stop "unless it passes the full checklist" — vague, invites excuses | Unconditional: 52 weeks after T1 with no partial sale → sell all. Untested (study used 104 weeks). |
| 5 | Can both adds fill in the same fall? | One add per week. T3 needs a fresh touch of L2 after T2 is bought, then its own reversal week. |
| 6 | Can the touch and the reversal be the same week? | Yes — a week whose low ≤ the level and whose close > the prior week's high qualifies. |
| 7 | Stop after the partial sale — undefined | Replaced by the 10W EMA trail. Unfilled tranches cancelled; no more adds. |
| 8 | "Cyclicals: allow one loss year" — subjective | Removed. Profit after tax > 0 in each of the last 3 years, no exceptions. |
| 9 | "Guidance cut" — cannot be checked consistently | Fail if net profit AND revenue are both down YoY in each of the last 2 reported quarters. |
| 10 | "FII + DII not falling sharply" | Fail if combined FII + DII holding fell > 3 percentage points over the last 2 quarters. |
| 11 | Governance: exclusion and event-risk overlapped | Company-level event (fraud allegation, regulatory enforcement, auditor resignation or qualified opinion) in the last 12 months = fail. Company-level 12–36 months ago, or promoter-group-level in the last 36 months = event-risk. New event while holding = exit. |
| 12 | Sector cap "2 stocks AND 25%" — the 25% can never bind (2 × 10% = 20%) | Max 2 open positions per sector (TradingView "Sector" column). |
| 13 | "Second signal within 6 months at a lower price" — lower than what? | Lower than the close of the previous trigger week within the last 26 weeks (traded or not). |
| 14 | Committed capital after the partial sale — undefined | Committed = A until the partial sale; afterwards = cost of the shares still held. |
| 15 | Results-week rule — which week? | The Mon–Fri week in which you would place the order. Skipped entries are not carried forward. |
| 16 | Execution "limit near the open" — vague | Market order in the first 15 minutes of the first session after the weekly close. |
| 17 | Tie-breakers (volume, support, divergence) — not rules | Dropped. More valid candidates than slots → rank by (stock 6M % − NIFTY 6M %), highest first. |
| 18 | Relative strength — how to measure? | Screener "Perf 6M %" of the stock minus that of NIFTY 50 (6 months ≈ 26 weeks). |
| 19 | ATR and levels — which values? | ATR% at the trigger week's close; s fixed for the whole trade; levels from the actual T1 fill price P1. |
| 20 | "Sell 50%" of what? | Half of the shares held, rounded down. |
| 21 | New listings lack a 200W EMA and 3 years of results | Universe requires ≥ 200 weekly candles (~4 years). Untested. |


## 1. Final V1 rules
- Notation: C = strategy capital · B = 10% of C · K / D = Stoch RSI lines at the weekly close · P1 = tranche-1 fill price · s = spacing · A = allocation for one stock.
- All conditions use the completed weekly candle (last trading day of the week). All orders go in the first 15 minutes of the next session.
| # | Rule | Exact V1 value |
|---|---|---|
| 1 | Stock universe | NIFTY 200 constituents only · ≥ 200 weekly candles of price history · not already held · no 26-week cooling-off active |
| 2 | Quality filters | All must pass — see the quality gate table below. Event-risk stocks: half allocation, max 1 add |
| 3 | Market cap | NIFTY 200 membership (no separate number). Red regime: NIFTY 100 members only |
| 4 | Liquidity | 30-day average daily traded value ≥ ₹20 cr (average volume × price) |
| 5 | Market regime | NIFTY 50, 1W: close < 40W EMA AND 40W EMA lower than 4 weeks ago = Red → max 1 new entry per week, NIFTY 100 only. Anything else = Normal → max 2 new entries per week |
| 6 | Stoch RSI settings | Built-in Stochastic RSI · K 3 · D 3 · RSI length 14 · Stochastic length 14 · source close · 1W chart · bands 20 and 90 |
| 7 | Arming | A weekly close with K < 20 AND D < 20. Armed for that week + the next 7 weeks; every new such close restarts the 8 weeks |
| 8 | BUY trigger | While armed: K > D this week AND K ≤ D last week, first cross since the latest oversold close, K < 50 → buy T1 at the next session's open (market order) |
| 9 | 50W EMA / relative strength | Weekly close > 50W EMA OR (stock Perf 6M % − NIFTY 50 Perf 6M %) > 0 |
| 10 | 52-week high | Weekly close ≥ 0.60 × 52-week high (within 40%) |
| 11 | ATR | ATR% = weekly ATR(14) ÷ weekly close, at the trigger week. Must be ≤ 12% |
| 12 | Position sizing | s = max(10%, 1.5 × ATR%) · A = B × 10% ÷ s · event-risk: A × 0.5 · tranches T1 40% / T2 30% / T3 30% of A · shares = amount ÷ price, rounded down |
| 13 | Averaging levels | L1 = P1 × (1 − s) · L2 = P1 × (1 − 2s) · fixed at entry, never recalculated |
| 14 | Conditions before each add | ALL: (a) a weekly low ≤ the level since the previous buy · (b) a weekly close > the prior week's high (same week as the touch allowed) · (c) that close > stop, > 200W EMA and < P1 · (d) quality gate still passes · (e) no results in the order week · (f) no partial sale yet · max 1 add per week |
| 15 | Max averaging entries | 2 adds (3 tranches); event-risk 1 add (2 tranches) |
| 16 | Stop | Weekly close < P1 × (1 − 3s) → sell all at the next open. Active until the partial sale. Thesis exit: quality gate fails or a new company/group governance event → sell all at the next open |
| 17 | Profit-taking | Weekly close with K > 90 AND D > 90 → sell half the shares (rounded down) at the next open. Once per trade |
| 18 | Trailing exit | After the partial sale: weekly close < 10W EMA → sell the rest. Or 52 weeks after the partial sale → sell the rest |
| 19 | Time-based exit | 52 weeks after the T1 buy with no partial sale → sell all. No exceptions |
| 20 | Re-entry | Closed with total P&L ≥ 0 → eligible on the next fresh arm + trigger. Closed with P&L < 0 → 26-week cooling-off, then a fresh setup AND weekly close > 50W EMA (RS alone not enough). One open trade per stock |
| 21 | Portfolio limits | Max 8 open positions · committed ≤ 80% of C · max 1 stock per promoter group · strategy equity −10% from peak → no new entries for 4 weeks · −20% → no new entries until you review the rules · more candidates than slots → highest (stock 6M % − NIFTY 6M %) first |
| 22 | Sector limits | Max 2 open positions per sector (TradingView "Sector" classification) |
| 23 | Cash | 20% of C never used · reserved tranches held as cash or liquid fund, never in other stocks · enough cash in the trading account for next week's possible orders |

Quality gate (rule 2)
| Check | Non-financials | Banks / NBFCs |
|---|---|---|
| Profit after tax | > 0 in each of the last 3 financial years | Same |
| Growth | 3-year sales CAGR > 0 AND 3-year profit CAGR > 0 | Same (use net interest income or total income for sales) |
| Returns | Average ROCE of the last 3 years ≥ 12% | Average ROE of the last 3 years ≥ 12% |
| Balance sheet | Debt/equity ≤ 1 OR interest coverage ≥ 3× | Gross NPA % not higher than 4 quarters ago |
| Cash flow | Operating cash flow > 0 in ≥ 4 of the last 5 years | Not applied |
| Promoter | Pledge ≤ 10% and not higher than last quarter | Same |
| Institutions | FII + DII combined not down > 3 percentage points over the last 2 quarters | Same |
| Recent results | NOT (net profit AND revenue both down YoY in each of the last 2 quarters) | Same |
| Governance | No fraud allegation, regulatory enforcement, auditor resignation or qualified opinion involving the company in the last 12 months | Same |
| Event-risk flag | Such an event involving the company 12–36 months ago, or its promoter group in the last 36 months | Same |

- Where to check: Screener.in (annual and quarterly tables, ratios, shareholding) plus the company's exchange announcements.
- Committed capital = A for each open position until its partial sale; afterwards the cost of the shares still held. Event-risk positions commit only T1 + T2 (70% of their halved A).

---

## 5. ₹10 lakh portfolio example
- Even with all 8 slots filled, only ~30–40% of capital is actually invested at a time; the rest is reserved for adds or is buffer. That is the price of controlled averaging.
Limits (C = ₹10,00,000, B = ₹1,00,000)
| Item | V1 value |
|---|---|
| Max number of stocks | 8 |
| Max allocation per stock (A) | ₹1,00,000 when s = 10%; less for volatile stocks (₹83,333 at s = 12%, ₹55,556 at s = 18%); event-risk half of that |
| Initial amount (T1) | 40% of A → max ₹40,000 |
| Reserved for averaging | 60% of A → max ₹60,000 per stock |
| Max capital committed | ₹8,00,000 (80%) |
| Cash buffer, never used | ₹2,00,000 (20%) |
| Max sector exposure | 2 stocks → max ₹2,00,000 committed |
| Max loss per stock at the stop | ₹12,000 with T1 only · ~₹18,600 with T1 + T2 · ₹22,300–23,800 with all 3 (s from 10% to 18%) |
| Max portfolio loss | 8 × ₹22,300–23,800 ≈ ₹1.78–1.90L (17.8–19.0%) if every position fully averages and stops. Gaps can add more: budget up to ~₹2.4L (24%) |

- The max-loss case needs all 8 stocks to fill all 3 tranches and then stop. In the study only ~12% of trades filled all 3 tranches under confirmation-based adds.
A realistic snapshot a few months in (hypothetical stocks)
| Stock | Sector | P1 | ATR% | s | A | Status | Shares | Deployed | Reserved | Committed | Exit level | If exit hit now |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| A — large-cap bank | Finance | ₹1,000 | 6% | 10% | ₹1,00,000 | T1 only; now ₹1,060 | 40 | ₹40,000 | ₹60,000 | ₹1,00,000 | Stop ₹700 | −₹12,000 |
| B — NBFC | Finance | ₹2,400 | 8% | 12% | ₹83,333 | T1 + T2 (T2 at ₹2,150 after a reversal week); now ₹2,180 | 24 | ₹54,850 | ₹25,000 | ₹83,333 | Stop ₹1,536 | −₹17,986 |
| C — IT services | Technology | ₹560 | 7.5% | 11.25% | ₹88,889 | Half sold at ₹690 (31 shares); 32 trailing; now ₹705 | 32 | ₹17,920 | ₹0 (cancelled) | ₹17,920 | Trail: close < 10W EMA (~₹650) | +₹2,880 |
| D — utility, event-risk | Utilities | ₹3,200 | 9% | 13.5% | ₹37,037 (half) | T1 only; 1 add max; now ₹3,050 | 4 | ₹12,800 | ₹11,111 | ₹25,926 | Stop ₹1,904 | −₹5,184 |
| Total |  |  |  |  |  |  |  | ₹1,25,570 | ₹96,111 | ₹2,27,179 (22.7%) |  | −₹32,290 (3.2%) |

What this snapshot tells you
- Slots: 4 of 8 used → up to 4 more positions allowed.
- Committed ₹2.27L vs the ₹8L cap → ₹5.73L of room.
- Sectors: Finance is full (2 of 2) → a new bank or NBFC signal is skipped. Technology has 1 slot left.
- Realised so far: C's partial sale, 31 × (₹690 − ₹560) = +₹4,030.
- Cash ₹8,78,460 (buffer ₹2L + reserves ₹0.96L + free ₹5.82L); positions worth ₹1,29,480 → equity ₹10,07,940.
- Open risk if every stop / trail hit this week: −₹32,290 (3.2% of capital).
- Worst case if every remaining tranche filled and then stopped: A −₹22,300 · B −₹21,730 · C +₹2,880 · D −₹8,640 → −₹49,790 (5.0%).
- Keep in the trading account: ~₹1L for next week's possible entries and adds; the rest in a liquid fund.
How B's numbers come out
- A = ₹1L × 10% ÷ 12% = ₹83,333 → T1 ₹33,333 → 13 shares at ₹2,400 (₹31,200).
- L1 = ₹2,112 · L2 = ₹1,824 · stop = ₹1,536.
- Weekly low touched ₹2,112 → a later week closed above the prior high → T2 ₹25,000 → 11 shares at ₹2,150 (₹23,650).
- T3 (₹25,000) only if price touches ₹1,824 after the T2 buy and then gives another reversal week.
## 6. Averaging mechanism
- Adds are earned, not automatic: the price must touch the level AND show a weekly reversal. Falling alone never buys anything.
- Example: weekly ATR 6% → s = max(10%, 1.5 × 6% = 9%) = 10% → A = ₹1,00,000.
| Stage | Condition | Price example | Amount | Action |
|---|---|---|---|---|
| T1 | Armed (K & D < 20 within 8 weeks) + K crossed above D + all filters pass | ₹1,000 | ₹40,000 (40 shares) | Buy at the next open |
| T2 | Weekly low ≤ L1 ₹900 since T1, THEN a weekly close > prior week's high, with that close > ₹700, > 200W EMA, < ₹1,000; gate OK; no results week | Level ₹900; fill ~₹930 (next open after the reversal week) | ₹30,000 (32 shares = ₹29,760) | Buy |
| T3 | After T2: weekly low ≤ L2 ₹800, THEN a new reversal week with the same conditions | Level ₹800; fill ~₹820 | ₹30,000 (36 shares = ₹29,520) | Buy |
| Stop | Weekly close < ₹700 (before any partial sale) | e.g., Friday close ₹690 | — | Exit all at the next open |

- After all three: 108 shares, cost ₹99,280, average ₹919.26. Breakeven needs +12.1% from the ₹820 fill.
- Loss if stopped: −₹23,680 at ₹700; −₹24,760 at ₹690.
- Event-risk stock: A halved; T1 40% + T2 30% only; T3 never.
Scenarios (same ₹1,000 example)
| Scenario | What you do | Result |
|---|---|---|
| Price falls but gives no reversal confirmation | No add. Hold T1; only the stop / thesis / time rules apply | If it reaches the stop without a reversal week: exit with T1 only → −₹12,000 (40 × ₹300) |
| Price hits the level and immediately falls further | No add while it keeps falling. The first reversal week (even below ₹800) buys T2 only. T3 then needs a fresh touch of ₹800 after the T2 buy plus another reversal week | If no reversal comes before the stop: −₹12,000 instead of the −₹22,300 a fully averaged position would lose |
| Price never reaches the second level | T3 is never bought; its ₹30,000 stays reserved until the partial sale or the exit | Normal and good outcome |
| Price gaps below the stop | Only the weekly close counts. Intraweek gap but Friday close ≥ ₹700 → no action. Friday close < ₹700 → sell all at the next open, at whatever price | Monday open ₹640: T1 only −₹14,400; fully averaged −₹30,160. No waiting for a bounce |
| Price goes up immediately after T1 | Hold 40 shares. Never add above P1. The ₹60,000 reserve stays reserved until the partial sale | At K & D > 90: sell 20 shares; reserve released; trail the other 20 |
| Price triggers T2 and then recovers | Hold 72 shares (average ₹968.89). T3 is still possible until the partial sale, but only via a fresh touch of ₹800 plus a reversal week. Stop stays ₹700 | At K & D > 90: sell 36 shares; T3 cancelled; trail the other 36 |
| Price triggers all three tranches | Fully invested (108 shares). No more buying | Exits only: stop ₹700 (−₹23,680), thesis, 52 weeks, or K & D > 90 → sell 54, trail 54 |

## 7. Stochastic RSI entry
- Your understanding is correct, with three precisions: (1) only weekly closing values count; (2) the cross must be fresh — K was ≤ D the week before; (3) the cross must come within 8 weekly closes of the latest close with both lines below 20. The cross itself may happen above 20.
```
ARMED   = K < 20 AND D < 20 on this week's close or any of the previous 7 weekly closes
TRIGGER = ARMED AND K(this week) > D(this week) AND K(last week) ≤ D(last week) AND first cross since the latest oversold close AND K < 50
BUY     = TRIGGER AND every filter passes  →  T1 at the next session's open
```
| Situation | Decision | Reason |
|---|---|---|
| K and D below 20, but K has not crossed above D | NO (not yet) | Keep it in V1 Armed; check again next weekend |
| K crosses above D, but K and D were never both below 20 in the last 8 weekly closes | NO | Not armed |
| Cross comes 10 weeks after the stock first became oversold, and K & D were both below 20 on at least one of the last 8 closes | YES | The window restarts on every oversold close |
| Cross comes 10 weeks after the first oversold close, and the latest both-below-20 close was more than 7 weeks ago | NO | Window expired; wait for a new oversold close |
| Another oversold signal within 6 months, stock not held, no cooling-off, this week's close ≥ the previous trigger's close | YES | Normal rules |
| Another oversold signal within 6 months, this week's close < the previous trigger's close | YES only if this week also closed above the prior week's high; otherwise NO | Lower lows = downtrend risk |
| Another signal while you still hold the stock | NO | One open trade per stock |
| Another signal within 26 weeks of a loss exit | NO | Cooling-off |
| Price below the 200W EMA, all other filters pass | Entry YES · adds NO while the weekly close is below the 200W EMA | The 200W EMA limits adds, not entries |
| Passes the Stoch RSI rules but fails the quality filter | NO | No exceptions, no "small starter position" |
| K crossed above D midweek but K ≤ D at Friday's close | NO | Weekly close only |
| K = D exactly at the close | NO | Needs K > D |
| Cross happens with K at 28 (above 20) while armed | YES | Arming needs < 20; the cross does not |
| Valid trigger, but results are due in the order week | NO | Not carried forward; wait for the next setup |

## 8. Exit
- Your summary is correct. Precisely: one partial sale per trade, triggered by the weekly close; after it the 10W EMA trail replaces the stop; the 52-week time exit counts from the T1 buy date.
| Question | Answer |
|---|---|
| Is the 90 condition based on the weekly close? | Yes. Only K and D at the weekly close count; intraweek readings are ignored |
| K and D crossed above 90 and fell back before I sold? | If the weekly close showed K > 90 AND D > 90, sell half at the next open, whatever Monday's reading is. If they were above 90 midweek but either was ≤ 90 at the close → no sale |
| Only K above 90, D not? | No sale. Both are required |
| Do I sell exactly on Monday? | Yes: market order in the first 15 minutes of the first session after the weekly close (Tuesday if Monday is a holiday). Missed it? Sell at the next session — never wait for a better price |
| After selling 50%, can I add again? | No. Unfilled tranches are cancelled, the reserve is released, and no new trade in this stock until the position is fully closed |
| Does the price stop still apply after the partial sale? | No. The 10W EMA trail replaces it. The thesis exit still applies |
| The stock keeps rising strongly? | Hold the remaining half while every weekly close is ≥ the 10W EMA. No second sale at 90, no price target. Hard limit: 52 weeks after the partial sale |
| Weekly close exactly equal to the 10W EMA? | Hold. Exit only on a close below it |
| K & D > 90 while the position is below its average cost? | Still sell half. The rule is about momentum, not your cost |
| Odd number of shares? | Sell half rounded down (73 held → sell 36, keep 37) |
| When does the 52-week time exit apply? | 52 weeks after the T1 buy date, only if no partial sale has happened. No exceptions |
| When is the position completely closed? | When the last share is sold via: stop, thesis exit, time exit, trail exit, or 52 weeks after the partial sale |
| What happens after it is closed? | Total P&L ≥ 0 → eligible on the next fresh arm + trigger. P&L < 0 → 26-week cooling-off, then a fresh setup AND a weekly close above the 50W EMA |
