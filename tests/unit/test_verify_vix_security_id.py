"""``scripts/verify_vix_security_id.py`` — no network in this test, ever.

``fetch_scrip_master_text`` is monkeypatched with canned CSV text shaped
exactly like the real Dhan master's INDEX rows (see this module's own
manual, real-network run recorded in ``config/strategies/straddle_920.yaml``
and ``docs/IMPLEMENTATION_STATUS_AND_RUNBOOK.md`` — this file only proves the
script's own PASS/FAIL logic against a fixture, not the live source).
"""

from __future__ import annotations

import pytest

import scripts.verify_vix_security_id as verify_vix

_HEADER = (
    "SEM_EXM_EXCH_ID,SEM_SEGMENT,SEM_SMST_SECURITY_ID,SEM_INSTRUMENT_NAME,"
    "SEM_EXPIRY_CODE,SEM_TRADING_SYMBOL,SEM_LOT_UNITS,SEM_CUSTOM_SYMBOL,"
    "SEM_EXPIRY_DATE,SEM_STRIKE_PRICE,SEM_OPTION_TYPE,SEM_TICK_SIZE,"
    "SEM_EXPIRY_FLAG,SEM_EXCH_INSTRUMENT_TYPE,SEM_SERIES,SM_SYMBOL_NAME"
)

#: Byte-for-byte the shape of the two real rows this file's docstring
#: recorded from a live fetch (2026-08-15) — NIFTY spot and India VIX.
_NIFTY_ROW = "NSE,I,13,INDEX,0,NIFTY,1.0,Nifty 50,0001-01-01,,XX,0.0500,,INDEX,X,NIFTY"
_VIX_ROW = "NSE,I,21,INDEX,0,INDIA VIX,1.0,India VIX,0001-01-01,,XX,0.0500,,INDEX,X,INDIA VIX"
#: An unrelated OPTIDX row — must never be picked up by the INDEX-only filter.
_OPTION_ROW = (
    "NSE,D,50001,OPTIDX,0,NIFTY-Jul2026-24000-CE,75,NIFTY 24000 CE,"
    "2026-07-30 14:30:00,24000,CE,5.0000,,OPTIDX,,NIFTY"
)

_MASTER = "\n".join([_HEADER, _NIFTY_ROW, _VIX_ROW, _OPTION_ROW])


def test_the_configured_vix_id_passes_against_a_fixture_shaped_like_the_real_master(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verify_vix, "fetch_scrip_master_text", lambda: _MASTER)
    exit_code = verify_vix.main(["--security-id", "21", "--symbol", "VIX"])
    assert exit_code == verify_vix.EXIT_PASS


def test_a_wrong_id_fails_rather_than_silently_passing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exact defect this script exists to catch: a configured id that
    resolves to nothing (or something else) in the current master."""
    monkeypatch.setattr(verify_vix, "fetch_scrip_master_text", lambda: _MASTER)
    exit_code = verify_vix.main(["--security-id", "99999", "--symbol", "VIX"])
    assert exit_code == verify_vix.EXIT_NO_MATCH


def test_an_id_that_exists_but_is_not_vix_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """13 is a real INDEX row (NIFTY) — proves this isn't just an
    "id exists" check; the resolved symbol must actually be VIX."""
    monkeypatch.setattr(verify_vix, "fetch_scrip_master_text", lambda: _MASTER)
    exit_code = verify_vix.main(["--security-id", "13", "--symbol", "VIX"])
    assert exit_code == verify_vix.EXIT_NO_MATCH


def test_an_option_row_sharing_the_id_number_is_never_mistaken_for_the_index_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The OPTIDX row's own id (50001) must never satisfy an INDEX lookup —
    the instrument-type filter is load-bearing, not cosmetic."""
    monkeypatch.setattr(verify_vix, "fetch_scrip_master_text", lambda: _MASTER)
    exit_code = verify_vix.main(["--security-id", "50001", "--symbol", "VIX"])
    assert exit_code == verify_vix.EXIT_NO_MATCH


def test_a_fetch_failure_is_a_clean_fail_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> str:
        raise RuntimeError("simulated network failure")

    monkeypatch.setattr(verify_vix, "fetch_scrip_master_text", _boom)
    exit_code = verify_vix.main(["--security-id", "21"])
    assert exit_code == verify_vix.EXIT_FETCH_FAILED
