"""Boundary-memory and experimental floor-policy regressions."""

from decimal import Decimal

from eth_credit_hedge.config import LockPolicy
from eth_credit_hedge.core.credit_spread import CreditSpread
from eth_credit_hedge.core.hedge_engine import HedgeEngine
from eth_credit_hedge.core.ledger import LedgerEventType


def make_engine() -> HedgeEngine:
    return HedgeEngine(
        CreditSpread("3010", "3000", "2980", "1", "10"),
        level_count=1,
        lock_policy=LockPolicy.BREAKEVEN_FLOOR,
    )


def test_start_exactly_at_entry_then_fall_opens_the_hedge() -> None:
    result = HedgeEngine(
        CreditSpread("3010", "3000", "2980", "1", "15"),
        level_count=1,
    ).run_with_accounting(["3000", "2990"])

    assert [event.event_type for event in result.events] == [LedgerEventType.ENTRY]
    assert result.events[0].price == Decimal("3000")
    assert result.levels[0].active_quantity == Decimal("1")


def test_breakeven_at_exact_entry_then_immediate_fall_reenters() -> None:
    result = make_engine().run_with_accounting(
        [
            "3010",
            "3000",
            "3003",
            "3000",
            "3003",
            "3000",
            "2999",
            "3000",
            "2999",
        ]
    )

    assert [event.event_type for event in result.events[-3:]] == [
        LedgerEventType.FLOOR_ENTRY,
        LedgerEventType.BREAKEVEN,
        LedgerEventType.FLOOR_ENTRY,
    ]


def test_floor_hedge_repeated_entry_oscillation_adds_no_stop_debt() -> None:
    result = make_engine().run_with_accounting(
        [
            "3010",
            "3000",
            "3003",
            "3000",
            "3003",
            "3000",
            "2999",
            "3000",
            "2999",
            "3000",
            "2999",
        ]
    )

    assert result.metrics.floor_entry_count == 3
    assert result.metrics.breakeven_exit_count == 2
    assert result.metrics.premium_budget_consumed == Decimal("6.4500")
    assert result.metrics.outstanding_recovery_debt == Decimal("6.4500")
