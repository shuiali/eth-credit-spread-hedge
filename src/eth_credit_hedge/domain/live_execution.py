"""Persistable one-level exchange execution state and pure reducers."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from eth_credit_hedge.domain.client_order_ids import ClientOrderId
from eth_credit_hedge.domain.execution import (
    ExchangePosition,
    ExecutionUpdate,
    LiveExecutionState,
    PlaceOrderRequest,
)


ZERO = Decimal("0")
_ENTRY_STATES = frozenset(
    {
        LiveExecutionState.ENTRY_REQUEST_PERSISTED,
        LiveExecutionState.ENTRY_SUBMITTED,
        LiveExecutionState.ENTRY_ACKNOWLEDGED,
        LiveExecutionState.ENTRY_PARTIALLY_FILLED,
        LiveExecutionState.ACTIVE_UNPROTECTED,
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


@dataclass(frozen=True, slots=True)
class EntryExecutionSnapshot:
    order_link_id: str
    state: LiveExecutionState
    target_quantity: Decimal
    entry_order_id: str | None
    filled_quantity: Decimal
    entry_notional: Decimal
    entry_fees: Decimal
    version: int
    updated_at: datetime

    def __post_init__(self) -> None:
        ClientOrderId.parse(self.order_link_id)
        state = LiveExecutionState(self.state)
        if state not in _ENTRY_STATES:
            raise ValueError("state is not valid for one-level entry execution")
        target = _decimal(self.target_quantity, "target quantity")
        filled = _decimal(self.filled_quantity, "filled quantity")
        notional = _decimal(self.entry_notional, "entry notional")
        fees = _decimal(self.entry_fees, "entry fees")
        if target <= ZERO:
            raise ValueError("target quantity must be positive")
        if not ZERO <= filled <= target:
            raise ValueError("filled quantity must be between zero and target")
        if notional < ZERO:
            raise ValueError("entry notional cannot be negative")
        if filled == ZERO and notional != ZERO:
            raise ValueError("unfilled entry must have zero notional")
        if filled > ZERO and notional <= ZERO:
            raise ValueError("filled entry must have positive notional")
        if self.entry_order_id is not None and not self.entry_order_id.strip():
            raise ValueError("entry order ID cannot be empty")
        if state in (
            LiveExecutionState.ENTRY_ACKNOWLEDGED,
            LiveExecutionState.ENTRY_PARTIALLY_FILLED,
            LiveExecutionState.ACTIVE_UNPROTECTED,
        ) and self.entry_order_id is None:
            raise ValueError("state requires an exchange order ID")
        if state is LiveExecutionState.ENTRY_PARTIALLY_FILLED and not (
            ZERO < filled < target
        ):
            raise ValueError("partial-fill state requires a partial quantity")
        if state is LiveExecutionState.ACTIVE_UNPROTECTED and filled <= ZERO:
            raise ValueError("active entry requires confirmed filled quantity")
        if type(self.version) is not int or self.version <= 0:
            raise ValueError("snapshot version must be positive")
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "target_quantity", target)
        object.__setattr__(self, "filled_quantity", filled)
        object.__setattr__(self, "entry_notional", notional)
        object.__setattr__(self, "entry_fees", fees)
        object.__setattr__(self, "updated_at", _utc(self.updated_at, "update time"))

    @property
    def average_entry_price(self) -> Decimal | None:
        if self.filled_quantity == ZERO:
            return None
        return self.entry_notional / self.filled_quantity

    @classmethod
    def for_intent(
        cls,
        request: PlaceOrderRequest,
        persisted_at: datetime,
    ) -> EntryExecutionSnapshot:
        if (
            request.category != "linear"
            or request.symbol != "ETHUSDT"
            or request.side != "Sell"
            or request.reduce_only
        ):
            raise ValueError("entry intent must open a short ETHUSDT linear position")
        return cls(
            order_link_id=request.order_link_id,
            state=LiveExecutionState.ENTRY_REQUEST_PERSISTED,
            target_quantity=request.quantity,
            entry_order_id=None,
            filled_quantity=ZERO,
            entry_notional=ZERO,
            entry_fees=ZERO,
            version=1,
            updated_at=persisted_at,
        )


def transition_entry_snapshot(
    snapshot: EntryExecutionSnapshot,
    state: LiveExecutionState,
    *,
    updated_at: datetime,
    entry_order_id: str | None = None,
) -> EntryExecutionSnapshot:
    return replace(
        snapshot,
        state=state,
        entry_order_id=(
            snapshot.entry_order_id
            if entry_order_id is None
            else entry_order_id
        ),
        version=snapshot.version + 1,
        updated_at=updated_at,
    )


def apply_entry_execution(
    snapshot: EntryExecutionSnapshot,
    execution: ExecutionUpdate,
    *,
    updated_at: datetime,
) -> EntryExecutionSnapshot:
    if execution.order_link_id != snapshot.order_link_id:
        raise ValueError("execution client ID does not match entry snapshot")
    if execution.symbol != "ETHUSDT" or execution.side != "Sell":
        raise ValueError("entry execution must be an ETHUSDT Sell")
    if (
        snapshot.entry_order_id is not None
        and execution.order_id != snapshot.entry_order_id
    ):
        raise ValueError("execution order ID does not match entry snapshot")
    filled = snapshot.filled_quantity + execution.quantity
    if filled > snapshot.target_quantity:
        raise ValueError("entry executions exceed target quantity")
    state = (
        LiveExecutionState.ACTIVE_UNPROTECTED
        if filled == snapshot.target_quantity
        else LiveExecutionState.ENTRY_PARTIALLY_FILLED
    )
    return replace(
        snapshot,
        state=state,
        entry_order_id=execution.order_id,
        filled_quantity=filled,
        entry_notional=snapshot.entry_notional
        + execution.price * execution.quantity,
        entry_fees=snapshot.entry_fees + execution.fee,
        version=snapshot.version + 1,
        updated_at=updated_at,
    )


def entry_position_matches(
    snapshot: EntryExecutionSnapshot,
    positions: tuple[ExchangePosition, ...],
) -> bool:
    nonzero = tuple(
        position
        for position in positions
        if position.category == "linear"
        and position.symbol == "ETHUSDT"
        and position.quantity > ZERO
    )
    if snapshot.filled_quantity == ZERO:
        return not nonzero
    return (
        len(nonzero) == 1
        and nonzero[0].side == "Sell"
        and nonzero[0].quantity == snapshot.filled_quantity
    )
