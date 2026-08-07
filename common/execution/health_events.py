"""Vocabulary for the diagnostic event tables migration 0002 shipped.

``auth_events`` and ``feed_events`` (``common/persistence/migrations/versions/
0002_feed_and_auth_health.sql``) each CHECK-constrain their ``event`` column at
the database layer. This module is the Python-side mirror of those two
vocabularies, so a caller gets a clear :class:`ValueError` naming the mistake
before ever reaching SQLite, and the mapping from
:class:`common.authentication.bootstrap.TokenOutcome`'s ``source`` field to the
``auth_events`` vocabulary lives in exactly one place rather than being
reinvented at each call site.

Deliberately has no dependency on :mod:`common.execution.repository` or
:mod:`common.authentication` — it is a leaf module either of those, and
:mod:`common.health`, may import without risking a cycle.
"""

from __future__ import annotations

AUTH_EVENTS: frozenset[str] = frozenset(
    {
        "token_reused_from_env",
        "token_reused_from_cache",
        "token_generated",
        "token_refreshed",
        "validation_succeeded",
        "validation_failed",
        "credentials_rejected",
        "rate_limited",
        "cooldown_suppressed",
        "transient_failure",
    }
)

FEED_EVENTS: frozenset[str] = frozenset(
    {
        "connected",
        "disconnected",
        "reconnect_attempted",
        "reconnect_exhausted",
        "resubscribed",
        "degraded",
        "recovered",
        "stale_instrument",
    }
)

#: Maps ``TokenOutcome.source`` ("environment"/"cache"/"generated") — a
#: successful :meth:`~common.authentication.bootstrap.AuthBootstrap.get_token`
#: call only — onto the ``auth_events`` vocabulary. Failure outcomes (a
#: rejected credential, a rate limit, a cooldown) are reported by the caller
#: directly with the matching event name; there is no single ``source`` value
#: to map them from.
_SOURCE_TO_EVENT = {
    "environment": "token_reused_from_env",
    "cache": "token_reused_from_cache",
    "generated": "token_generated",
}


def auth_event_for_source(source: str) -> str:
    """The ``auth_events`` event name for a successful token outcome's source."""
    try:
        return _SOURCE_TO_EVENT[source]
    except KeyError:
        raise ValueError(
            f"unknown TokenOutcome.source {source!r}; expected one of "
            f"{sorted(_SOURCE_TO_EVENT)}"
        ) from None
