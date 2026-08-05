"""Phase 4 Part 4. The shared secret-unwrap helper, promoted from two
independent copies (``scripts/auth_bootstrap.py``, ``scripts/capture_live_tape.py``)
now that a third caller (``runtimes/intraday_options/engine_worker.py``'s
warm-up token resolution) exists.
"""

from __future__ import annotations

from pydantic import SecretStr

from common.config.secrets import read_secret


def test_a_populated_secret_is_unwrapped() -> None:
    assert read_secret(SecretStr("shh")) == "shh"


def test_none_holder_returns_none() -> None:
    assert read_secret(None) is None


def test_an_empty_secret_returns_none_not_empty_string() -> None:
    assert read_secret(SecretStr("")) is None


def test_a_non_secret_object_with_no_get_secret_value_returns_none() -> None:
    assert read_secret(object()) is None


def test_a_plain_string_is_not_treated_as_a_secret_holder() -> None:
    # Strings have no get_secret_value; the caller must pass the SecretStr
    # itself, not an already-unwrapped value.
    assert read_secret("already-a-string") is None
