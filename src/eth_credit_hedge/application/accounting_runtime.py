"""Runtime façade for the one authoritative combined accounting ledger."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from collections.abc import Callable
from decimal import Decimal
from typing import cast

from eth_credit_hedge.application.accounting_reconciliation import (
    AccountingReconciliationService,
    AccountingReplayPort,
    AccountingStateReaderPort,
    AccountingStartupReconciliationResult,
)
from eth_credit_hedge.application.private_execution_accounting import (
    PrivateExecutionClassificationError,
    PrivateExecutionClassifier,
)
from eth_credit_hedge.domain.accounting.events import (
    AccountingEvent,
    AccountingSnapshotCreated,
    EventSource,
    ExitReason,
    FundingRecorded,
    HedgeExecutionRecorded,
    HedgeRole,
    OptionExecutionRecorded,
    PositionReconciled,
    RecoveryAllocationRecorded,
    RecoveryDebtIncremented,
)
from eth_credit_hedge.domain.accounting.errors import (
    DuplicateAccountingIdentifierError,
)
from eth_credit_hedge.domain.accounting.reconstruction import (
    CombinedLedgerReconstructor,
    CombinedLedgerState,
)
from eth_credit_hedge.domain.accounting.recovery_projection import HedgeAttemptKey
from eth_credit_hedge.domain.strategy_math.units import Money, Price, Quantity
from eth_credit_hedge.domain.execution import ExecutionUpdateBatch
from eth_credit_hedge.ports.accounting_persistence import AccountingLedgerPersistencePort


ExecutionAccountingEvent = OptionExecutionRecorded | HedgeExecutionRecorded


@dataclass(frozen=True, slots=True)
class AccountingValuation:
    """The latest executable perpetual valuation supplied to reconstruction."""

    hedge_mark: Price | None = None
    hedge_liquidation: Price | None = None


class AccountingRuntime:
    """Persist confirmed facts immediately, then expose only reconstructed state."""

    def __init__(
        self,
        *,
        store: AccountingLedgerPersistencePort,
        reconstructor: CombinedLedgerReconstructor,
    ) -> None:
        self._store = store
        self._reconstructor = reconstructor
        self._events: tuple[AccountingEvent, ...] = ()
        self._valuation = AccountingValuation()
        self._state = reconstructor.reconstruct(())

    @property
    def state(self) -> CombinedLedgerState:
        return self._state

    @property
    def events(self) -> tuple[AccountingEvent, ...]:
        return self._events

    async def initialize(self) -> CombinedLedgerState:
        self._events = await self._store.load_events_after(0)
        self._state = self._reconstruct(self._events)
        return self._state

    async def apply_private_execution_batch(
        self,
        events: tuple[ExecutionAccountingEvent, ...],
        *,
        valuation: AccountingValuation | None = None,
    ) -> CombinedLedgerState:
        if not events:
            return self._state
        if any(event.source is not EventSource.PRIVATE_STREAM for event in events):
            raise ValueError("private execution batches require PRIVATE_STREAM events")
        await self._record(events, valuation)
        return await self._settle_closed_recovery_lots()

    async def apply_private_update_batch(
        self,
        batch: ExecutionUpdateBatch,
        classifier: PrivateExecutionClassifier,
        *,
        valuation: AccountingValuation | None = None,
    ) -> CombinedLedgerState:
        batch = self._unseen_execution_batch(batch)
        if not batch.executions:
            return self._state
        try:
            events = classifier.classify_batch(batch, self._state)
        except PrivateExecutionClassificationError as error:
            await self.record_system_event(
                PositionReconciled(
                    event_id=f"private-classification-fault:{batch.raw_payload_hash}",
                    event_version=1,
                    cycle_id=classifier.cycle_id,
                    timestamp=batch.received_at,
                    source=EventSource.SYSTEM,
                    correlation_id=batch.raw_payload_hash,
                    execution_id=error.execution.execution_id,
                    order_id=error.execution.order_id,
                    order_link_id=error.execution.order_link_id,
                    symbol=error.execution.symbol,
                    internal_quantity=Quantity(error.execution.quantity),
                    external_quantity=Quantity(error.execution.quantity),
                    matched=False,
                    detail=str(error),
                ),
                valuation=valuation,
            )
            raise
        return await self.apply_private_execution_batch(events, valuation=valuation)

    def _unseen_execution_batch(
        self,
        batch: ExecutionUpdateBatch,
    ) -> ExecutionUpdateBatch:
        recorded = {
            event.execution.execution_id: event.execution
            for event in self._events
            if isinstance(event, (OptionExecutionRecorded, HedgeExecutionRecorded))
        }
        unseen = []
        for update in batch.executions:
            prior = recorded.get(update.execution_id)
            if prior is None:
                unseen.append(update)
                continue
            same = (
                prior.symbol == update.symbol
                and prior.side.value == update.side.upper()
                and prior.price.value == update.price
                and prior.quantity.value == update.quantity
                and prior.fee.value == update.fee
                and prior.timestamp == update.executed_at
                and prior.order_id == update.order_id
                and prior.order_link_id == update.order_link_id
            )
            if not same:
                raise DuplicateAccountingIdentifierError(
                    f"conflicting execution ID: {update.execution_id}"
                )
        return ExecutionUpdateBatch(
            executions=tuple(unseen),
            received_at=batch.received_at,
            raw_payload_hash=batch.raw_payload_hash,
        )

    async def apply_event(
        self,
        event: AccountingEvent,
        *,
        valuation: AccountingValuation | None = None,
    ) -> CombinedLedgerState:
        return await self._record((event,), valuation)

    async def apply_funding(
        self,
        event: FundingRecorded,
        *,
        valuation: AccountingValuation | None = None,
    ) -> CombinedLedgerState:
        return await self._record((event,), valuation)

    async def apply_rest_recovery(
        self,
        events: tuple[AccountingEvent, ...],
        *,
        valuation: AccountingValuation | None = None,
    ) -> CombinedLedgerState:
        if any(event.source is not EventSource.REST_RECOVERY for event in events):
            raise ValueError("REST recovery requires REST_RECOVERY events")
        return await self._record(events, valuation)

    async def apply_rest_update_batch(
        self,
        batch: ExecutionUpdateBatch,
        classifier: PrivateExecutionClassifier,
        *,
        valuation: AccountingValuation | None = None,
    ) -> CombinedLedgerState:
        batch = self._unseen_execution_batch(batch)
        if not batch.executions:
            return self._state
        events = classifier.classify_batch(
            batch,
            self._state,
            source=EventSource.REST_RECOVERY,
        )
        await self.apply_rest_recovery(events, valuation=valuation)
        return await self._settle_closed_recovery_lots()

    async def record_shutdown_snapshot(
        self,
        *,
        cycle_id: str,
        timestamp: datetime,
    ) -> CombinedLedgerState:
        if any(
            isinstance(event, AccountingSnapshotCreated)
            and event.cycle_id == cycle_id
            and event.correlation_id == "shutdown"
            for event in self._events
        ):
            return self._state
        return await self.record_system_event(
            AccountingSnapshotCreated(
                event_id=f"shutdown-snapshot:{cycle_id}",
                event_version=1,
                cycle_id=cycle_id,
                timestamp=timestamp,
                source=EventSource.SYSTEM,
                correlation_id="shutdown",
                sequence=len(self._events),
                ledger_digest=self._state.ledger_digest,
            )
        )

    async def record_system_event(
        self,
        event: AccountingEvent,
        *,
        valuation: AccountingValuation | None = None,
    ) -> CombinedLedgerState:
        return await self._record((event,), valuation)

    async def reconcile(
        self,
        *,
        state_reader: AccountingStateReaderPort,
        clock: Callable[[], datetime],
    ) -> AccountingStartupReconciliationResult:
        service = AccountingReconciliationService(
            store=cast("AccountingReplayPort", self._store),
            reconstructor=self._reconstructor,
            state_reader=state_reader,
            clock=clock,
        )
        result = await service.reconcile()
        self._events = await self._store.load_events_after(0)
        self._state = result.state
        return result

    async def _record(
        self,
        events: tuple[AccountingEvent, ...],
        valuation: AccountingValuation | None,
    ) -> CombinedLedgerState:
        if valuation is not None:
            self._valuation = valuation
        record_events, state = self._with_generated_recovery_debt(events)
        await self._store.append_events_and_snapshot(
            record_events,
            state,
            hedge_mark=self._valuation.hedge_mark,
            hedge_liquidation=self._valuation.hedge_liquidation,
        )
        self._events = await self._store.load_events_after(0)
        self._state = self._reconstruct(self._events)
        return self._state

    async def _settle_closed_recovery_lots(self) -> CombinedLedgerState:
        recorded_ids = {event.event_id for event in self._events}
        settlements: list[RecoveryAllocationRecorded] = []
        for lot in self._state.hedge.lots:
            if (
                lot.role is not HedgeRole.RECOVERY
                or lot.open_quantity > Decimal("0")
                or lot.net_realized_pnl.value <= Decimal("0")
                or self._state.confirmed_recovery_debt.value <= Decimal("0")
            ):
                continue
            available = lot.net_realized_pnl.value
            timestamps = tuple(
                event.timestamp
                for event in self._events
                if isinstance(event, HedgeExecutionRecorded)
                and event.lot_id == lot.lot_id
            )
            if not timestamps:
                raise AssertionError("closed recovery lot has no executions")
            for projection in self._state.recovery_level_projections:
                if (
                    projection.cycle_id != lot.cycle_id
                    or projection.level_id != lot.level_id
                ):
                    continue
                for attempt in projection.attempt_projections:
                    if available <= Decimal("0") or attempt.remaining_debt.value <= Decimal("0"):
                        continue
                    event_id = (
                        "recovery-allocation:"
                        f"{lot.lot_id}:{attempt.key.cycle_id}:"
                        f"{attempt.key.level_id}:{attempt.key.attempt}"
                    )
                    if event_id in recorded_ids:
                        continue
                    allocation = min(available, attempt.remaining_debt.value)
                    settlements.append(
                        RecoveryAllocationRecorded(
                            event_id=event_id,
                            event_version=2,
                            cycle_id=lot.cycle_id,
                            timestamp=max(timestamps),
                            source=EventSource.SYSTEM,
                            correlation_id=lot.lot_id,
                            level_id=lot.level_id,
                            target=attempt.key,
                            recovery_hedge_lot_id=lot.lot_id,
                            gross_realized_recovery_profit=lot.gross_realized_pnl,
                            fees=Money(lot.entry_fees.value + lot.exit_fees.value),
                            funding=lot.allocated_funding,
                            allocated_amount=Money(allocation),
                        )
                    )
                    available -= allocation
                    recorded_ids.add(event_id)
        if settlements:
            return await self._record(tuple(settlements), None)
        return self._state

    def _with_generated_recovery_debt(
        self,
        events: tuple[AccountingEvent, ...],
    ) -> tuple[tuple[AccountingEvent, ...], CombinedLedgerState]:
        candidate = (*self._events, *events)
        state = self._reconstruct(candidate)
        generated = self._recovery_debt_increments(candidate, state)
        if not generated:
            return events, state
        combined = (*events, *generated)
        return combined, self._reconstruct((*self._events, *combined))

    def _recovery_debt_increments(
        self,
        events: tuple[AccountingEvent, ...],
        state: CombinedLedgerState,
    ) -> tuple[RecoveryDebtIncremented, ...]:
        recorded_lots = {
            event.source_hedge_lot_id
            for event in events
            if isinstance(event, RecoveryDebtIncremented)
        }
        increments: list[RecoveryDebtIncremented] = []
        for lot in state.hedge.lots:
            if lot.debt_increment.value <= Decimal("0") or lot.lot_id in recorded_lots:
                continue
            stop_events = tuple(
                event
                for event in events
                if isinstance(event, HedgeExecutionRecorded)
                and event.lot_id == lot.lot_id
                and event.exit_reason is ExitReason.STOP
            )
            stop_execution_ids = tuple(
                event.execution.execution_id
                for event in stop_events
            )
            if not stop_execution_ids:
                raise AssertionError("stopped hedge lot has no stop executions")
            target = HedgeAttemptKey(lot.cycle_id, lot.level_id, lot.attempt)
            increments.append(
                RecoveryDebtIncremented(
                    event_id=(
                        "recovery-debt:"
                        f"{lot.cycle_id}:{lot.level_id}:{lot.attempt}:{lot.lot_id}"
                    ),
                    event_version=2,
                    cycle_id=lot.cycle_id,
                    timestamp=max(event.timestamp for event in stop_events),
                    source=EventSource.SYSTEM,
                    correlation_id=lot.lot_id,
                    level_id=lot.level_id,
                    target=target,
                    source_hedge_lot_id=lot.lot_id,
                    source_stop_execution_ids=stop_execution_ids,
                    amount=lot.debt_increment,
                )
            )
        return tuple(increments)

    def _reconstruct(self, events: tuple[AccountingEvent, ...]) -> CombinedLedgerState:
        return self._reconstructor.reconstruct(
            events,
            hedge_mark=self._valuation.hedge_mark,
            hedge_liquidation=self._valuation.hedge_liquidation,
        )
