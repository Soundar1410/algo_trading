"""``positional_stocks`` runtime — the paper book of ``wsr1_weekly_stochrsi``.

Phase 4a (persistence and paper accounting). The weekly run itself — the
``fetch`` / ``decide`` CLI, the report and Telegram — is Phase 4b, and nothing
here is scheduled or registered anywhere: there is no ``RUNTIMES`` entry and no
``auto_start`` route (spec section 13).

**Own migration set (spec 9, v1.2i).** ``positional_stocks.db`` is migrated from
``runtimes/positional_stocks/migrations/`` by the shared ``MigrationRunner``,
never from ``common/persistence/migrations/versions/``. The two paper runtimes
run from this working tree and verify migration checksums at every start; a
stock migration in the shared directory would reach their live databases, and
editing or removing it later would stop both of them.

**New files only (spec 13, v1.2i).** Nothing in this package is imported by
``intraday_options`` or ``positional_options``, and building it changed no module
they import.
"""
