"""Reading the ``exp`` claim from a Dhan access token.

The asymmetry that shapes every case here: returning ``None`` ("cannot tell")
makes the caller keep a token it cannot prove is dead, which costs at most one
rejected API call. Wrongly returning a number would either discard a good token
(forcing a generation attempt against a two-minute rate limit) or keep a dead one
past its expiry. So every unparseable input must yield ``None``, never a guess.
"""

from __future__ import annotations

import base64
import json

import pytest

from common.authentication import decode_token_exp


def _token(claims: object, *, parts: int = 3) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    pieces = ["eyJhbGciOiJIUzI1NiJ9", payload, "c2lnbmF0dXJl"][:parts]
    return ".".join(pieces)


def test_the_exp_claim_is_read():
    assert decode_token_exp(_token({"exp": 4_871_532_800})) == 4_871_532_800


def test_a_string_exp_is_coerced():
    """Dhan has been observed to send numeric claims as strings."""
    assert decode_token_exp(_token({"exp": "1785400000"})) == 1_785_400_000


def test_a_float_exp_is_truncated_to_seconds():
    assert decode_token_exp(_token({"exp": 1785400000.75})) == 1_785_400_000


def test_unpadded_base64url_is_decoded():
    """JWT payloads are conventionally unpadded; padding must be restored."""
    token = _token({"exp": 1, "pad": "abc"})
    assert "=" not in token.split(".")[1]
    assert decode_token_exp(token) == 1


def test_a_payload_with_no_exp_returns_none():
    assert decode_token_exp(_token({"dhanClientId": "1100000000"})) is None


@pytest.mark.parametrize(
    ("token", "why"),
    [
        ("", "empty"),
        ("no-dots-at-all", "not a JWT"),
        ("header-only.", "empty payload"),
        ("onlyone", "single segment"),
        ("header.!!!not-base64!!!.sig", "undecodable payload"),
        ("header.bm90IGpzb24.sig", "payload is not JSON"),
        ("header.WzEsIDIsIDNd.sig", "payload is a JSON array, not an object"),
        ("header.ImJhcmUgc3RyaW5nIg.sig", "payload is a bare JSON string"),
    ],
)
def test_unparseable_tokens_return_none(token: str, why: str):
    assert decode_token_exp(token) is None, why


def test_a_two_segment_token_still_yields_its_payload():
    """An unsigned JWT is unusual but its payload is still readable."""
    assert decode_token_exp(_token({"exp": 42}, parts=2)) == 42


@pytest.mark.parametrize("value", [None, True, False, "not-a-number", [], {}])
def test_a_non_numeric_exp_returns_none(value: object):
    """``exp: true`` is not an expiry.

    ``bool`` is an ``int`` subclass in Python, so ``int(True) == 1`` would
    otherwise report a token as having expired in 1970.
    """
    assert decode_token_exp(_token({"exp": value})) is None
