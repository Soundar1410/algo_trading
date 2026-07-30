#!/usr/bin/env python3
"""Pre-market authentication bootstrap — the spec's single token entry point.

    .venv/bin/python -m scripts.auth_bootstrap [--force] [--status] [--refresh]

Generates or reuses a Dhan access token and writes it to the atomic cache that
every supervisor and worker reads. Run once before the market opens; no trading
runtime generates its own token (spec line 1178).

**Read-only with respect to trading.** It calls exactly two endpoints:

* ``POST https://auth.dhan.co/app/generateAccessToken`` — credentials in, token
  out. Creates no order.
* ``GET  https://api.dhan.co/v2/profile`` — the spec's "safe read request" used
  to validate the token.

Order placement lives on ``/orders`` (``POST``/``PUT``/``DELETE``), which nothing
in this file, and nothing in Phase 2, touches. There is no import path from here
to a broker adapter.

**No secret is ever printed.** Output is limited to status, source, expiry and
request counts. The logging redactor is installed before anything else runs, and
the freshly minted token is registered with it the moment it exists.
"""

from __future__ import annotations

import argparse
import sys

from common.authentication import (
    AuthBootstrap,
    AuthCredentials,
    AuthError,
    TokenRejectedRecentlyError,
)
from common.config import load_settings
from common.config.paths import load_paths
from common.logging import get_logger, setup_logging

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_NO_CREDENTIALS = 2
EXIT_COOLDOWN = 3

_log = get_logger(__name__)


def _secret(holder: object) -> str | None:
    """Read a SecretStr without letting it reach a log or stdout."""
    getter = getattr(holder, "get_secret_value", None)
    if getter is None:
        return None
    value = str(getter())
    return value or None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Clear a recorded credential-rejection cooldown before trying. Use "
            "after fixing DHAN_PIN or DHAN_TOTP_SECRET in .env, so a corrected "
            "credential does not have to wait out the timer."
        ),
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Replace the cached token even if it still looks valid (Dhan code 807).",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Report what is cached and make no network call at all.",
    )
    args = parser.parse_args(argv)

    settings = load_settings()
    paths = load_paths(settings=settings)
    paths.ensure_writable_dirs()
    # Installed first: everything below this line is redacted.
    redactor = setup_logging(log_dir=paths.log_root, settings=settings)

    client_id = _secret(settings.dhan_client_id)
    if not client_id:
        print("DHAN_CLIENT_ID is not set. Fill it in .env (see .env.example).")
        return EXIT_NO_CREDENTIALS

    credentials = AuthCredentials(
        client_id=client_id,
        pin=_secret(settings.dhan_pin),
        totp_secret=_secret(settings.dhan_totp_secret),
        access_token=_secret(settings.dhan_access_token),
    )

    bootstrap = AuthBootstrap(
        credentials,
        cache_dir=paths.cache_root,
        # Mask the minted token before any code path can log it.
        on_token_minted=lambda token: redactor.add_secrets([token]),
    )

    if args.status:
        return _report_status(bootstrap, credentials)

    if args.force:
        bootstrap.clear_cooldown()
        print("Cleared any recorded credential-rejection cooldown.")

    try:
        if args.refresh:
            cached = bootstrap.cache.load(expected_client_id=client_id)
            token, outcome = bootstrap.refresh(
                current_token=cached.access_token if cached else None
            )
        else:
            token, outcome = bootstrap.get_token()
    except TokenRejectedRecentlyError as exc:
        print(f"Refused without contacting Dhan: {exc}")
        return EXIT_COOLDOWN
    except AuthError as exc:
        print(f"Authentication failed ({type(exc).__name__}): {exc}")
        print(f"Retryable: {exc.retryable}")
        return EXIT_FAILED

    print("Authentication OK.")
    print(f"  source        : {outcome.source}")
    print(f"  expiry        : {outcome.expiry_time or 'unknown'}")
    print(f"  auth requests : {outcome.requests_made}")
    print(f"  token cache   : {bootstrap.cache.path}")

    # Validate with the safe read request the spec requires. A validation failure
    # is reported but does not discard the token: the request itself may have
    # failed, and throwing away a good token would cost a generation attempt
    # against a ~1-per-2-minute limit.
    try:
        valid = bootstrap.validate(token)
    except AuthError as exc:
        print(f"  validation    : inconclusive ({exc})")
        return EXIT_OK

    print(f"  validation    : {'accepted by Dhan' if valid else 'REJECTED by Dhan'}")
    if not valid:
        print("    The token was rejected. Re-run with --refresh to mint a new one.")
        return EXIT_FAILED
    return EXIT_OK


def _report_status(bootstrap: AuthBootstrap, credentials: AuthCredentials) -> int:
    """Describe local state. Makes no network call."""
    print(f"Token cache : {bootstrap.cache.path}")
    cached = bootstrap.cache.load(expected_client_id=credentials.client_id)
    if cached is None:
        print("  no usable cached token for this client id")
    else:
        remaining = cached.seconds_until_expiry()
        window = (
            f" ({remaining / 60:.0f} min left)" if remaining is not None else " (expiry unknown)"
        )
        print(f"  created  : {cached.created_at or 'unknown'}")
        print(f"  expiry   : {cached.expiry_time or 'unknown'}")
        print(f"  usable   : {'yes' if cached.is_usable() else 'no'}{window}")

    blocked = bootstrap.cooldown.active()
    if blocked is None:
        print("Cooldown    : none")
    else:
        left = blocked.remaining(cooldown_seconds=bootstrap.cooldown.cooldown_seconds)
        print(f"Cooldown    : active, {left:.0f}s remaining — {blocked.reason}")

    print(f"Can generate: {'yes' if credentials.can_generate else 'no (needs PIN + TOTP secret)'}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
