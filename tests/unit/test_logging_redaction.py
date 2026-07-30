"""Secret redaction — no credential may reach a log record, ever."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from common.config import Settings
from common.logging import (
    REDACTED,
    SecretRedactingFilter,
    get_logger,
    secrets_from_settings,
    setup_logging,
)

FAKE_SETTINGS = Settings(
    dhan_client_id="1100112233",
    dhan_pin="4821",
    dhan_totp_secret="JBSWY3DPEHPK3PXP",
    telegram_bot_token="7654321:AAH9fakeTokenValueForTests",
    telegram_chat_id="-1001234567890",
)


# ------------------------------------------------------- literal redaction
@pytest.mark.parametrize(
    "secret",
    [
        "1100112233",
        "4821",
        "JBSWY3DPEHPK3PXP",
        "7654321:AAH9fakeTokenValueForTests",
        "-1001234567890",
    ],
)
def test_every_settings_secret_is_masked(secret: str):
    redactor = SecretRedactingFilter(secrets_from_settings(FAKE_SETTINGS))
    assert secret not in redactor.redact(f"connecting with value {secret} now")


def test_secrets_from_settings_reads_through_secretstr():
    values = secrets_from_settings(FAKE_SETTINGS)
    assert "JBSWY3DPEHPK3PXP" in values


def test_absent_secrets_produce_no_literals():
    assert secrets_from_settings(Settings()) == ()


def test_very_short_values_are_not_masked():
    """Masking a 1-char value would redact unrelated text everywhere."""
    redactor = SecretRedactingFilter(["a"])
    assert redactor.redact("a normal sentence") == "a normal sentence"


def test_longest_secret_wins_when_one_contains_another():
    redactor = SecretRedactingFilter(["abcd", "abcd1234"])
    assert redactor.redact("value=abcd1234") == f"value={REDACTED}"


# ------------------------------------------------------- pattern redaction
@pytest.mark.parametrize(
    "message",
    [
        "access_token=eyJhbGciOiJIUzI1NiJ9.unseen",
        'response {"access_token": "eyJhbGciOiJIUzI1NiJ9.unseen"}',
        "client_id: 9988776655",
        "Authorization=Bearer_unseen_value",
        "totp_secret=NEVERSEENBEFORE22",
        "bot_token='8888888:AAHunknownvalue'",
    ],
)
def test_sensitive_keys_are_masked_even_for_unknown_values(message: str):
    """Catches a secret echoed back by a broker that was never in .env."""
    redactor = SecretRedactingFilter()
    redacted = redactor.redact(message)
    assert REDACTED in redacted
    assert "unseen" not in redacted
    assert "unknownvalue" not in redacted
    assert "9988776655" not in redacted


def test_non_sensitive_key_values_survive_untouched():
    redactor = SecretRedactingFilter()
    message = "strategy_id=io_fixture_v1 execution_mode=paper quantity=75"
    assert redactor.redact(message) == message


# --------------------------------------------------------- record filtering
def test_filter_redacts_the_rendered_record(caplog: pytest.LogCaptureFixture):
    redactor = SecretRedactingFilter(secrets_from_settings(FAKE_SETTINGS))
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="logging in as %s",
        args=("1100112233",),
        exc_info=None,
    )
    redactor.filter(record)
    assert "1100112233" not in record.getMessage()
    assert REDACTED in record.getMessage()


def test_filter_clears_args_so_downstream_cannot_reassemble():
    redactor = SecretRedactingFilter(secrets_from_settings(FAKE_SETTINGS))
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="pin is %s",
        args=("4821",),
        exc_info=None,
    )
    redactor.filter(record)
    assert record.args == ()
    assert "4821" not in str(record.msg)


def test_broken_format_string_does_not_break_logging():
    redactor = SecretRedactingFilter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="%d items",
        args=("not_a_number",),
        exc_info=None,
    )
    assert redactor.filter(record) is True


# ------------------------------------------------------------ setup_logging
def test_setup_logging_writes_a_redacted_file(tmp_path: Path):
    setup_logging(level="INFO", log_dir=tmp_path, settings=FAKE_SETTINGS, console=False)
    logging.getLogger("phase0").info("auth ok for %s", "1100112233")
    logging.shutdown()

    contents = (tmp_path / "algo_trading.log").read_text(encoding="utf-8")
    assert "1100112233" not in contents
    assert REDACTED in contents


def test_repeated_setup_does_not_duplicate_handlers(tmp_path: Path):
    setup_logging(log_dir=tmp_path, settings=FAKE_SETTINGS, console=False)
    setup_logging(log_dir=tmp_path, settings=FAKE_SETTINGS, console=False)
    assert len(logging.getLogger().handlers) == 1


def test_redactor_accepts_secrets_discovered_later(tmp_path: Path):
    """An access token is fetched after startup and must be maskable then."""
    redactor = setup_logging(log_dir=tmp_path, settings=Settings(), console=False)
    redactor.add_secrets(["token_issued_at_runtime_value"])
    logging.getLogger("phase0").info("using token_issued_at_runtime_value")
    logging.shutdown()

    contents = (tmp_path / "algo_trading.log").read_text(encoding="utf-8")
    assert "token_issued_at_runtime_value" not in contents


def test_structured_context_is_appended(tmp_path: Path):
    setup_logging(log_dir=tmp_path, settings=Settings(), console=False)
    log = get_logger("phase0", strategy_id="io_fixture_v1", execution_mode="paper")
    log.info("candle closed")
    logging.shutdown()

    contents = (tmp_path / "algo_trading.log").read_text(encoding="utf-8")
    assert "strategy_id=io_fixture_v1" in contents
    assert "execution_mode=paper" in contents


# --------------------------------------------- Phase 2: the auth secret surface
#
# Phase 2 introduces DHAN_ACCESS_TOKEN and a runtime-minted token, and starts
# logging URLs that carry credentials as query parameters. Two real gaps were
# found and closed while writing these tests, and both are asserted below.

PHASE2_SETTINGS = Settings(
    dhan_client_id="1100112233",
    dhan_pin="4821",
    dhan_totp_secret="JBSWY3DPEHPK3PXP",
    dhan_access_token="eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjQ4NzE1MzI4MDB9.fakesignature",
    telegram_bot_token="7654321:AAH9fakeTokenValueForTests",
    telegram_chat_id="-1001234567890",
)


def test_the_manual_access_token_is_a_known_secret_value():
    """DHAN_ACCESS_TOKEN was absent from secrets_from_settings before Phase 2.

    Without it, the one credential a user can paste into .env would have been the
    only one relying on pattern matching alone.
    """
    values = secrets_from_settings(PHASE2_SETTINGS)
    assert PHASE2_SETTINGS.dhan_access_token is not None
    assert PHASE2_SETTINGS.dhan_access_token.get_secret_value() in values


def test_a_runtime_minted_token_is_masked_once_registered():
    """A generated token was never in .env, so value redaction cannot know it
    until the bootstrap hands it over.

    The gap is specifically a token appearing *without* a sensitive key beside
    it — pattern redaction has nothing to anchor on there, so registering the
    literal value is the only thing that masks it. This is why the bootstrap
    calls `on_token_minted` the moment the token exists, before anything can log
    it.
    """
    minted = "eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjQ4NzE1MzI4MDF9.mintedatruntime"
    bare = f"validated {minted} against /v2/profile"

    redactor = SecretRedactingFilter(secrets_from_settings(Settings()))
    assert minted in redactor.redact(bare), (
        "an unregistered token with no key beside it cannot be pattern-matched; "
        "if this ever passes, this test no longer proves add_secrets is needed"
    )

    redactor.add_secrets([minted])
    assert minted not in redactor.redact(bare)
    assert REDACTED in redactor.redact(bare)


def test_the_auth_url_is_redacted_parameter_by_parameter():
    """The exact shape the login logs on failure.

    `dhanClientId` needed adding to the key list: `\\bclientid\\b` cannot match
    inside `dhanClientId`, because there is no word boundary between "dhan" and
    "Client". Pattern redaction — which exists precisely for values not present
    in .env — was letting the camelCase spelling through.
    """
    redactor = SecretRedactingFilter()  # deliberately no literal secrets
    url = (
        "https://auth.dhan.co/app/generateAccessToken?dhanClientId=1100112233&pin=4821&totp=654321"
    )
    masked = redactor.redact(url)
    for secret in ("1100112233", "4821", "654321"):
        assert secret not in masked, f"{secret} leaked from the auth URL"


def test_query_parameter_order_does_not_change_what_is_masked():
    """Previously the greedy value match swallowed everything to the next space.

    That masked later secrets only by accident of ordering and left earlier ones
    exposed, so the property has to hold under permutation.
    """
    redactor = SecretRedactingFilter()
    for url in (
        "https://auth.dhan.co/x?dhanClientId=1100112233&pin=4821&totp=654321",
        "https://auth.dhan.co/x?totp=654321&pin=4821&dhanClientId=1100112233",
        "https://auth.dhan.co/x?pin=4821&dhanClientId=1100112233&totp=654321",
    ):
        masked = redactor.redact(url)
        for secret in ("1100112233", "4821", "654321"):
            assert secret not in masked, f"{secret} leaked from {url}"


def test_a_non_secret_parameter_survives_redaction():
    """The greedy match used to destroy whatever trailed a secret.

    Masking everything is safe but useless: a log line that cannot say which
    instrument or segment was involved does not help anyone debug.
    """
    redactor = SecretRedactingFilter()
    masked = redactor.redact(
        "https://api.dhan.co/v2/profile?dhanClientId=1100112233&segment=NSE_FNO"
    )
    assert "1100112233" not in masked
    assert "segment=NSE_FNO" in masked


def test_the_access_token_header_is_masked():
    """Dhan sends the token in an `access-token` header, hyphen not underscore."""
    redactor = SecretRedactingFilter()
    masked = redactor.redact(
        "headers={'access-token': 'eyJhbGciOi.payload.sig', 'dhanClientId': '1100112233'}"
    )
    assert "eyJhbGciOi.payload.sig" not in masked
    assert "1100112233" not in masked


def test_an_echoed_credential_in_a_response_body_is_masked():
    """A broker echoing our input back is a leak vector value redaction alone
    would catch only if the value came from .env."""
    redactor = SecretRedactingFilter()
    masked = redactor.redact(
        'auth failed: {"errorMessage": "bad pin", "pin": "9999", "totp": "111222"}'
    )
    assert "9999" not in masked
    assert "111222" not in masked


def test_a_rejection_reason_carries_no_credential(tmp_path: Path):
    """End to end through a real handler: the cooldown reason gets logged."""
    from common.authentication import RejectionCooldown

    setup_logging(log_dir=tmp_path, settings=PHASE2_SETTINGS, console=False)
    cooldown = RejectionCooldown(tmp_path / "rejected.json")
    cooldown.record("Dhan rejected the credentials (HTTP 401): Invalid PIN")
    logging.getLogger("phase2").error(
        "auth failed for dhanClientId=1100112233 pin=4821 totp=654321"
    )
    logging.shutdown()

    contents = (tmp_path / "algo_trading.log").read_text(encoding="utf-8")
    for secret in ("1100112233", "4821", "654321", "JBSWY3DPEHPK3PXP"):
        assert secret not in contents, f"{secret} reached the log file"
