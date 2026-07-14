"""Ordered crossing tests."""

from decimal import Decimal

from eth_credit_hedge.core.crossing_engine import (
    CrossingEngine,
    CrossingEvent,
    CrossingEventType,
    Direction,
)


class FakeStrategy:
    def __init__(self) -> None:
        self.states = {1: "READY", 2: "READY"}
        self.executed: list[CrossingEvent] = []

    def eligible_crossings(
        self, previous: Decimal, current: Decimal, direction: Direction
    ) -> list[CrossingEvent]:
        events: list[CrossingEvent] = []
        if direction is Direction.DOWN:
            if self.states[1] == "READY" and previous > Decimal("3000") >= current:
                events.append(CrossingEvent(CrossingEventType.ENTRY, 1, Decimal("3000"), 1))
            if self.states[1] == "ACTIVE" and previous > Decimal("2980") >= current:
                events.append(CrossingEvent(CrossingEventType.TP, 1, Decimal("2980"), 0))
            if self.states[2] == "READY" and previous > Decimal("2980") >= current:
                events.append(CrossingEvent(CrossingEventType.ENTRY, 2, Decimal("2980"), 1))
        elif self.states[1] == "ACTIVE" and previous < Decimal("3004.50") <= current:
            events.append(CrossingEvent(CrossingEventType.STOP, 1, Decimal("3004.50")))
        return events

    def execute_crossing(self, event: CrossingEvent, tick_index: int) -> None:
        self.executed.append(event)
        if event.event_type is CrossingEventType.ENTRY:
            self.states[event.level_id] = "ACTIVE"
        elif event.event_type is CrossingEventType.TP:
            self.states[event.level_id] = "PAID"
        elif event.event_type is CrossingEventType.STOP:
            self.states[event.level_id] = "READY"


def event_summary(events: list[CrossingEvent]) -> list[tuple[str, int, Decimal]]:
    return [(event.event_type.value, event.level_id, event.price) for event in events]


def test_no_cross() -> None:
    assert CrossingEngine().process_transition("3010", "3005", FakeStrategy()) == []


def test_single_entry_at_exact_trigger() -> None:
    events = CrossingEngine().process_transition("3010", "2995", FakeStrategy())
    assert event_summary(events) == [("ENTRY", 1, Decimal("3000"))]


def test_large_downward_move_recalculates_after_each_event() -> None:
    events = CrossingEngine().process_transition("3010", "2970", FakeStrategy())
    assert event_summary(events) == [
        ("ENTRY", 1, Decimal("3000")),
        ("TP", 1, Decimal("2980")),
        ("ENTRY", 2, Decimal("2980")),
    ]


def test_remaining_below_does_not_duplicate_entry() -> None:
    strategy = FakeStrategy()
    engine = CrossingEngine()
    engine.process_transition("3010", "2995", strategy)
    assert engine.process_transition("2995", "2990", strategy) == []


def test_upward_stop_executes_at_exact_trigger() -> None:
    strategy = FakeStrategy()
    strategy.states[1] = "ACTIVE"
    events = CrossingEngine().process_transition("2995", "3004.50", strategy)
    assert event_summary(events) == [("STOP", 1, Decimal("3004.50"))]
