from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from eth_credit_hedge.application.demo_runtime_state import LiveHedgeRole
from eth_credit_hedge.application.hedge_lot_allocation import (
    AllocationReconciliationError,
    HedgeLotAllocationService,
)
from eth_credit_hedge.application.net_position_allocator import HedgeLot
from eth_credit_hedge.domain.execution import ExchangePosition, ExecutionUpdate


NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)


class _AllocationStore:
    def __init__(self) -> None:
        self.values: dict[str, tuple[str, str]] = {}

    async def persist_hedge_lot_allocation(
        self,
        cycle_id: str,
        payload_json: str,
        digest: str,
        persisted_at: datetime,
    ) -> None:
        del persisted_at
        self.values[cycle_id] = (payload_json, digest)

    async def load_hedge_lot_allocation(
        self,
        cycle_id: str,
    ) -> tuple[str, str] | None:
        return self.values.get(cycle_id)


def _lot(level_id: int, attempt: int = 1) -> HedgeLot:
    return HedgeLot(
        lot_id=f"cycle-1:L{level_id:02d}:A{attempt:02d}",
        cycle_id="cycle-1",
        level_id=level_id,
        attempt=attempt,
        entry_order_link_id=f"entry-{level_id}-{attempt}",
        role=(
            LiveHedgeRole.BASELINE if attempt == 1 else LiveHedgeRole.RECOVERY
        ),
        accounting_lot_id=f"cycle-1:L{level_id:02d}:A{attempt:02d}",
    )


def _execution(
    execution_id: str,
    order_link_id: str,
    side: str,
    quantity: str,
    price: str,
    *,
    sequence: int,
) -> ExecutionUpdate:
    return ExecutionUpdate(
        execution_id=execution_id,
        order_id=f"order-{execution_id}",
        order_link_id=order_link_id,
        symbol="ETHUSDT",
        side=side,  # type: ignore[arg-type]
        price=Decimal(price),
        quantity=Decimal(quantity),
        fee=Decimal("0"),
        is_maker=False,
        executed_at=NOW + timedelta(seconds=sequence),
    )


def _short(quantity: str, average_price: str) -> ExchangePosition:
    return ExchangePosition(
        category="linear",
        symbol="ETHUSDT",
        side="Sell",
        quantity=Decimal(quantity),
        average_price=Decimal(average_price),
        mark_price=Decimal(average_price),
        unrealized_pnl=Decimal("0"),
        updated_at=NOW,
    )


def test_reordered_partial_entry_and_take_profit_are_durable_and_replayable() -> None:
    async def scenario() -> None:
        store = _AllocationStore()
        service = await HedgeLotAllocationService.restore(
            cycle_id="cycle-1",
            store=store,  # type: ignore[arg-type]
            clock=lambda: NOW,
        )
        lot = _lot(1)
        await service.register(lot)
        await service.bind_protection(
            lot.lot_id,
            take_profit_order_link_id="tp-1",
            stop_order_link_id="stop-1",
        )
        entry = _execution("entry-fill", "entry-1-1", "Sell", "0.01", "3000", sequence=1)
        take_profit = _execution("tp-fill", "tp-1", "Buy", "0.004", "2990", sequence=2)
        await service.apply_confirmed_executions((take_profit, entry, entry))

        allocated = service.allocator.lots[0]
        assert allocated.entered_quantity == Decimal("0.01")
        assert allocated.exited_quantity == Decimal("0.004")
        assert allocated.open_quantity == Decimal("0.006")
        assert allocated.reserved_take_profit_quantity == Decimal("0.006")
        assert allocated.reserved_stop_quantity == Decimal("0.006")

        restored = await HedgeLotAllocationService.restore(
            cycle_id="cycle-1",
            store=store,  # type: ignore[arg-type]
            clock=lambda: NOW,
        )
        assert restored.digest == service.digest
        assert restored.allocator.lots == service.allocator.lots

    asyncio.run(scenario())


def test_aggregate_manual_close_spans_lots_deterministically() -> None:
    async def scenario() -> None:
        store = _AllocationStore()
        service = await HedgeLotAllocationService.restore(
            cycle_id="cycle-1", store=store, clock=lambda: NOW  # type: ignore[arg-type]
        )
        first, second = _lot(1), _lot(2)
        await service.register(first)
        await service.register(second)
        await service.apply_confirmed_executions(
            (
                _execution("entry-2", "entry-2-1", "Sell", "0.01", "3002", sequence=2),
                _execution("entry-1", "entry-1-1", "Sell", "0.01", "3000", sequence=1),
                _execution("manual", "manual-close", "Buy", "0.015", "2995", sequence=3),
            )
        )
        lots = service.allocator.lots
        assert lots[0].open_quantity == Decimal("0")
        assert lots[1].open_quantity == Decimal("0.005")
        await service.reconcile_exchange_position((_short("0.005", "3002"),))

    asyncio.run(scenario())


def test_quantity_basis_and_ambiguous_protection_faults_are_explicit() -> None:
    async def scenario() -> None:
        store = _AllocationStore()
        service = await HedgeLotAllocationService.restore(
            cycle_id="cycle-1", store=store, clock=lambda: NOW  # type: ignore[arg-type]
        )
        lot = _lot(1)
        await service.register(lot)
        await service.apply_confirmed_executions(
            (_execution("entry", "entry-1-1", "Sell", "0.01", "3000", sequence=1),)
        )
        await service.reconcile_exchange_position((_short("0.01", "3000.005"),))
        with pytest.raises(AllocationReconciliationError, match="quantity"):
            await service.reconcile_exchange_position((_short("0.02", "3000"),))
        with pytest.raises(AllocationReconciliationError, match="basis"):
            await service.reconcile_exchange_position((_short("0.01", "3001"),))

        other = _lot(2)
        await service.register(other)
        await service.bind_protection(
            lot.lot_id,
            take_profit_order_link_id="tp-1",
            stop_order_link_id="stop-1",
        )
        with pytest.raises(ValueError, match="already belongs"):
            await service.bind_protection(
                other.lot_id,
                take_profit_order_link_id="tp-1",
                stop_order_link_id="stop-2",
            )

    asyncio.run(scenario())
