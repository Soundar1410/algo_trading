"""Correlation IDs: mode namespacing, broker constraints, round-tripping."""

from __future__ import annotations

import pytest

from common.config.models import ExecutionMode
from common.execution.correlation import (
    MAX_LENGTH,
    STRATEGY_TOKEN_LENGTH,
    CorrelationIdError,
    build_correlation_id,
    is_paper,
    parse_correlation_id,
    strategy_token,
)


def _build(mode: ExecutionMode = ExecutionMode.PAPER, sequence: int = 1) -> str:
    return build_correlation_id(
        execution_mode=mode,
        runtime_id="intraday_options",
        strategy_id="st01",
        trading_date="2026-07-29",
        sequence_number=sequence,
    )


def test_format_matches_the_specification_example():
    assert _build() == "p_io_st01_20260729_0001"


def test_paper_and_live_ids_differ_in_their_namespace_prefix():
    paper = _build(ExecutionMode.PAPER)
    live = _build(ExecutionMode.LIVE)

    assert paper.startswith("p_")
    assert live.startswith("l_")
    assert paper != live


def test_the_mode_is_recoverable_from_the_id_alone():
    """A log line or Telegram message carries no execution_mode column."""
    assert parse_correlation_id(_build(ExecutionMode.PAPER)).execution_mode is ExecutionMode.PAPER
    assert parse_correlation_id(_build(ExecutionMode.LIVE)).execution_mode is ExecutionMode.LIVE


def test_is_paper_identifies_simulated_orders():
    assert is_paper(_build(ExecutionMode.PAPER))
    assert not is_paper(_build(ExecutionMode.LIVE))


def test_ids_stay_within_the_broker_length_limit():
    identifier = build_correlation_id(
        execution_mode=ExecutionMode.PAPER,
        runtime_id="intraday_options",
        strategy_id="a_very_long_strategy_name_indeed",
        trading_date="2026-07-29",
        sequence_number=9999,
    )
    assert len(identifier) <= MAX_LENGTH


def test_an_id_that_would_exceed_the_limit_is_refused_not_truncated():
    """Silent truncation could collide two strategies onto one ID."""
    with pytest.raises(CorrelationIdError, match="over the"):
        build_correlation_id(
            execution_mode=ExecutionMode.PAPER,
            runtime_id="intraday_options",
            strategy_id="st01",
            trading_date="2026-07-29",
            sequence_number=1234567890,
        )


def test_multi_word_runtime_becomes_initials():
    assert _build().split("_")[1] == "io"


def test_sequence_is_zero_padded_so_ids_sort_chronologically():
    ids = [_build(sequence=n) for n in (1, 2, 10)]
    assert ids == sorted(ids)


def test_compact_and_hyphenated_dates_agree():
    hyphenated = build_correlation_id(
        execution_mode=ExecutionMode.PAPER,
        runtime_id="io",
        strategy_id="st01",
        trading_date="2026-07-29",
        sequence_number=1,
    )
    compact = build_correlation_id(
        execution_mode=ExecutionMode.PAPER,
        runtime_id="io",
        strategy_id="st01",
        trading_date="20260729",
        sequence_number=1,
    )
    assert hyphenated == compact


@pytest.mark.parametrize("bad_date", ["2026-7-29", "29-07-2026", "", "not-a-date"])
def test_a_malformed_trading_date_is_rejected(bad_date: str):
    with pytest.raises(CorrelationIdError, match="trading_date"):
        build_correlation_id(
            execution_mode=ExecutionMode.PAPER,
            runtime_id="io",
            strategy_id="st01",
            trading_date=bad_date,
            sequence_number=1,
        )


def test_a_negative_sequence_is_rejected():
    with pytest.raises(CorrelationIdError, match="negative"):
        _build(sequence=-1)


def test_an_identifier_with_no_alphanumerics_is_rejected():
    with pytest.raises(CorrelationIdError, match="alphanumeric"):
        build_correlation_id(
            execution_mode=ExecutionMode.PAPER,
            runtime_id="___",
            strategy_id="st01",
            trading_date="2026-07-29",
            sequence_number=1,
        )


def test_parsing_recovers_every_component():
    parsed = parse_correlation_id("p_io_st01_20260729_0042")
    assert parsed.runtime_token == "io"
    assert parsed.strategy_token == "st01"
    assert parsed.trading_date == "20260729"
    assert parsed.sequence_number == 42


@pytest.mark.parametrize(
    "bad",
    ["x_io_st01_20260729_0001", "p_io_st01_2026_0001", "io_st01_20260729_0001", "", "p_io_st01"],
)
def test_parsing_rejects_a_malformed_id(bad: str):
    with pytest.raises(CorrelationIdError):
        parse_correlation_id(bad)


# --------------------------------------------------------- strategy_token
def test_strategy_token_matches_what_build_correlation_id_embeds():
    """Public exactly so a caller admitting strategies (the supervisor) can
    check for a collision before either strategy ever places an order —
    see the function's own docstring, D78."""
    correlation_id = build_correlation_id(
        execution_mode=ExecutionMode.PAPER,
        runtime_id="intraday_options",
        strategy_id="io_supertrend_fast_v1",
        trading_date="2026-07-29",
        sequence_number=1,
    )
    embedded_token = correlation_id.split("_")[2]
    assert strategy_token("io_supertrend_fast_v1") == embedded_token


def test_strategy_token_is_case_insensitive_and_strips_non_alphanumerics():
    assert strategy_token("IO_Skel_Fix") == strategy_token("io-skel-fix")


def test_two_different_strategy_ids_can_produce_the_same_token():
    """The property that makes this collision-prone rather than collision-
    proof — pinned directly, not just implied by the supervisor's guard
    against it. D78: this is exactly what happened to "skelone"/"skeltwo"."""
    assert strategy_token("skelone") == strategy_token("skeltwo") == "skel"


def test_strategy_token_length_matches_the_documented_constant():
    assert len(strategy_token("intraday_options_supertrend")) == STRATEGY_TOKEN_LENGTH
