"""Dhan adapter: normalisation and SDK-isolation, without a network.

The live connection is exercised only by the opt-in smoke test. What is tested
here is the part that must not break silently: payload normalisation, and the
structural rule that no other module imports the SDK.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from common.market_data.dhan import DhanFeedError, DhanMarketFeedAdapter

REPO_ROOT = Path(__file__).resolve().parents[2]


def _adapter() -> DhanMarketFeedAdapter:
    return DhanMarketFeedAdapter(
        client_id="test-client",
        access_token="test-token",
        instrument_label="NIFTY",
    )


# ------------------------------------------------------------ construction
def test_missing_credentials_are_refused_at_construction():
    with pytest.raises(DhanFeedError, match="client id and an access token"):
        DhanMarketFeedAdapter(client_id="", access_token="token")


def test_starting_without_a_subscription_is_refused():
    with pytest.raises(DhanFeedError, match="no subscriptions"):
        _adapter().start(lambda tick: None)


def test_subscriptions_are_a_union_not_a_list():
    adapter = _adapter()
    adapter.subscribe(["1", "2"])
    adapter.subscribe(["2", "3"])
    assert adapter._security_ids == {"1", "2", "3"}


# ----------------------------------------------------------- normalisation
def test_a_valid_payload_becomes_a_tick():
    adapter = _adapter()
    tick = adapter._normalise({"security_id": "13", "LTP": "101.25", "last_quantity": 50})

    assert tick is not None
    assert tick.security_id == "13"
    assert tick.last_price == 101.25
    assert tick.last_quantity == 50
    assert tick.instrument == "NIFTY"
    assert tick.exchange_time.tzinfo is not None


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "not a dict",
        {},
        {"security_id": "13"},
        {"security_id": "13", "LTP": 0},
        {"security_id": "", "LTP": 100.0},
        {"LTP": 100.0},
    ],
)
def test_a_malformed_payload_is_counted_not_raised(payload: object):
    """One bad frame must not tear down the feed for every worker."""
    adapter = _adapter()
    assert adapter._normalise(payload) is None
    assert adapter.malformed_payloads == 1


def test_an_epoch_exchange_timestamp_is_parsed():
    adapter = _adapter()
    tick = adapter._normalise({"security_id": "13", "LTP": 100.0, "LTT": 1_785_000_000})
    assert tick is not None
    assert tick.exchange_time.year == 2026


def test_an_unparseable_timestamp_falls_back_to_receipt_time():
    adapter = _adapter()
    tick = adapter._normalise({"security_id": "13", "LTP": 100.0, "LTT": "garbage"})
    assert tick is not None
    assert tick.exchange_time == tick.received_at


def test_stopping_an_unstarted_adapter_is_safe():
    _adapter().stop()  # must not raise


# --------------------------------------------------------- SDK isolation
def test_only_the_dhan_adapter_imports_the_sdk():
    """The spec's rule that strategies never call the SDK, enforced structurally."""
    result = subprocess.run(
        [
            "grep",
            "-rln",
            "dhanhq",
            "--include=*.py",
            "common",
            "strategies",
            "runtimes",
            "dashboards",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    importers = {line for line in result.stdout.splitlines() if line}
    assert importers == {"common/market_data/dhan.py"}, (
        f"dhanhq must only be imported by the adapter, found: {sorted(importers)}"
    )


def test_the_sdk_is_not_imported_at_package_import_time():
    """A lazy import keeps credential-free test runs fast and offline."""
    code = (
        "import sys; import common.market_data.dhan; "
        "assert 'dhanhq' not in sys.modules, 'dhanhq imported eagerly'"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
