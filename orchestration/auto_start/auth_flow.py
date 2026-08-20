"""Authenticate, then prove Dhan actually accepts the token.

"Authentication succeeded" here means one thing only: Dhan answered a
read-only profile request with the token. It never means a token file exists,
and it never means a JWT ``exp`` claim looks far enough away — a cached token
can satisfy both and still be dead, which is exactly the case Dhan's own
disconnect code 807 exists to report.

The subtle failure this module is shaped around: when validation rejects a
*cached* token, calling :meth:`AuthBootstrap.get_token` again is useless. It
would find the same still-unexpired cache entry and hand back the same dead
token, on every retry, until the session deadline — a loop that looks like
diligence and accomplishes nothing. :meth:`AuthBootstrap.refresh` is the
correct door: passing ``current_token`` makes the under-lock cache check
discriminating, so a token another process minted meanwhile is accepted while
the known-bad one never is.
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass
from datetime import datetime, timedelta

from common.authentication import AuthBootstrap, TokenOutcome
from common.logging import get_logger

from .retry import TerminalStartupError

_log = get_logger(__name__)

#: How many times one attempt may mint a replacement before concluding the
#: credentials themselves are the problem. Deliberately tiny: each refresh is a
#: real login request against Dhan, and a freshly minted token that Dhan then
#: refuses is not a transient condition.
DEFAULT_MAX_REFRESH_ATTEMPTS = 2


@dataclass(frozen=True)
class ValidatedToken:
    """A token Dhan has affirmatively accepted."""

    outcome: TokenOutcome
    #: Number of refreshes it took. Non-zero means a cached token was rejected.
    refreshes: int

    @property
    def source(self) -> str:
        return self.outcome.source

    @property
    def expiry_time(self) -> str | None:
        return self.outcome.expiry_time


def cooldown_ready_at(bootstrap: AuthBootstrap, *, now: datetime) -> datetime:
    """When a recorded credential rejection stops suppressing logins.

    Returns ``now`` when nothing is in force. The cooldown is stored as a wall
    clock ``time.time()`` value by :class:`~common.authentication.RejectionCooldown`,
    so the remaining seconds are converted onto ``now``'s own timeline rather
    than compared across two different clocks.
    """
    cooldown = bootstrap.cooldown
    rejection = cooldown.active()
    if rejection is None:
        return now
    remaining = rejection.remaining(
        cooldown_seconds=cooldown.cooldown_seconds, now=_time.time()
    )
    return now + timedelta(seconds=remaining)


def authenticate_and_validate(
    bootstrap: AuthBootstrap,
    *,
    max_refresh_attempts: int = DEFAULT_MAX_REFRESH_ATTEMPTS,
) -> ValidatedToken:
    """Obtain a token and prove Dhan accepts it, or raise.

    Every exception raised out of here is classified by
    :func:`orchestration.auto_start.retry.classify`: the auth layer's own
    exceptions carry their retryability, and the one verdict this module adds
    itself — a freshly generated token that Dhan still refuses — is terminal.
    """
    token, outcome = bootstrap.get_token()

    if bootstrap.validate(token):
        _log.info("Dhan accepted the %s token", outcome.source)
        return ValidatedToken(outcome=_validated(outcome), refreshes=0)

    for attempt in range(1, max_refresh_attempts + 1):
        _log.warning(
            "Dhan rejected the %s token; refreshing (attempt %d/%d) rather than "
            "re-reading the same cache entry",
            outcome.source,
            attempt,
            max_refresh_attempts,
        )
        # `current_token` is what makes this discriminating: the known-bad
        # token is never handed back, but a replacement another process minted
        # under the shared refresh lock is.
        token, outcome = bootstrap.refresh(current_token=token)
        if bootstrap.validate(token):
            _log.info("Dhan accepted the refreshed %s token", outcome.source)
            return ValidatedToken(outcome=_validated(outcome), refreshes=attempt)

    raise TerminalStartupError(
        f"Dhan rejected the access token after {max_refresh_attempts} refresh "
        "attempt(s). A freshly generated token being refused is a credential or "
        "account problem, not a transient one — check DHAN_CLIENT_ID, DHAN_PIN "
        "and DHAN_TOTP_SECRET in .env, and that the account is not suspended. "
        "Retrying automatically would risk an account lockout."
    )


def _validated(outcome: TokenOutcome) -> TokenOutcome:
    """The same outcome, marked as having passed Dhan's own check."""
    return TokenOutcome(
        source=outcome.source,
        expiry_time=outcome.expiry_time,
        requests_made=outcome.requests_made,
        validated=True,
    )
