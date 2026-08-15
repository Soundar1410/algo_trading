"""Runtime-id -> entrypoint registry, shared by ``start_runtime.py`` and
``start_strategy.py``.

Before this module, both scripts imported ``runtimes.intraday_options.
__main__.main`` unconditionally and called it with whatever ``--runtime-id``
the operator passed — harmless while ``intraday_options`` was the only real
runtime (its own ``main()`` re-validates ``--runtime-id`` against
``config/runtimes/<id>.yaml``, so a typo still failed loudly), but wrong the
moment a second runtime with its own composition root existed:
``scripts.start_runtime positional_options`` would drive
``positional_options``'s strategies through *intraday's* worker/engine
wiring, admitting them under the wrong supervisor entirely. This registry is
the fix — one dict, so a third runtime group is one entry, not a new branch
copied into two scripts.
"""

from __future__ import annotations

from collections.abc import Callable

import runtimes.intraday_options.__main__ as _intraday_main
import runtimes.positional_options.__main__ as _positional_main

#: runtime_id -> that runtime's own ``main(argv) -> int`` entrypoint.
ENTRYPOINTS: dict[str, Callable[[list[str] | None], int]] = {
    "intraday_options": _intraday_main.main,
    "positional_options": _positional_main.main,
}


def resolve_entrypoint(runtime_id: str) -> Callable[[list[str] | None], int]:
    """The ``main()`` this ``runtime_id`` actually runs under.

    Raises :class:`KeyError` with every known id listed, rather than
    silently falling back to ``intraday_options``'s — the exact mistake
    this module exists to close.
    """
    try:
        return ENTRYPOINTS[runtime_id]
    except KeyError:
        raise KeyError(
            f"Unknown runtime_id {runtime_id!r}. Known runtimes: {sorted(ENTRYPOINTS)}."
        ) from None
