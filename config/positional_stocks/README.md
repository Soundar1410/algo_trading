# `positional_stocks` operator inputs

The CSVs that `wsr1_weekly_stochrsi` reads and never writes. Spec sections
4.14 and 6.1–6.5 of `strategies/positional_stocks/wsr1_weekly_stochrsi/WSR1_WEEKLY_STOCH_RSI_SPEC.md`
are the contract; `strategies/positional_stocks/wsr1_weekly_stochrsi/inputs.py`
enforces it and **fails closed** on any deviation — a renamed column, a
duplicated symbol or a non-ISO date stops the run rather than being guessed at.

These files are not strategy configuration. They live outside `config/strategies/`
and `config/runtimes/` deliberately: `common/config/loader.py`'s
`resolve_runtime_strategies` scans `config/strategies/**` and raises for a
strategy whose runtime file is missing, so a file placed there before Phase 5
would stop `intraday_options` and `positional_options` from starting at all.

## `universe.csv` (spec 6.3)

`symbol, isin, company, industry, nifty100, group, on_exit, as_of`

Seeded with the 200 NIFTY 200 constituents as of **2026-07-22**, taken from an
NSE constituent list. All 200 resolve against the Dhan scrip master.

**Two columns are unfilled and need the operator before Phase 4:**

- `nifty100` — blank, which the loader reads as `false`. That is the
  fail-closed reading: spec 4.3 restricts Red-regime entries to `nifty100`
  symbols, so an unfilled row is simply never eligible in Red. The loader warns
  with a count, and `UniverseFile.symbols_missing_nifty100` carries the list.
- `group` — blank means "its own promoter group" (spec 6.3), which is correct
  for a standalone company and wrong for, say, the Adani or Tata names. Until
  it is filled, the max-1-position-per-promoter-group limit of spec 4.12 binds
  only per symbol.

`as_of` is 2026-07-22, which **pre-dates the end-September NIFTY 200
reconstitution**. Refresh the file before relying on membership.

`on_exit` is `hold` for every row: a held symbol later removed from the file
exits as normal and takes no further adds.

## `quality_gate.csv` (spec 6.4)

`symbol, status, checked_on, valid_until, notes`

Shipped **header-only**, which is the correct fail-closed starting state: spec
4.2 allows an entry only against a row whose `status` is `PASS` or
`EVENT_RISK` and whose `valid_until` is on or after the execution date, so an
empty gate means no symbol may be entered. Missing, expired and `FAIL` are all
treated identically — no entry, no add, listed under "needs quality check".

The operator fills this between the Friday fetch/preview run and the Monday
decision run, for the candidates the preview report lists. That weekend window
is why spec 10.3 splits the job in two.

**Operator procedure: a governance event on a held stock (spec 4.2, v1.2f).**
When a new company-level or promoter-group-level governance event hits a symbol
you **hold**, set its row to `FAIL`. That is the only way to trigger the thesis
exit: the rules sell the whole position at the next open. Once the position is
closed you may change the row to `EVENT_RISK`, if that is the right status for
a stock you do not hold. The file has one status per symbol, so it cannot say
"exit if held, event-risk if not" (the V1 plan's rule); this procedure does.

What the rules do with each status on a **held** position:

- `FAIL`: thesis exit, even if the row has **expired**.
- `EVENT_RISK` on a position entered as `PASS`: the position keeps its sizing,
  but its third tranche (T3) is disabled from then on.
- An expired `PASS` or `EVENT_RISK` row, or no row at all: no further adds, but
  no exit.

## `results_calendar.csv` (spec 6.5)

`symbol, results_date`

Shipped header-only and **optional in paper mode** (v1.2 decision 7). A symbol
with no row is flagged "results date unknown" and still proceeds; a live mode
must fail closed instead, which `load_results_calendar(required=True)` does.
Several rows per symbol are expected across a year; only an exact repeat of the
same symbol and date is rejected.

## `gap_acknowledgements.csv` (spec 6.1)

`symbol, gap_session, ratio, acknowledged_on, note`

A close-to-close move of **30% or more** in a symbol's daily history is an
unexplained gap. Usually it is a corporate action Dhan failed to back-adjust
(MOTHERSON, runbook D94). It blocks new entries in that symbol until you
acknowledge it here. A move of 15–30% is only reported.

- **Keyed, not blanket.** A row covers exactly one gap: the symbol, the session
  the gap closed on, and the ratio (close ÷ previous close, matched to 4
  decimals). A new gap, or Dhan restating the same session to a different
  ratio, blocks again.
- **Only recent gaps block.** Only gaps in the most recent 520 weekly bars
  (about 10 years) block. Older ones are reported, never blocking (spec 6.1
  v1.2e).
- Shipped **header-only**. Nothing is acknowledged by default, and MOTHERSON
  stays blocked (operator decision, 23 September 2026). The loader fails
  closed on a malformed row, an ambiguous date, or the same gap acknowledged
  twice.

## `corporate_actions.csv` (spec 4.14)

`symbol, ex_session, kind, ratio, confirmed_on, note`

Dhan back-adjusts history for bonuses, splits and (sometimes) demergers. A
held position, though, keeps its fill prices, P1, L1, L2, Stop and share count
in the units of its fills. Every decision run compares each fill of each open
position with the cached open of that session. When they differ by more than
0.5%, the history was restated and the position is **frozen**:
- no exit, add or partial decision is made for it;
- it still counts toward every limit;
- it is marked at close ÷ f in its own units, so equity and the brakes see no
  false drop;
- it is flagged in the report.

After more than 2 weekly runs frozen it is escalated as an operator action.
Confirm the action here, within 2 weekly runs of the flag:

| `kind` | `ratio` | Examples |
|---|---|---|
| `BONUS_SPLIT` | new shares per old share (> 1) | 1:1 bonus → `2`; 1:2 bonus → `1.5`; 1:10 split → `10` |
| `DEMERGER` | the price factor Dhan applied (between 0 and 1) | `0.90` |

- **`DEMERGER` covers any price-only restatement**, for example a special
  dividend Dhan adjusts for. The share count is unchanged and the value
  removed is credited as cash.
- `ex_session` is the first session in the new units. A row is applied only
  if its factor matches the detected one within 0.5% (BONUS_SPLIT: 1 ÷ ratio;
  DEMERGER: ratio). Every fill before `ex_session` must be restated and every
  fill on or after it must not be. Otherwise the position stays frozen and the
  mismatch is reported.
- **What a rescale does:**
  - BONUS_SPLIT: shares × ratio, floored, with the fraction paid as cash at the
    adjusted pre-ex close. Prices and levels ÷ ratio; rupee cost unchanged.
  - DEMERGER: prices and levels × ratio; shares unchanged. The credit is
    shares × actual pre-ex close × (1 − ratio).
  - Either way, a pending sell decided before the restatement is re-issued in
    the new units for the next session.
  - The rescale is stored as its own record and the original fills are never
    edited. A trade's P&L includes the cash the action paid.
- Shipped **header-only**. The loader fails closed on a missing file, a wrong
  header, an unknown kind, a ratio out of range, a bad date, or the same
  (symbol, ex_session) twice.
- **Known limit:** two actions stacked on one open position (both unconfirmed
  at once) cannot be resolved by one row. The position stays frozen and
  escalates.
