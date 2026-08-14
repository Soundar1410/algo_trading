"""Read-only Streamlit page — Positional Options (not implemented yet).

No supervisor, no worker and no database exist for this runtime yet —
``runtimes/positional_options/__init__.py`` is inert scaffolding (see the
runbook's D56/D69 for the open persistence-identity question a real
positional worker will need to answer before it can be built). Every tab
says so plainly, in the tab's own terms, rather than rendering one flat
page or an empty panel that could be mistaken for "no positions today" when
the true state is "this does not run at all".

No side effects: no database is opened, because there is none to open. The
read-model interface these tabs are built against lives in
``dashboards/data/positional.py`` — real dataclasses, ready for a real
positional worker's persistence to satisfy, not queried today.

**The strategy selector is wired in today, deliberately showing nothing.**
``discover_strategy_options`` is called with ``conn=None`` (no database)
*and* ``config_root=None`` — a strategy configured under
``config/strategies/*.yaml`` cannot be reliably attributed to this runtime
(no real per-runtime membership mechanism exists yet; see
``dashboards.data.strategy_scope``'s own docstring), so showing
``intraday_options``'s own strategies on this page would be fabricated
membership, not a real "prepared" capability. The moment a real positional
runtime/database exists, passing its ``conn``/``config_root`` here is the
only change needed — no restructuring.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

for _parent in Path(__file__).resolve().parents:
    if (_parent / "pyproject.toml").is_file():
        if str(_parent) not in sys.path:
            sys.path.insert(0, str(_parent))
        break

from dashboards.data.positional import NOT_CONFIGURED  # noqa: E402
from dashboards.data.strategy_scope import (  # noqa: E402
    discover_strategy_options,
    render_strategy_selector,
)

#: Kept for backward-compat with anything importing the old flat-page name.
STATUS_MESSAGE = NOT_CONFIGURED

_TABS = (
    ("Overview", "No positional runtime is configured."),
    ("Active Cycles", "No cycle data exists — there is no engine producing cycles."),
    ("Legs", "No leg data exists — there is no engine producing legs."),
    ("Adjustments", "No adjustment/roll data exists yet."),
    ("Orders & Fills", "No positional order flow exists — orders are per-runtime-group."),
    ("History", "No completed cycle history exists yet."),
    ("Performance", "No closed cycle exists to compute performance from."),
    ("Health", "There is no supervisor or worker process to report health for."),
)


def render(streamlit: Any, config_root: object = None) -> None:
    streamlit.subheader("Positional Options")
    streamlit.warning(NOT_CONFIGURED)
    # config_root is accepted (matching every other page's render() shape)
    # but deliberately not passed to discover_strategy_options below — see
    # the module docstring for why config-based discovery would fabricate
    # membership for this runtime today.
    del config_root
    options = discover_strategy_options(None, None, "positional_options")
    render_strategy_selector(streamlit, options, key="po_strategy")
    tabs = streamlit.tabs([label for label, _ in _TABS])
    for tab, (label, detail) in zip(tabs, _TABS, strict=True):
        with tab:
            streamlit.info(f"{label}: not configured. {detail}")


def main() -> None:  # pragma: no cover - exercised manually via `streamlit run`
    import streamlit as st

    from common.config import load_paths

    st.set_page_config(
        page_title="algo_trading — Positional Options", layout="wide", page_icon="📅"
    )
    st.title("Positional Options")
    render(st, load_paths().config_root)


if __name__ == "__main__":  # pragma: no cover
    main()
