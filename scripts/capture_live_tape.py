#!/usr/bin/env python3
"""Capture a real Dhan tape into a test fixture (Block 2 ratification).

    .venv/bin/python -m scripts.capture_live_tape [--seconds 30] [--security-id 13]

Subscribes to one or more instruments, records the **raw payloads** that
``get_data()`` returns, and writes them to ``tests/fixtures/dhan_ticker_payloads.json``.
Replaces the Block 1 fixture — which was synthesised by driving the SDK's own
parsers — with observed data, so every later phase tests against what Dhan
actually sends.

**Read-only.** It opens the market-feed WebSocket and nothing else. It places no
order, and there is no import path from this file to a broker adapter. Requires
market hours to capture anything interesting.

**Writes no secret into the fixture.** Payloads are recorded field by field
through an allowlist, and the file is scanned for credential-shaped keys before
being written. Ticker/Quote frames contain no account data, but a scan is cheaper
than trusting that to remain true.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from common.authentication import AuthBootstrap, AuthCredentials, AuthError
from common.config import load_settings
from common.config.paths import load_paths
from common.logging import get_logger, setup_logging

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_NO_CREDENTIALS = 2
EXIT_NO_DATA = 4

#: Keys that must never appear in a committed fixture.
_FORBIDDEN_KEYS = ("token", "secret", "pin", "totp", "client", "password", "auth")

_log = get_logger(__name__)


def _secret(holder: object) -> str | None:
    getter = getattr(holder, "get_secret_value", None)
    if getter is None:
        return None
    return str(getter()) or None


def _scrub(payload: object) -> object:
    """Keep only credential-free content, recursively."""
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--seconds", type=float, default=30.0, help="Capture duration.")
    parser.add_argument(
        "--security-id",
        action="append",
        default=None,
        help="Instrument to subscribe to; repeatable. Defaults to NIFTY 50 (13).",
    )
    parser.add_argument(
        "--segment", type=int, default=0, help="Exchange segment (0=IDX, 2=NSE_FNO)."
    )
    parser.add_argument("--label", default="NIFTY", help="Instrument label for the ticks.")
    parser.add_argument("--max-frames", type=int, default=200, help="Stop after this many frames.")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("tests/fixtures/dhan_ticker_payloads.json"),
        help="Fixture path to write.",
    )
    args = parser.parse_args(argv)

    security_ids = args.security_id or ["13"]

    settings = load_settings()
    paths = load_paths(settings=settings)
    paths.ensure_writable_dirs()
    redactor = setup_logging(log_dir=paths.log_root, settings=settings)

    client_id = _secret(settings.dhan_client_id)
    if not client_id:
        print("DHAN_CLIENT_ID is not set. Fill it in .env (see .env.example).")
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
        print(f"Cannot authenticate ({type(exc).__name__}): {exc}")
        return EXIT_FAILED

    print(f"Authenticated (source={outcome.source}). Capturing for {args.seconds:.0f}s...")

    # Imported here so the module can be read and linted without the SDK present.
    from common.market_data.dhan import DhanMarketFeedAdapter

    adapter = DhanMarketFeedAdapter(
        client_id=client_id,
        access_token=token,
        exchange_segment=args.segment,
        instrument_label=args.label,
    )
    adapter.subscribe(security_ids)

    captured: list[dict[str, Any]] = []
    lock = threading.Lock()
    finished = threading.Event()

    def _record_raw() -> None:
        """Drive the feed, recording each raw payload before normalisation."""
        original_normalise = adapter.normalise

        def _spy(payload: object) -> Any:
            with lock:
                if len(captured) < args.max_frames:
                    captured.append(
                        {
                            "label": _label_for(payload),
                            "captured_at": datetime.now(UTC).isoformat(),
                            "payload": _scrub(payload),
                        }
                    )
                else:
                    finished.set()
            return original_normalise(payload)

        adapter.normalise = _spy  # type: ignore[method-assign]
        try:
            adapter.start(lambda _tick: None)
        except Exception as exc:  # a capture failure must not look like success
            _log.error("capture feed failed: %s", exc)
            finished.set()

    thread = threading.Thread(target=_record_raw, daemon=True)
    thread.start()

    deadline = time.monotonic() + args.seconds
    while time.monotonic() < deadline and not finished.is_set():
        time.sleep(0.2)

    adapter.stop()
    thread.join(timeout=10.0)

    with lock:
        frames = list(captured)

    if not frames:
        print(
            f"No frames captured in {args.seconds:.0f}s. Check market hours "
            f"(09:15-15:30 IST), the security id ({security_ids}) and the segment."
        )
        return EXIT_NO_DATA

    counters = {
        "ticks": adapter.counters.ticks,
        "non_tick_frames": adapter.counters.non_tick_frames,
        "malformed_payloads": adapter.counters.malformed_payloads,
        "exchange_time_fallbacks": adapter.counters.exchange_time_fallbacks,
        "untraded_frames": adapter.counters.untraded_frames,
        "empty_frames": adapter.counters.empty_frames,
    }
    document: dict[str, Any] = {
        "source": "captured",
        "note": (
            "Raw payloads recorded from a live Dhan market-feed connection during "
            "Block 2 ratification. Read-only capture; no order was placed. Scrubbed "
            "of credential-shaped keys and verified below."
        ),
        "sdk_version": "2.2.0",
        "captured_at": datetime.now(UTC).isoformat(),
        "instruments": security_ids,
        "segment": args.segment,
        "counters": counters,
        "frames": frames,
    }

    serialised = json.dumps(document, indent=2, default=str) + "\n"

    # Belt and braces: refuse to write a fixture containing a credential-shaped
    # key, even though the payloads were already scrubbed field by field.
    lowered = serialised.lower()
    for word in ("accesstoken", "access_token", "dhanclientid", '"pin"', '"totp"'):
        if word in lowered:
            print(f"Refusing to write the fixture: it contains {word!r}.")
            return EXIT_FAILED
    if client_id.lower() in lowered:
        print("Refusing to write the fixture: it contains the client id.")
        return EXIT_FAILED

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(serialised, encoding="utf-8")

    print(f"Wrote {len(frames)} frames to {args.out}")
    print("  counters:")
    for name, value in counters.items():
        print(f"    {name:26s} {value}")
    if adapter.counters.exchange_time_fallbacks:
        print(
            "  WARNING: some ticks fell back to receipt time. The LTT format may "
            "have changed — reconstruct_exchange_time needs review."
        )
    return EXIT_OK


def _label_for(payload: object) -> str:
    if isinstance(payload, str):
        return "status_string"
    if payload is None:
        return "empty"
    if isinstance(payload, dict):
        return str(payload.get("type", "unknown")).lower().replace(" ", "_")
    return "unexpected"


if __name__ == "__main__":
    sys.exit(main())
