"""Where to fetch warm-up history from — an engine-agnostic data descriptor.

Ported from the reference repository's ``framework/warmup/source.py``
(Phase 4 Part 4). Import paths only: this repository's
:class:`~common.engine.models.OptionContract` and
:class:`~common.market_data.scrip_master.IndexMeta` already carry the same
field names the reference's equivalents do, so no field renaming was needed.

A :class:`WarmupSource` names the instrument whose history warms an indicator:
the underlying index (single-leg strategies) or a specific option contract
(fixed-strike / multi-leg strategies, resolved intraday). It carries exactly
the three fields Dhan's intraday-candle endpoint needs, so
:class:`~common.warmup.manager.WarmupManager` never has to know about charts,
legs, or engines. It is a plain data descriptor, not an interface — there is
no Dhan-specific or recorded-tape subclass; the fetch itself lives in
:mod:`common.warmup.historical`.

``from_option`` is ported but has no caller in this repository: at the point
``engine_worker.py`` builds a warm-up source, no option contract has been
resolved yet — the strategy picks its strike on its first signal — and no
``MultiLegEngine``/``FixedStrikeEngine`` exists here to warm a per-leg premium
stream. It is kept for whichever of those arrives first, rather than dropped
and re-derived later.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from common.engine.models import OptionContract
    from common.market_data.scrip_master import IndexMeta


@dataclass(frozen=True)
class WarmupSource:
    """Instrument coordinates for a historical-candle fetch."""

    security_id: str
    exchange_segment: str  # e.g. "IDX_I" (index) | "NSE_FNO" (option)
    instrument_type: str  # e.g. "INDEX" | "OPTIDX"

    @classmethod
    def from_underlying(cls, meta: IndexMeta) -> WarmupSource:
        """Warm from the underlying index's own history."""
        return cls(
            security_id=str(meta.security_id),
            exchange_segment=meta.segment,
            instrument_type="INDEX",
        )

    @classmethod
    def from_option(cls, contract: OptionContract, fno_segment: str) -> WarmupSource:
        """Warm from a specific option contract's own history.

        ``fno_segment`` comes from the underlying's :class:`IndexMeta`
        (``"NSE_FNO"`` / ``"BSE_FNO"``); index options are ``OPTIDX``.
        """
        return cls(
            security_id=str(contract.security_id),
            exchange_segment=fno_segment,
            instrument_type="OPTIDX",
        )
