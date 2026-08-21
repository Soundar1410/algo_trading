"""``build_notifier``: the one place Telegram credentials decide what a
process notifies through.

Every ``Settings`` here is constructed with an explicit, empty ``.env`` file
in an isolated ``tmp_path`` — never the real one. This machine's own ``.env``
carries real Telegram credentials (found during Phase 7 Part 2 development),
so a test that let ``Settings`` fall through to the real file could build a
real, working ``TelegramNotifier``.

That care was necessary and turned out to be insufficient: other tests did let
``Settings`` reach the real file, and a ``pytest`` run once sent hundreds of
real ``strategy_id=skelfix`` messages. The guard added in response
(``ALGO_DISABLE_EXTERNAL_NOTIFICATIONS``, on for the whole session) means the
two tests below asserting *production* behaviour now have to lift it
explicitly, for their own scope only — which is why they take the
``allow_external_notifications`` fixture. Neither sends anything: the notifier
they build is only ever inspected, never used. See
``tests/unit/test_notification_guard.py``.
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


def test_both_credentials_build_a_configured_telegram_notifier(
    tmp_path: Path, allow_external_notifications: None
):
    notifier = build_notifier(
        _settings(tmp_path, telegram_bot_token="123:abc", telegram_chat_id="456")
    )
    assert isinstance(notifier, TelegramNotifier)
    assert notifier.is_configured is True


def test_the_bot_token_is_read_from_the_secret_not_a_default(
    tmp_path: Path, allow_external_notifications: None
):
    notifier = build_notifier(
        _settings(tmp_path, telegram_bot_token="real-token", telegram_chat_id="chat-1")
    )
    assert isinstance(notifier, TelegramNotifier)
    # No public getter for the token by design (base.py's own discipline: it
    # never leaves telegram.py). Indirect proof: is_configured is only True
    # when both were actually read, not defaulted.
    assert notifier.is_configured is True


def test_the_guard_outranks_both_credentials(tmp_path: Path):
    """No fixture lifting the guard, so the session default applies.

    The same ``Settings`` that produces a real ``TelegramNotifier`` two tests
    above produces a ``NullNotifier`` here. That difference is the entire
    fix — nothing about the credentials changed.
    """
    notifier = build_notifier(
        _settings(tmp_path, telegram_bot_token="123:abc", telegram_chat_id="456")
    )
    assert isinstance(notifier, NullNotifier)
