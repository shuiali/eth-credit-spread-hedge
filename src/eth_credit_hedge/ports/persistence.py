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
from eth_credit_hedge.domain.protected_execution import ProtectionSnapshot
from eth_credit_hedge.domain.live_recovery import RecoveryDebtSnapshot


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

    async def load_all_order_intents(self) -> tuple[PlaceOrderRequest, ...]: ...

    async def load_entry_snapshot(
        self,
        order_link_id: str,
    ) -> EntryExecutionSnapshot | None: ...

    async def load_all_entry_snapshots(
        self,
    ) -> tuple[EntryExecutionSnapshot, ...]: ...

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

    async def persist_protection_intent(
        self,
        request: PlaceOrderRequest,
        snapshot: ProtectionSnapshot,
        persisted_at: datetime,
    ) -> None: ...

    async def persist_take_profit_intent(
        self,
        previous_version: int,
        request: PlaceOrderRequest,
        snapshot: ProtectionSnapshot,
        persisted_at: datetime,
    ) -> None: ...

    async def persist_replacement_stop_intent(
        self,
        previous_version: int,
        request: PlaceOrderRequest,
        snapshot: ProtectionSnapshot,
        persisted_at: datetime,
    ) -> None: ...

    async def load_protection_snapshot(
        self,
        entry_order_link_id: str,
    ) -> ProtectionSnapshot | None: ...

    async def load_protection_snapshot_by_exit_id(
        self,
        order_link_id: str,
    ) -> ProtectionSnapshot | None: ...

    async def load_all_protection_snapshots(
        self,
    ) -> tuple[ProtectionSnapshot, ...]: ...

    async def transition_protection_snapshot(
        self,
        previous_version: int,
        snapshot: ProtectionSnapshot,
    ) -> None: ...

    async def record_exit_execution_and_snapshot(
        self,
        previous_version: int,
        execution: ExecutionUpdate,
        received_at: datetime,
        payload_hash: str,
        snapshot: ProtectionSnapshot,
    ) -> bool: ...

    async def has_execution(self, execution_id: str) -> bool: ...

    async def load_all_executions(self) -> tuple[ExecutionUpdate, ...]: ...

    async def persist_recovery_debt_snapshot(
        self,
        snapshot: RecoveryDebtSnapshot,
    ) -> None: ...

    async def load_recovery_debt_snapshot(
        self,
        level_id: int,
    ) -> RecoveryDebtSnapshot | None: ...

    async def transition_recovery_debt_snapshot(
        self,
        previous_version: int,
        snapshot: RecoveryDebtSnapshot,
    ) -> None: ...
