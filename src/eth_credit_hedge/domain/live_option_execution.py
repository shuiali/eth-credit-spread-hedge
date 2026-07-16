"""Durable long-first option-spread execution state and reducers."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from eth_credit_hedge.domain.client_order_ids import (
    ClientOrderId,
    ClientOrderRole,
)
from eth_credit_hedge.domain.execution import (
    ExecutionUpdate,
    OrderRequestAck,
    PlaceOrderRequest,
)
from eth_credit_hedge.domain.option_lifecycle import OptionEntryPolicy
from eth_credit_hedge.domain.option_position import (
    OptionPositionSnapshot,
    OptionPositionState,
)


ZERO = Decimal("0")


def _decimal(value: Decimal, field_name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be finite") from exc
    if not result.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return result


def _utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _role(order_link_id: str, role: ClientOrderRole) -> None:
    if ClientOrderId.parse(order_link_id).role is not role:
        raise ValueError(f"client order ID must have role {role.value}")


@dataclass(frozen=True, slots=True)
class OptionSpreadExecutionSnapshot:
    cycle_id: str
    state: OptionPositionState
    long_symbol: str
    short_symbol: str
    expiry_time_utc: datetime
    requested_quantity: Decimal
    expected_net_credit: Decimal
    long_order_link_id: str
    short_order_link_id: str | None
    long_order_id: str | None
    short_order_id: str | None
    long_filled_quantity: Decimal
    short_filled_quantity: Decimal
    long_notional: Decimal
    short_notional: Decimal
    long_fees: Decimal
    short_fees: Decimal
    opened_time_utc: datetime | None
    version: int
    updated_at: datetime

    def __post_init__(self) -> None:
        if not self.cycle_id.strip():
            raise ValueError("option cycle ID cannot be empty")
        for value, name in (
            (self.long_symbol, "long symbol"),
            (self.short_symbol, "short symbol"),
        ):
            if not value.strip():
                raise ValueError(f"{name} cannot be empty")
        if self.long_symbol == self.short_symbol:
            raise ValueError("option leg symbols must differ")
        _role(self.long_order_link_id, ClientOrderRole.OPTION_LONG)
        if self.short_order_link_id is not None:
            _role(self.short_order_link_id, ClientOrderRole.OPTION_SHORT)
        state = OptionPositionState(self.state)
        for field_name in (
            "requested_quantity",
            "expected_net_credit",
            "long_filled_quantity",
            "short_filled_quantity",
            "long_notional",
            "short_notional",
            "long_fees",
            "short_fees",
        ):
            object.__setattr__(
                self,
                field_name,
                _decimal(getattr(self, field_name), field_name.replace("_", " ")),
            )
        if self.requested_quantity <= ZERO:
            raise ValueError("requested quantity must be positive")
        if self.expected_net_credit <= ZERO:
            raise ValueError("expected net credit must be positive")
        if not ZERO <= self.long_filled_quantity <= self.requested_quantity:
            raise ValueError("long filled quantity is outside the request")
        if not ZERO <= self.short_filled_quantity <= self.long_filled_quantity:
            raise ValueError("short filled quantity cannot exceed protective long")
        for quantity, notional, fees, name in (
            (
                self.long_filled_quantity,
                self.long_notional,
                self.long_fees,
                "long",
            ),
            (
                self.short_filled_quantity,
                self.short_notional,
                self.short_fees,
                "short",
            ),
        ):
            if notional < ZERO or fees < ZERO:
                raise ValueError(f"{name} notional and fees cannot be negative")
            if quantity == ZERO and (notional != ZERO or fees != ZERO):
                raise ValueError(f"unfilled {name} leg cannot have cash amounts")
            if quantity > ZERO and notional <= ZERO:
                raise ValueError(f"filled {name} leg requires positive notional")
        if self.long_order_id is not None and not self.long_order_id.strip():
            raise ValueError("long exchange order ID cannot be empty")
        if self.short_order_id is not None and not self.short_order_id.strip():
            raise ValueError("short exchange order ID cannot be empty")
        if self.short_order_id is not None and self.short_order_link_id is None:
            raise ValueError("short exchange order requires a client order ID")
        opened = (
            None
            if self.opened_time_utc is None
            else _utc(self.opened_time_utc, "option opened time")
        )
        if state is OptionPositionState.OPEN:
            if (
                self.long_filled_quantity != self.requested_quantity
                or self.short_filled_quantity != self.requested_quantity
                or self.actual_net_credit <= ZERO
                or opened is None
            ):
                raise ValueError("OPEN requires equal full fills and positive credit")
        if type(self.version) is not int or self.version <= 0:
            raise ValueError("option snapshot version must be positive")
        object.__setattr__(self, "state", state)
        object.__setattr__(
            self,
            "expiry_time_utc",
            _utc(self.expiry_time_utc, "option expiry time"),
        )
        object.__setattr__(self, "opened_time_utc", opened)
        object.__setattr__(self, "updated_at", _utc(self.updated_at, "update time"))

    @property
    def matched_quantity(self) -> Decimal:
        return min(self.long_filled_quantity, self.short_filled_quantity)

    @property
    def has_naked_short(self) -> bool:
        return self.short_filled_quantity > self.long_filled_quantity

    @property
    def long_average_price(self) -> Decimal:
        if self.long_filled_quantity == ZERO:
            return ZERO
        return self.long_notional / self.long_filled_quantity

    @property
    def short_average_price(self) -> Decimal:
        if self.short_filled_quantity == ZERO:
            return ZERO
        return self.short_notional / self.short_filled_quantity

    @property
    def actual_net_credit(self) -> Decimal:
        matched = self.matched_quantity
        if matched == ZERO:
            return ZERO
        long_fee = self.long_fees * matched / self.long_filled_quantity
        short_fee = self.short_fees * matched / self.short_filled_quantity
        return (
            self.short_average_price * matched
            - self.long_average_price * matched
            - long_fee
            - short_fee
        )

    @property
    def actual_gross_credit(self) -> Decimal:
        matched = self.matched_quantity
        if matched == ZERO:
            return ZERO
        return (
            self.short_average_price * matched
            - self.long_average_price * matched
        )

    def position_snapshot(self) -> OptionPositionSnapshot:
        if self.state is not OptionPositionState.OPEN:
            raise ValueError("only an OPEN spread has a position snapshot")
        return OptionPositionSnapshot(
            short_symbol=self.short_symbol,
            long_symbol=self.long_symbol,
            matched_quantity=self.matched_quantity,
            short_average_entry_price=self.short_average_price,
            long_average_entry_price=self.long_average_price,
            actual_net_credit=self.actual_net_credit,
            total_fees=self.long_fees + self.short_fees,
            opened_time_utc=self.opened_time_utc,
            expiry_time_utc=self.expiry_time_utc,
            state=self.state,
        )

    @classmethod
    def for_long_intent(
        cls,
        request: PlaceOrderRequest,
        *,
        cycle_id: str,
        short_symbol: str,
        expiry_time_utc: datetime,
        expected_net_credit: Decimal,
        persisted_at: datetime,
    ) -> OptionSpreadExecutionSnapshot:
        if (
            request.category != "option"
            or request.side != "Buy"
            or request.reduce_only
        ):
            raise ValueError("first option intent must buy protective long")
        return cls(
            cycle_id=cycle_id,
            state=OptionPositionState.LONG_PROTECTION_PENDING,
            long_symbol=request.symbol,
            short_symbol=short_symbol,
            expiry_time_utc=expiry_time_utc,
            requested_quantity=request.quantity,
            expected_net_credit=expected_net_credit,
            long_order_link_id=request.order_link_id,
            short_order_link_id=None,
            long_order_id=None,
            short_order_id=None,
            long_filled_quantity=ZERO,
            short_filled_quantity=ZERO,
            long_notional=ZERO,
            short_notional=ZERO,
            long_fees=ZERO,
            short_fees=ZERO,
            opened_time_utc=None,
            version=1,
            updated_at=persisted_at,
        )


def acknowledge_option_order(
    snapshot: OptionSpreadExecutionSnapshot,
    acknowledgement: OrderRequestAck,
    *,
    updated_at: datetime,
) -> OptionSpreadExecutionSnapshot:
    if acknowledgement.order_link_id == snapshot.long_order_link_id:
        return replace(
            snapshot,
            long_order_id=acknowledgement.order_id,
            version=snapshot.version + 1,
            updated_at=updated_at,
        )
    if acknowledgement.order_link_id == snapshot.short_order_link_id:
        return replace(
            snapshot,
            short_order_id=acknowledgement.order_id,
            version=snapshot.version + 1,
            updated_at=updated_at,
        )
    raise ValueError("acknowledgement does not belong to the option spread")


def apply_option_execution(
    snapshot: OptionSpreadExecutionSnapshot,
    execution: ExecutionUpdate,
    *,
    updated_at: datetime,
) -> OptionSpreadExecutionSnapshot:
    if execution.order_link_id == snapshot.long_order_link_id:
        if execution.symbol != snapshot.long_symbol or execution.side != "Buy":
            raise ValueError("protective long execution does not match its intent")
        if snapshot.long_order_id not in (None, execution.order_id):
            raise ValueError("protective long exchange order ID differs")
        return replace(
            snapshot,
            long_order_id=execution.order_id,
            long_filled_quantity=snapshot.long_filled_quantity
            + execution.quantity,
            long_notional=snapshot.long_notional
            + execution.price * execution.quantity,
            long_fees=snapshot.long_fees + execution.fee,
            version=snapshot.version + 1,
            updated_at=updated_at,
        )
    if execution.order_link_id == snapshot.short_order_link_id:
        if execution.symbol != snapshot.short_symbol or execution.side != "Sell":
            raise ValueError("short premium execution does not match its intent")
        if snapshot.short_order_id not in (None, execution.order_id):
            raise ValueError("short premium exchange order ID differs")
        return replace(
            snapshot,
            short_order_id=execution.order_id,
            short_filled_quantity=snapshot.short_filled_quantity
            + execution.quantity,
            short_notional=snapshot.short_notional
            + execution.price * execution.quantity,
            short_fees=snapshot.short_fees + execution.fee,
            version=snapshot.version + 1,
            updated_at=updated_at,
        )
    raise ValueError("execution does not belong to the option spread")


def finalize_protective_long(
    snapshot: OptionSpreadExecutionSnapshot,
    policy: OptionEntryPolicy,
    *,
    updated_at: datetime,
) -> OptionSpreadExecutionSnapshot:
    acceptable = snapshot.long_filled_quantity >= policy.minimum_matched_quantity
    if not policy.allow_partial_spread:
        acceptable = acceptable and (
            snapshot.long_filled_quantity == snapshot.requested_quantity
        )
    return replace(
        snapshot,
        state=(
            OptionPositionState.LONG_PROTECTION_FILLED
            if acceptable
            else OptionPositionState.ERROR
        ),
        version=snapshot.version + 1,
        updated_at=updated_at,
    )


def start_short_premium(
    snapshot: OptionSpreadExecutionSnapshot,
    request: PlaceOrderRequest,
    *,
    updated_at: datetime,
) -> OptionSpreadExecutionSnapshot:
    if snapshot.state is not OptionPositionState.LONG_PROTECTION_FILLED:
        raise ValueError("short premium requires confirmed protective long")
    if (
        request.category != "option"
        or request.symbol != snapshot.short_symbol
        or request.side != "Sell"
        or request.reduce_only
        or request.quantity != snapshot.long_filled_quantity
    ):
        raise ValueError("short premium intent exceeds confirmed protection")
    return replace(
        snapshot,
        state=OptionPositionState.SHORT_PREMIUM_PENDING,
        short_order_link_id=request.order_link_id,
        version=snapshot.version + 1,
        updated_at=updated_at,
    )


def finalize_short_premium(
    snapshot: OptionSpreadExecutionSnapshot,
    policy: OptionEntryPolicy,
    *,
    updated_at: datetime,
) -> OptionSpreadExecutionSnapshot:
    matched = snapshot.matched_quantity
    expected_for_match = (
        snapshot.expected_net_credit * matched / snapshot.requested_quantity
    )
    # The operator bound is a leg-price/execution bound. Fees remain part of
    # actual net credit accounting, but are not adverse fill-price deviation.
    deviation = abs(snapshot.actual_gross_credit - expected_for_match)
    accepted = (
        snapshot.actual_net_credit >= policy.minimum_net_credit
        and snapshot.actual_net_credit > ZERO
        and policy.accepts_completion(
            requested_quantity=snapshot.requested_quantity,
            matched_quantity=matched,
            credit_deviation=deviation,
        )
    )
    state = OptionPositionState.ERROR
    opened_at = snapshot.opened_time_utc
    if accepted:
        state = (
            OptionPositionState.OPEN
            if matched == snapshot.requested_quantity
            else OptionPositionState.PARTIALLY_OPEN
        )
        opened_at = updated_at
    return replace(
        snapshot,
        state=state,
        opened_time_utc=opened_at,
        version=snapshot.version + 1,
        updated_at=updated_at,
    )


def mark_option_execution_error(
    snapshot: OptionSpreadExecutionSnapshot,
    *,
    updated_at: datetime,
) -> OptionSpreadExecutionSnapshot:
    if snapshot.state is OptionPositionState.ERROR:
        return snapshot
    return replace(
        snapshot,
        state=OptionPositionState.ERROR,
        version=snapshot.version + 1,
        updated_at=updated_at,
    )
