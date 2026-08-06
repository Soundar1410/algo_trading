"""The engine's execution seam, backed by the audited persistence path.

Phase 3 Part 2b-ii-B-1. Part 2b-i shipped ``ExecutionGateway`` with only
:class:`~common.engine.positions.InMemoryGateway` behind it, so the ported engine
structurally could not reach a persisted :class:`~common.models.Position` — which
is why the "real position against a real exit engine" acceptance item had to be
deferred twice.

:class:`LifecycleGateway` closes that by driving the **existing**
:meth:`~common.execution.lifecycle.OrderLifecycle.handle_signal`. Nothing is
re-implemented here: every open and close goes through ``record_signal`` →
``reserve_intent`` → ``broker.submit`` → ``record_submission`` → ``apply_fill``, so
each leg gets a correlation ID, an intent committed before the broker call, a fill
row and an ``execution_mode``.

The gateway's two verbs are *directional*, not open/close — closing a long is a
``sell`` — and ``ExecutionRepository._upsert_position`` already nets by side and
flips a position to ``CLOSED`` at quantity zero. So this class needs no notion of
which a call is, and cannot get that judgement wrong.

Two hazards, designed for rather than discovered
------------------------------------------------
**The signals UNIQUE constraint.** ``signals`` carries ``UNIQUE (strategy_id,
execution_mode, instrument, candle_end_at)`` so that one completed candle produces
exactly one order. But the engine is *not* candle-driven and Dhan's exchange
timestamp is second-resolution, so an exit and a re-entry on one contract inside
one second would collide — and ``record_signal`` turns a collision into ``None``,
which ``handle_signal`` turns into a silent skip. A suppressed **close** would
leave a position open while the engine believed it closed. See
:meth:`LifecycleGateway._window`.

**A call that did not trade must raise.** Never a fabricated
:class:`~common.engine.positions.FillOutcome`. A loud failure leaves the position
open in the database where restart recovery adopts it, which is recoverable; a
phantom close is not. The check is deliberately on the *fills*, not on
``ExecutionResult.traded`` — that property is ``True`` for a broker **rejection**,
because a rejected order is still an order.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from common.config.models import ExecutionMode
from common.logging import get_logger
from common.models import Candle, OrderStatus, Side, Signal

from .models import OptionContract, OrderSide
from .positions import FillOutcome
from .state_payload import OPEN_POSITION_KEY, merge_payload

if TYPE_CHECKING:  # typing only: keeps common.execution out of the import graph
    from common.execution.lifecycle import ExecutionResult, OrderLifecycle
    from common.execution.repository import ExecutionRepository

log = get_logger(__name__)

#: The window a synthesised signal claims to have been evaluated over. The engine
#: evaluated no candle at all, so anything here is a convention — one second is
#: chosen because ``Candle`` rejects a zero-length bar and because a second is the
#: resolution of the exchange timestamp that produced the decision.
SYNTHETIC_WINDOW = timedelta(seconds=1)

#: The smallest gap that makes two signals on one instrument distinct rows.
_DISAMBIGUATOR = timedelta(microseconds=1)


class GatewayExecutionError(RuntimeError):
    """Raised when a gateway call did not result in a fill.

    Always raised, never swallowed into a plausible-looking ``FillOutcome``: the
    caller is :class:`~common.engine.positions.PositionManager`, whose next act
    would be to record a position that does not exist or to forget one that does.
    """


class LifecycleGateway:
    """An :class:`~common.engine.positions.ExecutionGateway` that persists."""

    def __init__(
        self,
        lifecycle: OrderLifecycle,
        *,
        strategy_id: str,
        execution_mode: ExecutionMode,
        trading_date: str,
        repository: ExecutionRepository | None = None,
        runtime_id: str = "",
    ) -> None:
        self._lifecycle = lifecycle
        self._strategy_id = strategy_id
        self._mode = execution_mode
        self._trading_date = trading_date
        #: Optional, and only for the contract record described in
        #: :meth:`_record_contract`. ``None`` disables that write entirely, which is
        #: what every offline construction wants — the gateway's trading behaviour
        #: does not depend on it.
        self._repository = repository
        self._runtime_id = runtime_id
        #: instrument -> the last ``candle_end_at`` this gateway used for it.
        self._last_window_end: dict[str, datetime] = {}
        #: How many legs this gateway has executed. Observable so a worker can report
        #: the true number of orders rather than inferring it from the trade count,
        #: which halves it and misses an open position's entry entirely.
        self.executions = 0

    # -------------------------------------------------------------- the verbs
    def buy(
        self,
        contract: OptionContract,
        lots: int,
        *,
        ref_price: float,
        ts: datetime,
        stop_price: float | None = None,
        target_price: float | None = None,
    ) -> FillOutcome:
        return self._execute(
            OrderSide.BUY,
            contract,
            lots,
            ref_price=ref_price,
            ts=ts,
            stop_price=stop_price,
            target_price=target_price,
        )

    def sell(
        self,
        contract: OptionContract,
        lots: int,
        *,
        ref_price: float,
        ts: datetime,
        stop_price: float | None = None,
        target_price: float | None = None,
    ) -> FillOutcome:
        return self._execute(
            OrderSide.SELL,
            contract,
            lots,
            ref_price=ref_price,
            ts=ts,
            stop_price=stop_price,
            target_price=target_price,
        )

    # ------------------------------------------------------------- the plumbing
    def _execute(
        self,
        side: Side,
        contract: OptionContract,
        lots: int,
        *,
        ref_price: float,
        ts: datetime,
        stop_price: float | None = None,
        target_price: float | None = None,
    ) -> FillOutcome:
        quantity = lots * contract.lot_size
        start_at, end_at = self._window(contract.symbol, ts)
        signal = Signal(
            strategy_id=self._strategy_id,
            execution_mode=self._mode,
            instrument=contract.symbol,
            security_id=contract.security_id,
            side=side,
            quantity=quantity,
            candle=Candle(
                security_id=contract.security_id,
                instrument=contract.symbol,
                # A tick-driven decision has no bar. Recording the reference price
                # four times is the honest degenerate case; inventing a strategy
                # bar the engine never evaluated would not be.
                open=ref_price,
                high=ref_price,
                low=ref_price,
                close=ref_price,
                volume=0,
                start_at=start_at,
                end_at=end_at,
            ),
            reference_price=ref_price,
            evaluated_at=ts,
            reason=f"engine {side.value} {lots} lot(s) of {contract.symbol}",
        )

        result = self._lifecycle.handle_signal(
            signal,
            trading_date=self._trading_date,
            stop_price=stop_price,
            target_price=target_price,
        )
        self._require_a_fill(result, side, contract)
        self.executions += 1
        self._record_contract(contract, lots, side, result)
        return self._outcome(result)

    def _record_contract(
        self,
        contract: OptionContract,
        lots: int,
        side: Side,
        result: ExecutionResult,
    ) -> None:
        """Keep ``strategy_state.payload`` agreeing with the ``positions`` row.

        Restart recovery needs the option type, strike, expiry and lot size, none of
        which the ``positions`` row carries, so an :class:`OptionContract` cannot be
        rebuilt from it (runbook §8 item 6). This writes that record on open and
        removes it on close.

        **The database decides which this call was, not this class.** The gateway's
        verbs are directional on purpose — closing a long is a ``sell`` — and the
        whole reason it cannot get open-vs-close wrong is that it holds no opinion
        about it. So the judgement is read off ``ExecutionResult.position``: the
        persisted row that ``apply_fill`` returned, already netted by side and
        already flipped to ``CLOSED`` at quantity zero. Adding an open/close notion
        here to answer the same question would reintroduce exactly the judgement this
        design removed.

        A failure to write is logged, not raised. The order is already placed and
        persisted; turning a bookkeeping problem into an exception here would
        propagate into :class:`~common.engine.positions.PositionManager` and abandon a
        position that genuinely exists. The cost of the missing record is that a
        restart does not adopt the position — it stays open in the database, visible,
        which is the same outcome the operator already has to handle.
        """
        if self._repository is None:
            return
        position = result.position
        if position is None:  # pragma: no cover - _require_a_fill already refused
            return
        record = (
            {
                "symbol": contract.symbol,
                "security_id": contract.security_id,
                "strike": contract.strike,
                "option_type": contract.option_type.value,
                "expiry": contract.expiry,
                "lot_size": contract.lot_size,
                "side": side.value,
                "lots": lots,
            }
            if position.is_open
            else None
        )
        try:
            merge_payload(
                self._repository,
                {OPEN_POSITION_KEY: record},
                runtime_id=self._runtime_id,
                strategy_id=self._strategy_id,
                execution_mode=self._mode,
                trading_date=self._trading_date,
            )
        except Exception:  # see the docstring: never fail the trade
            log.exception(
                "could not record the contract for %s; a restart will not adopt this "
                "position, which stays open in the database",
                contract.symbol,
            )

    def _window(self, instrument: str, ts: datetime) -> tuple[datetime, datetime]:
        """A strictly increasing ``candle_end_at`` per instrument.

        The dedup key is ``(strategy_id, execution_mode, instrument,
        candle_end_at)``, so two executions on one instrument must not share an end
        time. Taking ``max(ts, last + 1µs)`` makes a within-process collision
        structurally impossible while keeping the recorded moment truthful to the
        microsecond, and keeps the rows in execution order — which a random or
        hashed disambiguator would not.

        Per instrument, not global: the constraint does not span instruments, and
        pushing unrelated contracts apart would distort their timestamps for no
        reason.
        """
        previous = self._last_window_end.get(instrument)
        end_at = ts if previous is None else max(ts, previous + _DISAMBIGUATOR)
        if end_at != ts:
            log.debug(
                "two executions on %s within one timestamp; recording the second at "
                "%s so the signals unique constraint cannot suppress it",
                instrument,
                end_at.isoformat(),
            )
        self._last_window_end[instrument] = end_at
        return end_at - SYNTHETIC_WINDOW, end_at

    @staticmethod
    def _require_a_fill(result: ExecutionResult, side: Side, contract: OptionContract) -> None:
        """Refuse to report anything but a real, filled order."""
        order = result.order
        problem: str | None = None
        if result.skipped_reason is not None:
            problem = result.skipped_reason
        elif order is None:
            problem = "the lifecycle returned no order"
        elif order.status is OrderStatus.REJECTED:
            problem = f"the order was rejected: {order.rejection_reason}"
        elif not order.fills:
            problem = f"the order is {order.status.value} with no fill recorded"
        elif order.average_fill_price is None:
            problem = "the order reports fills but no average fill price"
        elif order.status is OrderStatus.PARTIALLY_FILLED:
            # Phase 4 Part 5. This branch used to be absent, and its absence was
            # silent: a partially filled order has fills and an average price, so
            # it passed every check above and PositionManager went on to record an
            # OpenPosition sized at the *requested* lots. The engine's book would
            # then have believed in exposure the broker never gave it.
            #
            # Refusing is the only available correct answer here, because the
            # ported FillOutcome carries a price and charges and no quantity — so
            # there is no way to tell PositionManager "you got 25 of the 75 you
            # asked for" without widening the Phase 3 port and its regression
            # tests. Loud, and it leaves the position exactly as the database has
            # it, which is the state restart recovery knows how to adopt.
            problem = (
                f"the order filled {order.filled_quantity} of the requested quantity and "
                "the position manager cannot represent a partial fill"
            )
        if problem is None:
            return

        # Loud on purpose. The position stays as the database has it — open if this
        # was a close — which is the state restart recovery knows how to adopt.
        raise GatewayExecutionError(
            f"{side.value} of {contract.symbol} did not trade: {problem}. "
            "No fill is reported to the position manager, because a fabricated one "
            "would record a trade that never happened."
        )

    @staticmethod
    def _outcome(result: ExecutionResult) -> FillOutcome:
        """Read the outcome back off the persisted fills, not off the request.

        This is what makes the engine's in-memory ``Trade`` and the database agree
        by construction rather than by convention.
        """
        order = result.order
        assert order is not None and order.average_fill_price is not None
        return FillOutcome(
            fill_price=order.average_fill_price,
            charges=sum(fill.charges for fill in order.fills),
            # Empty by necessity, not by omission: ``Fill`` carries a charges
            # total, since ``PaperBroker`` computes ``total_for_leg``. The total is
            # the authoritative number and it is preserved; only the six-component
            # split ``InMemoryGateway`` reports is unavailable here, and inventing
            # one by recomputing from rates could disagree with what was persisted.
            charges_breakdown={},
        )
