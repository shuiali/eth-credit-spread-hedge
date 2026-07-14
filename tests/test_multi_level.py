"""Complete credit-spread level tests."""

from decimal import Decimal

from core.credit_spread import CreditSpread
from core.hedge_engine import HedgeEngine
from core.ledger import LedgerEventType
from core.virtual_levels import LevelState


def make_engine() -> HedgeEngine:
    return HedgeEngine(
        CreditSpread("3010", "3000", "2900", "1", "30"),
        level_count=5,
    )


def test_required_multi_level_handoff_path() -> None:
    engine = make_engine()
    events = engine.run(["3010", "3000", "2980", "2960"])

    assert [
        (event.event_type, event.level_id, event.price) for event in events
    ] == [
        (LedgerEventType.ENTRY, 1, Decimal("3000")),
        (LedgerEventType.TP, 1, Decimal("2980")),
        (LedgerEventType.ENTRY, 2, Decimal("2980")),
        (LedgerEventType.TP, 2, Decimal("2960")),
        (LedgerEventType.ENTRY, 3, Decimal("2960")),
    ]
    assert [level.state for level in engine.levels[:3]] == [
        LevelState.PAID,
        LevelState.PAID,
        LevelState.ACTIVE,
    ]


def test_single_segment_decline_hedges_the_entire_spread_loss() -> None:
    engine = make_engine()
    events = engine.run(["3010", "2890"])

    assert [event.event_type for event in events] == [
        LedgerEventType.ENTRY,
        LedgerEventType.TP,
        LedgerEventType.ENTRY,
        LedgerEventType.TP,
        LedgerEventType.ENTRY,
        LedgerEventType.TP,
        LedgerEventType.ENTRY,
        LedgerEventType.TP,
        LedgerEventType.ENTRY,
        LedgerEventType.TP,
    ]
    assert engine.ledger.gross_tp_profits == Decimal("100")
    assert engine.ledger.realized_hedge_pnl == Decimal("100")
    assert all(level.state is LevelState.PAID for level in engine.levels)
    assert all(level.attempts == 1 for level in engine.levels)
    assert not any(level.tp_price < engine.spread.long_put_strike for level in engine.levels)


def test_recovery_debt_does_not_change_another_levels_quantity() -> None:
    engine = make_engine()
    events = engine.run(["3010", "3000", "3004.50", "2950"])
    entries = [event for event in events if event.event_type is LedgerEventType.ENTRY]

    assert [(event.level_id, event.quantity) for event in entries[:3]] == [
        (1, Decimal("1")),
        (1, Decimal("1.2250")),
        (2, Decimal("1")),
    ]
    assert engine.levels[0].stop_loss_history == [Decimal("4.5000")]
    assert engine.levels[1].stop_loss_history == []
    assert engine.levels[1].recovery_debt == Decimal("0")
