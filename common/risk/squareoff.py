"""Intraday square-off.

Two spec rules shape this:

* **New entries stop at the cutoff, before square-off itself.** They are
  separate times so a strategy cannot open a position at 15:14 that the 15:15
  square-off immediately closes at a loss.
* **A restart must not reset square-off state or re-open entries after the
  cutoff.** State therefore lives in ``strategy_state``, not in memory, and the
  decision is a pure function of (clock, persisted state) so a recovering worker
  reaches the same conclusion as the one that died.

The trigger is driven by the candle clock rather than wall time. In Phase 1 that
makes the whole slice deterministic — square-off happens because the tape
crossed 15:15, not because a test was slow — and in production it keeps
square-off aligned with the same exchange timestamps that drive signals.

Phase 6 Part 4 adds a second, date-shaped trigger: a contract held past its own
expiry (minus a configurable lead) is force-closed regardless of the time of
day. It composes into :meth:`SquareOffPolicy.trigger_at` rather than becoming a
second decider — see :mod:`common.engine.square_off`'s module docstring for why
a single decision point matters here. ``ExpiryPolicy`` lives in this module,
not in ``common.config.models``, so that ``common.config`` (which pulls in
``pydantic_settings``) depends on this stdlib-only module rather than the
reverse — every spawned worker imports :mod:`common.risk` at module level.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = "Asia/Kolkata"


class SquareOffState(StrEnum):
    """Persisted square-off progress for one strategy-day."""

    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class SquareOffTrigger(StrEnum):
    """Why the policy says to act now."""

    NONE = "NONE"
    BLOCK_NEW_ENTRIES = "BLOCK_NEW_ENTRIES"
    SQUARE_OFF = "SQUARE_OFF"


class ExpiryPolicy(StrEnum):
    """How a strategy is required to behave as its contract approaches expiry.

    Spec section 11: ``force_square_off_before_expiry`` is "the safer default"
    and the only value usable today. ``simulate_exchange_settlement`` names the
    alternative the spec permits "only after settlement tests pass" — no such
    simulator exists in this repository, so config loading refuses that value
    (see :mod:`common.config.models`). It is defined here anyway, rather than
    left unrepresentable, so the refusal can name the precondition instead of
    surfacing as an opaque "not a valid enum member".
    """

    FORCE_SQUARE_OFF_BEFORE_EXPIRY = "force_square_off_before_expiry"
    SIMULATE_EXCHANGE_SETTLEMENT = "simulate_exchange_settlement"


@dataclass(frozen=True)
class SquareOffPolicy:
    """Cutoff and square-off times for one intraday strategy.

    ``expiry_policy``/``square_off_before_expiry_days`` add a second, date-shaped
    rule (Phase 6 Part 4): once a held contract's expiry is within the
    configured lead, :meth:`trigger_at` returns ``SQUARE_OFF`` regardless of
    time of day. The *expiry date itself* is deliberately not a field here — it
    is resolved per-contract at runtime (real contracts pick their nearest
    listed expiry at worker start) and passed into :meth:`trigger_at` by the
    caller who knows it.
    """

    entry_cutoff: time = time(15, 0)
    square_off_at: time = time(15, 15)
    timezone: str = DEFAULT_TIMEZONE
    expiry_policy: ExpiryPolicy = ExpiryPolicy.FORCE_SQUARE_OFF_BEFORE_EXPIRY
    #: Calendar days of lead before expiry at which holding becomes overdue.
    #: ``0`` — the default — means "not past expiry day itself", so every
    #: existing intraday config keeps its current behaviour unless it lives
    #: long enough to cross an expiry date. See runbook limitation 26 for why
    #: this counts calendar days rather than trading days.
    square_off_before_expiry_days: int = 0

    def __post_init__(self) -> None:
        if self.square_off_at < self.entry_cutoff:
            raise ValueError(
                f"square_off_at {self.square_off_at} must not precede "
                f"entry_cutoff {self.entry_cutoff}"
            )
        if self.expiry_policy is ExpiryPolicy.SIMULATE_EXCHANGE_SETTLEMENT:
            # Defence in depth: common/config/models.py refuses this at config
            # load, which is where an operator actually sees the message. This
            # check exists so nothing can reach a live SquareOffPolicy with this
            # value by constructing one directly, bypassing the config loader.
            raise ValueError(
                "expiry_policy=simulate_exchange_settlement is refused: exchange-"
                "settlement simulation is not implemented. Spec section 11 "
                "requires a versioned settlement policy (expiry calendar and "
                "last-trading-day handling, final settlement price capture, "
                "ITM/OTM determination, index-option cash settlement, exercise/"
                "assignment recording, effective-dated exercise STT and charges, "
                "T+1 settlement timing, stock-option physical delivery and "
                "margin) and states this value may be used only after settlement "
                "tests pass. Until then force_square_off_before_expiry is the "
                "only permitted value."
            )
        if self.square_off_before_expiry_days < 0:
            raise ValueError(
                "square_off_before_expiry_days must not be negative, got "
                f"{self.square_off_before_expiry_days}"
            )

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def _local_time(self, moment: datetime) -> time:
        """Delegates to the shared helper, which this method used to be.

        Phase 4 Part 3 promoted this conversion into
        :func:`common.utils.timeutils.local_time_in` because it was the only
        place in the repository getting it right, while `MarketSession` and
        `SessionSquareOffAuthority` compared unconverted wall times. Behaviour
        here is unchanged in one respect and tightened in another: a naive
        ``moment`` used to be silently read as system-local and is now refused.
        """
        from common.utils.timeutils import local_time_in

        return local_time_in(moment, self.timezone, argument="moment")

    def entries_allowed(self, moment: datetime) -> bool:
        """False once the cutoff has passed — and it never becomes True again."""
        return self._local_time(moment) < self.entry_cutoff

    def holding_overdue(self, moment: datetime, expiry: str | None) -> bool:
        """True once holding through ``moment`` violates the expiry lead.

        ``expiry`` is the contract's own expiry date, read from its leading 10
        characters (``"YYYY-MM-DD"``) — the same shape
        :func:`common.engine.regime._is_expiry_day` already reads off
        ``OptionContract.expiry``. ``None`` or anything unparseable makes the
        rule **inert** (never overdue), not overdue: unlike an unreadable
        persisted square-off state — where nothing else will ever close the
        position, so failing towards square-off is the only safe direction —
        the ordinary time-of-day ladder below still runs, so there is no unsafe
        direction to fail towards. A simulated contract's placeholder expiry
        (e.g. ``"WEEKLY"``) is the common case this protects: it must not
        force-close a fixture run on its first tick. See runbook limitation 28.
        """
        if not expiry:
            return False
        try:
            expiry_date = date.fromisoformat(str(expiry)[:10])
        except ValueError:
            return False
        from common.utils.timeutils import local_date_in

        local_date = local_date_in(moment, self.timezone, argument="moment")
        last_holding_day = expiry_date - timedelta(days=self.square_off_before_expiry_days)
        return local_date > last_holding_day

    def trigger_at(
        self,
        moment: datetime,
        *,
        state: SquareOffState,
        expiry: str | None = None,
    ) -> SquareOffTrigger:
        """What should happen at this moment, given persisted progress.

        Pure: the same inputs always give the same answer, which is what lets a
        restarted worker resume without re-deciding the day.

        ``expiry``, when given, composes the expiry-lead rule ahead of the
        ordinary time-of-day ladder: an overdue day forces ``SQUARE_OFF`` at any
        time, including before ``entry_cutoff``. Persisted state is still
        checked first, so ``COMPLETED``/``IN_PROGRESS`` suppress an overdue day
        exactly as they suppress a post-``square_off_at`` restart.
        """
        if state in {SquareOffState.COMPLETED, SquareOffState.IN_PROGRESS}:
            return SquareOffTrigger.NONE

        if self.holding_overdue(moment, expiry):
            return SquareOffTrigger.SQUARE_OFF

        local = self._local_time(moment)
        if local >= self.square_off_at:
            return SquareOffTrigger.SQUARE_OFF
        if local >= self.entry_cutoff:
            return SquareOffTrigger.BLOCK_NEW_ENTRIES
        return SquareOffTrigger.NONE
