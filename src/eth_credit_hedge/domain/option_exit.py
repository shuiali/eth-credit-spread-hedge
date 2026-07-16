"""Restartable close state for an opened protective put credit spread."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum


ZERO = Decimal("0")


class OptionExitState(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    SHORT_CLOSING = "SHORT_CLOSING"
    SHORT_CLOSED = "SHORT_CLOSED"
    LONG_CLOSING = "LONG_CLOSING"
    CLOSED = "CLOSED"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class OptionSpreadExitSnapshot:
    cycle_id: str
    short_symbol: str
    long_symbol: str
    state: OptionExitState
    short_remaining_quantity: Decimal
    long_remaining_quantity: Decimal
    active_order_link_id: str | None
    version: int
    updated_at: datetime

    def __post_init__(self) -> None:
        for value, name in (
            (self.cycle_id, "cycle ID"),
            (self.short_symbol, "short symbol"),
            (self.long_symbol, "long symbol"),
        ):
            if not value.strip():
                raise ValueError(f"{name} cannot be empty")
        if self.short_symbol == self.long_symbol:
            raise ValueError("option exit symbols must differ")
        object.__setattr__(self, "state", OptionExitState(self.state))
        for field_name in (
            "short_remaining_quantity",
            "long_remaining_quantity",
        ):
            normalized_quantity = Decimal(getattr(self, field_name))
            if not normalized_quantity.is_finite() or normalized_quantity < ZERO:
                raise ValueError(f"{field_name.replace('_', ' ')} cannot be negative")
            object.__setattr__(self, field_name, normalized_quantity)
        if self.active_order_link_id is not None and not self.active_order_link_id.strip():
            raise ValueError("active order-link ID cannot be empty")
        if type(self.version) is not int or self.version <= 0:
            raise ValueError("option exit version must be positive")
        if self.updated_at.tzinfo is None or self.updated_at.utcoffset() is None:
            raise ValueError("option exit timestamp must be timezone-aware")
        object.__setattr__(
            self,
            "updated_at",
            self.updated_at.astimezone(timezone.utc),
        )
        if self.state in (OptionExitState.SHORT_CLOSED, OptionExitState.LONG_CLOSING):
            if self.short_remaining_quantity != ZERO:
                raise ValueError("short-closed state requires zero short quantity")
        if self.state is OptionExitState.CLOSED and (
            self.short_remaining_quantity != ZERO
            or self.long_remaining_quantity != ZERO
        ):
            raise ValueError("closed option exit requires zero remaining quantity")


__all__ = ["OptionExitState", "OptionSpreadExitSnapshot"]
