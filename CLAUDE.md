# Project rules for Claude Code

- The single source of truth is docs/ALGO_TRADING_FORWARD_TESTING_ARCHITECTURE_FINAL.md.
- Work strictly phase by phase. After each phase, STOP and wait for my review. Never jump ahead.
- Use Plan Mode: show the plan before writing any file.
- PAPER MODE ONLY. Do not implement DhanLiveBroker order placement or anything in the controlled-live phase (Phase 10). Live stays fail-closed.
- Do NOT implement real trading strategies yet (Phase 9). Use the deterministic test-only signal fixture.
- Reuse Trading_Automation engines/policies read-only. Port their regression tests BEFORE changing internals. Never create a runtime dependency on that repo. Never copy its secrets, DBs, tokens, or logs.
- Never print, commit, or paste real Dhan/Telegram secrets. Secrets live only in .env (gitignored).
- **Every git command must pass `-C /Volumes/Trading/algo_trading` explicitly** — `git -C /Volumes/Trading/algo_trading status`, never a bare `git status`. Do not rely on the shell's current working directory. A `cd` from an earlier step persists across commands, and a read-only check in another tree (e.g. the `Trading_Automation` newest-`.py` mtime verification) leaves the shell there. A bare `git status`/`git add -A`/`git commit -a` then runs against **that** repository, which for the reference tree means committing files this project must never write. This is not hypothetical: it happened in Phase 3 Part 2b-ii-A and was caught only by reading the file list. Confirm with `git -C /Volumes/Trading/algo_trading rev-parse --show-toplevel` before any staging or commit.
- Pin dhanhq explicitly after a compatibility spike; do not use a loose version range. **Resolved in Phase 2: the pin is `2.2.0`.** The spike this rule required rejected 2.1.0 on three verified grounds — it is yanked on PyPI ("Breaking changes"), its `subscribe_symbols` reads a `ws.closed` attribute that no longer exists on `websockets>=14` (so resubscription raises), and its `disconnect()` never closes the socket. The tick/quote payload builders are byte-identical across the two versions. Evidence in the runbook, section 4. Do not revert to 2.1.0 without a new spike.
- Do not weaken or skip tests to make them pass. Run tests, lint (ruff), and type-check (mypy) each phase.
- Keep docs/IMPLEMENTATION_STATUS_AND_RUNBOOK.md updated after every phase.