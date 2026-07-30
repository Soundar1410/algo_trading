"""The authentication bootstrap — the single entry point for obtaining a token.

Nothing else in the platform knows how authentication works. Supervisors and
workers call :meth:`AuthBootstrap.get_token`; strategies never see any of this.

Token precedence, first usable wins (ported from the reference repository's
``framework/auth/manager.py``):

1. A still-usable ``DHAN_ACCESS_TOKEN`` from the environment — the manual
   override, which keeps the copy-paste workflow working and is what Phase 1's
   smoke test used.
2. A still-usable token in the on-disk cache.
3. A freshly generated token, which requires client id + PIN + TOTP secret.

**Fails closed.** When no token can be obtained the caller gets an exception and
the runtime does not start. There is no degraded mode: a trading process with no
market data is more dangerous than one that refused to start, because it looks
alive.

The whole design of the failure path exists to make a wrong PIN cost exactly one
request to Dhan. See :meth:`_generate`.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from common.logging import get_logger

from .dhan_login import DhanTotpLogin, GeneratedToken, validate_token
from .exceptions import (
    AuthError,
    InvalidCredentialsError,
    MissingCredentialsError,
    TokenGenerationError,
    TokenRejectedRecentlyError,
)
from .token_cache import (
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_EXPIRY_MARGIN_SECONDS,
    RejectionCooldown,
    StoredToken,
    TokenCache,
)
from .totp import TotpProvider, build_totp_provider

_log = get_logger(__name__)

#: Retries apply *only* to transient failures (network, timeout, 5xx). Every
#: credential-related failure is one attempt, full stop.
DEFAULT_RETRY_COUNT = 3
DEFAULT_RETRY_DELAY_SECONDS = 5.0

TOKEN_CACHE_FILENAME = "token_cache.json"
COOLDOWN_FILENAME = "token_cache.rejected.json"


@dataclass(frozen=True)
class AuthCredentials:
    """Everything the bootstrap may use. Values come from ``.env`` only."""

    client_id: str
    pin: str | None = None
    totp_secret: str | None = None
    access_token: str | None = None

    @property
    def can_generate(self) -> bool:
        return bool(self.client_id and self.pin and self.totp_secret)


@dataclass(frozen=True)
class TokenOutcome:
    """What :meth:`AuthBootstrap.get_token` produced, minus the token itself.

    Returned alongside the token so callers can log and persist an audit trail
    without ever handling the secret to do so.
    """

    source: str  # "environment" | "cache" | "generated"
    expiry_time: str | None
    requests_made: int
    validated: bool


class AuthBootstrap:
    """Obtains a valid Dhan access token, generating one only when necessary."""

    def __init__(
        self,
        credentials: AuthCredentials,
        *,
        cache_dir: Path,
        totp_provider: TotpProvider | None = None,
        login: DhanTotpLogin | None = None,
        token_validator: Callable[[str, str], bool] | None = None,
        expiry_margin_seconds: int = DEFAULT_EXPIRY_MARGIN_SECONDS,
        cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
        retry_count: int = DEFAULT_RETRY_COUNT,
        retry_delay: float = DEFAULT_RETRY_DELAY_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        on_token_minted: Callable[[str], None] | None = None,
    ) -> None:
        self._credentials = credentials
        self._cache = TokenCache(cache_dir / TOKEN_CACHE_FILENAME)
        self._cooldown = RejectionCooldown(
            cache_dir / COOLDOWN_FILENAME, cooldown_seconds=cooldown_seconds
        )
        self._expiry_margin = expiry_margin_seconds
        self._retry_count = max(1, retry_count)
        self._retry_delay = retry_delay
        self._sleep = sleep
        #: Called with each newly obtained token so the logging redactor can mask
        #: it. A generated token was never in ``.env``, so value-based redaction
        #: cannot know about it unless it is registered here, the moment it exists.
        self._on_token_minted = on_token_minted
        self._validator = token_validator

        self._login = login
        if self._login is None and credentials.can_generate:
            provider = totp_provider or build_totp_provider(str(credentials.totp_secret))
            self._login = DhanTotpLogin(
                credentials.client_id,
                str(credentials.pin),
                provider,
            )

    @property
    def cache(self) -> TokenCache:
        return self._cache

    @property
    def cooldown(self) -> RejectionCooldown:
        return self._cooldown

    # ------------------------------------------------------------------ public
    def get_token(self) -> tuple[str, TokenOutcome]:
        """Return a usable token by the precedence rules, or fail closed."""
        env_token = self._credentials.access_token
        if env_token and self._usable(env_token):
            self._announce(env_token)
            return env_token, TokenOutcome("environment", None, 0, False)

        cached = self._cache.load(expected_client_id=self._credentials.client_id)
        if cached is not None and cached.is_usable(margin_seconds=self._expiry_margin):
            self._announce(cached.access_token)
            _log.info("using cached access token expiry=%s", cached.expiry_time or "unknown")
            return cached.access_token, TokenOutcome("cache", cached.expiry_time, 0, False)

        if self._login is None:
            raise MissingCredentialsError(
                "No usable Dhan access token, and no way to generate one. Provide "
                "either a fresh DHAN_ACCESS_TOKEN, or DHAN_CLIENT_ID + DHAN_PIN + "
                "DHAN_TOTP_SECRET in .env for the headless TOTP login."
            )

        if env_token and not self._usable(env_token):
            _log.info("DHAN_ACCESS_TOKEN is expired; generating a new token")

        token, expiry, requests_made = self._generate()
        return token, TokenOutcome("generated", expiry, requests_made, False)

    def refresh(self, current_token: str | None = None) -> tuple[str, TokenOutcome]:
        """Replace a token Dhan has stopped honouring.

        Used when the feed reports Dhan disconnect code **807 (access token
        expired)**. The distinguishing feature of that situation is that the
        cached token still looks valid by its ``exp`` claim while the server has
        already rejected it, so expiry-based checks cannot detect it.

        ``current_token`` is the token known to be dead. Passing it makes the
        under-lock cache check *discriminating* rather than skipped: a token
        another process minted in the meantime is accepted (that is the whole
        point of the lock — four workers hitting 807 together should cause one
        login), but the known-bad token is never handed back. Omitting it forces
        an unconditional login.
        """
        if self._login is None:
            raise MissingCredentialsError(
                "Cannot refresh the token: DHAN_PIN and DHAN_TOTP_SECRET are required."
            )
        token, expiry, requests_made = self._generate(
            replacing=current_token,
            force=current_token is None,
        )
        source = "cache" if requests_made == 0 else "generated"
        return token, TokenOutcome(source, expiry, requests_made, False)

    def validate(self, token: str) -> bool:
        """Validate a token against Dhan with a read-only profile request."""
        validator = self._validator
        if validator is not None:
            return validator(self._credentials.client_id, token)
        return validate_token(self._credentials.client_id, token)

    def clear_cooldown(self) -> None:
        """Discard a recorded rejection — the ``--force`` path."""
        self._cooldown.clear()

    # ---------------------------------------------------------------- internals
    def _usable(self, token: str) -> bool:
        probe = StoredToken(
            access_token=token,
            client_id=self._credentials.client_id,
            created_at="",
            expiry_time=None,
        )
        return probe.is_usable(margin_seconds=self._expiry_margin)

    def _announce(self, token: str) -> None:
        if self._on_token_minted is not None:
            self._on_token_minted(token)

    def _generate(
        self, *, replacing: str | None = None, force: bool = False
    ) -> tuple[str, str | None, int]:
        """Mint a token under the refresh lock. Returns (token, expiry, requests).

        The failure policy, which is the point of this method:

        * **Cooldown first.** A recorded rejection short-circuits *before any
          network call*, so concurrent workers and repeated invocations cost
          zero requests rather than one each.
        * **Retry only what is retryable**, decided by ``exc.retryable`` rather
          than by the order of ``except`` clauses. A credential rejection or a
          rate-limit answer is one attempt, and a rejection is recorded so the
          next caller does not repeat it.
        * **Double-check the cache under the lock**, because a process that was
          blocked on the lock is very likely waiting for a token another process
          was in the middle of minting. ``replacing`` names a token already known
          to be dead, which the check must therefore not hand back, and ``force``
          skips the check entirely — see :meth:`refresh`.
        """
        login = self._login
        assert login is not None  # guarded by every caller

        blocked = self._cooldown.active()
        if blocked is not None:
            remaining = blocked.remaining(cooldown_seconds=self._cooldown.cooldown_seconds)
            raise TokenRejectedRecentlyError(
                f"Dhan rejected these credentials {int(time.time() - blocked.rejected_at)}s "
                f"ago and the cooldown has {remaining:.0f}s left, so no login was "
                f"attempted. Reason recorded: {blocked.reason} "
                f"Fix the credentials in .env, then clear the cooldown "
                f"({self._cooldown.path}) or re-run with --force."
            )

        requests_before = login.request_count
        with self._cache.refresh_lock():
            cached = self._cache.load(expected_client_id=self._credentials.client_id)
            if (
                not force
                and cached is not None
                and cached.is_usable(margin_seconds=self._expiry_margin)
                and cached.access_token != replacing
            ):
                _log.info("another process minted a token while we waited on the lock")
                self._announce(cached.access_token)
                return cached.access_token, cached.expiry_time, 0

            # Re-check inside the lock: the process that just released it may
            # have recorded a rejection rather than a token.
            blocked = self._cooldown.active()
            if blocked is not None:
                raise TokenRejectedRecentlyError(
                    "Another process had its credentials rejected while we waited "
                    f"on the refresh lock, so no login was attempted. Reason: {blocked.reason}"
                )

            last_error: AuthError | None = None
            for attempt in range(1, self._retry_count + 1):
                try:
                    generated = login.generate()
                except AuthError as exc:
                    if not exc.retryable:
                        if isinstance(exc, InvalidCredentialsError):
                            self._cooldown.record(str(exc))
                        _log.error("token generation failed permanently: %s", exc)
                        raise
                    last_error = exc
                    _log.warning(
                        "token generation attempt %d/%d failed transiently: %s",
                        attempt,
                        self._retry_count,
                        exc,
                    )
                    if attempt < self._retry_count:
                        self._sleep(self._retry_delay)
                    continue

                return self._persist(generated, login.request_count - requests_before)

            raise TokenGenerationError(
                f"Token generation failed after {self._retry_count} transient "
                f"attempt(s): {last_error}"
            )

    def _persist(
        self, generated: GeneratedToken, requests_made: int
    ) -> tuple[str, str | None, int]:
        # Register with the redactor before anything else can log it.
        self._announce(generated.access_token)
        self._cooldown.clear()
        self._cache.save(
            generated.access_token,
            client_id=self._credentials.client_id,
            expiry_time=generated.expiry_time,
        )
        _log.info(
            "authentication successful; token cached expiry=%s",
            generated.expiry_time or "unknown",
        )
        return generated.access_token, generated.expiry_time, requests_made
