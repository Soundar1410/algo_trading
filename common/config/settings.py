"""Environment-sourced settings — secrets and the project root.

Secrets live *only* here, read from an ignored local ``.env``. They are never
written to YAML, never persisted to SQLite, never sent to Streamlit, and never
logged (see :mod:`common.logging.redaction`).

``SecretStr`` is deliberate: printing or interpolating one yields ``**********``,
so an accidental ``log.info("%s", settings)`` cannot leak a credential.
"""

from __future__ import annotations

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process-wide environment configuration.

    Loaded from the real environment first, then ``.env``. Absent secrets are
    ``None`` rather than an error: paper-mode forward testing needs market data
    and a token, but a fresh checkout must still be able to run the test suite
    with no credentials present at all.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    dhan_client_id: SecretStr | None = None
    dhan_pin: SecretStr | None = None
    dhan_totp_secret: SecretStr | None = None

    # Manual override for the token the auth bootstrap would otherwise mint. Kept
    # because it is occasionally the fastest way to work around an auth outage,
    # and because Phase 1's smoke test used it. The bootstrap prefers it over the
    # cache when it is still valid, and ignores it once expired.
    dhan_access_token: SecretStr | None = None

    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: SecretStr | None = None

    project_root: str | None = None

    algo_log_level: str = "INFO"
    algo_env: str = "local"

    # Kill switch only. ``common.config.loader.apply_env_overrides`` honours this
    # when it parses to false and ignores it otherwise — an environment variable
    # must never be able to turn real-money trading on.
    algo_live_trading_enabled: str | None = None

    def has_dhan_credentials(self) -> bool:
        """True when every Dhan credential needed by the auth bootstrap is present.

        Callers use this to fail *closed* with a clear message rather than
        letting the SDK raise something opaque halfway through startup.
        """
        return all(
            v is not None and v.get_secret_value() != ""
            for v in (self.dhan_client_id, self.dhan_pin, self.dhan_totp_secret)
        )

    def has_dhan_access_token(self) -> bool:
        """True when a token was supplied directly, bypassing generation."""
        token = self.dhan_access_token
        return token is not None and token.get_secret_value() != ""

    def has_telegram_credentials(self) -> bool:
        """True when Telegram can be used. Notifications are never required."""
        return all(
            v is not None and v.get_secret_value() != ""
            for v in (self.telegram_bot_token, self.telegram_chat_id)
        )


def load_settings() -> Settings:
    """Read settings from the environment and ``.env``."""
    return Settings()
