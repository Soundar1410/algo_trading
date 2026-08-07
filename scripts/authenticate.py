#!/usr/bin/env python3
"""``scripts/authenticate`` — the name spec section 11 gives this command.

    .venv/bin/python -m scripts.authenticate [--force] [--status] [--refresh]

A pure alias for ``scripts/auth_bootstrap.py``, which keeps its own name and
its own tests unchanged (Phase 7 Part 4: "rename/alias ... keeping the old
entry point working"). Nothing here is reimplemented — see that module's
docstring for what this command actually does and why it is read-only.
"""

from __future__ import annotations

import sys

from scripts.auth_bootstrap import (
    EXIT_COOLDOWN,
    EXIT_FAILED,
    EXIT_NO_CREDENTIALS,
    EXIT_OK,
    main,
)

__all__ = ["EXIT_COOLDOWN", "EXIT_FAILED", "EXIT_NO_CREDENTIALS", "EXIT_OK", "main"]

if __name__ == "__main__":
    sys.exit(main())
