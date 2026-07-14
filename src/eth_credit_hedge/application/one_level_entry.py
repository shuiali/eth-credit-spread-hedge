"""Persistence-first execution service for one short ETH perpetual entry."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from eth_credit_hedge.domain.execution import (
    ExchangePosition,
    ExecutionUpdate,
    LiveExecutionState,
    OrderRequestAck,
    OrderRequestKind,
    PlaceOrderRequest,
    UncertainOrderOutcomeError,
)
from eth_credit_hedge.domain.live_execution import (
    EntryExecutionSnapshot,
    apply_entry_execution,
    entry_position_matches,
    transition_entry_snapshot,
)
from eth_credit_hedge.ports.persistence import ExecutionPersistencePort
from eth_credit_hedge.ports.trading import TradingPort


class OneLevelEntryService:
    """Submit and account for one entry without inferring fills from order state."""

    def __init__(
        self,
        *,
        trading: TradingPort,
        store: ExecutionPersistencePort,
        clock: Callable[[], datetime],
    ) -> None:
        self._trading = trading
        self._store = store
        self._clock = clock
        self._active_order_link_id: str | None = None

    async def submit_entry(
        self,
        request: PlaceOrderRequest,
    ) -> EntryExecutionSnapshot:
        persisted_at = self._clock()
        snapshot = EntryExecutionSnapshot.for_intent(request, persisted_at)
        await self._store.persist_entry_intent(request, snapshot, persisted_at)
        self._active_order_link_id = request.order_link_id

        submitted = transition_entry_snapshot(
            snapshot,
            LiveExecutionState.ENTRY_SUBMITTED,
            updated_at=self._clock(),
        )
        await self._store.transition_entry_snapshot(snapshot.version, submitted)

        try:
            acknowledgement = await self._trading.place_order(request)
        except UncertainOrderOutcomeError:
            discovered = await self._trading.get_order_by_link_id(
                request.category,
                request.symbol,
                request.order_link_id,
            )
            if discovered is None:
                reconciling = transition_entry_snapshot(
                    submitted,
                    LiveExecutionState.RECONCILING,
                    updated_at=self._clock(),
                )
                await self._store.transition_entry_snapshot(
                    submitted.version,
                    reconciling,
                )
                raise
            acknowledgement = OrderRequestAck(
                request_kind=OrderRequestKind.PLACE,
                order_id=discovered.order_id,
                order_link_id=discovered.order_link_id,
                acknowledged_at=self._clock(),
            )

        acknowledged = transition_entry_snapshot(
            submitted,
            LiveExecutionState.ENTRY_ACKNOWLEDGED,
            entry_order_id=acknowledgement.order_id,
            updated_at=self._clock(),
        )
        await self._store.record_acknowledgement_and_snapshot(
            submitted.version,
            acknowledgement,
            acknowledged,
        )
        return acknowledged

    async def apply_execution(
        self,
        execution: ExecutionUpdate,
        *,
        received_at: datetime,
        payload_hash: str,
    ) -> EntryExecutionSnapshot:
        snapshot = await self._required_snapshot(execution.order_link_id)
        if await self._store.has_execution(execution.execution_id):
            return snapshot
        updated = apply_entry_execution(
            snapshot,
            execution,
            updated_at=received_at,
        )
        inserted = await self._store.record_execution_and_snapshot(
            snapshot.version,
            execution,
            received_at,
            payload_hash,
            updated,
        )
        if inserted:
            return updated
        return await self._required_snapshot(execution.order_link_id)

    async def reconcile_position(
        self,
        positions: tuple[ExchangePosition, ...],
    ) -> bool:
        if self._active_order_link_id is None:
            raise RuntimeError("no entry has been submitted by this service")
        snapshot = await self._required_snapshot(self._active_order_link_id)
        if entry_position_matches(snapshot, positions):
            return True
        if snapshot.state is not LiveExecutionState.RECONCILING:
            reconciling = transition_entry_snapshot(
                snapshot,
                LiveExecutionState.RECONCILING,
                updated_at=self._clock(),
            )
            await self._store.transition_entry_snapshot(
                snapshot.version,
                reconciling,
            )
        return False

    async def _required_snapshot(
        self,
        order_link_id: str,
    ) -> EntryExecutionSnapshot:
        snapshot = await self._store.load_entry_snapshot(order_link_id)
        if snapshot is None:
            raise ValueError(f"no persisted entry for {order_link_id}")
        return snapshot
