#!/usr/bin/env python3
"""Bounded, read-only diagnostic: verify India VIX's configured security ID
and segment against Dhan's *current* public instrument master.

    .venv/bin/python -m scripts.verify_vix_security_id [--security-id 21] [--symbol "INDIA VIX"]

Correction P1-2: a *wrong configured* VIX security ID is a configuration
defect, not ordinary temporary data absence — ``straddle_920``'s fail-open
behaviour on a *missing* VIX tick (spec section 9.3) must never be relied on
to paper over a wrong ID silently subscribed to the wrong instrument. This
script exists to catch that before the strategy is ever enabled.

**Strictly read-only, bounded, and never touches an order-adjacent surface.**

* The only network call is one unauthenticated ``GET`` to Dhan's public daily
  scrip master CSV (``common.market_data.scrip_master.SCRIP_MASTER_URL`` —
  the same file ``ScripMaster``/``ScripMasterCache`` already download for
  option-chain resolution), bounded by a 60-second timeout
  (:func:`~common.market_data.scrip_master.fetch_scrip_master_text`).
* No credential of any kind is read, sent, or required — this endpoint is
  not account-specific.
* Never constructs a broker/order client, never imports anything from
  ``common.market_data.dhan``'s order-adjacent surface, and never calls an
  order-related endpoint.

**What this proves, and what it deliberately does not.** This confirms the
*static* instrument-master entry: that the configured security ID genuinely
names "INDIA VIX" today, per Dhan's own published master, on the expected
exchange/instrument shape. It does **not** confirm the live market-feed
WebSocket actually delivers real-time VIX ticks for that ID/segment during
market hours — only a real, bounded, market-hours subscription can prove
that. Use ``scripts/diagnose_live_feed.py --security-id <id> --segment 0
--mode ticker`` for the feed half (segment 0 = IDX_I, the same
:data:`~common.market_data.scrip_master.SEGMENT_CODES` mapping the option
worker uses) — it already generalises to any index security id/segment with
no VIX-specific code, so this script does not duplicate it. Both checks
must pass, during market hours, before ``config/strategies/straddle_920.yaml``
may be flipped to ``enabled: true`` (P1-3).
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
from dataclasses import dataclass
from datetime import UTC, datetime

from common.market_data.scrip_master import SCRIP_MASTER_URL, fetch_scrip_master_text

EXIT_PASS = 0
EXIT_NO_MATCH = 1
EXIT_FETCH_FAILED = 2

#: The raw scrip-master row shape a spot index carries — distinct from an
#: option row (``SEM_INSTRUMENT_NAME == "OPTIDX"``), which is why
#: ``common.market_data.scrip_master.ScripMaster`` (built for option-chain
#: resolution) never parses these; this script's own minimal parse is
#: intentionally separate rather than widening that class for a one-off.
_INDEX_INSTRUMENT_NAME = "INDEX"


@dataclass(frozen=True)
class IndexRow:
    security_id: str
    exchange: str
    raw_segment: str
    trading_symbol: str
    custom_symbol: str


def _parse_index_rows(csv_text: str) -> list[IndexRow]:
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = []
    for record in reader:
        if record.get("SEM_INSTRUMENT_NAME") != _INDEX_INSTRUMENT_NAME:
            continue
        rows.append(
            IndexRow(
                security_id=str(record.get("SEM_SMST_SECURITY_ID", "")),
                exchange=str(record.get("SEM_EXM_EXCH_ID", "")),
                raw_segment=str(record.get("SEM_SEGMENT", "")),
                trading_symbol=str(record.get("SEM_TRADING_SYMBOL", "")),
                custom_symbol=str(record.get("SEM_CUSTOM_SYMBOL", "")),
            )
        )
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--security-id",
        default="21",
        help="The configured parameters.vix_security_id to verify (default: 21, the "
        "value currently committed in config/strategies/straddle_920.yaml).",
    )
    parser.add_argument(
        "--symbol",
        default="VIX",
        help="Case-insensitive substring to also search for, independent of the "
        "security id, so a drifted id can still be found by name (default: VIX).",
    )
    args = parser.parse_args(argv)

    print(f"Fetching {SCRIP_MASTER_URL} (bounded, read-only, no credentials) ...")
    try:
        csv_text = fetch_scrip_master_text()
    except Exception as exc:  # network/HTTP failure — never a crash, a clear FAIL
        print(f"FAIL: could not fetch the scrip master: {type(exc).__name__}: {exc}")
        return EXIT_FETCH_FAILED

    rows = _parse_index_rows(csv_text)
    by_id = [r for r in rows if r.security_id == args.security_id]
    by_name = [r for r in rows if args.symbol.upper() in r.trading_symbol.upper()]

    checked_at = datetime.now(UTC).isoformat()
    print()
    print(f"=== verify_vix_security_id — checked_at={checked_at} ===")
    print(f"Rows matching security_id={args.security_id!r}:")
    for row in by_id:
        print(f"  {row}")
    print(f"Rows matching symbol substring {args.symbol!r} (independent cross-check):")
    for row in by_name:
        print(f"  {row}")
    print()

    if not by_id:
        print(
            f"FAIL: no INDEX row in the current scrip master has "
            f"security_id={args.security_id!r}."
        )
        if by_name:
            print("The name search above found a different id — config may be stale/wrong.")
        return EXIT_NO_MATCH

    if len(by_id) > 1:
        print(f"FAIL: {len(by_id)} INDEX rows share security_id={args.security_id!r} — ambiguous.")
        return EXIT_NO_MATCH

    row = by_id[0]
    if "VIX" not in row.trading_symbol.upper():
        print(
            f"FAIL: security_id={args.security_id!r} exists but names "
            f"{row.trading_symbol!r}, not an India VIX row."
        )
        return EXIT_NO_MATCH

    print(
        f"PASS: security_id={args.security_id!r} names {row.trading_symbol!r} "
        f"on exchange={row.exchange!r} (raw SEM_SEGMENT={row.raw_segment!r}). "
        "Record this checked_at alongside the config value; re-run before every "
        "first-of-day paper session until the value has stayed stable across "
        "several runs."
    )
    print(
        "REMINDER: this proves the *static* instrument-master entry only. It does "
        "NOT prove the live feed delivers VIX ticks for this id — run "
        "`scripts/diagnose_live_feed.py --security-id "
        f"{args.security_id} --segment 0 --mode ticker` during market hours "
        "before enabling the strategy (P1-2/P1-3)."
    )
    return EXIT_PASS


if __name__ == "__main__":
    sys.exit(main())
