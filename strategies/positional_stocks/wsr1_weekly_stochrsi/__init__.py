"""``wsr1_weekly_stochrsi`` — weekly Stochastic RSI on NIFTY 200 equities.

Authoritative specification: ``WSR1_WEEKLY_STOCH_RSI_SPEC.md`` in this folder.

Phase 1 builds the data foundation only — the modules here turn Dhan's daily
candles and three operator CSVs into the completed weekly bars later phases
compute on. Indicators (Phase 2), the pure rules core (Phase 3) and the runtime
(Phase 4) do not exist yet.

Nothing reads the clock on its own: every module that needs "now" takes it as
an argument.

**The rules core** (``rules.py``, Phase 3) imports nothing from
``common.engine``, ``runtimes``, the broker or the database — spec section 13,
enforced there by its own guard test. That is what will let it be reused
unchanged by a future live adapter.

The **data layer** is deliberately not held to that. ``trading_calendar.py``
reads the NSE holiday calendar through
:class:`~common.engine.session.MarketSession`, because spec 6.2 v1.2b requires
the expected last session to come from the existing session/calendar code
rather than from a second copy of the weekend/holiday rules. Phase 1's
docstring claimed the no-``common.engine`` rule for the whole package; that
was broader than the spec asks, and the two requirements cannot both hold
here. The spec's choice wins.
"""
