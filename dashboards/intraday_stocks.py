"""Read-only Streamlit page — Intraday Stocks (stub).

No scanner, no supervisor, no worker and no database exist for this runtime
yet. This page says so plainly rather than rendering an empty panel that
could be mistaken for "no candidates today" when the true state is "this
does not run at all".

No side effects: no database is opened, because there is none to open.
"""

from __future__ import annotations

from typing import Any

STATUS_MESSAGE = (
    "Intraday stocks is not implemented yet. There is no scanner, "
    "supervisor, worker or database for this runtime group."
)


def render(streamlit: Any) -> None:
    streamlit.subheader("Intraday Stocks")
    streamlit.info(STATUS_MESSAGE)


def main() -> None:  # pragma: no cover - exercised manually via `streamlit run`
    import streamlit as st

    st.set_page_config(page_title="algo_trading — Intraday Stocks", layout="wide")
    st.title("Intraday Stocks")
    render(st)


if __name__ == "__main__":  # pragma: no cover
    main()
