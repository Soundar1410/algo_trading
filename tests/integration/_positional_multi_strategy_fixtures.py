"""Module-level, picklable fixture builders for
``test_positional_runtime_multi_strategy.py`` — not a test module itself
(leading underscore; mirrors ``_weekly_delta_neutral_fixtures.py``'s own
convention).

**Why module-level, not closures.** ``WorkerConfig.chain_fetcher_factory``/
``scrip_master_factory``/``margin_fetcher_factory`` are ``"module:function"``
dotted-path strings, resolved fresh inside each spawned child process (see
``runtimes.positional_options.worker._resolve_factory``) — never a live
closure/object pickled across the ``multiprocessing`` "spawn" boundary. The
functions below must therefore be importable by dotted path from a freshly
started interpreter, which is exactly what a plain module-level function
is; a closure captured inside a test function is not.

``multiprocessing``'s "spawn" bootstrap propagates the parent's own
``sys.path`` into the child (see ``multiprocessing.spawn.
get_preparation_data``), and pytest has already put this file's own
directory (``tests/integration/``) on ``sys.path`` by the time a test here
runs — the same mechanism that already lets sibling test modules
``from _weekly_delta_neutral_fixtures import ...`` — so
``"_positional_multi_strategy_fixtures:fixture_chain_fetcher"`` resolves
correctly in a spawned child too, with no extra wiring.

None of these ever reach the network: the fixture strategy that uses them
(``strategies.positional_options._fixture_second_strategy``) never enters a
cycle, so the chain/margin fetchers are constructed but never actually
called; the scrip master is loaded from an in-memory CSV, never
downloaded.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Callable
from typing import Any

from common.margin import LegMarginRequest
from common.market_data.scrip_master import ScripMaster

#: Deliberately far in the future so ScripMaster.nearest_expiry() (which
#: only ever compares against the real wall clock — this fixture module
#: injects no clock) never finds every listed expiry already in the past.
_FIXTURE_EXPIRY = "2030-12-25"


def _fixture_chain_fetcher(security_id: int, segment: str, expiry: str) -> dict[str, object]:
    """A structurally valid, empty chain — never actually invoked by
    ``FixtureSecondStrategy`` (which never enters a cycle), but ``build_
    engine`` still constructs an ``OptionChainService`` around whatever this
    returns, so it must be a real, importable, argument-compatible
    ``ChainFetcher``."""
    return {"status": "success", "data": {"last_price": 24000.0, "oc": {}}}


def build_fixture_chain_fetcher() -> Callable[[int, str, str], dict[str, object]]:
    """A ``WorkerConfig.chain_fetcher_factory`` target: every factory ref
    this field family resolves is called with **no arguments** and must
    itself return the object the real construction site otherwise builds
    (mirrors ``build_fixture_margin_fetcher``/``fixture_scrip_master``'s own
    zero-arg-factory shape below) — so this returns the fetcher function,
    it is not the fetcher function itself."""
    return _fixture_chain_fetcher


def _fixture_margin_fetcher(_leg: LegMarginRequest) -> float:
    """Never actually invoked (see :func:`_fixture_chain_fetcher`'s own
    docstring) — present only so ``MarginEstimator`` has a real, importable
    fetcher to construct."""
    return 20_000.0


def build_fixture_margin_fetcher() -> Callable[[Any], float]:
    """A ``WorkerConfig.margin_fetcher_factory`` target — see
    :func:`build_fixture_chain_fetcher`'s own docstring for why this
    returns the fetcher rather than being one."""
    return _fixture_margin_fetcher


def fixture_scrip_master() -> ScripMaster:
    """A minimal, in-memory NIFTY master — one strike, both option types, one
    expiry safely in the future. ``DhanOptionChainResolver.__init__`` calls
    ``ScripMaster.nearest_expiry()`` unconditionally inside ``build_engine``
    (even though ``FixtureSecondStrategy`` never selects a candidate), so at
    least one real, future-dated row is required for that call alone to
    succeed."""
    rows = [
        [
            "SEM_SMST_SECURITY_ID", "SEM_INSTRUMENT_NAME", "SEM_TRADING_SYMBOL",
            "SEM_CUSTOM_SYMBOL", "SEM_EXPIRY_DATE", "SEM_STRIKE_PRICE",
            "SEM_OPTION_TYPE", "SEM_LOT_UNITS", "SEM_EXM_EXCH_ID", "SEM_SEGMENT",
        ]
    ]
    for option_type, security_id in (("CE", "970001"), ("PE", "970002")):
        rows.append(
            [
                security_id, "OPTIDX", f"NIFTY-25DEC2030-24000-{option_type}",
                f"NIFTY 24000 {option_type}", f"{_FIXTURE_EXPIRY} 00:00:00",
                "24000", option_type, "75", "NSE", "D",
            ]
        )
    buffer = io.StringIO()
    csv.writer(buffer).writerows(rows)
    return ScripMaster("NIFTY", exchange="NSE").load_from_text(buffer.getvalue())
