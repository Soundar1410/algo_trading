"""Token cache durability, identity safety, and the rejection cooldown.

The cache holds a live bearer token, so the properties under test are the ones a
crash or a mix-up would violate: never a partial file, never another account's
token, never world-readable, and never wedged by corruption.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import tempfile
import time
from pathlib import Path

import pytest

from common.authentication import (
    Rejection,
    RejectionCooldown,
    StoredToken,
    TokenCache,
    TokenStoreError,
)

CLIENT_ID = "1100000000"
OTHER_CLIENT_ID = "1199999999"


def _jwt(exp: int | None) -> str:
    """A structurally real JWT with the given exp claim."""
    claims: dict[str, object] = {"dhanClientId": CLIENT_ID}
    if exp is not None:
        claims["exp"] = exp
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJIUzI1NiJ9.{payload}.c2ln"


FAR_FUTURE = _jwt(int(time.time()) + 86_400)
ALREADY_EXPIRED = _jwt(int(time.time()) - 60)
NO_EXP = _jwt(None)


# ------------------------------------------------------------------ round trip


def test_a_saved_token_is_read_back(tmp_path: Path):
    cache = TokenCache(tmp_path / "token_cache.json")
    cache.save(FAR_FUTURE, client_id=CLIENT_ID, expiry_time="2026-07-31T09:00:00Z")

    loaded = cache.load(expected_client_id=CLIENT_ID)
    assert loaded is not None
    assert loaded.access_token == FAR_FUTURE
    assert loaded.client_id == CLIENT_ID
    assert loaded.expiry_time == "2026-07-31T09:00:00Z"
    assert loaded.created_at, "created_at is the audit trail; it must be populated"


def test_the_cache_file_is_owner_only(tmp_path: Path):
    """It holds a live bearer token. 0600, not 0644."""
    cache = TokenCache(tmp_path / "token_cache.json")
    cache.save(FAR_FUTURE, client_id=CLIENT_ID)

    mode = stat.S_IMODE(cache.path.stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {mode:o}"


def test_the_parent_directory_is_created(tmp_path: Path):
    cache = TokenCache(tmp_path / "nested" / "deeper" / "token_cache.json")
    cache.save(FAR_FUTURE, client_id=CLIENT_ID)
    assert cache.path.exists()


# ------------------------------------------------------------------- atomicity


def test_a_crash_between_write_and_replace_leaves_the_old_cache_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The property that makes a mid-write crash survivable.

    The new content is written to a temp file in the same directory and only then
    renamed. If the process dies before the rename, the previous complete token
    is still there — as opposed to a truncated file that parses as valid JSON
    with a missing key, or an empty one.
    """
    cache = TokenCache(tmp_path / "token_cache.json")
    cache.save(FAR_FUTURE, client_id=CLIENT_ID, expiry_time="original")

    def _die(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated crash during rename")

    monkeypatch.setattr(os, "replace", _die)
    with pytest.raises(TokenStoreError):
        cache.save(NO_EXP, client_id=CLIENT_ID, expiry_time="replacement")

    monkeypatch.undo()
    survivor = cache.load(expected_client_id=CLIENT_ID)
    assert survivor is not None
    assert survivor.access_token == FAR_FUTURE, "the old token must survive"
    assert survivor.expiry_time == "original"


def test_a_failed_write_leaves_no_temporary_file_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A litter of .tmp-*.json files would each hold a real token."""
    cache = TokenCache(tmp_path / "token_cache.json")

    monkeypatch.setattr(os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(TokenStoreError):
        cache.save(FAR_FUTURE, client_id=CLIENT_ID)
    monkeypatch.undo()

    leftovers = list(tmp_path.glob(".tmp-*"))
    assert leftovers == [], f"temporary files leaked: {leftovers}"


def test_the_temporary_file_is_written_in_the_destination_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Same-filesystem is a correctness requirement, not tidiness.

    Across filesystems os.replace degrades to copy-then-delete, which is exactly
    the non-atomic behaviour being avoided. Writing the temp file beside the
    destination guarantees they share a filesystem.
    """
    destination = tmp_path / "nested" / "token_cache.json"
    cache = TokenCache(destination)
    observed: list[Path] = []
    real_mkstemp = tempfile.mkstemp

    def _spy(*args: object, **kwargs: object) -> tuple[int, str]:
        observed.append(Path(str(kwargs["dir"])))
        return real_mkstemp(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(tempfile, "mkstemp", _spy)
    cache.save(FAR_FUTURE, client_id=CLIENT_ID)

    assert observed == [destination.parent]


# -------------------------------------------------------------- unusable input


def test_a_missing_cache_returns_none(tmp_path: Path):
    assert TokenCache(tmp_path / "absent.json").load(expected_client_id=CLIENT_ID) is None


@pytest.mark.parametrize(
    "contents",
    [
        "",
        "{",
        "not json at all",
        '{"access_token": ""}',
        '{"expiry_time": "2026-01-01"}',
        "[1, 2, 3]",
        '"a bare string"',
    ],
)
def test_a_corrupt_cache_is_treated_as_absent_not_raised(tmp_path: Path, contents: str):
    """A bad file must never wedge startup — the fix is to mint a new token."""
    path = tmp_path / "token_cache.json"
    path.write_text(contents, encoding="utf-8")
    assert TokenCache(path).load(expected_client_id=CLIENT_ID) is None


def test_a_token_belonging_to_another_client_is_refused(tmp_path: Path):
    """The spec's client-identity validation (line 1395).

    Presenting another account's token would fail in ways that look like anything
    except a mixed-up cache.
    """
    cache = TokenCache(tmp_path / "token_cache.json")
    cache.save(FAR_FUTURE, client_id=OTHER_CLIENT_ID)
    assert cache.load(expected_client_id=CLIENT_ID) is None
    # Without an expectation the record is readable — the bootstrap always passes one.
    assert cache.load() is not None


def test_clearing_removes_the_file_and_is_idempotent(tmp_path: Path):
    cache = TokenCache(tmp_path / "token_cache.json")
    cache.save(FAR_FUTURE, client_id=CLIENT_ID)
    cache.clear()
    assert not cache.path.exists()
    cache.clear()  # must not raise


# ----------------------------------------------------------------- expiry rules


def test_a_far_future_token_is_usable():
    token = StoredToken(FAR_FUTURE, CLIENT_ID, "", None)
    assert token.is_usable() is True


def test_an_expired_token_is_not_usable():
    token = StoredToken(ALREADY_EXPIRED, CLIENT_ID, "", None)
    assert token.is_usable() is False


def test_a_token_inside_the_margin_is_not_usable():
    """Expiring in 60s with a 300s margin: refresh now, not mid-session."""
    soon = _jwt(int(time.time()) + 60)
    assert StoredToken(soon, CLIENT_ID, "", None).is_usable(margin_seconds=300) is False


def test_a_token_with_no_exp_claim_is_treated_as_usable():
    """Unknown must mean "don't know", never "expired".

    Treating unknown as expired discards a possibly-good token and forces a
    generation attempt against a two-minute limit. A genuinely dead token is
    rejected by the first real API call, which is far cheaper.
    """
    assert StoredToken(NO_EXP, CLIENT_ID, "", None).is_usable() is True
    assert StoredToken(NO_EXP, CLIENT_ID, "", None).seconds_until_expiry() is None


def test_a_non_jwt_token_is_treated_as_usable():
    assert StoredToken("not-a-jwt", CLIENT_ID, "", None).is_usable() is True


# -------------------------------------------------------------------- cooldown


def test_a_recorded_rejection_is_active_then_expires(tmp_path: Path):
    cooldown = RejectionCooldown(tmp_path / "rejected.json", cooldown_seconds=600)
    cooldown.record("Dhan rejected the credentials (HTTP 401)")

    active = cooldown.active()
    assert active is not None
    assert "401" in active.reason

    later = time.time() + 601
    assert cooldown.active(now=later) is None


def test_no_cooldown_file_means_no_cooldown(tmp_path: Path):
    assert RejectionCooldown(tmp_path / "absent.json").active() is None


def test_the_cooldown_file_holds_no_secret(tmp_path: Path):
    """It records a time and a reason. Never a PIN, TOTP or token."""
    cooldown = RejectionCooldown(tmp_path / "rejected.json")
    cooldown.record("Dhan rejected the credentials (HTTP 401): Invalid PIN")

    raw = cooldown.path.read_text(encoding="utf-8")
    assert set(json.loads(raw)) == {"rejected_at", "rejected_at_iso", "reason"}
    for secret in ("1234", "654321", FAR_FUTURE):
        assert secret not in raw


@pytest.mark.parametrize(
    "contents", ['{"rejected_at": "yesterday"}', '{"rejected_at": true}', "{}"]
)
def test_a_malformed_cooldown_file_does_not_block_forever(tmp_path: Path, contents: str):
    """Fail *open* here, deliberately.

    A corrupt cooldown must not permanently prevent authentication — the
    protection it offers is bounded, whereas an unclearable block would be an
    outage. The single-attempt rule still holds independently.
    """
    path = tmp_path / "rejected.json"
    path.write_text(contents, encoding="utf-8")
    assert RejectionCooldown(path).active() is None


def test_clearing_the_cooldown_allows_attempts_again(tmp_path: Path):
    cooldown = RejectionCooldown(tmp_path / "rejected.json")
    cooldown.record("bad pin")
    assert cooldown.active() is not None
    cooldown.clear()
    assert cooldown.active() is None


def test_remaining_never_goes_negative():
    rejection = Rejection(rejected_at=1000.0, reason="x")
    assert rejection.remaining(cooldown_seconds=60, now=2000.0) == 0.0
    assert rejection.remaining(cooldown_seconds=60, now=1030.0) == pytest.approx(30.0)


# ---------------------------------------------------------------- refresh lock


def test_the_refresh_lock_is_reentrant_across_sequential_uses(tmp_path: Path):
    cache = TokenCache(tmp_path / "token_cache.json")
    with cache.refresh_lock():
        pass
    with cache.refresh_lock():
        pass  # a lock that is not released is worse than no lock


def test_a_contended_refresh_lock_times_out_rather_than_generating_anyway(tmp_path: Path):
    """Falling through on timeout is how one bad start becomes N logins."""
    cache = TokenCache(tmp_path / "token_cache.json")
    other = TokenCache(tmp_path / "token_cache.json")
    with (
        cache.refresh_lock(),
        pytest.raises(TokenStoreError, match="Timed out"),
        other.refresh_lock(timeout=0.1),
    ):
        pass
