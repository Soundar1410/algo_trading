"""Exception taxonomy for Dhan authentication.

The operationally important distinction is **retryable or not**, and here it is
an explicit class attribute rather than a consequence of the class hierarchy.

The reference implementation makes ``InvalidCredentialsError`` and
``TokenRateLimitedError`` subclasses of the retryable
``TokenGenerationError``, so its correctness depends entirely on the order of
``except`` clauses at the call site: reorder them and a wrong PIN silently
becomes something the bootstrap retries. Retrying a rejected credential is
exactly what risks an account lockout, so that property is too important to
leave implicit. :attr:`AuthError.retryable` states it, the bootstrap branches on
it, and a test asserts the value for every class in this module.
"""

from __future__ import annotations


class AuthError(Exception):
    """Base class for every authentication failure.

    Subclasses set :attr:`retryable`. The default is ``False`` — a new failure
    mode is assumed permanent until someone has thought about it, because the
    cost of wrongly retrying (burnt token-generation attempts, possible lockout)
    is much higher than the cost of wrongly failing closed.
    """

    retryable: bool = False


class MissingCredentialsError(AuthError):
    """No usable credentials: neither a valid token nor client id + PIN + TOTP secret."""


class TokenStoreError(AuthError):
    """The token cache could not be read or written (I/O, permissions, corruption)."""


class TokenGenerationError(AuthError):
    """Generation failed for a *transient* reason — network, timeout, 5xx.

    The only retryable failure. Bounded retries with a delay are appropriate.
    """

    retryable = True


class InvalidCredentialsError(AuthError):
    """Dhan rejected the credentials — wrong PIN, TOTP or client id.

    Permanent. Retrying with the same secrets cannot succeed and risks locking
    the account, so the bootstrap fails immediately and records a cooldown.
    """


class TokenRateLimitedError(AuthError):
    """Dhan refused because a token was requested too recently.

    Dhan caps generation at roughly one request every two minutes. Retrying
    within a retry window of seconds is futile, so this fails fast rather than
    burning the remaining attempts.
    """


class TokenRejectedRecentlyError(AuthError):
    """A previous attempt was rejected and the cooldown has not expired.

    Raised **without contacting Dhan at all**. This is what stops N concurrent
    workers, or an operator re-running the bootstrap, from turning one bad PIN
    into many rejected login attempts.
    """
