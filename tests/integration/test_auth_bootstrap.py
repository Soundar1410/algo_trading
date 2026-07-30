"""Bootstrap behaviour: token precedence, retry policy, and attempt budget.

The headline property, asserted several ways: **a wrong PIN costs exactly one
request to Dhan**, no matter how many processes start or how many times the
bootstrap is re-run. Two mechanisms combine to give that — never retrying a
non-retryable failure, and recording a cooldown so later callers do not repeat
the attempt at all.
"""

from __future__ import annotations

import base64
import json
import multiprocessing as mp
import time
from pathlib import Path

import pytest

from common.authentication import (
    AuthBootstrap,
    AuthCredentials,
    DhanTotpLogin,
    InvalidCredentialsError,
    MissingCredentialsError,
    TokenGenerationError,
    TokenRateLimitedError,
    TokenRejectedRecentlyError,
)
from common.authentication.bootstrap import COOLDOWN_FILENAME, TOKEN_CACHE_FILENAME

CLIENT_ID = "1100000000"
PIN = "1234"
TOTP_SECRET = "JBSWY3DPEHPK3PXP"
TOTP = "654321"


def _jwt(exp_epoch: int) -> str:
    claims = {"exp": exp_epoch, "dhanClientId": CLIENT_ID}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJIUzI1NiJ9.{payload}.c2ln"


# Absolute epochs, deliberately not `time.time() + offset`.
#
# The spawn-based tests below re-import this module in each child process. A
# clock-relative constant is recomputed there, so two children that straddle a
# second boundary mint *different* "same" tokens and the comparison fails —
# a flake that appears only under load. Fixed epochs are identical in every
# process, forever.
_FRESH_EXP = 4_871_532_800  # 2124-05-10, comfortably beyond any test lifetime
_STALE_EXP = 1_000_000_000  # 2001-09-09, comfortably expired

FRESH = _jwt(_FRESH_EXP)
STALE = _jwt(_STALE_EXP)


class _FakeLogin:
    """Stands in for DhanTotpLogin, counting attempts and raising on script."""

    def __init__(self, *outcomes: object) -> None:
        self._outcomes = list(outcomes)
        self.request_count = 0

    def generate(self) -> object:
        self.request_count += 1
        if not self._outcomes:
            raise AssertionError("the bootstrap attempted more logins than scripted")
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _token(access_token: str = FRESH, expiry: str | None = "2026-07-31T09:00:00Z"):
    from common.authentication import GeneratedToken

    return GeneratedToken(access_token=access_token, expiry_time=expiry)


def _bootstrap(
    tmp_path: Path,
    *,
    login: object = None,
    access_token: str | None = None,
    can_generate: bool = True,
    **kwargs: object,
) -> AuthBootstrap:
    credentials = AuthCredentials(
        client_id=CLIENT_ID,
        pin=PIN if can_generate else None,
        totp_secret=TOTP_SECRET if can_generate else None,
        access_token=access_token,
    )
    return AuthBootstrap(
        credentials,
        cache_dir=tmp_path,
        login=login,  # type: ignore[arg-type]
        totp_provider=lambda: TOTP,
        sleep=lambda _seconds: None,
        **kwargs,  # type: ignore[arg-type]
    )


# ------------------------------------------------------------------ precedence


def test_a_fresh_environment_token_is_used_without_any_request(tmp_path: Path):
    login = _FakeLogin()
    token, outcome = _bootstrap(tmp_path, login=login, access_token=FRESH).get_token()

    assert token == FRESH
    assert outcome.source == "environment"
    assert login.request_count == 0


def test_a_cached_token_is_reused_without_any_request(tmp_path: Path):
    login = _FakeLogin()
    boot = _bootstrap(tmp_path, login=login)
    boot.cache.save(FRESH, client_id=CLIENT_ID, expiry_time="tomorrow")

    token, outcome = boot.get_token()
    assert token == FRESH
    assert outcome.source == "cache"
    assert outcome.expiry_time == "tomorrow"
    assert login.request_count == 0


def test_an_expired_environment_token_triggers_generation(tmp_path: Path):
    login = _FakeLogin(_token())
    token, outcome = _bootstrap(tmp_path, login=login, access_token=STALE).get_token()

    assert token == FRESH
    assert outcome.source == "generated"
    assert login.request_count == 1


def test_an_expired_cached_token_triggers_generation(tmp_path: Path):
    login = _FakeLogin(_token())
    boot = _bootstrap(tmp_path, login=login)
    boot.cache.save(STALE, client_id=CLIENT_ID)

    token, _ = boot.get_token()
    assert token == FRESH
    assert login.request_count == 1


def test_a_cached_token_for_another_client_triggers_generation(tmp_path: Path):
    login = _FakeLogin(_token())
    boot = _bootstrap(tmp_path, login=login)
    boot.cache.save(FRESH, client_id="1199999999")

    token, outcome = boot.get_token()
    assert outcome.source == "generated"
    assert token == FRESH


def test_a_generated_token_is_persisted_for_the_next_process(tmp_path: Path):
    boot = _bootstrap(tmp_path, login=_FakeLogin(_token()))
    boot.get_token()

    reloaded = _bootstrap(tmp_path, login=_FakeLogin()).get_token()
    assert reloaded[1].source == "cache"


def test_no_credentials_at_all_fails_closed(tmp_path: Path):
    boot = _bootstrap(tmp_path, can_generate=False)
    with pytest.raises(MissingCredentialsError, match="DHAN_TOTP_SECRET"):
        boot.get_token()


# ------------------------------------------------- the attempt budget on failure


def test_a_credential_rejection_costs_exactly_one_request(tmp_path: Path):
    login = _FakeLogin(InvalidCredentialsError("Dhan rejected the credentials (HTTP 401)"))
    boot = _bootstrap(tmp_path, login=login, retry_count=3)

    with pytest.raises(InvalidCredentialsError):
        boot.get_token()

    assert login.request_count == 1, "retry_count must not apply to a rejection"


def test_a_rate_limit_answer_costs_exactly_one_request(tmp_path: Path):
    login = _FakeLogin(TokenRateLimitedError("once every 2 minutes"))
    boot = _bootstrap(tmp_path, login=login, retry_count=3)

    with pytest.raises(TokenRateLimitedError):
        boot.get_token()

    assert login.request_count == 1


def test_a_rejection_does_not_overwrite_a_previously_cached_token(tmp_path: Path):
    """A failed regeneration must not destroy a token another process may use."""
    boot = _bootstrap(tmp_path, login=_FakeLogin(InvalidCredentialsError("bad pin")))
    boot.cache.save(STALE, client_id=CLIENT_ID, expiry_time="original")

    with pytest.raises(InvalidCredentialsError):
        boot.get_token()

    survivor = boot.cache.load(expected_client_id=CLIENT_ID)
    assert survivor is not None
    assert survivor.access_token == STALE
    assert survivor.expiry_time == "original"


def test_transient_failures_are_retried_then_succeed(tmp_path: Path):
    login = _FakeLogin(
        TokenGenerationError("connection reset"),
        TokenGenerationError("503"),
        _token(),
    )
    boot = _bootstrap(tmp_path, login=login, retry_count=3)

    token, outcome = boot.get_token()
    assert token == FRESH
    assert login.request_count == 3
    assert outcome.requests_made == 3


def test_exhausted_transient_retries_fail_closed(tmp_path: Path):
    login = _FakeLogin(*[TokenGenerationError("timeout") for _ in range(3)])
    boot = _bootstrap(tmp_path, login=login, retry_count=3)

    with pytest.raises(TokenGenerationError, match="after 3 transient"):
        boot.get_token()
    assert login.request_count == 3


def test_a_transient_failure_records_no_cooldown(tmp_path: Path):
    """A network blip must not suppress the next legitimate attempt."""
    boot = _bootstrap(tmp_path, login=_FakeLogin(*[TokenGenerationError("x")] * 3), retry_count=3)
    with pytest.raises(TokenGenerationError):
        boot.get_token()
    assert boot.cooldown.active() is None


# ---------------------------------------------------------------- the cooldown


def test_a_second_invocation_after_a_rejection_makes_zero_requests(tmp_path: Path):
    """The mechanism that stops repeated re-runs from burning attempts."""
    first = _bootstrap(tmp_path, login=_FakeLogin(InvalidCredentialsError("HTTP 401 Invalid PIN")))
    with pytest.raises(InvalidCredentialsError):
        first.get_token()

    second_login = _FakeLogin()  # scripted with nothing: any request is a failure
    second = _bootstrap(tmp_path, login=second_login)
    with pytest.raises(TokenRejectedRecentlyError) as caught:
        second.get_token()

    assert second_login.request_count == 0
    assert "--force" in str(caught.value)


def test_clearing_the_cooldown_allows_a_fresh_attempt(tmp_path: Path):
    first = _bootstrap(tmp_path, login=_FakeLogin(InvalidCredentialsError("HTTP 401")))
    with pytest.raises(InvalidCredentialsError):
        first.get_token()

    second = _bootstrap(tmp_path, login=_FakeLogin(_token()))
    second.clear_cooldown()
    token, _ = second.get_token()
    assert token == FRESH


def test_the_cooldown_expires_on_its_own(tmp_path: Path):
    boot = _bootstrap(
        tmp_path,
        login=_FakeLogin(InvalidCredentialsError("HTTP 401")),
        cooldown_seconds=1,
    )
    with pytest.raises(InvalidCredentialsError):
        boot.get_token()

    time.sleep(1.05)
    later = _bootstrap(tmp_path, login=_FakeLogin(_token()), cooldown_seconds=1)
    assert later.get_token()[0] == FRESH


def test_a_successful_generation_clears_a_stale_cooldown(tmp_path: Path):
    cooldown_path = tmp_path / COOLDOWN_FILENAME
    boot = _bootstrap(tmp_path, login=_FakeLogin(_token()))
    boot.cooldown.record("an old rejection")
    boot.clear_cooldown()

    boot.get_token()
    assert not cooldown_path.exists()


# ---------------------------------------------- concurrent processes, for real


def _child_rejects(cache_dir: str, queue: mp.Queue) -> None:  # type: ignore[type-arg]
    """Run in a spawned process: attempt a login that Dhan rejects."""
    from common.authentication import AuthBootstrap, AuthCredentials, InvalidCredentialsError
    from common.authentication.exceptions import TokenRejectedRecentlyError

    class _Login:
        def __init__(self) -> None:
            self.request_count = 0

        def generate(self) -> object:
            self.request_count += 1
            raise InvalidCredentialsError("Dhan rejected the credentials (HTTP 401)")

    login = _Login()
    boot = AuthBootstrap(
        AuthCredentials(client_id=CLIENT_ID, pin=PIN, totp_secret=TOTP_SECRET),
        cache_dir=Path(cache_dir),
        login=login,  # type: ignore[arg-type]
        sleep=lambda _s: None,
    )
    try:
        boot.get_token()
        queue.put(("unexpected-success", login.request_count))
    except TokenRejectedRecentlyError:
        queue.put(("suppressed", login.request_count))
    except InvalidCredentialsError:
        queue.put(("rejected", login.request_count))


def test_concurrent_processes_produce_one_rejection_between_them(tmp_path: Path):
    """The hole that "we never retry" does not close on its own.

    The refresh lock serialises generation and re-checks the cache — but only for
    *success*. Without the cooldown every loser finds an empty cache and makes
    its own doomed request, so eight workers starting together would mean eight
    rejected logins. Asserted across real spawned processes because that is the
    situation it protects.
    """
    context = mp.get_context("spawn")
    queue: mp.Queue = context.Queue()  # type: ignore[type-arg]
    workers = [
        context.Process(target=_child_rejects, args=(str(tmp_path), queue)) for _ in range(4)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=60)

    results = [queue.get(timeout=10) for _ in range(4)]
    outcomes = [kind for kind, _ in results]
    attempts = sum(count for _, count in results)

    assert "unexpected-success" not in outcomes
    assert attempts == 1, f"expected exactly one login attempt in total, got {attempts}: {results}"
    assert outcomes.count("rejected") == 1
    assert outcomes.count("suppressed") == 3


def _child_generates(cache_dir: str, queue: mp.Queue) -> None:  # type: ignore[type-arg]
    """Run in a spawned process: mint a token, reporting whether it had to."""
    from common.authentication import AuthBootstrap, AuthCredentials, GeneratedToken

    class _Login:
        def __init__(self) -> None:
            self.request_count = 0

        def generate(self) -> object:
            self.request_count += 1
            time.sleep(0.2)  # widen the window in which others contend
            return GeneratedToken(access_token=FRESH, expiry_time="tomorrow")

    login = _Login()
    boot = AuthBootstrap(
        AuthCredentials(client_id=CLIENT_ID, pin=PIN, totp_secret=TOTP_SECRET),
        cache_dir=Path(cache_dir),
        login=login,  # type: ignore[arg-type]
        sleep=lambda _s: None,
    )
    token, outcome = boot.get_token()
    queue.put((outcome.source, login.request_count, token == FRESH))


def test_concurrent_processes_perform_one_login_and_share_the_token(tmp_path: Path):
    """N workers starting together must mint one token, not N."""
    context = mp.get_context("spawn")
    queue: mp.Queue = context.Queue()  # type: ignore[type-arg]
    workers = [
        context.Process(target=_child_generates, args=(str(tmp_path), queue)) for _ in range(4)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=60)

    results = [queue.get(timeout=10) for _ in range(4)]
    total_logins = sum(count for _, count, _ in results)

    assert all(correct for _, _, correct in results), "every worker must get the same token"
    assert total_logins == 1, f"expected one login across all workers, got {total_logins}"
    assert (tmp_path / TOKEN_CACHE_FILENAME).exists()


# --------------------------------------------------------- redaction hand-off


def test_a_minted_token_is_handed_to_the_redactor_before_it_can_be_logged(tmp_path: Path):
    """A generated token was never in .env, so value redaction cannot know it.

    It has to be registered the moment it exists, which is what on_token_minted
    is for.
    """
    announced: list[str] = []
    boot = _bootstrap(
        tmp_path,
        login=_FakeLogin(_token()),
        on_token_minted=announced.append,
    )
    boot.get_token()
    assert announced == [FRESH]


def test_a_cached_token_is_also_handed_to_the_redactor(tmp_path: Path):
    announced: list[str] = []
    boot = _bootstrap(tmp_path, login=_FakeLogin(), on_token_minted=announced.append)
    boot.cache.save(FRESH, client_id=CLIENT_ID)
    boot.get_token()
    assert announced == [FRESH]


# ------------------------------------------------------------------- refresh


def test_refresh_replaces_a_token_the_server_has_stopped_honouring(tmp_path: Path):
    """Dhan disconnect code 807.

    The situation's defining feature is that the cached token still looks valid
    by its ``exp`` claim while the server has already rejected it — so an
    expiry-based check cannot detect it, and handing the cached token back would
    leave an 807 recovery loop re-presenting a dead token forever.
    """
    replacement = _jwt(_FRESH_EXP + 3600)
    login = _FakeLogin(_token(replacement))
    boot = _bootstrap(tmp_path, login=login)
    boot.cache.save(FRESH, client_id=CLIENT_ID)

    token, outcome = boot.refresh(current_token=FRESH)
    assert token == replacement, "the known-bad token must not be handed back"
    assert outcome.source == "generated"
    assert login.request_count == 1


def test_refresh_accepts_a_token_another_process_already_replaced_it_with(tmp_path: Path):
    """The lock's dedup must still work when several workers hit 807 together.

    Four workers seeing 807 should cause one login, not four — so a cached token
    that is *not* the one we know is dead is accepted without a request.
    """
    replacement = _jwt(_FRESH_EXP + 3600)
    login = _FakeLogin()  # scripted with nothing: any request fails the test
    boot = _bootstrap(tmp_path, login=login)
    boot.cache.save(replacement, client_id=CLIENT_ID, expiry_time="minted by a peer")

    token, outcome = boot.refresh(current_token=FRESH)
    assert token == replacement
    assert outcome.source == "cache"
    assert login.request_count == 0


def test_refresh_without_a_current_token_forces_an_unconditional_login(tmp_path: Path):
    replacement = _jwt(_FRESH_EXP + 3600)
    login = _FakeLogin(_token(replacement))
    boot = _bootstrap(tmp_path, login=login)
    boot.cache.save(FRESH, client_id=CLIENT_ID)

    token, _ = boot.refresh()
    assert token == replacement
    assert login.request_count == 1


def test_refresh_without_generation_credentials_fails_closed(tmp_path: Path):
    boot = _bootstrap(tmp_path, can_generate=False)
    with pytest.raises(MissingCredentialsError):
        boot.refresh()


# ------------------------------------------------------------------ validation


def test_validation_is_delegated_to_the_injected_validator(tmp_path: Path):
    seen: list[tuple[str, str]] = []
    boot = _bootstrap(
        tmp_path,
        login=_FakeLogin(),
        token_validator=lambda client, token: bool(seen.append((client, token))) or True,
    )
    assert boot.validate(FRESH) is True
    assert seen == [(CLIENT_ID, FRESH)]


def test_a_real_login_object_is_built_when_credentials_allow(tmp_path: Path):
    """Without an injected login, credentials must produce a working one."""
    boot = AuthBootstrap(
        AuthCredentials(client_id=CLIENT_ID, pin=PIN, totp_secret=TOTP_SECRET),
        cache_dir=tmp_path,
        totp_provider=lambda: TOTP,
    )
    assert isinstance(boot._login, DhanTotpLogin)
