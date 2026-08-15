"""The fallback Greeks model — spec section 4.2 priority 2.

**No handwritten pricing math.** This wraps ``vollib``'s Black-Scholes-Merton
implementation (``vollib.black_scholes_merton.greeks.analytical``) — the
actual runtime dependency the repository-approved, already-declared
``pyproject.toml`` extra (``greeks = ["py_vollib>=1.0"]``) pulls in. ``vollib``
is used directly rather than through its own ``py_vollib`` compatibility
shim, which is itself deprecated in favour of ``vollib`` (a
``DeprecationWarning`` fires on import if code reaches through it instead).
Every input this module needs — spot, strike, option type, implied
volatility, risk-free rate, dividend/carry assumption, evaluation timestamp,
time to the *persisted* actual expiry — is explicit; nothing is read from
strategy state or inferred.

Conventions verified directly against the installed package's own source
(not assumed): ``theta`` is already per-calendar-day (divided by 365
internally — see ``vollib.black_scholes_merton.greeks.analytical.theta``'s
own docstring/Hull worked example), and ``vega`` is already per-1%-IV-change
(multiplied by 0.01 internally) — the conventional options-desk units this
module's own callers expect, so no post-hoc unit conversion is applied here.

**Fails closed, never falls back to a second, unreviewed implementation.**
If ``vollib`` cannot be imported (extra not installed, or its own import
raises for any reason), :class:`GreeksModelUnavailable` is raised at
first use and the caller — :class:`~common.greeks.service.GreeksService` —
treats this exactly like a stale/missing broker-chain response: it blocks
entry and normal adjustment, and never blocks an exit (spec section 4.2).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from common.models import OptionType

from .models import GreekInputs, GreekSnapshot, GreekSource

#: Cached on first successful import; re-attempted every call while it keeps
#: failing (an operator installing the extra mid-session should not need a
#: process restart to pick it up).
_vollib_greeks: object | None = None


class GreeksModelUnavailable(RuntimeError):
    """``vollib`` could not be imported. See this module's own docstring for
    why that fails closed rather than falling back to a second model."""


def _load_vollib_greeks() -> object:
    global _vollib_greeks
    if _vollib_greeks is not None:
        return _vollib_greeks
    try:
        import vollib.black_scholes_merton.greeks.analytical as greeks_module
    except Exception as exc:  # pragma: no cover - exercised via monkeypatch
        raise GreeksModelUnavailable(
            "vollib is not importable — install the 'greeks' extra "
            "(pyproject.toml: greeks = [\"py_vollib>=1.0\"]) before entry/"
            "adjustment decisions can use the fallback Greeks model. This "
            "never falls back to a second, unreviewed pricing implementation."
        ) from exc
    _vollib_greeks = greeks_module
    return greeks_module


@dataclass(frozen=True)
class _RawGreeks:
    delta: float
    gamma: float
    theta: float
    vega: float


def _compute(inputs: GreekInputs) -> _RawGreeks:
    greeks_module = _load_vollib_greeks()
    flag = "c" if inputs.option_type is OptionType.CE else "p"
    time_to_expiry_years = _time_to_expiry_years(inputs)
    if time_to_expiry_years <= 0:
        raise GreeksModelUnavailable(
            f"evaluation_timestamp {inputs.evaluation_timestamp.isoformat()} is not strictly "
            f"before expiry_at {inputs.expiry_at.isoformat()} — cannot price an expired contract"
        )
    if inputs.implied_volatility <= 0:
        raise GreeksModelUnavailable(
            f"implied_volatility must be positive, got {inputs.implied_volatility!r}"
        )
    args = (
        flag,
        inputs.spot,
        inputs.strike,
        time_to_expiry_years,
        inputs.risk_free_rate,
        inputs.implied_volatility,
        inputs.dividend_yield,
    )
    delta = float(greeks_module.delta(*args))  # type: ignore[attr-defined]
    gamma = float(greeks_module.gamma(*args))  # type: ignore[attr-defined]
    theta = float(greeks_module.theta(*args))  # type: ignore[attr-defined]
    vega = float(greeks_module.vega(*args))  # type: ignore[attr-defined]
    return _RawGreeks(delta=delta, gamma=gamma, theta=theta, vega=vega)


def _time_to_expiry_years(inputs: GreekInputs) -> float:
    """Timezone-aware time-to-expiry, in years (ACT/365) — never naive."""
    if inputs.evaluation_timestamp.tzinfo is None or inputs.expiry_at.tzinfo is None:
        raise ValueError("evaluation_timestamp and expiry_at must both be timezone-aware")
    delta_seconds = (inputs.expiry_at - inputs.evaluation_timestamp).total_seconds()
    return delta_seconds / (365.0 * 24.0 * 3600.0)


def black_scholes_merton_greeks(
    inputs: GreekInputs, *, security_id: str, now: datetime | None = None
) -> GreekSnapshot:
    """Compute one option's delta/gamma/theta/vega from explicit inputs.

    Raises :class:`GreeksModelUnavailable` if ``vollib`` is not importable,
    the option has already expired, or ``implied_volatility`` is
    non-positive — every one of these must block a risk-increasing decision,
    never silently substitute a guessed value.
    """
    raw = _compute(inputs)
    received_at = now if now is not None else datetime.now(UTC)
    return GreekSnapshot(
        security_id=security_id,
        option_type=inputs.option_type,
        strike=inputs.strike,
        delta=raw.delta,
        gamma=raw.gamma,
        theta=raw.theta,
        vega=raw.vega,
        implied_volatility=inputs.implied_volatility,
        source=GreekSource.MODEL,
        source_timestamp=inputs.evaluation_timestamp,
        received_at=received_at,
        model_inputs=inputs,
    )
