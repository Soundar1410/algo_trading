"""``build_notifier``: the one place Telegram credentials decide what a
process notifies through.

Every ``Settings`` here is constructed with an explicit, empty ``.env`` file
in an isolated ``tmp_path`` — never the real one. This machine's own ``.env``
carries real Telegram credentials (found during Phase 7 Part 2 development),
so a test that let ``Settings`` fall through to the real file could build a
real, working ``TelegramNotifier``.
"""

from __future__ import annotations

from pathlib import Path

from common.config import Settings
from common.notifications import NullNotifier, TelegramNotifier, build_notifier


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    """A ``Settings`` isolated from both the real ``.env`` and the real shell
    environment. Constructor kwargs are pydantic-settings' highest-precedence
    source, so explicit ``None`` here overrides a stray exported env var too —
    not just an empty ``.env`` file."""
    empty_env = tmp_path / ".env"
    empty_env.write_text("", encoding="utf-8")
    fields: dict[str, object] = {"telegram_bot_token": None, "telegram_chat_id": None}
    fields.update(overrides)
    return Settings(_env_file=empty_env, **fields)  # type: ignore[call-arg]


def test_no_credentials_builds_a_null_notifier(tmp_path: Path):
    notifier = build_notifier(_settings(tmp_path))
    assert isinstance(notifier, NullNotifier)


def test_a_bot_token_alone_still_builds_a_null_notifier(tmp_path: Path):
    """has_telegram_credentials() requires both; one alone is not enough."""
    notifier = build_notifier(_settings(tmp_path, telegram_bot_token="123:abc"))
    assert isinstance(notifier, NullNotifier)


def test_both_credentials_build_a_configured_telegram_notifier(tmp_path: Path):
    notifier = build_notifier(
        _settings(tmp_path, telegram_bot_token="123:abc", telegram_chat_id="456")
    )
    assert isinstance(notifier, TelegramNotifier)
    assert notifier.is_configured is True


def test_the_bot_token_is_read_from_the_secret_not_a_default(tmp_path: Path):
    notifier = build_notifier(
        _settings(tmp_path, telegram_bot_token="real-token", telegram_chat_id="chat-1")
    )
    assert isinstance(notifier, TelegramNotifier)
    # No public getter for the token by design (base.py's own discipline: it
    # never leaves telegram.py). Indirect proof: is_configured is only True
    # when both were actually read, not defaulted.
    assert notifier.is_configured is True
