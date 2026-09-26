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
- **Held positions (spec 4.14 item 7, v1.2k).** For a symbol already held, a
  gap of **15%** or more (either direction) after its first fill freezes the
  position until it is acknowledged here or Dhan back-adjusts it (see
  `corporate_actions.csv`). Acknowledge only a **real price move**. Never
  acknowledge a gap you know is a bonus, split or consolidation: that resumes
  decisions on mismatched units (a false stop). Add a `corporate_actions.csv`
  row instead.

## `corporate_actions.csv` (spec 4.14)

`symbol, ex_session, kind, ratio, confirmed_on, note`

Dhan back-adjusts history for bonuses, splits and (sometimes) demergers. A
held position, though, keeps its fill prices, P1, L1, L2, Stop and share count
in the units of its fills. Every decision run checks each open position two
ways, and **freezes** it when either finds a problem:

- **Restated history (item 1).** Each fill is compared with the cached open of
  its session. More than 0.5% apart means Dhan restated the history, by a
  factor f.
- **Unadjusted action (item 7, v1.2k).** An unacknowledged close-to-close gap
  of 15% or more, in either direction, on a session after the position's
  first fill. This is a bonus, split or consolidation Dhan has **not yet**
  back-adjusted, or a real move you have not acknowledged. A gap on or before
  the T1 fill session never counts.

A frozen position:
- gets no exit, add or partial decision;
- has its pending orders held;
- still counts toward every limit;
- is marked at close ÷ f (or ÷ the gap ratio) in its own units, so equity and
  the brakes see no false drop;
- is flagged in the report.

After more than 2 weekly runs frozen it is escalated as an operator action.
Confirm the action here, within 2 weekly runs of the flag:

| `kind` | `ratio` | Examples |
|---|---|---|
| `BONUS_SPLIT` | new shares per old share (positive, not 1) | 1:1 bonus → `2`; 1:2 bonus → `1.5`; 1:10 split → `10`; **10:1 consolidation → `0.1`** |
| `DEMERGER` | the price factor Dhan applied (between 0 and 1) | `0.90` |
| `PRICE_CORRECTION` | the price factor (positive, not 1) | `0.99` (a Dhan data correction) |

- **`DEMERGER` vs `PRICE_CORRECTION`.** `DEMERGER` covers price-only
  restatements that carry value, for example a special dividend Dhan adjusts
  for: the value removed is credited as cash. `PRICE_CORRECTION` covers those
  that do not, such as a Dhan data correction, and **no cash is credited**.
  Confirming a data correction as `DEMERGER` would credit cash that never
  existed.
- `ex_session` is the first session in the new units. A row is applied only
  when **Dhan has restated the history**, and only if its factor matches the
  detected one within 0.5% (BONUS_SPLIT: 1 ÷ ratio; the others: ratio). Every
  fill before `ex_session` must be restated and every fill on or after it must
  not be. Otherwise the position stays frozen and the mismatch is reported.
  You may add the row before Dhan restates: until then the report says "CSV
  row present; waiting for Dhan restatement".
- **What a rescale does:**
  - BONUS_SPLIT: shares × ratio, floored, with the fraction paid as cash at the
    adjusted pre-ex close. Prices and levels ÷ ratio; rupee cost unchanged. A
    consolidation that floors the holding to **0 shares** pays it all as cash
    in lieu and closes the position as a normal exit at that price, dated to
    the ex session's week, with no sell costs (D101).
  - DEMERGER: prices and levels × ratio; shares unchanged. The credit is
    shares × actual pre-ex close × (1 − ratio).
  - PRICE_CORRECTION: as DEMERGER, with no credit.
  - A pending sell decided before the restatement is re-issued in the new
    units. It keeps its original execution session, so it fills at that
    session's restated open, flagged `late_fill` (v1.2k).
  - The rescale is stored as its own record and the original fills are never
    edited. A trade's P&L includes the cash the action paid.
- **Stuck freeze (D102).** Dhan's back-adjustment can be missing, or partial:
  MOTHERSON's 1:2 bonus was adjusted back only to 30 Apr 2024. Then a row can
  never apply, and the position would stay frozen forever, holding a slot
  with no stop able to fire. So after 3 or more consecutive frozen runs, if a
  row here matches the freeze factor within 0.5% but cannot be applied, the
  position is exited:
  - a SELL_ALL of its stored shares at the next session's open ÷ the factor
    (its own units), with normal sell costs;
  - reason "exit: corporate action not adjusted by Dhan".

  With no matching row, it stays frozen and escalated.
- **Never acknowledge a gap you know is a bonus, split or consolidation.** An
  entry in `gap_acknowledgements.csv` means "this was a real price move". It
  lifts the freeze and resumes decisions on mismatched units, which gives a
  false stop. Add a row here instead.
- Shipped **header-only**. The loader fails closed on a missing file, a wrong
  header, an unknown kind, a ratio out of range, a bad date, or the same
  (symbol, ex_session) twice.
- **Known limit:** two actions stacked on one open position (both unconfirmed
  at once) cannot be resolved by one row. The position stays frozen and
  escalates, and then exits under D102 only if a row explains the combined
  factor.
