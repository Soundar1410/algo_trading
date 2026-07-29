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
