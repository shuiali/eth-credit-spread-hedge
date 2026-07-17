"""Durable internal hedge lots for Bybit's aggregate one-way ETHUSDT short."""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from enum import Enum

from eth_credit_hedge.application.demo_runtime_state import LiveHedgeRole


ZERO = Decimal("0")


class ExitReservationRole(str, Enum):
    TAKE_PROFIT = "TAKE_PROFIT"
    STOP = "STOP"


@dataclass(frozen=True, slots=True)
class LotExecution:
    execution_id: str
    quantity: Decimal
    price: Decimal

    def __post_init__(self) -> None:
        if not self.execution_id.strip():
            raise ValueError("execution ID cannot be empty")
        for name in ("quantity", "price"):
            value = Decimal(getattr(self, name))
            if not value.is_finite() or value <= ZERO:
                raise ValueError(f"{name} must be positive")
            object.__setattr__(self, name, value)


@dataclass(frozen=True, slots=True)
class HedgeLot:
    lot_id: str
    cycle_id: str
    level_id: int
    attempt: int
    entry_order_link_id: str
    role: LiveHedgeRole
    side: str = "Sell"
    accounting_lot_id: str | None = None
    entry_executions: tuple[LotExecution, ...] = ()
    exit_executions: tuple[LotExecution, ...] = ()
    take_profit_order_link_id: str | None = None
    stop_order_link_id: str | None = None
    reserved_take_profit_quantity: Decimal = ZERO
    reserved_stop_quantity: Decimal = ZERO

    def __post_init__(self) -> None:
        for value, name in (
            (self.lot_id, "lot ID"),
            (self.cycle_id, "cycle ID"),
            (self.entry_order_link_id, "entry order-link ID"),
        ):
            if not value.strip():
                raise ValueError(f"{name} cannot be empty")
        if self.level_id <= 0 or self.attempt <= 0:
            raise ValueError("level ID and attempt must be positive")
        object.__setattr__(self, "role", LiveHedgeRole(self.role))
        if self.side != "Sell":
            raise ValueError("hedge lot side must be Sell in one-way short mode")
        accounting_lot_id = (
            self.lot_id if self.accounting_lot_id is None else self.accounting_lot_id
        )
        if not accounting_lot_id.strip():
            raise ValueError("accounting lot ID cannot be empty")
        object.__setattr__(self, "accounting_lot_id", accounting_lot_id)
        entries = tuple(self.entry_executions)
        exits = tuple(self.exit_executions)
        if len({value.execution_id for value in entries}) != len(entries):
            raise ValueError("entry execution IDs must be unique")
        if len({value.execution_id for value in exits}) != len(exits):
            raise ValueError("exit execution IDs must be unique")
        object.__setattr__(self, "entry_executions", entries)
        object.__setattr__(self, "exit_executions", exits)
        for name in (
            "reserved_take_profit_quantity",
            "reserved_stop_quantity",
        ):
            normalized_reservation = Decimal(getattr(self, name))
            if not normalized_reservation.is_finite() or normalized_reservation < ZERO:
                raise ValueError(f"{name.replace('_', ' ')} cannot be negative")
            object.__setattr__(self, name, normalized_reservation)
        if self.open_quantity < ZERO:
            raise ValueError("exit executions exceed entry executions")
        if self.reserved_take_profit_quantity > self.open_quantity:
            raise ValueError("take-profit reservation exceeds open quantity")
        if self.reserved_stop_quantity > self.open_quantity:
            raise ValueError("stop reservation exceeds open quantity")

    @property
    def entered_quantity(self) -> Decimal:
        return sum((value.quantity for value in self.entry_executions), ZERO)

    @property
    def exited_quantity(self) -> Decimal:
        return sum((value.quantity for value in self.exit_executions), ZERO)

    @property
    def open_quantity(self) -> Decimal:
        return self.entered_quantity - self.exited_quantity

    @property
    def average_entry_price(self) -> Decimal | None:
        quantity = self.entered_quantity
        if quantity == ZERO:
            return None
        return sum(
            (value.quantity * value.price for value in self.entry_executions),
            ZERO,
        ) / quantity

    @property
    def reserved_close_capacity(self) -> Decimal:
        return max(
            self.reserved_take_profit_quantity,
            self.reserved_stop_quantity,
        )


class NetPositionAllocator:
    def __init__(self, lots: tuple[HedgeLot, ...] = ()) -> None:
        normalized = tuple(lots)
        ids = [lot.lot_id for lot in normalized]
        if len(ids) != len(set(ids)):
            raise ValueError("lot IDs must be unique")
        entry_ids = [lot.entry_order_link_id for lot in normalized]
        if len(entry_ids) != len(set(entry_ids)):
            raise ValueError("entry order-link IDs must be unique")
        self._lots = {lot.lot_id: lot for lot in normalized}
        self._execution_ids = {
            execution.execution_id
            for lot in normalized
            for execution in (*lot.entry_executions, *lot.exit_executions)
        }

    @property
    def lots(self) -> tuple[HedgeLot, ...]:
        return tuple(sorted(self._lots.values(), key=_lot_priority))

    @property
    def total_open_quantity(self) -> Decimal:
        return sum((lot.open_quantity for lot in self._lots.values()), ZERO)

    @property
    def total_reserved_close_capacity(self) -> Decimal:
        return sum(
            (lot.reserved_close_capacity for lot in self._lots.values()),
            ZERO,
        )

    def add_lot(self, lot: HedgeLot) -> None:
        if lot.lot_id in self._lots:
            raise ValueError(f"lot already exists: {lot.lot_id}")
        if any(
            current.entry_order_link_id == lot.entry_order_link_id
            for current in self._lots.values()
        ):
            raise ValueError("entry order-link ID already belongs to a lot")
        self._lots[lot.lot_id] = lot

    def bind_protection(
        self,
        lot_id: str,
        *,
        take_profit_order_link_id: str,
        stop_order_link_id: str,
    ) -> None:
        lot = self._required_lot(lot_id)
        if any(
            other.lot_id != lot_id
            and take_profit_order_link_id
            in (other.take_profit_order_link_id, other.stop_order_link_id)
            for other in self._lots.values()
        ):
            raise ValueError("take-profit order-link ID already belongs to another lot")
        if any(
            other.lot_id != lot_id
            and stop_order_link_id
            in (other.take_profit_order_link_id, other.stop_order_link_id)
            for other in self._lots.values()
        ):
            raise ValueError("stop order-link ID already belongs to another lot")
        if lot.take_profit_order_link_id not in (None, take_profit_order_link_id):
            raise ValueError("take-profit order-link ID already belongs to the lot")
        if lot.stop_order_link_id not in (None, stop_order_link_id):
            raise ValueError("stop order-link ID already belongs to the lot")
        self._lots[lot_id] = replace(
            lot,
            take_profit_order_link_id=take_profit_order_link_id,
            stop_order_link_id=stop_order_link_id,
        )

    def record_entry_execution(
        self,
        lot_id: str,
        execution: LotExecution,
    ) -> bool:
        if execution.execution_id in self._execution_ids:
            return False
        lot = self._required_lot(lot_id)
        self._lots[lot_id] = replace(
            lot,
            entry_executions=lot.entry_executions + (execution,),
        )
        self._execution_ids.add(execution.execution_id)
        return True

    def reserve_exit(
        self,
        lot_id: str,
        *,
        role: ExitReservationRole,
        order_link_id: str,
        quantity: Decimal,
    ) -> None:
        lot = self._required_lot(lot_id)
        normalized = Decimal(quantity)
        if not normalized.is_finite() or normalized <= ZERO:
            raise ValueError("exit reservation quantity must be positive")
        if normalized > lot.open_quantity:
            raise ValueError("exit reservation exceeds lot open quantity")
        if role is ExitReservationRole.TAKE_PROFIT:
            updated = replace(
                lot,
                take_profit_order_link_id=order_link_id,
                reserved_take_profit_quantity=normalized,
            )
        else:
            updated = replace(
                lot,
                stop_order_link_id=order_link_id,
                reserved_stop_quantity=normalized,
            )
        self._lots[lot_id] = updated
        if self.total_reserved_close_capacity > self.total_open_quantity:
            self._lots[lot_id] = lot
            raise ValueError("aggregate exit reservations exceed net short quantity")

    def allocate_owned_exit(
        self,
        order_link_id: str,
        execution: LotExecution,
    ) -> str | None:
        if execution.execution_id in self._execution_ids:
            return None
        matches = tuple(
            lot
            for lot in self._lots.values()
            if order_link_id
            in (lot.take_profit_order_link_id, lot.stop_order_link_id)
        )
        if len(matches) != 1:
            raise ValueError("exit order-link ID must belong to exactly one lot")
        lot = matches[0]
        if execution.quantity > lot.open_quantity:
            raise ValueError("owned exit execution exceeds lot open quantity")
        self._lots[lot.lot_id] = replace(
            lot,
            exit_executions=lot.exit_executions + (execution,),
            reserved_take_profit_quantity=min(
                lot.reserved_take_profit_quantity,
                lot.open_quantity - execution.quantity,
            ),
            reserved_stop_quantity=min(
                lot.reserved_stop_quantity,
                lot.open_quantity - execution.quantity,
            ),
        )
        self._execution_ids.add(execution.execution_id)
        return lot.lot_id

    def allocate_aggregate_exit(
        self,
        execution_id: str,
        quantity: Decimal,
        price: Decimal,
    ) -> tuple[tuple[str, Decimal], ...]:
        if execution_id in self._execution_ids:
            return ()
        remaining = Decimal(quantity)
        if not remaining.is_finite() or remaining <= ZERO:
            raise ValueError("aggregate exit quantity must be positive")
        if remaining > self.total_open_quantity:
            raise ValueError("aggregate exit exceeds net short quantity")
        allocations: list[tuple[str, Decimal]] = []
        for lot in self.lots:
            allocated = min(lot.open_quantity, remaining)
            if allocated == ZERO:
                continue
            synthetic_id = f"{execution_id}:{lot.lot_id}"
            updated = self._required_lot(lot.lot_id)
            self._lots[lot.lot_id] = replace(
                updated,
                exit_executions=updated.exit_executions
                + (LotExecution(synthetic_id, allocated, price),),
                reserved_take_profit_quantity=min(
                    updated.reserved_take_profit_quantity,
                    updated.open_quantity - allocated,
                ),
                reserved_stop_quantity=min(
                    updated.reserved_stop_quantity,
                    updated.open_quantity - allocated,
                ),
            )
            allocations.append((lot.lot_id, allocated))
            remaining -= allocated
            if remaining == ZERO:
                break
        self._execution_ids.add(execution_id)
        return tuple(allocations)

    def reconcile_exchange_short(self, exchange_short_quantity: Decimal) -> bool:
        normalized = Decimal(exchange_short_quantity)
        if not normalized.is_finite() or normalized < ZERO:
            raise ValueError("exchange short quantity cannot be negative")
        return self.total_open_quantity == normalized

    def _required_lot(self, lot_id: str) -> HedgeLot:
        lot = self._lots.get(lot_id)
        if lot is None:
            raise ValueError(f"unknown hedge lot: {lot_id}")
        return lot


def _lot_priority(lot: HedgeLot) -> tuple[str, int, int, str]:
    return (
        lot.entry_order_link_id,
        lot.level_id,
        lot.attempt,
        lot.lot_id,
    )


__all__ = [
    "ExitReservationRole",
    "HedgeLot",
    "LotExecution",
    "NetPositionAllocator",
]
