"""Minimal, dependency-free JWT payload inspection.

Ported from the reference repository's ``framework/auth/jwt_utils.py``. Dhan
access tokens are JWTs whose payload carries an ``exp`` (expiry, epoch seconds)
claim. We only ever *read* that claim to decide whether a cached token is worth
trying — the signature is verified server-side by Dhan on every API call, so
reading one unverified claim does not justify a PyJWT dependency.

``None`` means "expiry could not be determined", which callers must treat as
"don't know", never as "expired". Treating unknown as expired would throw away a
perfectly good token and force an unnecessary generation attempt against a
two-minute rate limit.
"""

from __future__ import annotations

import base64
import binascii
import json


def decode_token_exp(token: str) -> int | None:
    """Return the ``exp`` claim (epoch seconds) of a JWT, or ``None``.

    ``None`` covers every "cannot tell" case: not a JWT, malformed base64,
    non-JSON payload, no ``exp`` claim, or an ``exp`` that is not an integer.
    """
    if not token:
        return None
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload_b64 = parts[1]
    # base64url payloads are usually unpadded; restore padding before decoding.
    padding = "=" * (-len(payload_b64) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload_b64 + padding)
        claims = json.loads(raw)
    except (ValueError, binascii.Error):
        return None
    if not isinstance(claims, dict):
        return None
    exp = claims.get("exp")
    if exp is None or isinstance(exp, bool):
        # bool is an int subclass, and `exp: true` is not an expiry.
        return None
    try:
        return int(exp)
    except (TypeError, ValueError):
        return None
