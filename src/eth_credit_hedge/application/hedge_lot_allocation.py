"""Durable bridge between confirmed hedge fills and internal lot ownership."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from datetime import datetime
from decimal import Decimal

from eth_credit_hedge.application.demo_runtime_state import LiveHedgeRole
from eth_credit_hedge.application.net_position_allocator import (
    ExitReservationRole,
    HedgeLot,
    LotExecution,
    NetPositionAllocator,
)
from eth_credit_hedge.domain.execution import ExchangePosition, ExecutionUpdate
from eth_credit_hedge.ports.persistence import ExecutionPersistencePort


ZERO = Decimal("0")


class AllocationReconciliationError(RuntimeError):
    """An execution or aggregate position cannot be explained by owned lots."""


class HedgeLotAllocationService:
    """Mutate and persist the sole allocator from confirmed execution facts."""

    def __init__(
        self,
        *,
        cycle_id: str,
        allocator: NetPositionAllocator,
        store: ExecutionPersistencePort,
        clock: Callable[[], datetime],
        average_price_tolerance: Decimal = Decimal("0.01"),
    ) -> None:
        if not cycle_id.strip():
            raise ValueError("cycle ID cannot be empty")
        if average_price_tolerance < ZERO:
            raise ValueError("average-price tolerance cannot be negative")
        self.cycle_id = cycle_id
        self.allocator = allocator
        self._store = store
        self._clock = clock
        self._average_price_tolerance = Decimal(average_price_tolerance)

    @classmethod
    async def restore(
        cls,
        *,
        cycle_id: str,
        store: ExecutionPersistencePort,
        clock: Callable[[], datetime],
        average_price_tolerance: Decimal = Decimal("0.01"),
    ) -> HedgeLotAllocationService:
        persisted = await store.load_hedge_lot_allocation(cycle_id)
        if persisted is None:
            allocator = NetPositionAllocator()
        else:
            payload, digest = persisted
            if hashlib.sha256(payload.encode("utf-8")).hexdigest() != digest:
                raise AllocationReconciliationError("persisted allocation digest mismatch")
            allocator = NetPositionAllocator(_lots_from_payload(payload))
        return cls(
            cycle_id=cycle_id,
            allocator=allocator,
            store=store,
            clock=clock,
            average_price_tolerance=average_price_tolerance,
        )

    async def register(self, lot: HedgeLot) -> None:
        if lot.cycle_id != self.cycle_id:
            raise ValueError("hedge lot cycle does not match allocator cycle")
        self.allocator.add_lot(lot)
        await self._persist()

    async def bind_protection(
        self,
        lot_id: str,
        *,
        take_profit_order_link_id: str,
        stop_order_link_id: str,
    ) -> None:
        self.allocator.bind_protection(
            lot_id,
            take_profit_order_link_id=take_profit_order_link_id,
            stop_order_link_id=stop_order_link_id,
        )
        await self._persist()

    async def apply_confirmed_executions(
        self,
        executions: Iterable[ExecutionUpdate],
    ) -> None:
        changed = False
        for execution in sorted(
            executions,
            key=lambda item: (item.executed_at, item.execution_id),
        ):
            if execution.symbol != "ETHUSDT":
                continue
            lot_execution = LotExecution(
                execution.execution_id,
                execution.quantity,
                execution.price,
            )
            entry = next(
                (
                    lot
                    for lot in self.allocator.lots
                    if lot.entry_order_link_id == execution.order_link_id
                ),
                None,
            )
            if entry is not None:
                if execution.side != "Sell":
                    raise AllocationReconciliationError("hedge entry is not a Sell")
                changed = self.allocator.record_entry_execution(entry.lot_id, lot_execution) or changed
                continue
            owned = tuple(
                lot
                for lot in self.allocator.lots
                if execution.order_link_id in (lot.take_profit_order_link_id, lot.stop_order_link_id)
            )
            if len(owned) == 1:
                before = self.allocator.total_open_quantity
                self.allocator.allocate_owned_exit(execution.order_link_id, lot_execution)
                changed = changed or before != self.allocator.total_open_quantity
                continue
            if len(owned) > 1:
                raise AllocationReconciliationError("exit order-link ownership is ambiguous")
            if execution.side != "Buy":
                raise AllocationReconciliationError("unowned hedge execution is not a close")
            try:
                allocations = self.allocator.allocate_aggregate_exit(
                    execution.execution_id,
                    execution.quantity,
                    execution.price,
                )
            except ValueError as error:
                raise AllocationReconciliationError(str(error)) from error
            changed = changed or bool(allocations)
        changed = self._reserve_bound_protection() or changed
        if changed:
            await self._persist()

    @property
    def digest(self) -> str:
        return hashlib.sha256(
            _lots_payload(self.allocator.lots).encode("utf-8")
        ).hexdigest()

    async def reconcile_exchange_position(
        self,
        positions: tuple[ExchangePosition, ...],
    ) -> None:
        shorts = tuple(
            position
            for position in positions
            if position.category == "linear"
            and position.symbol == "ETHUSDT"
            and position.side == "Sell"
            and position.quantity > ZERO
        )
        if len(shorts) > 1:
            raise AllocationReconciliationError("aggregate ETHUSDT short is ambiguous")
        exchange_quantity = ZERO if not shorts else shorts[0].quantity
        if not self.allocator.reconcile_exchange_short(exchange_quantity):
            raise AllocationReconciliationError("internal hedge quantity does not match exchange short")
        if exchange_quantity == ZERO:
            return
        internal_basis = sum(
            (lot.open_quantity * (lot.average_entry_price or ZERO) for lot in self.allocator.lots),
            ZERO,
        ) / exchange_quantity
        exchange_basis = shorts[0].average_price
        if exchange_basis is None or abs(internal_basis - exchange_basis) > self._average_price_tolerance:
            raise AllocationReconciliationError("internal hedge basis exceeds exchange tolerance")

    async def _persist(self) -> None:
        payload = _lots_payload(self.allocator.lots)
        await self._store.persist_hedge_lot_allocation(
            self.cycle_id,
            payload,
            self.digest,
            self._clock(),
        )

    def _reserve_bound_protection(self) -> bool:
        changed = False
        for lot in self.allocator.lots:
            if lot.open_quantity == ZERO:
                continue
            if (
                lot.take_profit_order_link_id is not None
                and lot.reserved_take_profit_quantity == ZERO
            ):
                self.allocator.reserve_exit(
                    lot.lot_id,
                    role=ExitReservationRole.TAKE_PROFIT,
                    order_link_id=lot.take_profit_order_link_id,
                    quantity=lot.open_quantity,
                )
                changed = True
            refreshed = next(
                value for value in self.allocator.lots if value.lot_id == lot.lot_id
            )
            if (
                refreshed.stop_order_link_id is not None
                and refreshed.reserved_stop_quantity == ZERO
            ):
                self.allocator.reserve_exit(
                    refreshed.lot_id,
                    role=ExitReservationRole.STOP,
                    order_link_id=refreshed.stop_order_link_id,
                    quantity=refreshed.open_quantity,
                )
                changed = True
        return changed


def _lots_payload(lots: tuple[HedgeLot, ...]) -> str:
    return json.dumps(
        [_lot_payload(lot) for lot in lots],
        separators=(",", ":"),
        sort_keys=True,
    )


def _lot_payload(lot: HedgeLot) -> dict[str, object]:
    return {
        "lot_id": lot.lot_id,
        "cycle_id": lot.cycle_id,
        "level_id": lot.level_id,
        "attempt": lot.attempt,
        "entry_order_link_id": lot.entry_order_link_id,
        "role": lot.role.value,
        "side": lot.side,
        "accounting_lot_id": lot.accounting_lot_id,
        "entry_executions": [
            _execution_payload(value) for value in lot.entry_executions
        ],
        "exit_executions": [
            _execution_payload(value) for value in lot.exit_executions
        ],
        "take_profit_order_link_id": lot.take_profit_order_link_id,
        "stop_order_link_id": lot.stop_order_link_id,
        "reserved_take_profit_quantity": str(lot.reserved_take_profit_quantity),
        "reserved_stop_quantity": str(lot.reserved_stop_quantity),
    }


def _execution_payload(execution: LotExecution) -> dict[str, str]:
    return {
        "execution_id": execution.execution_id,
        "quantity": str(execution.quantity),
        "price": str(execution.price),
    }


def _lots_from_payload(payload: str) -> tuple[HedgeLot, ...]:
    values = json.loads(payload)
    if not isinstance(values, list):
        raise AllocationReconciliationError("persisted allocation payload is not a list")
    return tuple(
        HedgeLot(
            lot_id=str(value["lot_id"]),
            cycle_id=str(value["cycle_id"]),
            level_id=int(value["level_id"]),
            attempt=int(value["attempt"]),
            entry_order_link_id=str(value["entry_order_link_id"]),
            role=LiveHedgeRole(str(value["role"])),
            side=str(value["side"]),
            accounting_lot_id=str(value["accounting_lot_id"]),
            entry_executions=_executions(value["entry_executions"]),
            exit_executions=_executions(value["exit_executions"]),
            take_profit_order_link_id=_optional_text(value["take_profit_order_link_id"]),
            stop_order_link_id=_optional_text(value["stop_order_link_id"]),
            reserved_take_profit_quantity=Decimal(str(value["reserved_take_profit_quantity"])),
            reserved_stop_quantity=Decimal(str(value["reserved_stop_quantity"])),
        )
        for value in values
    )


def _executions(value: object) -> tuple[LotExecution, ...]:
    if not isinstance(value, list):
        raise AllocationReconciliationError("persisted lot executions are not a list")
    return tuple(
        LotExecution(
            str(item["execution_id"]),
            Decimal(str(item["quantity"])),
            Decimal(str(item["price"])),
        )
        for item in value
        if _execution_mapping(item)
    )


def _execution_mapping(value: object) -> bool:
    if isinstance(value, dict):
        return True
    raise AllocationReconciliationError("persisted lot execution is not an object")


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)
