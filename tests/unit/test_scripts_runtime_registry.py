"""``scripts._runtimes`` and the ``start_runtime.py``/``start_strategy.py``
delegation it fixed: before this, both scripts always called
``runtimes.intraday_options.__main__.main`` regardless of ``--runtime-id``,
so ``scripts.start_runtime positional_options`` would have driven
``positional_options``'s strategies through intraday's own composition
root. These tests prove the registry resolves the *right* entrypoint and
that a positional-runtime strategy start is verified against what is
actually enabled before anything runs."""

from __future__ import annotations

import scripts._runtimes as runtimes_registry
import scripts.start_runtime as start_runtime
import scripts.start_strategy as start_strategy


def _replacing(registry, runtime_id: str, main):
    """That runtime's registry entry with `main` swapped in, codes untouched.

    Replacing the whole entry rather than mutating a separate callable map is
    what keeps one authoritative table: there is no second mapping a test could
    patch and leave the real one stale.
    """
    original = registry.RUNTIMES[runtime_id]
    return runtimes_registry.RuntimeEntrypoint(
        main=main,
        terminal_exit_codes=original.terminal_exit_codes,
        retryable_exit_codes=original.retryable_exit_codes,
    )


def test_resolve_entrypoint_knows_both_runtimes() -> None:
    assert runtimes_registry.resolve_entrypoint("intraday_options") is (
        runtimes_registry.RUNTIMES["intraday_options"].main
    )
    assert runtimes_registry.resolve_entrypoint("positional_options") is (
        runtimes_registry.RUNTIMES["positional_options"].main
    )


def test_there_is_exactly_one_runtime_table() -> None:
    """A derived ENTRYPOINTS copy would be a drift hazard, so it must not exist."""
    assert not hasattr(runtimes_registry, "ENTRYPOINTS")


def test_each_entry_carries_its_own_exit_code_classification() -> None:
    import runtimes.intraday_options.__main__ as intraday
    import runtimes.positional_options.__main__ as positional

    entry = runtimes_registry.RUNTIMES["positional_options"]
    assert entry.classify(positional.EXIT_RUNTIME_DISABLED) == "terminal"
    assert entry.classify(positional.EXIT_NO_CREDENTIALS) == "retryable"
    # Intraday's disabled code (3) means nothing to positional; it must not be
    # silently accepted as terminal just because intraday says so.
    assert entry.classify(intraday.EXIT_RUNTIME_DISABLED) == "unknown"


def test_resolve_entrypoint_fails_closed_on_an_unknown_runtime() -> None:
    try:
        runtimes_registry.resolve_entrypoint("not_a_real_runtime")
    except KeyError as exc:
        assert "not_a_real_runtime" in str(exc)
        assert "intraday_options" in str(exc)
        assert "positional_options" in str(exc)
    else:
        raise AssertionError("expected KeyError")


def test_start_runtime_calls_the_registered_entrypoint_for_positional(
    monkeypatch, tmp_path
) -> None:
    calls: list[list[str]] = []

    def _fake_positional_main(argv: list[str] | None) -> int:
        calls.append(list(argv or []))
        return 0

    monkeypatch.setitem(
        runtimes_registry.RUNTIMES,
        "positional_options",
        _replacing(runtimes_registry, "positional_options", _fake_positional_main),
    )
    exit_code = start_runtime.main(["positional_options", "--config-root", str(tmp_path)])
    assert exit_code == 0
    assert calls == [["--runtime-id", "positional_options", "--config-root", str(tmp_path)]]


def test_start_runtime_refuses_an_unknown_runtime(tmp_path, capsys) -> None:
    exit_code = start_runtime.main(["not_a_real_runtime", "--config-root", str(tmp_path)])
    assert exit_code == start_runtime.EXIT_UNKNOWN_RUNTIME
    assert "not_a_real_runtime" in capsys.readouterr().out


def test_start_strategy_still_uses_the_strategy_id_flag_for_intraday(
    monkeypatch, tmp_path
) -> None:
    calls: list[list[str]] = []

    def _fake_intraday_main(argv: list[str] | None) -> int:
        calls.append(list(argv or []))
        return 0

    monkeypatch.setitem(
        runtimes_registry.RUNTIMES,
        "intraday_options",
        _replacing(runtimes_registry, "intraday_options", _fake_intraday_main),
    )
    exit_code = start_strategy.main(
        ["c921_ema_cross_buy", "--runtime-id", "intraday_options", "--config-root", str(tmp_path)]
    )
    assert exit_code == 0
    assert calls == [
        [
            "--runtime-id", "intraday_options",
            "--strategy-id", "c921_ema_cross_buy",
            "--config-root", str(tmp_path),
        ]
    ]


def test_start_strategy_refuses_a_strategy_not_enabled_under_positional(
    tmp_path, monkeypatch
) -> None:
    """positional_options's own main() (Phase 5: a real --strategy-id filter,
    same as intraday_options) verifies the requested id is actually enabled
    before starting anything, and start_strategy.py simply propagates
    whatever exit code that real main() returns."""
    _write_positional_config_root(tmp_path)

    exit_code = start_strategy.main(
        [
            "not_the_configured_strategy",
            "--runtime-id", "positional_options",
            "--config-root", str(tmp_path / "config"),
        ]
    )
    assert exit_code != 0


def test_start_strategy_delegates_a_correctly_named_positional_strategy(
    tmp_path, monkeypatch
) -> None:
    _write_positional_config_root(tmp_path)
    calls: list[list[str]] = []

    def _fake_positional_main(argv: list[str] | None) -> int:
        calls.append(list(argv or []))
        return 0

    monkeypatch.setitem(
        runtimes_registry.RUNTIMES,
        "positional_options",
        _replacing(runtimes_registry, "positional_options", _fake_positional_main),
    )
    exit_code = start_strategy.main(
        [
            "weekly_delta_neutral",
            "--runtime-id", "positional_options",
            "--config-root", str(tmp_path / "config"),
        ]
    )
    assert exit_code == 0
    # Phase 5: positional_options now supports the same real --strategy-id
    # filter intraday_options always has.
    assert calls == [
        [
            "--runtime-id", "positional_options",
            "--strategy-id", "weekly_delta_neutral",
            "--config-root", str(tmp_path / "config"),
        ]
    ]


def _write_positional_config_root(tmp_path) -> None:  # type: ignore[no-untyped-def]
    config_root = tmp_path / "config"
    (config_root / "runtimes").mkdir(parents=True)
    (config_root / "strategies").mkdir(parents=True)
    (config_root / "global.yaml").write_text(
        "global:\n  live_trading_enabled: false\n  timezone: Asia/Kolkata\n"
        "runtime_defaults:\n  enabled: false\n  live_execution_allowed: false\n"
        "strategy_defaults:\n  enabled: false\n  mode: paper\n  live_approved: false\n",
        encoding="utf-8",
    )
    (config_root / "runtimes" / "positional_options.yaml").write_text(
        "runtime_id: positional_options\nenabled: true\nlive_execution_allowed: false\n",
        encoding="utf-8",
    )
    (config_root / "strategies" / "weekly_delta_neutral.yaml").write_text(
        "strategy_id: weekly_delta_neutral\nruntime_id: positional_options\n"
        "enabled: true\nmode: paper\nlive_approved: false\n"
        "engine: positional_multi_leg_engine\n",
        encoding="utf-8",
    )
