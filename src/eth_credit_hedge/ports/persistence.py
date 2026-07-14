"""Persistence boundary for durable live-entry execution state."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from eth_credit_hedge.domain.execution import (
    ExecutionUpdate,
    OrderRequestAck,
    PlaceOrderRequest,
)
from eth_credit_hedge.domain.live_execution import EntryExecutionSnapshot


class ExecutionPersistencePort(Protocol):
    async def persist_order_intent(
        self,
        request: PlaceOrderRequest,
        persisted_at: datetime,
    ) -> None: ...

    async def persist_entry_intent(
        self,
        request: PlaceOrderRequest,
        snapshot: EntryExecutionSnapshot,
        persisted_at: datetime,
    ) -> None: ...

    async def load_order_intent(
        self,
        order_link_id: str,
    ) -> PlaceOrderRequest | None: ...

    async def load_entry_snapshot(
        self,
        order_link_id: str,
    ) -> EntryExecutionSnapshot | None: ...

    async def transition_entry_snapshot(
        self,
        previous_version: int,
        snapshot: EntryExecutionSnapshot,
    ) -> None: ...

    async def record_acknowledgement_and_snapshot(
        self,
        previous_version: int,
        acknowledgement: OrderRequestAck,
        snapshot: EntryExecutionSnapshot,
    ) -> None: ...

    async def record_acknowledgement(
        self,
        acknowledgement: OrderRequestAck,
    ) -> None: ...

    async def record_execution_and_snapshot(
        self,
        previous_version: int,
        execution: ExecutionUpdate,
        received_at: datetime,
        payload_hash: str,
        snapshot: EntryExecutionSnapshot,
    ) -> bool: ...

    async def has_execution(self, execution_id: str) -> bool: ...
