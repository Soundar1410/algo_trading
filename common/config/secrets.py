"""Read a `pydantic.SecretStr`-shaped value without letting it reach a log.

Promoted here in Phase 4 Part 4 rather than left as a private copy. Two scripts
(``scripts/auth_bootstrap.py``, ``scripts/capture_live_tape.py``) already carried
identical four-line copies of this helper; a third independent copy — this
part's own token resolution for the historical-data warm-up client, in
``runtimes/intraday_options/engine_worker.py`` — is the point at which sharing
it beats copying it again.
"""

from __future__ import annotations


def read_secret(holder: object) -> str | None:
    """Unwrap a `SecretStr`-like value, or ``None`` if unset or empty.

    ``holder`` is typically a `Settings` field such as `dhan_access_token`,
    which is ``SecretStr | None``. Duck-typed on `get_secret_value` rather than
    importing `pydantic.SecretStr` directly, so this has no dependency beyond
    the standard library.
    """
    getter = getattr(holder, "get_secret_value", None)
    if getter is None:
        return None
    value = str(getter())
    return value or None
