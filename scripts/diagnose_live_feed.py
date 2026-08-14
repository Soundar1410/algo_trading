#!/usr/bin/env python3
"""Bounded, read-only diagnostic for the live Dhan market-feed WebSocket.

    .venv/bin/python -m scripts.diagnose_live_feed [--seconds 15] [--security-id 13]

Built for Phase 10's feed investigation: prove, independent of the supervisor
and the hub, whether the cached credentials can open a Dhan v2 WebSocket,
subscribe to the NIFTY underlying, and receive at least one real tick within a
bounded window. Exits with a clear ``PASS``/``FAIL`` and a machine-readable
reason.

**Strictly read-only and bounded.**

* Uses the *cached* token via :class:`~common.authentication.AuthBootstrap` —
  never runs a fresh interactive login flow.
* Subscribes to **one** instrument by default: the NIFTY underlying, on its
  real segment (``IDX_I``/0) and Ticker mode — never an option contract, and
  never more than what ``--security-id``/``--segment``/``--mode`` name.
* Waits at most ``--seconds`` (default 15) for a first tick, then stops the
  feed itself — the same same-thread ``adapter.stop()`` pattern
  ``scripts/capture_live_tape.py`` uses, for the same reason (a cross-thread
  stop races the SDK's asyncio loop).
* **Never constructs or imports anything from ``common.market_data.dhan``'s
  order-adjacent surface** — no ``build_dhan_order_client``, no
  ``dhanhq.Order``/``ForeverOrder``/``SuperOrder``, and no import of
  ``runtimes.intraday_options.live_runtime``. The only Dhan surfaces touched
  are the market-feed WebSocket (``DhanMarketFeedAdapter``) and, optionally,
  the read-only ``/profile`` endpoint via
  ``common.market_data.dhan.fetch_dhan_profile`` — a GET request; it places no
  order and modifies no account state. ``dhanhq`` itself is never imported
  from this script directly: ``common.market_data.dhan`` is the repository's
  one and only importer of that SDK
  (``test_only_the_dhan_adapter_imports_the_sdk``), and this script keeps it
  that way.
* Never prints a credential. The redacting log handler
  (``common.logging.setup_logging``) is installed before the token is
  resolved, and the profile response and every event line are scrubbed of
  token/secret/pin/totp/client-id-shaped keys before being printed.

**On its own, a successful ``/profile`` call does not prove WebSocket
entitlement.** It proves the token is valid for REST. Dhan's own
market-feed disconnection code ``806`` ("data APIs not subscribed on this
account") only appears *after* a WebSocket connects and subscribes — so the
only real proof of feed entitlement is a tick, which is why this script's
PASS/FAIL verdict is keyed on receiving one, not on the profile call
succeeding.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from common.authentication import AuthBootstrap, AuthCredentials, AuthError
from common.config import load_settings
from common.config.paths import load_paths
from common.config.secrets import read_secret as _secret
from common.logging import get_logger, setup_logging

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_NO_CREDENTIALS = 2
EXIT_AUTH_FAILED = 3

#: Keys that must never reach stdout or the log, wherever they turn up.
#: Deliberately specific rather than a bare "id" substring match: "id" alone
#: also matches "valid" and "validity" — exactly the entitlement-relevant
#: /profile fields (tokenValidity, dataValidity) this script exists to show —
#: so "clientid" is spelled out instead of the broader, self-defeating match.
_FORBIDDEN_KEYS = (
    "token",
    "secret",
    "pin",
    "totp",
    "password",
    "auth",
    "clientid",
    "email",
    "mobile",
    "pan",
)

_log = get_logger(__name__)


def _scrub(payload: object) -> object:
    """Keep only credential-free content, recursively. Same rule as capture_live_tape."""
    if isinstance(payload, dict):
        clean: dict[str, Any] = {}
        for key, value in payload.items():
            lowered = str(key).lower()
            if any(word in lowered for word in _FORBIDDEN_KEYS):
                continue
            clean[str(key)] = _scrub(value)
        return clean
    if isinstance(payload, list):
        return [_scrub(item) for item in payload]
    return payload


@dataclass
class DiagnosticEvent:
    """One row of the bounded diagnostic's timeline. Never carries a secret."""

    at: str
    stage: str
    detail: str


@dataclass
class DiagnosticResult:
    events: list[DiagnosticEvent] = field(default_factory=list)
    ticks_received: int = 0
    passed: bool = False
    reason: str = ""


def _record(events: list[DiagnosticEvent], stage: str, detail: str) -> None:
    event = DiagnosticEvent(at=datetime.now(UTC).isoformat(), stage=stage, detail=detail)
    events.append(event)
    _log.info("diagnose_live_feed stage=%s detail=%s", stage, detail)


def _check_profile(client_id: str, token: str, events: list[DiagnosticEvent]) -> None:
    """Read-only ``/profile`` call. Never raises past this function.

    A failure here is recorded as an event, not a fatal error: the WebSocket
    check below is the thing that actually answers the entitlement question,
    and a REST hiccup should not stop it from running.
    """
    try:
        from common.market_data.dhan import fetch_dhan_profile
    except ImportError as exc:  # pragma: no cover - dependency is pinned
        _record(events, "profile", f"dhanhq not importable: {exc}")
        return
    try:
        profile = fetch_dhan_profile(client_id=client_id, access_token=token)
    except Exception as exc:
        _record(events, "profile", f"REST /profile call failed: {type(exc).__name__}: {exc}")
        return
    safe = _scrub(profile) if isinstance(profile, dict) else "<non-dict response, not printed>"
    _record(events, "profile", f"REST /profile succeeded (redacted fields): {safe}")
    _record(
        events,
        "profile",
        "NOTE: a successful /profile call proves the token is valid for REST only — "
        "it does NOT by itself prove WebSocket market-data entitlement. Only a real "
        "tick over the feed below proves that.",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--seconds", type=float, default=15.0, help="Bounded wait for a first tick."
    )
    parser.add_argument(
        "--security-id", default="13", help="Instrument to subscribe to. Defaults to NIFTY (13)."
    )
    parser.add_argument(
        "--segment",
        type=int,
        default=0,
        help="Exchange segment (0=IDX_I, the NIFTY underlying's real segment).",
    )
    parser.add_argument(
        "--mode",
        choices=("ticker", "quote", "full"),
        default="ticker",
        help="Subscription mode. An index has no order book, so 'ticker' is correct here.",
    )
    parser.add_argument(
        "--skip-profile",
        action="store_true",
        help="Skip the read-only /profile REST check and go straight to the WebSocket.",
    )
    args = parser.parse_args(argv)

    settings = load_settings()
    paths = load_paths(settings=settings)
    paths.ensure_writable_dirs()
    redactor = setup_logging(
        level=settings.algo_log_level, log_dir=paths.log_root, settings=settings
    )

    events: list[DiagnosticEvent] = []

    client_id = _secret(settings.dhan_client_id)
    if not client_id:
        print("FAIL: DHAN_CLIENT_ID is not set. Fill it in .env (see .env.example).")
        return EXIT_NO_CREDENTIALS

    bootstrap = AuthBootstrap(
        AuthCredentials(
            client_id=client_id,
            pin=_secret(settings.dhan_pin),
            totp_secret=_secret(settings.dhan_totp_secret),
            access_token=_secret(settings.dhan_access_token),
        ),
        cache_dir=paths.cache_root,
        on_token_minted=lambda token: redactor.add_secrets([token]),
    )
    try:
        token, outcome = bootstrap.get_token()
    except AuthError as exc:
        print(f"FAIL: cannot authenticate ({type(exc).__name__}): {exc}")
        return EXIT_AUTH_FAILED
    _record(events, "auth", f"authenticated source={outcome.source}")

    if not args.skip_profile:
        _check_profile(client_id, token, events)

    # Imported here, after credentials resolve, so the module can be read and
    # linted with no Dhan SDK present — the same discipline
    # common.market_data.dhan and capture_live_tape.py both use. Deliberately
    # NOT importing build_dhan_order_client or anything from live_runtime:
    # this script never touches an order-placement surface.
    from common.market_data.dhan import DhanFeedError, DhanMarketFeedAdapter

    modes = {"ticker": 15, "quote": 17, "full": 21}
    adapter = DhanMarketFeedAdapter(
        client_id=client_id,
        access_token=token,
        exchange_segment=args.segment,
        instrument_label="DIAGNOSTIC",
        feed_mode=modes[args.mode],
    )
    adapter.subscribe([args.security_id])
    _record(
        events,
        "subscribe",
        f"requested security_id={args.security_id} segment={args.segment} mode={args.mode}",
    )

    result = DiagnosticResult(events=events)
    finished = threading.Event()
    deadline = time.monotonic() + args.seconds

    def _drive() -> None:
        """Run the feed on its own thread; stop it from inside its own callback.

        Same reasoning as capture_live_tape.py's _record_raw: the stop must
        happen on the thread that owns the SDK's asyncio loop, or
        close_connection() races a get_data() still in flight.
        """
        original_normalise = adapter.normalise

        def _spy(payload: object) -> Any:
            tick = original_normalise(payload)
            if tick is not None:
                result.ticks_received += 1
                if result.ticks_received == 1:
                    _record(
                        events,
                        "tick",
                        f"first real tick received security_id={tick.security_id} "
                        f"exchange_time={tick.exchange_time.isoformat()}",
                    )
            if (result.ticks_received or time.monotonic() >= deadline) and not finished.is_set():
                finished.set()
                adapter.stop()  # same thread as adapter.start()'s loop
            return tick

        adapter.normalise = _spy  # type: ignore[method-assign]
        try:
            adapter.start(lambda _tick: None)
        except DhanFeedError as exc:
            _record(events, "error", f"feed error: {exc}")
            finished.set()
        except Exception as exc:  # must not look like a silent pass
            _record(events, "error", f"unexpected exception: {type(exc).__name__}: {exc}")
            finished.set()
        else:
            _record(events, "close", "feed loop returned")

    thread = threading.Thread(target=_drive, daemon=True, name="diagnose-live-feed")
    thread.start()
    # Watchdog only — the real stop() call happens inside _spy above. If
    # literally zero frames ever arrive there is nothing to trigger a
    # same-thread stop from; +5s covers one final frame just after deadline.
    finished.wait(timeout=args.seconds + 5.0)
    thread.join(timeout=10.0)

    if adapter.last_disconnect_code is not None:
        from common.market_data.dhan import DISCONNECT_REASONS

        _record(
            events,
            "disconnect",
            f"code={adapter.last_disconnect_code} "
            f"reason={DISCONNECT_REASONS.get(adapter.last_disconnect_code, 'unknown')}",
        )

    if not finished.is_set():
        _record(events, "timeout", f"no stop signal within {args.seconds + 5:.0f}s watchdog")

    result.passed = result.ticks_received > 0
    result.reason = (
        f"received {result.ticks_received} real tick(s)"
        if result.passed
        else (events[-1].detail if events else "no events recorded")
    )

    print()
    print("=== diagnose_live_feed timeline ===")
    for event in events:
        print(f"  [{event.at}] {event.stage:<12s} {event.detail}")
    print()
    print(f"counters: {adapter.counters}")
    print()
    if result.passed:
        print(f"PASS: {result.reason}")
        return EXIT_PASS
    print(f"FAIL: {result.reason}")
    print(
        "Check: market hours (09:15-15:30 IST), the security id/segment, and whether "
        "the account has an active real-time market-data plan (Dhan disconnect code 806 "
        "= 'data APIs not subscribed on this account', logged above under 'disconnect' "
        "if it occurred)."
    )
    return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
