"""Persistence boundary for order intents and execution deduplication."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from eth_credit_hedge.domain.execution import ExecutionUpdate, PlaceOrderRequest


class ExecutionPersistencePort(Protocol):
    async def persist_order_intent(
        self,
        request: PlaceOrderRequest,
        persisted_at: datetime,
    ) -> None: ...

    async def load_order_intent(
        self,
        order_link_id: str,
    ) -> PlaceOrderRequest | None: ...

    async def record_execution_if_new(
        self,
        execution: ExecutionUpdate,
        received_at: datetime,
        payload_hash: str,
    ) -> bool: ...

    async def has_execution(self, execution_id: str) -> bool: ...
