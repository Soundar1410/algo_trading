"""Phase 4 Part 4. :func:`~runtimes.intraday_options.engine_worker.
build_warmup_manager` -- the wiring that turns ``warmup_source="dhan"`` into a
real ``WarmupManager``/``WarmupSource`` pair, or safely into ``(None, None)``.

No network anywhere in this file: credentials come from an env-var override
so the token-cache filesystem path is never reached, and constructing
``DhanHistoricalDataClient`` never makes a call by itself.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from pydantic import SecretStr

from common.config import Settings
from common.config.models import ExecutionMode
from common.risk import SquareOffPolicy
from common.warmup.manager import WarmupManager
from common.warmup.source import WarmupSource
from runtimes.intraday_options.engine_worker import build_warmup_manager
from runtimes.intraday_options.worker import EngineWorkerConfig, WorkerConfig


def _worker_config(
    tmp_path: Path, *, security_id: str = "13", **engine_kwargs: object
) -> WorkerConfig:
    return WorkerConfig(
        runtime_id="intraday_options",
        strategy_id="engine01",
        security_id=security_id,  # NIFTY spot index by default
        instrument="NIFTY",
        database_path=tmp_path / "db.sqlite",
        lock_dir=tmp_path,
        pid_dir=tmp_path,
        log_dir=tmp_path,
        trading_date="2026-08-01",
        execution_mode=ExecutionMode.PAPER,
        square_off_policy=SquareOffPolicy(),
        engine=EngineWorkerConfig(
            strategy_ref="strategies.intraday_options.engine_fixture_strategy:EngineFixtureStrategy",
            strike_step=50,
            lot_size=50,
            **engine_kwargs,  # type: ignore[arg-type]
        ),
    )


def _settings(*, client_id: str | None = None, access_token: str | None = None) -> Settings:
    return Settings(
        dhan_client_id=SecretStr(client_id) if client_id else None,
        dhan_access_token=SecretStr(access_token) if access_token else None,
    )


def test_warmup_source_none_builds_nothing_and_touches_no_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import common.config as config_module

    def _raise() -> Settings:
        raise AssertionError("load_settings must not be called when warmup_source='none'")

    monkeypatch.setattr(config_module, "load_settings", _raise)
    config = _worker_config(tmp_path)  # default: warmup_source="none"
    manager, source = build_warmup_manager(config, config.engine)  # type: ignore[arg-type]
    assert (manager, source) == (None, None)


def test_warmup_source_dhan_without_credentials_cold_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import common.config as config_module

    monkeypatch.setattr(config_module, "load_settings", lambda: _settings())
    config = _worker_config(tmp_path, warmup_source="dhan")
    with caplog.at_level(logging.WARNING):
        manager, source = build_warmup_manager(config, config.engine)  # type: ignore[arg-type]
    assert (manager, source) == (None, None)
    assert any("cold start" in r.message for r in caplog.records)


def test_warmup_source_dhan_with_credentials_builds_manager_and_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import common.config as config_module

    monkeypatch.setattr(
        config_module,
        "load_settings",
        lambda: _settings(client_id="client-1", access_token="token-1"),
    )
    config = _worker_config(tmp_path, warmup_source="dhan", warmup_max_lookback_sessions=7)
    manager, source = build_warmup_manager(config, config.engine)  # type: ignore[arg-type]
    assert isinstance(manager, WarmupManager)
    assert manager._max_lookback_sessions == 7
    assert source == WarmupSource(
        security_id="13", exchange_segment="IDX_I", instrument_type="INDEX"
    )


def test_warmup_source_dhan_is_independent_of_contract_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import common.config as config_module

    monkeypatch.setattr(
        config_module,
        "load_settings",
        lambda: _settings(client_id="client-1", access_token="token-1"),
    )
    config = _worker_config(tmp_path, warmup_source="dhan", contract_resolver="simulated")
    manager, source = build_warmup_manager(config, config.engine)  # type: ignore[arg-type]
    assert manager is not None
    assert source is not None


def test_unknown_warmup_source_raises(tmp_path: Path) -> None:
    config = _worker_config(tmp_path, warmup_source="bogus")
    with pytest.raises(ValueError, match="bogus"):
        build_warmup_manager(config, config.engine)  # type: ignore[arg-type]


def test_a_mismatched_resolved_security_id_refuses_to_warm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The worker's own security id (what the live feed actually subscribes)
    must match what resolve_index_meta resolves for the configured instrument
    -- otherwise warm-up would silently seed indicators from a different
    instrument's history. Not hypothetical: index_security_id is an
    independent override that could go stale.
    """
    import common.config as config_module

    monkeypatch.setattr(
        config_module,
        "load_settings",
        lambda: _settings(client_id="client-1", access_token="token-1"),
    )
    # NIFTY's registry entry is security_id "13"; this worker claims "999".
    config = _worker_config(tmp_path, security_id="999", warmup_source="dhan")
    with caplog.at_level(logging.ERROR):
        manager, source = build_warmup_manager(config, config.engine)  # type: ignore[arg-type]
    assert (manager, source) == (None, None)
    assert any("does not match" in r.message for r in caplog.records)
