# Project rules for Claude Code

- The single source of truth is docs/ALGO_TRADING_FORWARD_TESTING_ARCHITECTURE_FINAL.md.
- Work strictly phase by phase. After each phase, STOP and wait for my review. Never jump ahead.
- Use Plan Mode: show the plan before writing any file.
- PAPER MODE ONLY for operational activation. **Phase 10 controlled-live CODE infrastructure is hardened and remains fully disabled.** The production entrypoint runs broker-authoritative mode-transition reads plus parent admission preflight; a spawned live worker independently forces fresh token/account/static-IP/database/connectivity/confirmation preflight, starts the live order-update stream, idempotently recovers broker-confirmed orders/trades, rebuilds broker-authoritative account state, continuously enforces the account loss emergency latch, and requires a final broker-flat reconciliation before clean shutdown. `DhanLiveBroker`, account-wide reservation/MTM/rate limiting, reconciliation, migration checksum/restore validation, the audited confirmation issue/revoke workflow, and the one-live-worker controlled-rollout lease are tested only with deterministic mocks/fakes (no real Dhan network or order-placement call was made). No production `EgressIpProvider` is shipped: configuration resolves an operator-approved `module:attribute` plugin and missing/invalid providers fail closed. Every committed config value separately keeps every live gate disabled (`global.live_trading_enabled: false`, `live_execution_allowed: false`, `live_approved: false`, no `mode: live` in committed YAML — enforced by `scripts/assert_no_live_config_committed.py`). **`OPERATIONAL LIVE ACTIVATION ELIGIBLE` remains NO — BLOCKED**: the 30-day paper evaluation, a separately specified second real paper strategy, EMA-specific minimum-quantity approval, static-IP/provider setup, live auth revalidation, and separate approval to flip gates remain outstanding. Do not flip a committed live gate or add/choose a production `EgressIpProvider` without that separate approval.
- Real strategies, all `enabled: true` and all `mode: paper` as of 8 September
  2026 — `intraday_options`: `c921_ema_cross_buy`, `c509_ema_cross_buy`,
  `c521_ema_cross_buy`, `st12_supertrend_buy`, `st05_supertrend_buy`,
  `straddle_920`, `rolling_strangle_otm1`; `positional_options`:
  `weekly_delta_neutral`. (`skeleton_fixture` is disabled and not real.)
  Verify against `config/strategies/**` rather than trusting this list — it has
  gone stale before.
- Two rename events, both forced by the same defect: `common.execution.
  correlation.strategy_token()` truncates a sanitised `strategy_id` to 4
  characters, and the supervisor refuses to admit two strategies whose tokens
  collide, so at most one of a colliding family could ever run in the
  `intraday_options` group at a time. On 31 August 2026
  `ema_cross_9_21_buy`/`ema_cross_5_9_buy`/`ema_cross_5_21_buy` (all `emac`)
  became `c921`/`c509`/`c521_ema_cross_buy`; on 8 September 2026
  `supertrend_buy_1_1p2` (`supe`) became `st12_supertrend_buy` so the new
  `st05_supertrend_buy` variant could coexist. Put the distinguishing part
  FIRST in any new strategy id, and check `strategy_token()` pairwise against
  every committed id before adding one —
  `tests/unit/test_st05_supertrend_buy_config.py` does this automatically
  against the real config tree. Do not modify `correlation.py` or
  `STRATEGY_TOKEN_LENGTH`; Dhan's 25-character correlation-ID limit leaves no
  room. See docs/IMPLEMENTATION_STATUS_AND_RUNBOOK.md for both root causes and
  the cutover sequencing (rename only with a flat book and the supervisor
  stopped — recovery filters strictly by the current `strategy_id`).
- Additional real strategies require separate approval per strategy. Live
  order placement stays fail-closed until Phase 10.
- Reuse Trading_Automation engines/policies read-only. Port their regression tests BEFORE changing internals. Never create a runtime dependency on that repo. Never copy its secrets, DBs, tokens, or logs.
- Never print, commit, or paste real Dhan/Telegram secrets. Secrets live only in .env (gitignored).
- **Every git command must pass `-C /Volumes/Trading/algo_trading` explicitly** — `git -C /Volumes/Trading/algo_trading status`, never a bare `git status`. Do not rely on the shell's current working directory. A `cd` from an earlier step persists across commands, and a read-only check in another tree (e.g. the `Trading_Automation` newest-`.py` mtime verification) leaves the shell there. A bare `git status`/`git add -A`/`git commit -a` then runs against **that** repository, which for the reference tree means committing files this project must never write. This is not hypothetical: it happened in Phase 3 Part 2b-ii-A and was caught only by reading the file list. Confirm with `git -C /Volumes/Trading/algo_trading rev-parse --show-toplevel` before any staging or commit.
- Pin dhanhq explicitly after a compatibility spike; do not use a loose version range. **Resolved in Phase 2: the pin is `2.2.0`.** The spike this rule required rejected 2.1.0 on three verified grounds — it is yanked on PyPI ("Breaking changes"), its `subscribe_symbols` reads a `ws.closed` attribute that no longer exists on `websockets>=14` (so resubscription raises), and its `disconnect()` never closes the socket. The tick/quote payload builders are byte-identical across the two versions. Evidence in the runbook, section 4. Do not revert to 2.1.0 without a new spike.
- Do not weaken or skip tests to make them pass. Run tests, lint (ruff), and type-check (mypy) each phase.
- Keep docs/IMPLEMENTATION_STATUS_AND_RUNBOOK.md updated after every phase.
