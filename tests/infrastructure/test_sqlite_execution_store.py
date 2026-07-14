"""Transactional guarantees for the live execution SQLite store."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from eth_credit_hedge.domain.execution import (
    ExecutionUpdate,
    LiveExecutionState,
    PlaceOrderRequest,
)
from eth_credit_hedge.domain.live_execution import (
    EntryExecutionSnapshot,
    apply_entry_execution,
    transition_entry_snapshot,
)
from eth_credit_hedge.infrastructure.persistence.sqlite_execution_store import (
    ConcurrentSnapshotUpdateError,
    SqliteExecutionStore,
)


NOW = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
ORDER_LINK_ID = "ECH-01-C0001-L01-ENTRY-A01-9F3C"


def request() -> PlaceOrderRequest:
    return PlaceOrderRequest(
        category="linear",
        symbol="ETHUSDT",
        side="Sell",
        order_type="Limit",
        quantity=Decimal("0.010"),
        order_link_id=ORDER_LINK_ID,
        price=Decimal("3000"),
        time_in_force="IOC",
    )


def execution() -> ExecutionUpdate:
    return ExecutionUpdate(
        execution_id="execution-stale",
        order_id="exchange-order-1",
        order_link_id=ORDER_LINK_ID,
        symbol="ETHUSDT",
        side="Sell",
        price=Decimal("3000"),
        quantity=Decimal("0.004"),
        fee=Decimal("0.001"),
        is_maker=False,
        executed_at=NOW + timedelta(seconds=1),
    )


def test_stale_snapshot_rolls_back_execution_insert(tmp_path: Path) -> None:
    async def exercise() -> None:
        store = SqliteExecutionStore(tmp_path / "execution.sqlite3")
        await store.initialize()
        intent = request()
        initial = EntryExecutionSnapshot.for_intent(intent, NOW)
        await store.persist_entry_intent(intent, initial, NOW)
        stale_basis = transition_entry_snapshot(
            initial,
            LiveExecutionState.ENTRY_SUBMITTED,
            updated_at=NOW + timedelta(seconds=1),
        )
        updated = apply_entry_execution(
            stale_basis,
            execution(),
            updated_at=NOW + timedelta(seconds=2),
        )

        with pytest.raises(ConcurrentSnapshotUpdateError):
            await store.record_execution_and_snapshot(
                previous_version=stale_basis.version,
                execution=execution(),
                received_at=NOW + timedelta(seconds=2),
                payload_hash="d" * 64,
                snapshot=updated,
            )

        assert not await store.has_execution("execution-stale")
        assert await store.execution_count() == 0
        assert await store.load_entry_snapshot(ORDER_LINK_ID) == initial

    asyncio.run(exercise())
