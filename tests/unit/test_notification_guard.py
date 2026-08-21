"""The external-notification guard: the fix for a real incident.

A previous ``pytest`` run delivered **hundreds of real Telegram messages** to
the operator's bot — ``worker_started`` / ``order_filled`` / ``worker_stopped``,
every one tagged ``strategy_id=skelfix``, which exists only as a test fixture.
The route was ordinary: ``tests/end_to_end/test_supervisor_signal.py`` starts
its child with ``cwd=REPO_ROOT``, ``Settings`` reads ``.env`` relative to the
working directory, and each ``spawn``ed worker builds its own notifier from
:data:`~runtimes.intraday_options.worker.NOTIFIER_FROM_SETTINGS`.

``tests/conftest.py`` had cleared every ``TELEGRAM_*`` variable and it changed
nothing, because the credentials were never in the environment. These tests pin
the property that actually holds: a guard *downstream* of credential loading,
so real credentials plus the guard is unconditionally a
:class:`~common.notifications.base.NullNotifier`.

Every credential here is fake and shaped like a real one on purpose — the point
of each test is that something real-looking is refused anyway.
"""

from __future__ import annotations

import socket
import urllib.request
from pathlib import Path

import pytest

from common.config import Settings
from common.notifications import (
    DISABLE_EXTERNAL_NOTIFICATIONS_ENV,
    NotificationEvent,
    NullNotifier,
    SafeNotifier,
    TelegramNotifier,
    build_notifier,
    external_notifications_disabled,
)

#: Shaped exactly like a Telegram bot token (``<digits>:<35 chars>``) and
#: exactly as fake as it looks. Never a real credential.
FAKE_BOT_TOKEN = "7000000000:AAFfakefakefakefakefakefakefakefake1"
FAKE_CHAT_ID = "-1009999999999"


def _event() -> NotificationEvent:
    return NotificationEvent(
        event_type="order_filled", message="fake fill", runtime_id="intraday_options",
        strategy_id="skelfix",
    )


def _credentialled_settings(**overrides: str) -> Settings:
    return Settings(
        telegram_bot_token=FAKE_BOT_TOKEN,  # type: ignore[arg-type]
        telegram_chat_id=FAKE_CHAT_ID,  # type: ignore[arg-type]
        **overrides,  # type: ignore[arg-type]
    )


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Make any real egress attempt an immediate, loud test failure.

    Two layers, because the assertion is "nothing was attempted", not "nothing
    succeeded": ``urlopen`` catches the HTTP path, and ``socket.connect``
    catches anything that reached for a socket by another route.
    """
    attempts: list[object] = []

    def _no_urlopen(*args: object, **kwargs: object) -> object:
        attempts.append(("urlopen", args))
        raise AssertionError("a test attempted a real HTTP request")

    def _no_connect(self: socket.socket, address: object) -> None:
        attempts.append(("connect", address))
        raise AssertionError(f"a test attempted a real socket connection to {address!r}")

    monkeypatch.setattr(urllib.request, "urlopen", _no_urlopen)
    monkeypatch.setattr(socket.socket, "connect", _no_connect)
    return attempts


# ------------------------------------------------------------ the bootstrap
def test_the_root_conftest_and_the_guard_module_name_the_same_variable():
    """Both spell the variable as a literal, by design; they must not drift.

    The root ``conftest.py`` cannot import :mod:`common.notifications.guard`
    for the constant — its entire job is to run *before* the first project
    import — so the duplication is deliberate and this is what keeps it
    honest. Loaded by path rather than by ``import conftest``: pytest already
    owns that module name for ``tests/conftest.py``.
    """
    import importlib.util

    root_conftest_path = Path(__file__).resolve().parents[2] / "conftest.py"
    spec = importlib.util.spec_from_file_location("_root_conftest", root_conftest_path)
    assert spec is not None and spec.loader is not None
    root_conftest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(root_conftest)

    import tests.conftest as tests_conftest

    assert root_conftest.DISABLE_EXTERNAL_NOTIFICATIONS_ENV == (
        DISABLE_EXTERNAL_NOTIFICATIONS_ENV
    )
    assert tests_conftest._NOTIFICATION_GUARD_ENV == DISABLE_EXTERNAL_NOTIFICATIONS_ENV


def test_the_guard_is_already_active_in_every_test(monkeypatch: pytest.MonkeyPatch):
    """Set at session bootstrap, so no test has to remember to ask for it."""
    import os

    assert os.environ[DISABLE_EXTERNAL_NOTIFICATIONS_ENV] == "1"
    assert external_notifications_disabled() is True


def test_isolated_env_clears_the_other_algo_variables_but_never_this_one(
    monkeypatch: pytest.MonkeyPatch,
):
    """The exemption is the whole point: ``ALGO_`` prefix matching would eat it."""
    import os

    assert "ALGO_LIVE_SMOKE" not in os.environ
    assert DISABLE_EXTERNAL_NOTIFICATIONS_ENV in os.environ


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "Yes", "on", " 1 "])
def test_truthy_spellings_all_disable(monkeypatch: pytest.MonkeyPatch, value: str):
    monkeypatch.setenv(DISABLE_EXTERNAL_NOTIFICATIONS_ENV, value)
    assert external_notifications_disabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_falsy_spellings_do_not_disable(monkeypatch: pytest.MonkeyPatch, value: str):
    monkeypatch.setenv(DISABLE_EXTERNAL_NOTIFICATIONS_ENV, value)
    assert external_notifications_disabled() is False


def test_the_guard_defaults_to_off_so_production_is_unchanged(
    allow_external_notifications: None,
):
    """Absent the variable and absent the setting, nothing is disabled."""
    assert external_notifications_disabled() is False
    assert external_notifications_disabled(Settings()) is False


# ------------------------------------------------- credentials lose to the guard
def test_real_looking_credentials_still_build_a_null_notifier(no_network: list[object]):
    notifier = build_notifier(_credentialled_settings())

    assert isinstance(notifier, NullNotifier)
    assert notifier.channel == "null"
    assert no_network == []


def test_the_guard_beats_credentials_reloaded_from_a_dotenv(
    tmp_path: Path, no_network: list[object]
):
    """The exact shape of the incident: a populated ``.env`` in the CWD.

    ``isolated_env`` chdirs into ``tmp_path``, so writing a ``.env`` here is
    precisely what the supervisor-signal child process saw at ``REPO_ROOT``.
    The first assertion is load-bearing: it proves the credentials really were
    read off disk, so the ``NullNotifier`` below is the guard doing its job and
    not an empty ``Settings`` accidentally passing the test.
    """
    Path(".env").write_text(
        f"TELEGRAM_BOT_TOKEN={FAKE_BOT_TOKEN}\nTELEGRAM_CHAT_ID={FAKE_CHAT_ID}\n",
        encoding="utf-8",
    )
    from common.config import load_settings

    settings = load_settings()
    assert settings.has_telegram_credentials() is True, "the .env was not actually read"

    assert isinstance(build_notifier(settings), NullNotifier)
    assert no_network == []


def test_the_setting_alone_disables_even_with_the_variable_absent(
    allow_external_notifications: None, no_network: list[object]
):
    """An operator may park the switch in ``.env`` permanently."""
    settings = _credentialled_settings(algo_disable_external_notifications="1")

    assert external_notifications_disabled(settings) is True
    assert isinstance(build_notifier(settings), NullNotifier)
    assert no_network == []


# ------------------------------------------- the notifier itself is the backstop
def test_a_directly_constructed_telegram_notifier_sends_nothing(no_network: list[object]):
    """The factory is not the only door; this class is the last one."""
    notifier = TelegramNotifier(bot_token=FAKE_BOT_TOKEN, chat_id=FAKE_CHAT_ID)

    assert notifier.is_configured is True
    assert notifier.send(_event()) is False
    assert no_network == []


def test_wrapping_it_in_a_safe_notifier_does_not_reopen_the_door(no_network: list[object]):
    safe = SafeNotifier(TelegramNotifier(bot_token=FAKE_BOT_TOKEN, chat_id=FAKE_CHAT_ID))

    assert safe.send(_event()) is False
    assert no_network == []


def test_an_injected_transport_is_not_a_way_round_the_guard(no_network: list[object]):
    """Injection is for tests that lifted the guard, not for bypassing it."""
    calls: list[str] = []

    def _transport(url: str, payload: bytes, timeout: float) -> bytes:
        calls.append(url)
        return b'{"ok": true}'

    notifier = TelegramNotifier(
        bot_token=FAKE_BOT_TOKEN, chat_id=FAKE_CHAT_ID, transport=_transport
    )

    assert notifier.send(_event()) is False
    assert calls == [], "the guard must short-circuit before any transport runs"


# --------------------------------------------------- production, guard absent
def test_production_builds_a_real_telegram_notifier_when_the_guard_is_absent(
    allow_external_notifications: None, no_network: list[object]
):
    """The guard must not have quietly disabled the product itself."""
    notifier = build_notifier(_credentialled_settings())

    assert isinstance(notifier, TelegramNotifier)
    assert notifier.channel == "telegram"
    assert no_network == [], "merely building a notifier must not touch the network"


def test_production_delivery_works_against_an_injected_fake_transport(
    allow_external_notifications: None, no_network: list[object]
):
    """Real send logic, real payload, zero sockets."""
    seen: list[tuple[str, bytes, float]] = []

    def _transport(url: str, payload: bytes, timeout: float) -> bytes:
        seen.append((url, payload, timeout))
        return b'{"ok": true}'

    notifier = TelegramNotifier(
        bot_token=FAKE_BOT_TOKEN, chat_id=FAKE_CHAT_ID, transport=_transport
    )

    assert notifier.send(_event()) is True
    assert len(seen) == 1
    url, payload, _timeout = seen[0]
    assert url.endswith("/sendMessage")
    assert FAKE_CHAT_ID.replace("-", "%2D") in payload.decode() or "chat_id" in payload.decode()
    assert b"order_filled" in payload
    assert no_network == []


def test_a_transport_failure_is_still_swallowed(
    allow_external_notifications: None, no_network: list[object]
):
    """Unchanged contract: notification failure never reaches trading code."""

    def _transport(url: str, payload: bytes, timeout: float) -> bytes:
        raise TimeoutError("fake timeout")

    notifier = TelegramNotifier(
        bot_token=FAKE_BOT_TOKEN, chat_id=FAKE_CHAT_ID, transport=_transport
    )

    assert notifier.send(_event()) is False
    assert no_network == []


# ------------------------------------------------------------------- secrets
def test_no_token_reaches_the_logs_or_stdout(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
):
    """A guard that leaked the credential while blocking it would be no better."""
    import logging

    caplog.set_level(logging.DEBUG)
    notifier = build_notifier(_credentialled_settings())
    notifier.send(_event())
    TelegramNotifier(bot_token=FAKE_BOT_TOKEN, chat_id=FAKE_CHAT_ID).send(_event())

    captured = capsys.readouterr()
    for text in (caplog.text, captured.out, captured.err):
        assert FAKE_BOT_TOKEN not in text
        assert FAKE_BOT_TOKEN.split(":")[1] not in text
