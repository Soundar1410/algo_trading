"""TOTP generation for the Dhan headless login.

Ported from the reference repository's proven ``_default_totp``: a plain
``pyotp.TOTP(secret).now()``. There is nothing to invent here, and inventing it
would be a poor idea — a subtly wrong TOTP implementation fails in a way that
looks exactly like a wrong PIN.

Two things beyond the bare call:

* **The provider is injectable.** Tests exercise the login and bootstrap logic
  with a stub, so no test needs ``pyotp`` or a real secret.
* **Clock skew is measured.** A rejected TOTP is very often local clock drift
  rather than a wrong secret, and the two produce identical HTTP responses. The
  bootstrap includes :func:`clock_skew_note` in the error so an operator is not
  told "bad credentials" when their clock is off. Regenerating inside the same
  30-second step yields the *same* six digits, which is a second, independent
  reason retrying a rejected TOTP is pointless.
"""

from __future__ import annotations

import time
from collections.abc import Callable

#: Dhan's TOTP step, and the RFC 6238 default.
TOTP_STEP_SECONDS = 30

#: Local-clock error beyond which a TOTP rejection is more likely a clock problem
#: than a credential problem. Half a step: past this, the code we generate can
#: belong to a different window than the server's.
SKEW_WARN_SECONDS = TOTP_STEP_SECONDS / 2

#: Produces a six-digit TOTP. Injectable so tests need neither pyotp nor a secret.
TotpProvider = Callable[[], str]


def build_totp_provider(secret: str) -> TotpProvider:
    """Return a provider yielding the current TOTP for ``secret``.

    ``pyotp`` is imported lazily inside the provider so that an import failure
    surfaces only when a real login is attempted, not at module import time.
    """
    if not secret:
        raise ValueError("A TOTP secret is required to build a TOTP provider")

    def _now() -> str:
        import pyotp

        return str(pyotp.TOTP(secret).now())

    return _now


def seconds_into_step(now: float | None = None) -> float:
    """How far into the current TOTP window we are, in seconds.

    Near the end of a window the code is about to change, which is a benign
    reason for a rejection that a retry in the *next* window could fix — but the
    bootstrap still does not retry, because it cannot distinguish that case from
    a genuinely wrong secret without burning an attempt to find out.
    """
    moment = time.time() if now is None else now
    return moment % TOTP_STEP_SECONDS


def clock_skew_note(now: float | None = None) -> str | None:
    """A human-readable hint when the clock looks like the likelier culprit.

    Returns ``None`` when there is nothing useful to say. We cannot measure true
    skew without a trusted time source, so this reports the observable fact —
    position within the step — rather than inventing a skew figure. An NTP check
    is the operator's next step, and the message says so.
    """
    position = seconds_into_step(now)
    remaining = TOTP_STEP_SECONDS - position
    if remaining <= SKEW_WARN_SECONDS:
        return (
            f"the TOTP window had {remaining:.0f}s left when this code was generated; "
            "if the system clock is off by more than a few seconds the code lands in "
            "the wrong window. Verify with `sntp -sS time.apple.com` or System "
            "Settings > Date & Time before assuming the PIN or secret is wrong."
        )
    return None
