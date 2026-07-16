"""Single-level lifecycle tests."""

from decimal import Decimal

from eth_credit_hedge.core.credit_spread import CreditSpread
from eth_credit_hedge.core.hedge_engine import HedgeEngine
from eth_credit_hedge.core.ledger import LedgerEventType
from eth_credit_hedge.core.virtual_levels import LevelState
from eth_credit_hedge.domain.strategy_math import PriceStepFractionStopConfig, Rate


def make_engine() -> HedgeEngine:
    spread = CreditSpread("3010", "3000", "2980", "1", "20")
    return HedgeEngine(
        spread,
        level_count=1,
        stop=PriceStepFractionStopConfig(Rate(Decimal("0.15"))),
    )


def test_entry_then_tp_reconciles_exactly() -> None:
    engine = make_engine()
    events = engine.run(["3010", "3000", "2980"])
    level = engine.levels[0]

    assert [(event.event_type, event.price) for event in events] == [
        (LedgerEventType.ENTRY, Decimal("3000")),
        (LedgerEventType.TP, Decimal("2980")),
    ]
    assert events[0].quantity == Decimal("1")
    assert events[1].realized_pnl == Decimal("20")
    assert engine.ledger.realized_hedge_pnl == Decimal("20")
    assert level.active_quantity == Decimal("0")
    assert level.state is LevelState.PAID


def test_entry_then_stop_reconciles_exactly() -> None:
    engine = make_engine()
    events = engine.run(["3010", "3000", "3003"])
    level = engine.levels[0]

    assert [(event.event_type, event.price) for event in events] == [
        (LedgerEventType.ENTRY, Decimal("3000")),
        (LedgerEventType.STOP, Decimal("3003.00")),
    ]
    assert events[1].quantity == Decimal("1")
    assert events[1].realized_pnl == Decimal("-3.00")
    assert engine.ledger.realized_hedge_pnl == Decimal("-3.00")
    assert level.realized_stop_losses == Decimal("3.00")
    assert level.active_quantity == Decimal("0")
    assert level.state is LevelState.READY
