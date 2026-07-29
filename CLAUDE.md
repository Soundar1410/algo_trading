# Project rules for Claude Code

- The single source of truth is docs/ALGO_TRADING_FORWARD_TESTING_ARCHITECTURE_FINAL.md.
- Work strictly phase by phase. After each phase, STOP and wait for my review. Never jump ahead.
- Use Plan Mode: show the plan before writing any file.
- PAPER MODE ONLY. Do not implement DhanLiveBroker order placement or anything in the controlled-live phase (Phase 10). Live stays fail-closed.
- Do NOT implement real trading strategies yet (Phase 9). Use the deterministic test-only signal fixture.
- Reuse Trading_Automation engines/policies read-only. Port their regression tests BEFORE changing internals. Never create a runtime dependency on that repo. Never copy its secrets, DBs, tokens, or logs.
- Never print, commit, or paste real Dhan/Telegram secrets. Secrets live only in .env (gitignored).
- Pin dhanhq explicitly after a compatibility spike. Default to the stable 2.1.0 unless a feature in 2.2.0 is required; do not use a loose version range.
- Do not weaken or skip tests to make them pass. Run tests, lint (ruff), and type-check (mypy) each phase.
- Keep docs/IMPLEMENTATION_STATUS_AND_RUNBOOK.md updated after every phase.