"""Secret redaction for log records.

The spec is unambiguous: client ID, PIN, TOTP secret, access token and Telegram
token must never be printed, persisted or notified. Relying on every call site
to remember that is how secrets end up in a log file, so redaction is enforced
once, at the logging boundary, where nothing can route around it.

Two complementary strategies:

* **Value redaction** — the concrete secrets loaded from ``.env`` are matched
  literally. This catches an unwitting ``log.info("token=%s", token)``.
* **Pattern redaction** — ``key=value`` / ``"key": "value"`` shapes with a
  sensitive-looking key are masked even when the value was never in ``.env``.
  This catches a secret echoed back from a broker response, which value
  redaction alone cannot know about.

Filters mutate the formatted message rather than the argument tuple, so a
downstream handler cannot reassemble the original.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from re import Pattern
from typing import Protocol, runtime_checkable

REDACTED = "***REDACTED***"

#: What :class:`SecretRedactingFilter` accepts: one value, many values, or none.
SecretSource = Iterable[str | None] | str | None


@runtime_checkable
class SupportsSecretValue(Protocol):
    """The ``SecretStr`` shape, structurally.

    Typed as a protocol so this module stays independent of
    :mod:`common.config` — redaction must be usable before configuration has
    successfully loaded, which is exactly when errors get logged.
    """

    def get_secret_value(self) -> str: ...


#: Minimum length before a literal value is worth masking. Short values (a
#: one-character PIN in a test, an empty string) would otherwise match
#: everywhere and turn every log line into noise.
_MIN_SECRET_LENGTH = 4

#: Keys whose values are masked wherever they appear in a message.
_SENSITIVE_KEYS = (
    "access_token",
    "accesstoken",
    "account_identity_pepper",
    "api_key",
    "apikey",
    "app_secret",
    "authorization",
    "bot_token",
    "client_id",
    "clientid",
    "dhan_client_id",
    "dhan_pin",
    "dhan_totp_secret",
    # Dhan's own query-parameter and header spelling. Listed explicitly because
    # `\bclientid\b` cannot match inside `dhanClientId` — there is no word
    # boundary between "dhan" and "Client", so the camelCase form slipped through
    # pattern redaction entirely and was only caught when the literal value
    # happened to be registered.
    "dhanclientid",
    "password",
    "pin",
    "secret",
    "telegram_bot_token",
    "token",
    "totp",
    "totp_secret",
)

_KEY_ALTERNATION = "|".join(sorted(_SENSITIVE_KEYS, key=len, reverse=True))

#: ``key=value``, ``key: value``, ``"key": "value"`` — quoted or bare.
#:
#: ``&`` terminates a value so each query parameter of a URL is matched on its
#: own. Without it the value match ran greedily to the next whitespace, so
#: ``?dhanClientId=X&pin=Y&totp=Z`` collapsed into a single redaction starting at
#: the first *recognised* key — masking later secrets only by accident of
#: ordering, leaving earlier ones exposed, and destroying any non-secret
#: parameter that trailed one.
_PATTERN_RULES: tuple[Pattern[str], ...] = (
    re.compile(
        rf'(?i)(["\']?\b(?:{_KEY_ALTERNATION})\b["\']?\s*[:=]\s*)(["\']?)([^\s,;&}}\)"\']+)(\2)'
    ),
)


def _redact_patterns(message: str) -> str:
    for rule in _PATTERN_RULES:
        message = rule.sub(rf"\1\2{REDACTED}\4", message)
    return message


class SecretRedactingFilter(logging.Filter):
    """Masks known secret values and sensitive-looking key/value pairs.

    Attached to handlers rather than loggers: a filter on a logger does not run
    for records propagated from child loggers, which would leave a whole subtree
    unprotected.
    """

    def __init__(self, secrets: SecretSource = (), name: str = "") -> None:
        super().__init__(name)
        self._literals: list[str] = []
        self.add_secrets(secrets)

    def add_secrets(self, secrets: SecretSource) -> None:
        """Register literal secret values to mask. Short/empty values are skipped."""
        if secrets is None:
            return
        values: Iterable[str | None] = (secrets,) if isinstance(secrets, str) else secrets
        for secret in values:
            if secret is None:
                continue
            text = str(secret)
            if len(text) >= _MIN_SECRET_LENGTH and text not in self._literals:
                self._literals.append(text)
        # Longest first, so a secret that contains another is masked whole.
        self._literals.sort(key=len, reverse=True)

    def redact(self, message: str) -> str:
        for literal in self._literals:
            message = message.replace(literal, REDACTED)
        return _redact_patterns(message)

    def filter(self, record: logging.LogRecord) -> bool:
        # Render once here so args cannot carry an unredacted copy downstream.
        try:
            rendered = record.getMessage()
        except Exception:
            # A broken format string is a bug in the caller, not a reason to
            # lose the whole logging pipeline. Let the record through
            # unredacted-but-unrendered rather than raising inside a handler.
            return True
        redacted = self.redact(rendered)
        if redacted != rendered:
            record.msg = redacted
            record.args = ()
        return True


def secrets_from_settings(settings: object) -> tuple[str, ...]:
    """Extract the concrete secret values from a :class:`~common.config.Settings`.

    Reads through ``SecretStr`` deliberately — this is the one place the plain
    values are needed, precisely so they can be masked everywhere else.
    """
    values: list[str] = []
    for attr in (
        "dhan_client_id",
        "dhan_pin",
        "dhan_totp_secret",
        # Added in Phase 2. Its absence here was a real gap: the field existed
        # nowhere, so once introduced it would have been the one credential in
        # .env that pattern redaction alone had to catch.
        "dhan_access_token",
        "telegram_bot_token",
        "telegram_chat_id",
        # Phase 10. Not a Dhan credential, but its whole purpose is to make an
        # HMAC irreversible — the same "never let it leak" property as the
        # things above, for the same reason (see Settings.account_identity_pepper).
        "account_identity_pepper",
    ):
        holder = getattr(settings, attr, None)
        if holder is None:
            continue
        raw = holder.get_secret_value() if isinstance(holder, SupportsSecretValue) else str(holder)
        if raw:
            values.append(raw)
    return tuple(values)
