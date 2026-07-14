"""Ordered tick-segment crossing engine."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from collections.abc import Callable
from typing import Protocol

from core.credit_spread import DecimalLike, to_decimal


class Direction(str, Enum):
    DOWN = "DOWN"
    UP = "UP"


class CrossingEventType(str, Enum):
    ENTRY = "ENTRY"
    TP = "TP"
    STOP = "STOP"
    BREAKEVEN = "BREAKEVEN"


@dataclass(frozen=True, slots=True)
class CrossingEvent:
    event_type: CrossingEventType
    level_id: int
    price: Decimal
    priority: int = 0


class CrossingStrategy(Protocol):
    def eligible_crossings(
        self,
        previous_price: Decimal,
        current_price: Decimal,
        direction: Direction,
    ) -> list[CrossingEvent]: ...

    def execute_crossing(self, event: CrossingEvent, tick_index: int) -> None: ...


class CrossingEngine:
    """Process every newly eligible trigger within an ordered price segment."""

    def process_transition(
        self,
        previous_price: DecimalLike,
        current_price: DecimalLike,
        strategy: CrossingStrategy,
        tick_index: int = 0,
        on_executed: Callable[[CrossingEvent], None] | None = None,
    ) -> list[CrossingEvent]:
        previous = to_decimal(previous_price)
        current = to_decimal(current_price)
        if previous == current:
            return []

        direction = Direction.DOWN if current < previous else Direction.UP
        cursor = previous
        executed: list[CrossingEvent] = []
        processed: set[tuple[CrossingEventType, int, Decimal]] = set()

        while True:
            candidates = [
                event
                for event in strategy.eligible_crossings(previous, current, direction)
                if self._inside_remaining_segment(
                    event.price, cursor, current, direction
                )
                and (event.event_type, event.level_id, event.price) not in processed
            ]
            if not candidates:
                return executed

            event = self._nearest(candidates, direction)
            strategy.execute_crossing(event, tick_index)
            if on_executed is not None:
                on_executed(event)
            executed.append(event)
            processed.add((event.event_type, event.level_id, event.price))
            cursor = event.price

    @staticmethod
    def _inside_remaining_segment(
        price: Decimal,
        cursor: Decimal,
        current: Decimal,
        direction: Direction,
    ) -> bool:
        if direction is Direction.DOWN:
            return current <= price <= cursor
        return cursor <= price <= current

    @staticmethod
    def _nearest(
        candidates: list[CrossingEvent], direction: Direction
    ) -> CrossingEvent:
        if direction is Direction.DOWN:
            return sorted(
                candidates,
                key=lambda event: (-event.price, event.priority, event.level_id),
            )[0]
        return sorted(
            candidates, key=lambda event: (event.price, event.priority, event.level_id)
        )[0]
