"""TOTP generation and the clock-skew diagnostic.

A rejected TOTP and a wrong PIN produce byte-identical HTTP responses from Dhan,
and one of the two common causes of a rejected TOTP is a local clock that has
drifted. Since the bootstrap deliberately does not retry either case, the only
way an operator learns which they are looking at is the message — hence the
diagnostic being tested here rather than left to a comment.
"""

from __future__ import annotations

import pytest

from common.authentication import build_totp_provider, clock_skew_note
from common.authentication.totp import (
    SKEW_WARN_SECONDS,
    TOTP_STEP_SECONDS,
    seconds_into_step,
)

# A valid base32 secret. Not a real credential — it is the RFC 4648 test vector
# used throughout pyotp's own documentation.
TEST_SECRET = "JBSWY3DPEHPK3PXP"


def test_a_provider_yields_six_digits():
    code = build_totp_provider(TEST_SECRET)()
    assert code.isdigit()
    assert len(code) == 6


def test_two_calls_in_the_same_window_agree():
    """The reason retrying a rejected TOTP is provably useless.

    Within one 30-second step the generated code is identical, so an immediate
    retry re-sends exactly what Dhan just rejected.
    """
    provider = build_totp_provider(TEST_SECRET)
    assert provider() == provider()


def test_an_empty_secret_is_refused():
    with pytest.raises(ValueError, match="TOTP secret is required"):
        build_totp_provider("")


def test_pyotp_is_not_imported_until_a_code_is_generated():
    """Lazy import: a missing pyotp must surface at login, not at module import.

    Building the provider is what configuration does; generating is what a real
    login does. Only the latter needs the dependency.
    """
    import sys

    sys.modules.pop("pyotp", None)
    provider = build_totp_provider(TEST_SECRET)
    assert "pyotp" not in sys.modules
    provider()
    assert "pyotp" in sys.modules


# ------------------------------------------------------------------ step maths


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (0.0, 0.0),
        (10.0, 10.0),
        (29.999, 29.999),
        (30.0, 0.0),
        (45.5, 15.5),
        (1_785_400_000.0, 1_785_400_000.0 % 30),
    ],
)
def test_position_within_the_step_is_computed(now: float, expected: float):
    assert seconds_into_step(now) == pytest.approx(expected)


def test_the_step_is_the_rfc_default():
    assert TOTP_STEP_SECONDS == 30
    assert SKEW_WARN_SECONDS == 15


# -------------------------------------------------------------- skew reporting


def test_no_note_early_in_the_window():
    """20s remaining: the code is comfortably inside its window."""
    assert clock_skew_note(now=10.0) is None


@pytest.mark.parametrize("now", [16.0, 20.0, 29.5])
def test_a_note_appears_late_in_the_window(now: float):
    note = clock_skew_note(now=now)
    assert note is not None
    assert "window" in note


def test_the_note_points_at_the_clock_and_not_at_the_credentials():
    """The whole purpose: stop an operator chasing a PIN that is actually fine."""
    note = clock_skew_note(now=29.0)
    assert note is not None
    assert "clock" in note.lower()
    assert "sntp" in note.lower() or "date & time" in note.lower()


def test_the_note_reports_only_observable_facts():
    """No invented skew figure.

    Without a trusted time source the true offset is unknowable, so the message
    reports position within the step and tells the operator how to check.
    """
    note = clock_skew_note(now=29.0)
    assert note is not None
    assert "1s left" in note
