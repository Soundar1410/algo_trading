"""Read-only Streamlit page — Positional Options (stub).

No supervisor, no worker and no database exist for this runtime yet —
``runtimes/positional_options/__init__.py`` is inert scaffolding (see the
runbook's D56/D69 for the persistence-identity question a real positional
worker will need to answer before it can be built). This page says so
plainly rather than rendering an empty panel that could be mistaken for "no
positions today" when the true state is "this does not run at all".

No side effects: no database is opened, because there is none to open.
"""

from __future__ import annotations

from typing import Any

STATUS_MESSAGE = (
    "Positional options is not implemented yet. There is no supervisor, "
    "worker or database for this runtime group — see the runbook's Phase 6 "
    "Part 5 record (D56, D69) for the open persistence-identity question a "
    "real positional strategy will need to answer first."
)


def render(streamlit: Any) -> None:
    streamlit.subheader("Positional Options")
    streamlit.info(STATUS_MESSAGE)


def main() -> None:  # pragma: no cover - exercised manually via `streamlit run`
    import streamlit as st

    st.set_page_config(page_title="algo_trading — Positional Options", layout="wide")
    st.title("Positional Options")
    render(st)


if __name__ == "__main__":  # pragma: no cover
    main()
