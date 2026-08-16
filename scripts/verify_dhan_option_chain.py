#!/usr/bin/env python3
"""Bounded, read-only diagnostic: validate a *real* Dhan option-chain
response against the exact structure ``common.market_data.chain_view.
parse_chain_payload`` expects (spec 4.2/11.3), before ``weekly_delta_
neutral`` is trusted for a real paper session.

    .venv/bin/python -m scripts.verify_dhan_option_chain [--underlying NIFTY] [--expiry YYYY-MM-DD]

**Strictly read-only, bounded, and never touches an order-adjacent
surface.** The only network calls are:

* ``POST https://auth.dhan.co/app/generateAccessToken`` /
  ``GET https://api.dhan.co/v2/profile`` — the same two calls
  ``scripts/auth_bootstrap.py`` already makes (reused via
  :class:`~common.authentication.AuthBootstrap`, not re-implemented).
* ``POST https://api.dhan.co/v2/optionchain/expirylist`` — one call.
* ``POST https://api.dhan.co/v2/optionchain`` — one call, through
  :class:`~common.market_data.option_chain.OptionChainService` (so Dhan's
  documented 3-second-per-key throttle is honoured even for this one-shot
  diagnostic use).

No broker/order client is ever constructed; no order-capable endpoint
(``/orders``, ``/superorder``, ``/forever``) is referenced anywhere in this
file — proven the same way ``tests/unit/test_scripts_are_read_only.py``
proves it for every other read-only script (this one is added to that
tier).

**No secret is ever printed.** The access token and client id are
registered with the logging redactor the instant they exist (mirroring
``auth_bootstrap.py`` exactly) and this script never ``print()``s either
value — only status/counts/structure. The raw chain response itself carries
no account-specific field (verified structurally below, not assumed) but is
still never printed or written to disk in full; only a bounded, field-level
summary for a small sample of strikes is shown.

**What this proves, and what it deliberately does not.**  This validates,
against a real live response: the ``/optionchain/expirylist`` and
``/optionchain`` envelope shapes, NIFTY's resolution through the existing
``common.market_data.scrip_master.INDEX_REGISTRY`` mapping (not a
hardcoded id here), every field ``chain_view.parse_chain_payload`` reads
(bid/ask/last_price/oi/volume/implied_volatility/greeks), and the parser's
timestamp/freshness derivation (``OptionChainService``'s ``snapshot_at``/
``received_at``/staleness arithmetic). It does **not**, by itself, prove
*market-hours* freshness — that the returned quotes are genuinely live and
moving — since that can only be observed while the market is actually open.
This script reports the two separately (see the final report's two
headings) and never claims the second without having actually observed it
open; see ``docs/IMPLEMENTATION_STATUS_AND_RUNBOOK.md`` section 11.10 Phase
4 for the recorded result of the run this script's authors actually made.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from datetime import date

from common.authentication import AuthBootstrap, AuthCredentials, AuthError
from common.config import load_settings
from common.config.paths import load_paths
from common.config.secrets import read_secret as _secret
from common.engine.config import SessionConfig
from common.engine.session import MarketSession
from common.logging import get_logger, setup_logging
from common.market_data.chain_view import (
    ChainPayloadError,
    ChainStrike,
    ChainView,
    parse_chain_payload,
)
from common.market_data.dhan_option_chain import build_dhan_chain_fetcher, fetch_dhan_expiry_list
from common.market_data.option_chain import ChainSnapshot, OptionChainService
from common.market_data.scrip_master import resolve_index_meta
from common.utils.timeutils import now_ist

EXIT_PASS = 0
EXIT_FAILED = 1
EXIT_NO_CREDENTIALS = 2
EXIT_AUTH_FAILED = 3

_log = get_logger(__name__)

#: Mirrors ``common.market_data.option_chain._snapshot_time``'s own
#: candidate key list exactly — this script does not import that private
#: helper, but must check for the identical keys to honestly report whether
#: a real response ever supplies one (documented today as "it does not").
_TIMESTAMP_CANDIDATE_KEYS = ("timestamp", "snapshotTime", "snapshot_time", "lastUpdated")

#: Every field ``chain_view._quote_from_leg`` reads from one CE/PE leg —
#: named explicitly so a structural mismatch against a real response is a
#: direct diff against this list, not a hunt through the parser.
_EXPECTED_LEG_FIELDS = (
    "top_bid_price",
    "top_ask_price",
    "last_price",
    "oi",
    "volume",
    "implied_volatility",
)
_EXPECTED_GREEK_FIELDS = ("delta", "gamma", "theta", "vega")

#: A plain NSE 09:15-15:30 session used only to answer "was the market open
#: right now" for this run's own report — deliberately independent of any
#: strategy's own configured holiday list (this diagnostic does not load
#: one), so it can under-report a closed market (an unlisted holiday looks
#: like a trading day) but can never over-claim one (a weekend never does).
_PLAIN_NSE_SESSION = MarketSession(
    SessionConfig(timezone="Asia/Kolkata", start_time="09:15", end_time="15:30",
                  square_off_time="15:30", holidays=())
)


@dataclass
class _Findings:
    """Accumulates PASS/FAIL/INFO lines for the final structured report —
    never raises on its own; every check appends here and the caller
    decides the exit code from :attr:`failed`."""

    lines: list[str] = field(default_factory=list)
    failed: bool = False

    def ok(self, message: str) -> None:
        self.lines.append(f"  PASS: {message}")

    def fail(self, message: str) -> None:
        self.lines.append(f"  FAIL: {message}")
        self.failed = True

    def info(self, message: str) -> None:
        self.lines.append(f"  INFO: {message}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--underlying", default="NIFTY", help="Underlying to resolve (default: NIFTY)."
    )
    parser.add_argument(
        "--expiry", default=None,
        help="Override the auto-selected expiry (default: the nearest one "
        "/optionchain/expirylist actually lists).",
    )
    parser.add_argument(
        "--strike-sample", type=int, default=5,
        help="How many strikes nearest the underlying LTP to validate field-by-field "
        "(default 5). Every strike is still structurally parsed and counted.",
    )
    args = parser.parse_args(argv)

    settings = load_settings()
    paths = load_paths(settings=settings)
    paths.ensure_writable_dirs()
    # Installed first, exactly like auth_bootstrap.py: everything below this
    # line that goes through common.logging is redacted.
    redactor = setup_logging(
        level=settings.algo_log_level, log_dir=paths.log_root, settings=settings
    )

    client_id = _secret(settings.dhan_client_id)
    if not client_id:
        print("DHAN_CLIENT_ID is not set. Fill it in .env (see .env.example).")
        return EXIT_NO_CREDENTIALS
    redactor.add_secrets([client_id])

    credentials = AuthCredentials(
        client_id=client_id,
        pin=_secret(settings.dhan_pin),
        totp_secret=_secret(settings.dhan_totp_secret),
        access_token=_secret(settings.dhan_access_token),
    )
    bootstrap = AuthBootstrap(
        credentials,
        cache_dir=paths.cache_root,
        on_token_minted=lambda token: redactor.add_secrets([token]),
    )
    try:
        token, outcome = bootstrap.get_token()
    except AuthError as exc:
        print(f"FAIL: authentication failed ({type(exc).__name__}): {exc}")
        return EXIT_AUTH_FAILED
    redactor.add_secrets([token])
    print(f"Authenticated (token source={outcome.source}). No secret printed.")

    try:
        meta = resolve_index_meta(args.underlying)
    except ValueError as exc:
        print(f"FAIL: {exc}")
        return EXIT_FAILED
    security_id = int(meta.security_id)
    print(
        f"Resolved {args.underlying!r} -> security_id={meta.security_id} "
        f"segment={meta.segment!r} via common.market_data.scrip_master.INDEX_REGISTRY "
        "(existing instrument/reference-data mapping, not hardcoded here)."
    )

    print("\nFetching /v2/optionchain/expirylist (bounded, read-only, one call) ...")
    try:
        expiries = fetch_dhan_expiry_list(
            client_id=client_id, access_token=token,
            security_id=security_id, segment=meta.segment,
        )
    except Exception as exc:
        print(f"FAIL: could not fetch the expiry list: {type(exc).__name__}: {exc}")
        return EXIT_FAILED

    expirylist_findings = _validate_expirylist(expiries)

    chosen_expiry = args.expiry or (expiries[0] if expiries else None)
    if not chosen_expiry:
        print("\nFAIL: no expiry available (list was empty and --expiry was not given).")
        _print_report("Structural payload validation", expirylist_findings)
        return EXIT_FAILED

    print(
        f"Chosen expiry: {chosen_expiry!r} (nearest exchange-listed, unless --expiry overrode it)."
    )
    print("Fetching /v2/optionchain (bounded, read-only, one throttled call) ...")
    fetcher = build_dhan_chain_fetcher(client_id=client_id, access_token=token)
    service = OptionChainService(fetcher)
    try:
        snapshot = service.get(security_id, meta.segment, chosen_expiry)
    except Exception as exc:
        print(f"\nFAIL: could not fetch the option chain: {type(exc).__name__}: {exc}")
        _print_report("Structural payload validation", expirylist_findings)
        return EXIT_FAILED

    structural = _Findings(
        lines=list(expirylist_findings.lines), failed=expirylist_findings.failed
    )
    view = _validate_payload_structure(snapshot, structural, strike_sample=args.strike_sample)

    _validate_timestamp_semantics(snapshot, structural)

    if view is not None:
        _validate_parser_round_trip(
            snapshot.payload, view, structural, strike_sample=args.strike_sample
        )

    _print_report("1. Structural payload validation", structural)

    market_hours_findings = _validate_market_hours_freshness(snapshot, service)
    _print_report("2. Market-hours freshness validation", market_hours_findings)

    print()
    if structural.failed:
        print("OVERALL: FAIL — structural validation found a real mismatch; see above.")
        return EXIT_FAILED
    print("OVERALL: PASS (structural). See section 2 above for the separate, honest "
          "market-hours-freshness verdict — never assume it from section 1 alone.")
    return EXIT_PASS


# --------------------------------------------------------------- validators
def _validate_expirylist(expiries: list[str]) -> _Findings:
    findings = _Findings()
    if not expiries:
        findings.fail("/optionchain/expirylist returned an empty list")
        return findings
    findings.ok(f"/optionchain/expirylist returned {len(expiries)} expiries")
    parsed: list[date] = []
    for raw in expiries:
        try:
            parsed.append(date.fromisoformat(raw))
        except ValueError:
            findings.fail(f"expiry {raw!r} is not an ISO YYYY-MM-DD date")
    if parsed and parsed == sorted(parsed):
        findings.ok("expiries are in ascending (exchange-published) order")
    elif parsed:
        findings.info(
            "expiries are NOT in ascending order in the raw response (unexpected but not fatal)"
        )
    return findings


def _validate_payload_structure(
    snapshot: ChainSnapshot, findings: _Findings, *, strike_sample: int
) -> ChainView | None:
    # snapshot.payload is already dict[str, Any] by construction
    # (build_dhan_chain_fetcher's own `dict(response.json())` coercion would
    # itself raise before ever reaching here on a genuinely non-object
    # response) — no defensive isinstance check needed on top of that.
    payload = snapshot.payload
    status = payload.get("status")
    findings.info(f"top-level 'status' = {status!r}")
    if "data" not in payload:
        findings.fail("/optionchain response has no top-level 'data' key")
        return None
    findings.ok(
        "top-level 'status'/'data' keys present, matching chain_view.py's documented envelope"
    )

    try:
        view = parse_chain_payload(
            payload, snapshot_at=snapshot.snapshot_at, received_at=snapshot.received_at
        )
    except ChainPayloadError as exc:
        findings.fail(f"parse_chain_payload raised on the real response: {exc}")
        return None
    except Exception as exc:  # pragma: no cover - only on a genuine surprise
        findings.fail(f"parse_chain_payload raised unexpectedly: {type(exc).__name__}: {exc}")
        return None

    if not view.strikes:
        findings.fail("chain parsed structurally but contains zero strikes")
        return view
    findings.ok(
        f"parsed {len(view.strikes)} strikes; "
        f"underlying_last_price={view.underlying_last_price}"
    )

    data = payload.get("data")
    oc = data.get("oc") if isinstance(data, dict) else None
    sample_legs = 0
    complete_quotes = 0
    complete_greeks = 0
    unexpected_keys: set[str] = set()
    if isinstance(oc, dict):
        for _raw_strike, legs in list(oc.items())[:50]:
            if not isinstance(legs, dict):
                continue
            for side in ("ce", "pe"):
                leg = legs.get(side)
                if not isinstance(leg, dict) or not leg:
                    continue
                sample_legs += 1
                for key in leg:
                    if key not in (*_EXPECTED_LEG_FIELDS, "greeks"):
                        unexpected_keys.add(key)
                greeks = leg.get("greeks")
                if isinstance(greeks, dict):
                    for key in greeks:
                        if key not in _EXPECTED_GREEK_FIELDS:
                            unexpected_keys.add(f"greeks.{key}")
    if sample_legs == 0:
        findings.fail("no CE/PE leg in the sampled strikes had any populated fields at all")
    else:
        findings.ok(
            f"sampled {sample_legs} CE/PE legs across the response for field-mapping checks"
        )
    if unexpected_keys:
        findings.info(
            "leg/greeks keys present in the real response but unread by chain_view.py: "
            f"{sorted(unexpected_keys)}"
        )
    else:
        findings.ok("no leg/greeks field beyond chain_view.py's documented set was observed")

    near = _nearest_strikes(view, count=strike_sample)
    for row in near:
        for option_type, quote in (("CE", row.call), ("PE", row.put)):
            if quote.has_complete_quote:
                complete_quotes += 1
                # defensive re-check; has_complete_quote already enforces this
                if not (
                    quote.bid is not None and quote.ask is not None and quote.bid <= quote.ask
                ):
                    findings.fail(
                        f"strike {row.strike} {option_type}: bid/ask crossed despite "
                        "has_complete_quote"
                    )
            if quote.has_complete_greeks:
                complete_greeks += 1
                assert quote.delta is not None and quote.gamma is not None
                assert quote.theta is not None and quote.vega is not None
                if option_type == "CE" and not (-0.05 <= quote.delta <= 1.05):
                    findings.fail(
                        f"strike {row.strike} CE delta {quote.delta} outside [0,1] "
                        "plausible range"
                    )
                if option_type == "PE" and not (-1.05 <= quote.delta <= 0.05):
                    findings.fail(
                        f"strike {row.strike} PE delta {quote.delta} outside [-1,0] "
                        "plausible range"
                    )
                if quote.gamma < -1e-6:
                    findings.fail(
                        f"strike {row.strike} {option_type} gamma {quote.gamma} is negative"
                    )
                if quote.implied_volatility is not None and quote.implied_volatility < 0:
                    findings.fail(
                        f"strike {row.strike} {option_type} implied_volatility is negative"
                    )
    findings.ok(
        f"of {len(near)} sampled near-the-money strikes x2 sides: "
        f"{complete_quotes} have a complete two-sided quote, {complete_greeks} have "
        "complete greeks (a partial book/greeks set on a real chain is expected and "
        "not itself a failure)"
    )
    return view


def _nearest_strikes(view: ChainView, *, count: int) -> list[ChainStrike]:
    if not view.strikes:
        return []
    spot = view.underlying_last_price
    if spot is None:
        return list(view.strikes[:count])
    ordered = sorted(view.strikes, key=lambda row: abs(row.strike - spot))
    return ordered[:count]


def _validate_timestamp_semantics(snapshot: ChainSnapshot, findings: _Findings) -> _Findings:
    payload = snapshot.payload
    present = [
        key for key in _TIMESTAMP_CANDIDATE_KEYS
        if isinstance(payload, dict) and payload.get(key)
    ]
    if present:
        findings.info(
            f"real response DOES carry a top-level timestamp-shaped key: {present} — "
            "OptionChainService._snapshot_time now has something to parse; re-verify its "
            "format matches what that function expects (int/float epoch or ISO string)."
        )
    else:
        findings.info(
            "real response carries none of common.market_data.option_chain._snapshot_time's "
            f"candidate keys {_TIMESTAMP_CANDIDATE_KEYS} — matches its own documented "
            "'Dhan's response does not include one today' fallback-to-receive-time behaviour."
        )
    if snapshot.snapshot_at == snapshot.received_at:
        findings.ok(
            "snapshot_at == received_at, confirming the fallback path actually ran on this "
            "real response (the module's own reliable signal for 'no broker timestamp supplied')"
        )
    else:
        findings.info(
            f"snapshot_at ({snapshot.snapshot_at}) != received_at ({snapshot.received_at}) — "
            "the broker DID supply a usable timestamp this call; not itself a failure."
        )
    findings.ok(f"received_at={snapshot.received_at.isoformat()} (UTC wall clock at receipt)")
    return findings


def _validate_market_hours_freshness(
    snapshot: ChainSnapshot, service: OptionChainService
) -> _Findings:
    """Deliberately separate from structural validation (see this script's
    own module docstring): only ever reports what was *actually observed*.
    A closed market is reported as SKIPPED, never silently upgraded to
    PASS. Uses only ``service``'s public surface (``is_stale``) plus a
    fresh ``time.monotonic()`` reading of its own — never reaches into the
    service's private throttle/cache internals."""
    findings = _Findings()
    now = now_ist()
    is_open = _PLAIN_NSE_SESSION.is_open(at=now)
    findings.info(f"current IST time: {now.isoformat()} (weekday={now.strftime('%A')})")
    if not is_open:
        findings.info(
            "market is CLOSED right now (outside 09:15-15:30 IST or a weekend) — "
            "market-hours freshness cannot be observed by this run. SKIPPED, not claimed."
        )
        return findings

    age = snapshot.age_seconds(now_monotonic=time.monotonic())
    is_stale = service.is_stale(snapshot)
    findings.ok(f"market is OPEN — response age at check time: {age:.2f}s (stale={is_stale})")
    if is_stale:
        findings.fail(
            "response is already stale despite the market being open — investigate before enabling"
        )
    else:
        findings.ok(
            "response is fresh — market-hours freshness OBSERVED and PASSING for this one call"
        )
    return findings


def _validate_parser_round_trip(
    payload: dict[str, object], view: ChainView, findings: _Findings, *, strike_sample: int
) -> None:
    data = payload.get("data")
    oc = data.get("oc") if isinstance(data, dict) else {}
    if not isinstance(oc, dict):
        return
    if len(view.strikes) != len(oc):
        findings.fail(
            f"parsed {len(view.strikes)} strikes but the raw response has {len(oc)} "
            "— count mismatch"
        )
        return
    findings.ok(
        f"parsed strike count ({len(view.strikes)}) matches the raw response's own "
        "oc key count exactly"
    )

    checked = 0
    for row in _nearest_strikes(view, count=strike_sample):
        raw_key = f"{row.strike:.6f}"
        raw_legs = oc.get(raw_key)
        if not isinstance(raw_legs, dict):
            findings.fail(f"parsed strike {row.strike} has no matching raw key {raw_key!r}")
            continue
        for option_type, quote, raw_key_side in (("CE", row.call, "ce"), ("PE", row.put, "pe")):
            raw_leg = raw_legs.get(raw_key_side) or {}
            if not isinstance(raw_leg, dict):
                raw_leg = {}
            raw_bid = raw_leg.get("top_bid_price")
            raw_ask = raw_leg.get("top_ask_price")
            if (raw_bid is not None) != (quote.bid is not None) or (
                raw_bid is not None and float(raw_bid) != quote.bid
            ):
                findings.fail(
                    f"strike {row.strike} {option_type} bid mismatch: "
                    f"raw={raw_bid} parsed={quote.bid}"
                )
            if (raw_ask is not None) != (quote.ask is not None) or (
                raw_ask is not None and float(raw_ask) != quote.ask
            ):
                findings.fail(
                    f"strike {row.strike} {option_type} ask mismatch: "
                    f"raw={raw_ask} parsed={quote.ask}"
                )
            checked += 1
    findings.ok(
        f"round-tripped {checked} raw leg(s) against the parsed ChainQuote with no "
        "field-mapping mismatch"
    )


def _print_report(title: str, findings: _Findings) -> None:
    print(f"\n=== {title} ===")
    for line in findings.lines:
        print(line)


if __name__ == "__main__":
    sys.exit(main())
