"""Logging setup — structured records, console + rotating file, redaction always on.

Structured ``key=value`` context is appended to each line so logs stay greppable
(``grep 'strategy_id=io_fixture_v1'``) without pulling in a JSON logging
dependency. The spec asks for structured logs to keep an AWS migration open;
this format is machine-parseable and still readable in a terminal at 07:00.

Every handler carries :class:`~common.logging.redaction.SecretRedactingFilter`.
There is no way to obtain a logger from this module without it.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path
from typing import Any

from .redaction import SecretRedactingFilter, secrets_from_settings

DEFAULT_FORMAT = "%(asctime)s %(levelname)-8s %(name)s %(message)s"
DEFAULT_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"

_MAX_BYTES = 10 * 1024 * 1024
_BACKUP_COUNT = 10

#: Set by :func:`setup_logging` so later calls can extend the secret list
#: (for example once a token is fetched) without rebuilding every handler.
_ACTIVE_FILTER: SecretRedactingFilter | None = None


class StructuredAdapter(logging.LoggerAdapter):  # type: ignore[type-arg]
    """Appends stable ``key=value`` context to every message from this logger."""

    def process(self, msg: Any, kwargs: Any) -> tuple[Any, Any]:
        context = " ".join(f"{k}={v}" for k, v in sorted((self.extra or {}).items()))
        return (f"{msg} {context}" if context else msg), kwargs


def setup_logging(
    *,
    level: str = "INFO",
    log_dir: Path | None = None,
    log_file_name: str = "algo_trading.log",
    settings: object | None = None,
    console: bool = True,
) -> SecretRedactingFilter:
    """Configure root logging. Idempotent — safe to call again in a worker.

    Args:
        level: root log level name.
        log_dir: directory for the rotating file handler. When None, logs go to
            the console only (the default in tests, which must not litter disk).
        settings: optional :class:`~common.config.Settings`; its secret values
            are registered for literal redaction.
        console: whether to attach a stream handler.

    Returns:
        The active redaction filter, so a caller can register secrets discovered
        later (an access token, say) with ``.add_secrets(...)``.
    """
    global _ACTIVE_FILTER

    redactor = SecretRedactingFilter()
    if settings is not None:
        redactor.add_secrets(secrets_from_settings(settings))

    root = logging.getLogger()
    # Replace prior handlers so a re-entrant call cannot double-log every line.
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    root.setLevel(level.upper())
    formatter = logging.Formatter(DEFAULT_FORMAT, datefmt=DEFAULT_DATE_FORMAT)

    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        stream.addFilter(redactor)
        root.addHandler(stream)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / log_file_name,
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.addFilter(redactor)
        root.addHandler(file_handler)

    _ACTIVE_FILTER = redactor
    return redactor


def active_redactor() -> SecretRedactingFilter | None:
    """The filter installed by the last :func:`setup_logging` call, if any."""
    return _ACTIVE_FILTER


def redact_for_persistence(text: str | None) -> str | None:
    """Redact free text before it is written straight to a persisted column
    that a logging handler never sees — ``reconciliation_mismatches.detail``,
    ``audit_events.detail``/``actor``, and any future ``live_confirmations``
    writer. :class:`~common.logging.redaction.SecretRedactingFilter` only
    protects text that passes through a logging call; a value going
    directly into a SQLite ``INSERT`` bypasses it entirely unless a call
    site does this explicitly.

    Falls back to returning ``text`` unchanged when no filter is active yet
    (nothing in this process has called :func:`setup_logging` — most unit
    tests, and any script that persists before logging is configured)
    rather than raising: a missing filter must never block a write that
    would otherwise succeed, and today's callers only ever pass synthetic,
    computer-generated strings (no secret has ever flowed through either
    column) — this is deliberate defense-in-depth against a future call
    site that starts passing raw broker text through, not a fix for a
    currently-exploitable leak.
    """
    if text is None:
        return None
    redactor = _ACTIVE_FILTER
    if redactor is None:
        return text
    return redactor.redact(text)


def get_logger(name: str, **context: Any) -> logging.Logger | StructuredAdapter:
    """Return a logger, optionally bound to structured context.

    Workers pass ``runtime_id``, ``strategy_id`` and ``execution_mode`` here, so
    every line they emit is attributable without repeating it at each call site.
    """
    logger = logging.getLogger(name)
    if context:
        return StructuredAdapter(logger, context)
    return logger
