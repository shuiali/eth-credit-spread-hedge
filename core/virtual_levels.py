"""Deterministic virtual hedge-level generation."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from core.credit_spread import CreditSpread, DecimalLike, ZERO, to_decimal


class LevelState(str, Enum):
    READY = "READY"
    ACTIVE = "ACTIVE"
    PAID = "PAID"
    LOCKED = "LOCKED"


@dataclass(slots=True)
class HedgeLevel:
    level_id: int
    entry_price: Decimal
    tp_price: Decimal
    stop_price: Decimal
    option_budget: Decimal
    state: LevelState = LevelState.READY
    attempts: int = 0
    active_quantity: Decimal = ZERO
    active_is_floor: bool = False
    entry_armed: bool = False
    recovery_debt: Decimal = ZERO
    recovery_tps_remaining: int = 0
    realized_stop_losses: Decimal = ZERO
    realized_tp_profit: Decimal = ZERO
    stop_loss_history: list[Decimal] = field(default_factory=list)
    active_recovery_allocations: dict[int, Decimal] = field(default_factory=dict)

    @property
    def tp_distance(self) -> Decimal:
        return self.entry_price - self.tp_price

    @property
    def stop_distance(self) -> Decimal:
        return self.stop_price - self.entry_price

    @property
    def initial_quantity(self) -> Decimal:
        return self.option_budget / self.tp_distance


def generate_virtual_levels(
    spread: CreditSpread,
    level_count: int,
    stop_rate: DecimalLike = "0.0015",
) -> list[HedgeLevel]:
    """Partition only the interval between the two put strikes."""
    if level_count <= 0:
        raise ValueError("level count must be positive")
    rate = to_decimal(stop_rate)
    if rate <= ZERO:
        raise ValueError("stop rate must be positive")

    width = spread.short_put_strike - spread.long_put_strike
    count = Decimal(level_count)
    boundaries = [
        spread.short_put_strike - width * Decimal(index) / count
        for index in range(level_count)
    ]
    boundaries.append(spread.long_put_strike)

    levels: list[HedgeLevel] = []
    for index in range(level_count):
        entry_price = boundaries[index]
        tp_price = boundaries[index + 1]
        levels.append(
            HedgeLevel(
                level_id=index + 1,
                entry_price=entry_price,
                tp_price=tp_price,
                stop_price=entry_price * (Decimal("1") + rate),
                option_budget=spread.option_quantity * (entry_price - tp_price),
            )
        )
    return levels
