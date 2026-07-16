"""Recovery sizing tests."""

from decimal import Decimal

from eth_credit_hedge.core.credit_spread import CreditSpread
from eth_credit_hedge.core.hedge_engine import HedgeEngine
from eth_credit_hedge.core.ledger import LedgerEventType
from eth_credit_hedge.core.virtual_levels import LevelState
from eth_credit_hedge.domain.strategy_math import PriceStepFractionStopConfig, Rate


PRICE_STEP_STOP = PriceStepFractionStopConfig(Rate(Decimal("0.15")))


def make_engine() -> HedgeEngine:
    return HedgeEngine(
        CreditSpread("3010", "3000", "2980", "1", "20"),
        level_count=1,
        stop=PRICE_STEP_STOP,
    )


def test_same_level_reentry_recovers_one_stop_completely() -> None:
    engine = make_engine()
    events = engine.run(["3010", "3000", "3003", "3000", "2980"])
    level = engine.levels[0]

    assert [event.event_type for event in events] == [
        LedgerEventType.ENTRY,
        LedgerEventType.STOP,
        LedgerEventType.ENTRY,
        LedgerEventType.TP,
    ]
    assert events[0].quantity == Decimal("1")
    assert events[1].realized_pnl == Decimal("-3.00")
    assert events[2].quantity == Decimal("1.15")
    assert events[3].realized_pnl == Decimal("23.00")
    assert events[3].zone_profit_component == Decimal("20")
    assert events[3].recovery_profit_component == Decimal("3.00")
    assert engine.ledger.realized_hedge_pnl == Decimal("20.00")
    assert level.recovery_debt == Decimal("0.00")
    assert level.state is LevelState.PAID


def test_two_stops_then_tp_recovers_to_original_zone_budget() -> None:
    engine = make_engine()
    events = engine.run(["3010", "3000", "3003", "3000", "3003", "3000", "2980"])
    entries = [event for event in events if event.event_type is LedgerEventType.ENTRY]
    stops = [event for event in events if event.event_type is LedgerEventType.STOP]
    tp = next(event for event in events if event.event_type is LedgerEventType.TP)
    level = engine.levels[0]

    assert [entry.quantity for entry in entries] == [
        Decimal("1"),
        Decimal("1.15"),
        Decimal("1.3225"),
    ]
    assert [stop.realized_pnl for stop in stops] == [
        Decimal("-3.00"),
        Decimal("-3.4500"),
    ]
    assert tp.realized_pnl == Decimal("26.4500")
    assert tp.recovery_profit_component == Decimal("6.4500")
    assert sum((event.realized_pnl for event in events), Decimal("0")) == Decimal("20")
    assert engine.ledger.realized_hedge_pnl == level.option_budget
    assert level.recovery_debt == Decimal("0")
    assert level.attempts == 3
    assert level.state is LevelState.PAID


def test_premium_budget_locks_the_first_unaffordable_attempt() -> None:
    engine = HedgeEngine(
        CreditSpread("3010", "3000", "2980", "1", "10"),
            level_count=1,
            stop=PRICE_STEP_STOP,
    )
    events = engine.run(["3010", "3000", "3003", "3000", "3003", "3000", "2980"])
    level = engine.levels[0]

    assert [event.event_type for event in events] == [
        LedgerEventType.ENTRY,
        LedgerEventType.STOP,
        LedgerEventType.ENTRY,
        LedgerEventType.STOP,
        LedgerEventType.LOCKED,
    ]
    locked = events[-1]
    assert engine.used_stop_budget == Decimal("6.4500")
    assert engine.remaining_stop_budget == Decimal("3.5500")
    assert locked.quantity == Decimal("1.3225")
    assert locked.projected_stop_loss == Decimal("3.967500")
    assert engine.used_stop_budget + locked.projected_stop_loss == Decimal(
        "10.417500"
    )
    assert level.attempts == 2
    assert level.active_quantity == Decimal("0")
    assert level.state is LevelState.LOCKED


def test_projected_stop_equal_to_budget_is_allowed() -> None:
    engine = HedgeEngine(
        CreditSpread("3010", "3000", "2980", "1", "10.4175"),
        level_count=1,
        stop=PRICE_STEP_STOP,
    )
    events = engine.run(["3010", "3000", "3003", "3000", "3003", "3000"])

    assert events[-1].event_type is LedgerEventType.ENTRY
    assert events[-1].quantity == Decimal("1.3225")
    assert engine.levels[0].state is LevelState.ACTIVE
