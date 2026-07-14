"""Distributed recovery tests."""

from decimal import Decimal

from eth_credit_hedge.config import RecoveryMode
from eth_credit_hedge.core.credit_spread import CreditSpread
from eth_credit_hedge.core.hedge_engine import HedgeEngine
from eth_credit_hedge.core.ledger import LedgerEventType


def make_engine(mode: RecoveryMode) -> HedgeEngine:
    return HedgeEngine(
        CreditSpread("3010", "3000", "2900", "1", "30"),
        level_count=5,
        recovery_mode=mode,
        recovery_tp_count=3,
    )


def test_distributed_mode_reduces_source_debt_over_three_tps() -> None:
    engine = make_engine(RecoveryMode.DISTRIBUTED)
    events = engine.run(["3010", "3000", "3004.50", "3000", "2980", "2960", "2940"])
    tps = [event for event in events if event.event_type is LedgerEventType.TP]
    entries = [event for event in events if event.event_type is LedgerEventType.ENTRY]

    assert [entry.quantity for entry in entries[:4]] == [
        Decimal("1"),
        Decimal("1.0750"),
        Decimal("1.0750"),
        Decimal("1.0750"),
    ]
    assert [tp.recovery_profit_component for tp in tps] == [
        Decimal("1.5000"),
        Decimal("1.5000"),
        Decimal("1.5000"),
    ]
    assert [tp.recovery_allocations for tp in tps] == [
        {1: Decimal("1.5000")},
        {1: Decimal("1.5000")},
        {1: Decimal("1.5000")},
    ]
    assert engine.levels[0].recovery_debt == Decimal("0")
    assert engine.levels[0].recovery_tps_remaining == 0
    assert engine.ledger.realized_hedge_pnl == Decimal("60")


def test_full_next_tp_mode_is_still_available() -> None:
    engine = make_engine(RecoveryMode.FULL_NEXT_TP)
    events = engine.run(["3010", "3000", "3004.50", "3000", "2980"])
    entries = [
        event
        for event in events
        if event.event_type is LedgerEventType.ENTRY and event.level_id == 1
    ]
    tp = next(event for event in events if event.event_type is LedgerEventType.TP)

    assert entries[-1].quantity == Decimal("1.2250")
    assert tp.recovery_profit_component == Decimal("4.5000")
    assert engine.levels[0].recovery_debt == Decimal("0")


def test_distributed_mode_uses_oldest_source_and_one_claim_per_tp() -> None:
    engine = make_engine(RecoveryMode.DISTRIBUTED)
    events = engine.run(
        [
            "3010",
            "3000",
            "3004.5",
            "3000",
            "2980",
            "2984.47",
            "2980",
            "2960",
            "2940",
            "2920",
            "2900",
        ]
    )
    allocations = [
        event.recovery_allocations
        for event in events
        if event.event_type is LedgerEventType.TP
    ]

    assert allocations == [
        {1: Decimal("1.5000")},
        {1: Decimal("1.5000")},
        {1: Decimal("1.5000")},
        {2: Decimal("1.60175000")},
        {2: Decimal("1.60175000")},
    ]
    assert all(len(allocation) <= 1 for allocation in allocations)
    assert engine.levels[0].recovery_debt == Decimal("0")
    assert engine.levels[1].recovery_debt == Decimal("1.60175000")
