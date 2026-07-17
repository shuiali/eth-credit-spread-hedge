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
    FundingRecorded,
    HedgeExecutionRecorded,
    HedgeRole,
    OptionExecutionRecorded,
    PositionReconciled,
    RecoveryDebtChanged,
)
from eth_credit_hedge.domain.accounting.errors import (
    DuplicateAccountingIdentifierError,
)
from eth_credit_hedge.domain.accounting.reconstruction import (
    CombinedLedgerReconstructor,
    CombinedLedgerState,
)
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
        for event in events:
            candidate = (*self._events, event)
            state = self._reconstruct(candidate)
            await self._store.append_event_and_snapshot(
                event,
                state,
                hedge_mark=self._valuation.hedge_mark,
                hedge_liquidation=self._valuation.hedge_liquidation,
            )
            self._events = await self._store.load_events_after(0)
        self._state = self._reconstruct(self._events)
        return self._state

    async def _settle_closed_recovery_lots(self) -> CombinedLedgerState:
        recorded_ids = {event.event_id for event in self._events}
        for lot in self._state.hedge.lots:
            event_id = f"recovery-settlement:{lot.lot_id}"
            if (
                lot.role is not HedgeRole.RECOVERY
                or lot.open_quantity > Decimal("0")
                or lot.net_realized_pnl.value <= Decimal("0")
                or event_id in recorded_ids
                or self._state.confirmed_recovery_debt.value <= Decimal("0")
            ):
                continue
            allocation = min(
                lot.net_realized_pnl.value,
                self._state.confirmed_recovery_debt.value,
            )
            timestamps = tuple(
                event.timestamp
                for event in self._events
                if isinstance(event, HedgeExecutionRecorded)
                and event.lot_id == lot.lot_id
            )
            if not timestamps:
                raise AssertionError("closed recovery lot has no executions")
            await self.record_system_event(
                RecoveryDebtChanged(
                    event_id=event_id,
                    event_version=1,
                    cycle_id=lot.cycle_id,
                    timestamp=max(timestamps),
                    source=EventSource.SYSTEM,
                    correlation_id=lot.lot_id,
                    level_id=lot.level_id,
                    increment=Money(Decimal("0")),
                    actual_recovery_allocation=Money(allocation),
                    reason="actual net realized recovery profit",
                )
            )
            recorded_ids.add(event_id)
        return self._state

    def _reconstruct(self, events: tuple[AccountingEvent, ...]) -> CombinedLedgerState:
        return self._reconstructor.reconstruct(
            events,
            hedge_mark=self._valuation.hedge_mark,
            hedge_liquidation=self._valuation.hedge_liquidation,
        )
