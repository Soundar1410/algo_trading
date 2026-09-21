"""``wsr1_weekly_stochrsi`` — weekly Stochastic RSI on NIFTY 200 equities.

Authoritative specification: ``WSR1_WEEKLY_STOCH_RSI_SPEC.md`` in this folder.

Phase 1 builds the data foundation only — the modules here turn Dhan's daily
candles and three operator CSVs into the completed weekly bars later phases
compute on. Indicators (Phase 2), the pure rules core (Phase 3) and the runtime
(Phase 4) do not exist yet.

Nothing in this package imports ``common.engine``, ``runtimes``, the broker or
the database, and nothing reads the clock on its own: every module that needs
"now" takes it as an argument. That is what will let the Phase 3 rules core be
reused unchanged by a future live adapter.
"""
