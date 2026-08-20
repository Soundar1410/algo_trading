"""orchestration.auto_start.auth_flow: validated auth, and the rejected-cache trap.

The bug this module exists to avoid: when Dhan rejects a *cached* token,
calling ``get_token()`` again returns the same still-unexpired cache entry.
Retrying that way looks like diligence and accomplishes nothing until 15:15.
``refresh(current_token=...)`` is the only door that discriminates against a
token known to be dead.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from common.authentication import TokenOutcome
from orchestration.auto_start.auth_flow import authenticate_and_validate, cooldown_ready_at
from orchestration.auto_start.retry import Retryability, TerminalStartupError, classify

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 8, 20, 9, 0, tzinfo=IST)


class _FakeBootstrap:
    """Mimics AuthBootstrap's contract without any network or filesystem.

    ``get_token`` deliberately keeps returning the same cached token, exactly
    as the real one does while the cache entry is unexpired — that is the
    behaviour the module under test must not lean on.
    """

    def __init__(
        self,
        *,
        cached_token: str = "cached-token",
        accepts: set[str] | None = None,
        refresh_tokens: list[str] | None = None,
    ) -> None:
        self.cached_token = cached_token
        self.accepts = accepts if accepts is not None else {cached_token}
        self.refresh_tokens = list(refresh_tokens or [])
        self.get_token_calls = 0
        self.refresh_calls: list[str | None] = []
        self.validated: list[str] = []

    def get_token(self) -> tuple[str, TokenOutcome]:
        self.get_token_calls += 1
        return self.cached_token, TokenOutcome("cache", "2026-08-21T09:00:00", 0, False)

    def refresh(self, current_token: str | None = None) -> tuple[str, TokenOutcome]:
        self.refresh_calls.append(current_token)
        token = self.refresh_tokens.pop(0) if self.refresh_tokens else "minted-token"
        return token, TokenOutcome("generated", "2026-08-21T09:00:00", 1, False)

    def validate(self, token: str) -> bool:
        self.validated.append(token)
        return token in self.accepts


def test_an_accepted_token_needs_no_refresh():
    bootstrap = _FakeBootstrap()
    result = authenticate_and_validate(bootstrap)
    assert result.outcome.validated
    assert result.refreshes == 0
    assert bootstrap.refresh_calls == []


def test_success_is_only_ever_claimed_after_dhan_validates():
    bootstrap = _FakeBootstrap()
    authenticate_and_validate(bootstrap)
    assert bootstrap.validated == ["cached-token"]


def test_a_rejected_cached_token_is_refreshed_not_re_read():
    """The heart of it: get_token is called once, refresh does the recovery."""
    bootstrap = _FakeBootstrap(
        cached_token="stale", accepts={"fresh"}, refresh_tokens=["fresh"]
    )
    result = authenticate_and_validate(bootstrap)

    assert result.outcome.validated
    assert result.refreshes == 1
    assert bootstrap.get_token_calls == 1, "the same dead cache entry must not be re-read"
    assert bootstrap.refresh_calls == ["stale"], "the known-bad token must be passed in"


def test_the_rejected_token_is_never_reused(tmp_path=None):
    bootstrap = _FakeBootstrap(
        cached_token="stale", accepts={"fresh"}, refresh_tokens=["fresh"]
    )
    authenticate_and_validate(bootstrap)
    assert bootstrap.validated == ["stale", "fresh"]


def test_a_freshly_minted_token_that_dhan_still_refuses_is_terminal():
    """Not transient: retrying this until 15:15 risks an account lockout."""
    bootstrap = _FakeBootstrap(cached_token="stale", accepts=set())
    with pytest.raises(TerminalStartupError, match="refresh attempt"):
        authenticate_and_validate(bootstrap)


def test_the_refusal_is_classified_terminal_so_it_never_loops():
    bootstrap = _FakeBootstrap(cached_token="stale", accepts=set())
    try:
        authenticate_and_validate(bootstrap)
    except TerminalStartupError as exc:
        assert classify(exc) is Retryability.TERMINAL
    else:  # pragma: no cover
        pytest.fail("expected a terminal refusal")


def test_refreshes_are_bounded():
    bootstrap = _FakeBootstrap(cached_token="stale", accepts=set())
    with pytest.raises(TerminalStartupError):
        authenticate_and_validate(bootstrap, max_refresh_attempts=2)
    assert len(bootstrap.refresh_calls) == 2, "a refresh storm is exactly what this bounds"


def test_a_token_another_process_minted_under_the_lock_is_accepted():
    """refresh() may return a cached replacement (requests_made == 0) when a
    sibling process won the shared refresh lock. That still counts."""
    bootstrap = _FakeBootstrap(
        cached_token="stale", accepts={"sibling-minted"}, refresh_tokens=["sibling-minted"]
    )
    result = authenticate_and_validate(bootstrap)
    assert result.outcome.validated
    assert result.refreshes == 1


# ------------------------------------------------------------------- cooldown
class _Cooldown:
    def __init__(self, rejection) -> None:
        self._rejection = rejection
        self.cooldown_seconds = 600

    def active(self, **_kwargs):
        return self._rejection


class _Rejection:
    def __init__(self, remaining: float) -> None:
        self._remaining = remaining

    def remaining(self, *, cooldown_seconds: int, now: float | None = None) -> float:
        return self._remaining


class _BootstrapWithCooldown:
    def __init__(self, rejection) -> None:
        self.cooldown = _Cooldown(rejection)


def test_no_recorded_rejection_means_no_waiting():
    bootstrap = _BootstrapWithCooldown(None)
    assert cooldown_ready_at(bootstrap, now=NOW) == NOW


def test_a_recorded_rejection_pushes_the_ready_time_out():
    bootstrap = _BootstrapWithCooldown(_Rejection(300.0))
    assert cooldown_ready_at(bootstrap, now=NOW) == NOW + timedelta(seconds=300)


def test_an_expired_rejection_is_ready_immediately():
    bootstrap = _BootstrapWithCooldown(_Rejection(0.0))
    assert cooldown_ready_at(bootstrap, now=NOW) == NOW
