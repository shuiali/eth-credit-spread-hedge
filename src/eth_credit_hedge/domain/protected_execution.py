"""Pure reducer for exchange-hosted protection and actual exit fills."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from eth_credit_hedge.domain.client_order_ids import (
    ClientOrderId,
    ClientOrderRole,
)
from eth_credit_hedge.domain.execution import (
    ExchangePosition,
    ExecutionUpdate,
    LiveExecutionState,
)
from eth_credit_hedge.domain.live_execution import EntryExecutionSnapshot


ZERO = Decimal("0")
_PROTECTION_STATES = frozenset(
    {
        LiveExecutionState.ACTIVE_UNPROTECTED,
        LiveExecutionState.ACTIVE_PROTECTED,
        LiveExecutionState.EXIT_PARTIALLY_FILLED,
        LiveExecutionState.CANCEL_PENDING,
        LiveExecutionState.CLOSED_TP,
        LiveExecutionState.CLOSED_STOP,
        LiveExecutionState.RECONCILING,
        LiveExecutionState.ERROR,
    }
)


def _decimal(value: Decimal, field_name: str) -> Decimal:
    try:
        normalized = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be finite") from exc
    if not normalized.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return normalized


def _utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _role(order_link_id: str, expected: ClientOrderRole) -> None:
    if ClientOrderId.parse(order_link_id).role is not expected:
        raise ValueError(f"client order ID must have role {expected.value}")


@dataclass(frozen=True, slots=True)
class ProtectionSnapshot:
    entry_order_link_id: str
    state: LiveExecutionState
    entry_quantity: Decimal
    open_quantity: Decimal
    average_entry_price: Decimal
    entry_fees: Decimal
    stop_order_link_id: str
    stop_order_id: str | None
    stop_trigger_price: Decimal
    tp_order_link_id: str | None
    tp_order_id: str | None
    tp_price: Decimal | None
    tp_filled_quantity: Decimal
    stop_filled_quantity: Decimal
    exit_notional: Decimal
    exit_fees: Decimal
    confirmed_recovery_debt: Decimal
    pending_terminal_state: LiveExecutionState | None
    version: int
    updated_at: datetime

    def __post_init__(self) -> None:
        _role(self.entry_order_link_id, ClientOrderRole.HEDGE_ENTRY)
        _role(self.stop_order_link_id, ClientOrderRole.HEDGE_STOP)
        if self.tp_order_link_id is not None:
            _role(self.tp_order_link_id, ClientOrderRole.HEDGE_TP)
        state = LiveExecutionState(self.state)
        if state not in _PROTECTION_STATES:
            raise ValueError("state is not valid for protection execution")
        for field_name in (
            "entry_quantity",
            "open_quantity",
            "average_entry_price",
            "entry_fees",
            "stop_trigger_price",
            "tp_filled_quantity",
            "stop_filled_quantity",
            "exit_notional",
            "exit_fees",
            "confirmed_recovery_debt",
        ):
            object.__setattr__(
                self,
                field_name,
                _decimal(getattr(self, field_name), field_name.replace("_", " ")),
            )
        tp_price = None if self.tp_price is None else _decimal(self.tp_price, "TP price")
        if self.entry_quantity <= ZERO or self.average_entry_price <= ZERO:
            raise ValueError("entry quantity and average price must be positive")
        if not ZERO <= self.open_quantity <= self.entry_quantity:
            raise ValueError("open quantity must be within entry quantity")
        for field_name in (
            "entry_fees",
            "tp_filled_quantity",
            "stop_filled_quantity",
            "exit_notional",
            "exit_fees",
            "confirmed_recovery_debt",
        ):
            if getattr(self, field_name) < ZERO:
                raise ValueError(f"{field_name.replace('_', ' ')} cannot be negative")
        closed_quantity = self.tp_filled_quantity + self.stop_filled_quantity
        if closed_quantity + self.open_quantity != self.entry_quantity:
            raise ValueError("open and closed quantities must equal entry quantity")
        if closed_quantity == ZERO and self.exit_notional != ZERO:
            raise ValueError("unfilled exits must have zero notional")
        if closed_quantity > ZERO and self.exit_notional <= ZERO:
            raise ValueError("filled exits must have positive notional")
        if self.stop_trigger_price <= ZERO:
            raise ValueError("stop trigger price must be positive")
        if self.stop_order_id is not None and not self.stop_order_id.strip():
            raise ValueError("stop order ID cannot be empty")
        if self.tp_order_id is not None and not self.tp_order_id.strip():
            raise ValueError("TP order ID cannot be empty")
        if self.tp_order_link_id is None:
            if self.tp_order_id is not None or tp_price is not None:
                raise ValueError("TP details require a TP client order ID")
        elif tp_price is None or tp_price <= ZERO:
            raise ValueError("TP intent requires a positive price")
        if state is LiveExecutionState.ACTIVE_PROTECTED and self.stop_order_id is None:
            raise ValueError("protected state requires a confirmed stop")
        pending = (
            None
            if self.pending_terminal_state is None
            else LiveExecutionState(self.pending_terminal_state)
        )
        if pending not in (
            None,
            LiveExecutionState.CLOSED_TP,
            LiveExecutionState.CLOSED_STOP,
        ):
            raise ValueError("pending terminal state must be CLOSED_TP or CLOSED_STOP")
        if state is LiveExecutionState.CANCEL_PENDING:
            if self.open_quantity != ZERO or pending is None:
                raise ValueError("cancel pending requires a closed local position")
        if state in (LiveExecutionState.CLOSED_TP, LiveExecutionState.CLOSED_STOP):
            if self.open_quantity != ZERO or pending is not None:
                raise ValueError("closed state requires zero open quantity")
        if type(self.version) is not int or self.version <= 0:
            raise ValueError("snapshot version must be positive")
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "tp_price", tp_price)
        object.__setattr__(self, "pending_terminal_state", pending)
        object.__setattr__(self, "updated_at", _utc(self.updated_at, "update time"))

    @property
    def realized_pnl(self) -> Decimal:
        closed_quantity = self.tp_filled_quantity + self.stop_filled_quantity
        allocated_entry_fees = (
            self.entry_fees * closed_quantity / self.entry_quantity
        )
        return (
            self.average_entry_price * closed_quantity
            - self.exit_notional
            - allocated_entry_fees
            - self.exit_fees
        )

    @classmethod
    def for_stop_intent(
        cls,
        entry: EntryExecutionSnapshot,
        *,
        stop_order_link_id: str,
        stop_trigger_price: Decimal,
        persisted_at: datetime,
    ) -> ProtectionSnapshot:
        average_price = entry.average_entry_price
        if entry.state is not LiveExecutionState.ACTIVE_UNPROTECTED:
            raise ValueError("stop protection requires ACTIVE_UNPROTECTED entry")
        if average_price is None or entry.filled_quantity <= ZERO:
            raise ValueError("stop protection requires confirmed entry executions")
        return cls(
            entry_order_link_id=entry.order_link_id,
            state=LiveExecutionState.ACTIVE_UNPROTECTED,
            entry_quantity=entry.filled_quantity,
            open_quantity=entry.filled_quantity,
            average_entry_price=average_price,
            entry_fees=entry.entry_fees,
            stop_order_link_id=stop_order_link_id,
            stop_order_id=None,
            stop_trigger_price=stop_trigger_price,
            tp_order_link_id=None,
            tp_order_id=None,
            tp_price=None,
            tp_filled_quantity=ZERO,
            stop_filled_quantity=ZERO,
            exit_notional=ZERO,
            exit_fees=ZERO,
            confirmed_recovery_debt=ZERO,
            pending_terminal_state=None,
            version=1,
            updated_at=persisted_at,
        )


def confirm_stop(
    snapshot: ProtectionSnapshot,
    *,
    order_id: str,
    updated_at: datetime,
) -> ProtectionSnapshot:
    return replace(
        snapshot,
        state=LiveExecutionState.ACTIVE_PROTECTED,
        stop_order_id=order_id,
        version=snapshot.version + 1,
        updated_at=updated_at,
    )


def replace_stop_intent(
    snapshot: ProtectionSnapshot,
    *,
    order_link_id: str,
    trigger_price: Decimal,
    updated_at: datetime,
) -> ProtectionSnapshot:
    if snapshot.open_quantity <= ZERO:
        raise ValueError("replacement stop requires confirmed open quantity")
    if snapshot.state not in (
        LiveExecutionState.ACTIVE_PROTECTED,
        LiveExecutionState.EXIT_PARTIALLY_FILLED,
        LiveExecutionState.RECONCILING,
    ):
        raise ValueError("snapshot state does not allow replacement protection")
    return replace(
        snapshot,
        state=LiveExecutionState.ACTIVE_UNPROTECTED,
        stop_order_link_id=order_link_id,
        stop_order_id=None,
        stop_trigger_price=trigger_price,
        version=snapshot.version + 1,
        updated_at=updated_at,
    )


def add_take_profit_intent(
    snapshot: ProtectionSnapshot,
    *,
    order_link_id: str,
    price: Decimal,
    updated_at: datetime,
) -> ProtectionSnapshot:
    if snapshot.state is not LiveExecutionState.ACTIVE_PROTECTED:
        raise ValueError("TP requires confirmed stop protection")
    if snapshot.tp_order_link_id is not None:
        raise ValueError("TP intent already exists")
    return replace(
        snapshot,
        tp_order_link_id=order_link_id,
        tp_price=price,
        version=snapshot.version + 1,
        updated_at=updated_at,
    )


def confirm_take_profit(
    snapshot: ProtectionSnapshot,
    *,
    order_id: str,
    updated_at: datetime,
) -> ProtectionSnapshot:
    if snapshot.tp_order_link_id is None:
        raise ValueError("TP confirmation requires a persisted intent")
    return replace(
        snapshot,
        tp_order_id=order_id,
        version=snapshot.version + 1,
        updated_at=updated_at,
    )


def mark_protection_reconciling(
    snapshot: ProtectionSnapshot,
    *,
    updated_at: datetime,
) -> ProtectionSnapshot:
    if snapshot.state is LiveExecutionState.RECONCILING:
        return snapshot
    return replace(
        snapshot,
        state=LiveExecutionState.RECONCILING,
        version=snapshot.version + 1,
        updated_at=updated_at,
    )


def apply_exit_execution(
    snapshot: ProtectionSnapshot,
    execution: ExecutionUpdate,
    *,
    updated_at: datetime,
) -> ProtectionSnapshot:
    if execution.symbol != "ETHUSDT" or execution.side != "Buy":
        raise ValueError("protective execution must be an ETHUSDT Buy")
    is_stop = execution.order_link_id == snapshot.stop_order_link_id
    is_tp = execution.order_link_id == snapshot.tp_order_link_id
    if not is_stop and not is_tp:
        raise ValueError("execution does not belong to a protective exit")
    expected_order_id = snapshot.stop_order_id if is_stop else snapshot.tp_order_id
    if expected_order_id is not None and execution.order_id != expected_order_id:
        raise ValueError("execution order ID does not match protective exit")
    if execution.quantity > snapshot.open_quantity:
        raise ValueError("exit execution exceeds confirmed open quantity")

    open_quantity = snapshot.open_quantity - execution.quantity
    tp_filled = snapshot.tp_filled_quantity
    stop_filled = snapshot.stop_filled_quantity
    debt = snapshot.confirmed_recovery_debt
    if is_stop:
        stop_filled += execution.quantity
        allocated_entry_fee = (
            snapshot.entry_fees * execution.quantity / snapshot.entry_quantity
        )
        debt += max(
            (execution.price - snapshot.average_entry_price) * execution.quantity
            + allocated_entry_fee
            + execution.fee,
            ZERO,
        )
    else:
        tp_filled += execution.quantity
    pending_terminal = None
    state = LiveExecutionState.EXIT_PARTIALLY_FILLED
    if open_quantity == ZERO:
        pending_terminal = (
            LiveExecutionState.CLOSED_STOP
            if stop_filled > ZERO
            else LiveExecutionState.CLOSED_TP
        )
        state = LiveExecutionState.CANCEL_PENDING
    return replace(
        snapshot,
        state=state,
        open_quantity=open_quantity,
        tp_filled_quantity=tp_filled,
        stop_filled_quantity=stop_filled,
        exit_notional=snapshot.exit_notional
        + execution.price * execution.quantity,
        exit_fees=snapshot.exit_fees + execution.fee,
        confirmed_recovery_debt=debt,
        pending_terminal_state=pending_terminal,
        version=snapshot.version + 1,
        updated_at=updated_at,
    )


def confirm_exit_reconciliation(
    snapshot: ProtectionSnapshot,
    *,
    updated_at: datetime,
) -> ProtectionSnapshot:
    if (
        snapshot.state is not LiveExecutionState.CANCEL_PENDING
        or snapshot.pending_terminal_state is None
    ):
        raise ValueError("exit reconciliation requires CANCEL_PENDING state")
    return replace(
        snapshot,
        state=snapshot.pending_terminal_state,
        pending_terminal_state=None,
        version=snapshot.version + 1,
        updated_at=updated_at,
    )


def protection_position_matches(
    snapshot: ProtectionSnapshot,
    positions: tuple[ExchangePosition, ...],
) -> bool:
    nonzero = tuple(
        position
        for position in positions
        if position.category == "linear"
        and position.symbol == "ETHUSDT"
        and position.quantity > ZERO
    )
    if snapshot.open_quantity == ZERO:
        return not nonzero
    return (
        len(nonzero) == 1
        and nonzero[0].side == "Sell"
        and nonzero[0].quantity == snapshot.open_quantity
    )
