"""Positional Options and Intraday Stocks: professional, tabbed
"not configured" states — never a fabricated empty table, never one flat
undifferentiated page."""

from __future__ import annotations

from _dashboard_fakes import FakeStreamlit

import dashboards.intraday_stocks as stocks_page
import dashboards.positional_options as positional_page


def test_positional_options_renders_eight_tabs_each_with_its_own_message():
    st = FakeStreamlit()
    positional_page.render(st)
    assert st.tab_labels == [
        [
            "Overview",
            "Active Cycles",
            "Legs",
            "Adjustments",
            "Orders & Fills",
            "History",
            "Performance",
            "Health",
        ]
    ]
    assert len(st.infos) == 8
    assert any(positional_page.NOT_CONFIGURED in w for w in st.warnings)


def test_intraday_stocks_renders_eight_tabs_each_with_its_own_message():
    st = FakeStreamlit()
    stocks_page.render(st)
    assert st.tab_labels == [
        [
            "Overview",
            "Scanner & Candidates",
            "Decisions",
            "Live Positions",
            "Orders & Fills",
            "Closed Trades",
            "Performance",
            "Health",
        ]
    ]
    assert len(st.infos) == 8
    assert any(stocks_page.NOT_CONFIGURED in w for w in st.warnings)


def test_positional_options_shows_a_disabled_strategy_selector():
    """The reusable component is wired in today, even with nothing to
    select — spec: "the dropdown must work without restructuring the
    dashboard" once a real runtime exists."""
    st = FakeStreamlit()
    positional_page.render(st)
    assert any(label == "Strategy" for label, _options, _kwargs in st.selectbox_calls)
    strategy_call = next(c for c in st.selectbox_calls if c[0] == "Strategy")
    assert strategy_call[2].get("disabled") is True


def test_intraday_stocks_shows_a_disabled_strategy_selector():
    st = FakeStreamlit()
    stocks_page.render(st)
    assert any(label == "Strategy" for label, _options, _kwargs in st.selectbox_calls)
    strategy_call = next(c for c in st.selectbox_calls if c[0] == "Strategy")
    assert strategy_call[2].get("disabled") is True


def test_no_fabricated_data_appears_in_either_stub():
    """Neither stub ever calls dataframe/metric — there is nothing real to
    show, so nothing tabular or numeric is rendered."""
    st_positional = FakeStreamlit()
    positional_page.render(st_positional)
    assert st_positional.dataframes == []
    assert st_positional.metrics == []

    st_stocks = FakeStreamlit()
    stocks_page.render(st_stocks)
    assert st_stocks.dataframes == []
    assert st_stocks.metrics == []
