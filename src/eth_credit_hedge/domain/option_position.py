"""Option-leg and put-credit-spread lifecycle state."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Literal

from eth_credit_hedge.domain.instruments import OptionContract


LegSide = Literal["Long", "Short"]
ZERO = Decimal("0")


class OptionPositionState(str, Enum):
    PLANNED = "PLANNED"
    LONG_PROTECTION_PENDING = "LONG_PROTECTION_PENDING"
    LONG_PROTECTION_FILLED = "LONG_PROTECTION_FILLED"
    SHORT_PREMIUM_PENDING = "SHORT_PREMIUM_PENDING"
    OPEN = "OPEN"
    PARTIALLY_OPEN = "PARTIALLY_OPEN"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    ERROR = "ERROR"


@dataclass(slots=True)
class OptionLegPosition:
    contract: OptionContract
    side: LegSide
    requested_quantity: Decimal
    filled_quantity: Decimal
    average_entry_price: Decimal
    fees_paid: Decimal

    def __post_init__(self) -> None:
        if self.side not in ("Long", "Short"):
            raise ValueError("leg side must be Long or Short")
        for field_name in (
            "requested_quantity",
            "filled_quantity",
            "average_entry_price",
            "fees_paid",
        ):
            value = Decimal(str(getattr(self, field_name)))
            if not value.is_finite():
                raise ValueError(f"{field_name} must be finite")
            setattr(self, field_name, value)

        if self.requested_quantity <= ZERO:
            raise ValueError("requested quantity must be positive")
        if not ZERO <= self.filled_quantity <= self.requested_quantity:
            raise ValueError("filled quantity must be between zero and requested")
        if self.fees_paid < ZERO:
            raise ValueError("fees paid cannot be negative")
        if self.filled_quantity == ZERO and self.average_entry_price != ZERO:
            raise ValueError("unfilled leg must have zero average entry price")
        if self.filled_quantity > ZERO and self.average_entry_price <= ZERO:
            raise ValueError("filled leg must have a positive average entry price")

    @property
    def remaining_quantity(self) -> Decimal:
        return self.requested_quantity - self.filled_quantity


@dataclass(slots=True)
class PutCreditSpreadPosition:
    short_put: OptionLegPosition
    long_put: OptionLegPosition
    state: OptionPositionState

    def __post_init__(self) -> None:
        self.state = OptionPositionState(self.state)
        self._validate_contracts()
        self._validate_protection()
        self._validate_state()

    @property
    def matched_quantity(self) -> Decimal:
        return min(self.short_put.filled_quantity, self.long_put.filled_quantity)

    @property
    def has_naked_short(self) -> bool:
        return self.short_put.filled_quantity > self.long_put.filled_quantity

    def _validate_contracts(self) -> None:
        if self.short_put.side != "Short" or self.long_put.side != "Long":
            raise ValueError("credit spread requires short and long leg sides")
        short = self.short_put.contract
        long = self.long_put.contract
        if short.option_type != "Put" or long.option_type != "Put":
            raise ValueError("credit spread legs must both be puts")
        if (
            short.base_coin,
            short.quote_coin,
            short.settle_coin,
        ) != (
            long.base_coin,
            long.quote_coin,
            long.settle_coin,
        ):
            raise ValueError("credit spread legs must use the same coins")
        if short.expiry_time_utc != long.expiry_time_utc:
            raise ValueError("credit spread legs must have the same expiry")
        if short.strike <= long.strike:
            raise ValueError("short put strike must be above long put strike")

    def _validate_protection(self) -> None:
        if self.has_naked_short:
            raise ValueError("short fill would create naked short option exposure")

    def _validate_state(self) -> None:
        if self.state is OptionPositionState.PLANNED and (
            self.short_put.filled_quantity != ZERO
            or self.long_put.filled_quantity != ZERO
        ):
            raise ValueError("PLANNED position cannot contain fills")
        if self.state is OptionPositionState.LONG_PROTECTION_PENDING:
            if self.short_put.filled_quantity != ZERO:
                raise ValueError("short cannot fill while long protection is pending")
        if self.state is OptionPositionState.LONG_PROTECTION_FILLED:
            if self.long_put.filled_quantity <= ZERO:
                raise ValueError("state requires confirmed protective long")
            if self.short_put.filled_quantity != ZERO:
                raise ValueError("short cannot fill before premium submission")
        if self.state is OptionPositionState.SHORT_PREMIUM_PENDING:
            if self.long_put.filled_quantity <= ZERO:
                raise ValueError("state requires confirmed protective long")
        if self.state is OptionPositionState.OPEN:
            if self.matched_quantity <= ZERO:
                raise ValueError("OPEN requires confirmed matched quantity")
            if self.short_put.filled_quantity != self.long_put.filled_quantity:
                raise ValueError("OPEN requires equal confirmed leg quantities")
        if self.state is OptionPositionState.PARTIALLY_OPEN:
            if self.matched_quantity <= ZERO:
                raise ValueError("PARTIALLY_OPEN requires confirmed matched quantity")
            if self.short_put.filled_quantity == self.long_put.filled_quantity:
                raise ValueError("equal filled quantities must use OPEN state")
