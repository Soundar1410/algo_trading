"""Read-only Streamlit page — Intraday Stocks (not implemented yet).

No scanner, supervisor, worker or database exist for this runtime group;
``config/runtimes/intraday_stocks.yaml`` does not exist. Every tab says so
plainly, in the tab's own terms, rather than one flat page or an empty
panel that could be mistaken for "no candidates today" when the true state
is "this does not run at all".

No side effects: no database is opened, because there is none to open. The
read-model interface these tabs are built against lives in
``dashboards/data/stocks.py`` — real dataclasses, ready for a real scanner's
persistence to satisfy, not queried today.
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

from dashboards.data.stocks import NOT_CONFIGURED  # noqa: E402

#: Kept for backward-compat with anything importing the old flat-page name.
STATUS_MESSAGE = NOT_CONFIGURED

_TABS = (
    ("Overview", "No scanner runtime is configured."),
    ("Scanner & Candidates", "No scanner snapshots exist — there is no scanner running."),
    ("Decisions", "No accept/reject decision data exists yet."),
    ("Live Positions", "No paper-position data exists for this runtime group."),
    ("Orders & Fills", "No order flow exists for this runtime group."),
    ("Closed Trades", "No closed-trade data exists yet."),
    ("Performance", "No closed trade exists to compute performance from."),
    ("Health", "There is no supervisor or worker process to report health for."),
)


def render(streamlit: Any) -> None:
    streamlit.subheader("Intraday Stocks")
    streamlit.warning(NOT_CONFIGURED)
    tabs = streamlit.tabs([label for label, _ in _TABS])
    for tab, (label, detail) in zip(tabs, _TABS, strict=True):
        with tab:
            streamlit.info(f"{label}: not configured. {detail}")


def main() -> None:  # pragma: no cover - exercised manually via `streamlit run`
    import streamlit as st

    st.set_page_config(page_title="algo_trading — Intraday Stocks", layout="wide", page_icon="📉")
    st.title("Intraday Stocks")
    render(st)


if __name__ == "__main__":  # pragma: no cover
    main()
