"""Dhan authentication: TOTP login, token cache, and the bootstrap.

The spec's model (section "Authentication design", line 1360): one pre-market
bootstrap generates or refreshes the access token and writes an atomic cache that
every supervisor and worker reads. No runtime independently generates a token.

Nothing in this package imports ``dhanhq``. Authentication speaks to Dhan's HTTP
endpoints through ``httpx`` directly, which keeps
:mod:`common.market_data.dhan` the single importer of the SDK — a rule enforced
by test, not convention.
"""

from __future__ import annotations

from .bootstrap import (
    AuthBootstrap,
    AuthCredentials,
    TokenOutcome,
)
from .dhan_login import (
    AUTH_ENDPOINT,
    PROFILE_ENDPOINT,
    DhanTotpLogin,
    GeneratedToken,
    validate_token,
)
from .exceptions import (
    AuthError,
    InvalidCredentialsError,
    MissingCredentialsError,
    TokenGenerationError,
    TokenRateLimitedError,
    TokenRejectedRecentlyError,
    TokenStoreError,
)
from .jwt_claims import decode_token_exp
from .token_cache import (
    Rejection,
    RejectionCooldown,
    StoredToken,
    TokenCache,
)
from .totp import TotpProvider, build_totp_provider, clock_skew_note

__all__ = [
    "AUTH_ENDPOINT",
    "PROFILE_ENDPOINT",
    "AuthBootstrap",
    "AuthCredentials",
    "AuthError",
    "DhanTotpLogin",
    "GeneratedToken",
    "InvalidCredentialsError",
    "MissingCredentialsError",
    "Rejection",
    "RejectionCooldown",
    "StoredToken",
    "TokenCache",
    "TokenGenerationError",
    "TokenOutcome",
    "TokenRateLimitedError",
    "TokenRejectedRecentlyError",
    "TokenStoreError",
    "TotpProvider",
    "build_totp_provider",
    "clock_skew_note",
    "decode_token_exp",
    "validate_token",
]
