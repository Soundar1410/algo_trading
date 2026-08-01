"""Wiring the real resolver into the engine worker (Phase 4 Part 1).

Three seams, each of which could close limitation 17 on paper while leaving it
open in practice:

1. :func:`build_option_selector` — which resolver the worker actually builds, and
   whether the exchange's lot size beats the configured one.
2. ``_subscription_sender`` — whether the option's exchange segment travels with
   the id the engine chose. It is decided here rather than in the engine, so it
   is only tested here.
3. ``_parse_subscription_request`` — whether the supervisor understands what the
   worker now puts on the control queue, *and* still understands the old shape.
"""

from __future__ import annotations

import queue as queue_module
from pathlib import Path

import pytest

from common.config.models import ExecutionMode
from common.engine.selection import DhanOptionChainResolver, SimulatedOptionChainResolver
from common.market_data.scrip_master import segment_code
from common.risk import SquareOffPolicy
from runtimes.intraday_options.engine_worker import (
    _subscription_sender,
    build_option_selector,
)
from runtimes.intraday_options.supervisor import _parse_subscription_request
from runtimes.intraday_options.worker import EngineWorkerConfig, WorkerConfig

MULTI = Path(__file__).resolve().parents[1] / "fixtures" / "dhan_scrip_master_multi.csv"
NSE_FNO = segment_code("NSE_FNO")


def _worker_config(tmp_path: Path, **engine_kwargs: object) -> WorkerConfig:
    return WorkerConfig(
        runtime_id="intraday_options",
        strategy_id="engine01",
        security_id="13",  # the NIFTY spot index
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


def _seed_cache(tmp_path: Path) -> Path:
    """Put today's master in a cache directory so no test reaches the network."""
    from datetime import date

    from common.market_data.scrip_master import ScripMasterCache
    from common.utils.timeutils import now_ist

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    today: date = now_ist().date()
    ScripMasterCache(cache_dir).path_for(today).write_text(MULTI.read_text())
    return cache_dir


# ------------------------------------------------------------------- selector
def test_the_default_is_still_the_simulated_resolver(tmp_path: Path):
    """Every existing config must behave exactly as it did before Part 1."""
    config = _worker_config(tmp_path)
    selector, segment = build_option_selector(config, config.engine)  # type: ignore[arg-type]
    assert isinstance(selector._resolver, SimulatedOptionChainResolver)
    assert segment is None, "a synthetic contract has no exchange segment"


def test_the_dhan_resolver_yields_real_ids_and_the_option_segment(tmp_path: Path):
    config = _worker_config(
        tmp_path,
        contract_resolver="dhan",
        scrip_master_cache_dir=str(_seed_cache(tmp_path)),
        expiry="2026-08-04",
    )
    selector, segment = build_option_selector(config, config.engine)  # type: ignore[arg-type]

    assert isinstance(selector._resolver, DhanOptionChainResolver)
    assert segment == NSE_FNO

    from common.models import OptionType

    contract = selector.select(24148.0, OptionType.CE)
    assert contract.security_id == "8103"
    assert not contract.security_id.startswith("SIM:")


def test_the_exchange_lot_size_beats_the_configured_one(tmp_path: Path):
    """The configured 50 must lose to the master's 75 — the other half of
    limitation 17, and the half that silently mis-sizes every position."""
    config = _worker_config(
        tmp_path,
        contract_resolver="dhan",
        scrip_master_cache_dir=str(_seed_cache(tmp_path)),
        expiry="2026-08-04",
    )
    assert config.engine is not None and config.engine.lot_size == 50

    from common.models import OptionType

    selector, _ = build_option_selector(config, config.engine)
    assert selector.select(24148.0, OptionType.CE).lot_size == 75


def test_the_selector_and_the_resolver_agree_on_the_expiry(tmp_path: Path):
    """With no expiry configured the resolver picks one; the selector must be
    told which, or the two disagree about the series being traded."""
    config = _worker_config(
        tmp_path,
        contract_resolver="dhan",
        scrip_master_cache_dir=str(_seed_cache(tmp_path)),
    )
    selector, _ = build_option_selector(config, config.engine)  # type: ignore[arg-type]
    assert selector._expiry == selector._resolver.expiry  # type: ignore[attr-defined]


def test_an_unknown_resolver_name_is_refused(tmp_path: Path):
    config = _worker_config(tmp_path, contract_resolver="optionchain")
    with pytest.raises(ValueError, match="Unknown contract_resolver"):
        build_option_selector(config, config.engine)  # type: ignore[arg-type]


def test_building_the_dhan_resolver_needs_no_network(tmp_path: Path):
    """The cache is seeded, so a fetch attempt would be a defect. Proven by
    handing the builder a cache dir and no reachable URL."""
    import common.market_data.scrip_master as module

    original = module.fetch_scrip_master_text

    def _explode(**_: object) -> str:
        raise AssertionError("the worker reached the network for the scrip master")

    module.fetch_scrip_master_text = _explode  # type: ignore[assignment]
    try:
        config = _worker_config(
            tmp_path,
            contract_resolver="dhan",
            scrip_master_cache_dir=str(_seed_cache(tmp_path)),
            expiry="2026-08-04",
        )
        build_option_selector(config, config.engine)  # type: ignore[arg-type]
    finally:
        module.fetch_scrip_master_text = original  # type: ignore[assignment]


# --------------------------------------------------------- subscription sender
def test_the_option_carries_the_segment_and_the_underlying_does_not(tmp_path: Path):
    """The underlying is already subscribed on the hub's default segment. Sending
    the option's segment for it would move the index onto ``NSE_FNO``, where it
    does not exist."""
    config = _worker_config(tmp_path)
    control: queue_module.Queue = queue_module.Queue()
    send = _subscription_sender(config, control, option_segment=NSE_FNO)
    assert send is not None

    send("13")  # the underlying
    send("8103")  # the option the engine chose

    assert control.get_nowait() == ("13", None)
    assert control.get_nowait() == ("8103", NSE_FNO)


def test_a_simulated_run_sends_no_segment_at_all(tmp_path: Path):
    config = _worker_config(tmp_path)
    control: queue_module.Queue = queue_module.Queue()
    send = _subscription_sender(config, control, option_segment=None)
    assert send is not None
    send("SIM:NIFTY:WEEKLY:24000:CE")
    assert control.get_nowait() == ("SIM:NIFTY:WEEKLY:24000:CE", None)


def test_no_control_queue_still_means_no_sender(tmp_path: Path):
    """``None`` makes HubTickFeed say so rather than swallow the subscription."""
    assert _subscription_sender(_worker_config(tmp_path), None) is None


def test_a_full_control_queue_does_not_stop_the_run(tmp_path: Path):
    class _Full:
        def put_nowait(self, item: object) -> None:
            raise RuntimeError("queue is full")

    send = _subscription_sender(_worker_config(tmp_path), _Full(), option_segment=NSE_FNO)
    assert send is not None
    send("8103")  # must not raise


# ------------------------------------------------------ supervisor wire format
def test_the_supervisor_reads_the_new_tuple_shape():
    assert _parse_subscription_request(("8103", NSE_FNO)) == ("8103", NSE_FNO)
    assert _parse_subscription_request(("8103", None)) == ("8103", None)


def test_the_supervisor_still_reads_a_bare_id():
    """The recorded paths and any pre-Phase-4 sender put a plain string on the
    queue. Dropping that shape would break them silently."""
    assert _parse_subscription_request("8103") == ("8103", None)


@pytest.mark.parametrize(
    "malformed",
    [
        "",
        None,
        42,
        (),
        ("8103",),
        ("8103", "NSE_FNO"),  # the name, not the code
        ("8103", True),  # a bool is an int, and would subscribe to segment 1
        ("", 2),
        (2, "8103"),  # the right pieces in the wrong order
        ["8103", 2],
    ],
)
def test_a_malformed_request_is_dropped_rather_than_crashing_the_group(malformed):
    assert _parse_subscription_request(malformed) is None
