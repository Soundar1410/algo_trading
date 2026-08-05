"""Phase 4 Part 4. :class:`~common.warmup.source.WarmupSource` — a plain,
frozen data descriptor. See its module docstring for why ``from_option`` is
ported but has no caller in this repository.
"""

from __future__ import annotations

import dataclasses

import pytest

from common.engine.models import OptionContract, OptionType
from common.market_data.scrip_master import IndexMeta
from common.warmup.source import WarmupSource


def _index_meta(**overrides: object) -> IndexMeta:
    fields = {
        "security_id": "13",
        "segment": "IDX_I",
        "fno_segment": "NSE_FNO",
        "exchange": "NSE",
    }
    fields.update(overrides)
    return IndexMeta(**fields)  # type: ignore[arg-type]


def _option_contract(**overrides: object) -> OptionContract:
    fields: dict[str, object] = {
        "symbol": "NIFTY 04AUG26 24100 CE",
        "security_id": "65697",
        "strike": 24100.0,
        "option_type": OptionType.CE,
        "expiry": "2026-08-04",
        "lot_size": 65,
    }
    fields.update(overrides)
    return OptionContract(**fields)  # type: ignore[arg-type]


def test_from_underlying_builds_the_expected_source() -> None:
    meta = _index_meta()
    source = WarmupSource.from_underlying(meta)
    assert source == WarmupSource(
        security_id="13", exchange_segment="IDX_I", instrument_type="INDEX"
    )


def test_from_underlying_stringifies_a_non_string_security_id() -> None:
    meta = _index_meta(security_id="13")
    source = WarmupSource.from_underlying(meta)
    assert isinstance(source.security_id, str)


def test_from_option_builds_the_expected_source() -> None:
    contract = _option_contract()
    source = WarmupSource.from_option(contract, "NSE_FNO")
    assert source == WarmupSource(
        security_id="65697", exchange_segment="NSE_FNO", instrument_type="OPTIDX"
    )


def test_from_option_takes_the_segment_from_the_argument_not_the_contract() -> None:
    # OptionContract carries no exchange segment of its own (Part 1's
    # design: that lives on IndexMeta.fno_segment) — the caller must supply
    # it, and this is what proves the parameter is actually used rather than
    # a contract-derived default silently winning.
    contract = _option_contract()
    source = WarmupSource.from_option(contract, "BSE_FNO")
    assert source.exchange_segment == "BSE_FNO"


def test_warmup_source_is_frozen() -> None:
    source = WarmupSource(security_id="13", exchange_segment="IDX_I", instrument_type="INDEX")
    with pytest.raises(dataclasses.FrozenInstanceError):
        source.security_id = "99"  # type: ignore[misc]
