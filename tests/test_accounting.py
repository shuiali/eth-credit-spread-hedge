"""Strategy-level terminal-value accounting tests."""

from decimal import Decimal

from eth_credit_hedge.config import LockPolicy
from eth_credit_hedge.core.credit_spread import CreditSpread
from eth_credit_hedge.core.hedge_engine import HedgeEngine
from eth_credit_hedge.core.ledger import LedgerEventType
from eth_credit_hedge.core.virtual_levels import LevelState


def make_engine(premium: str = "30") -> HedgeEngine:
    return HedgeEngine(
        CreditSpread("3010", "3000", "2900", "1", premium),
        level_count=5,
    )


def test_smooth_decline_keeps_terminal_value_combined_pnl_at_credit() -> None:
    result = make_engine().run_with_accounting(["3010", "2890"])
    initial = result.snapshots[0]
    final = result.snapshots[-1]

    assert initial.option_terminal_value_pnl == Decimal("30")
    assert initial.combined_terminal_value_pnl == Decimal("30")
    assert initial.incremental_pnl_since_entry == Decimal("0")
    assert final.option_terminal_value_pnl == Decimal("-70")
    assert final.realized_hedge_pnl == Decimal("100")
    assert final.open_hedge_pnl == Decimal("0")
    assert final.combined_terminal_value_pnl == Decimal("30")
    assert final.incremental_pnl_since_entry == Decimal("0")
    assert result.metrics.minimum_combined_pnl == Decimal("30")
    assert result.metrics.floor_pass is True


def test_open_short_pnl_is_accounted_separately() -> None:
    result = make_engine().run_with_accounting(["3010", "2990"])
    final = result.snapshots[-1]

    assert final.option_terminal_value_pnl == Decimal("20")
    assert final.realized_hedge_pnl == Decimal("0")
    assert final.open_hedge_pnl == Decimal("10")
    assert final.combined_terminal_value_pnl == Decimal("30")


def test_stop_accounting_tracks_debt_and_incremental_loss() -> None:
    result = make_engine().run_with_accounting(["3010", "3000", "3003"])
    final = result.snapshots[-1]

    assert final.option_terminal_value_pnl == Decimal("30")
    assert final.realized_hedge_pnl == Decimal("-3.00")
    assert final.combined_terminal_value_pnl == Decimal("27.00")
    assert final.incremental_pnl_since_entry == Decimal("-3.00")
    assert final.gross_stop_losses == Decimal("3.00")
    assert final.gross_tp_profits == Decimal("0")
    assert final.outstanding_recovery_debt == Decimal("3.00")
    assert final.remaining_premium_stop_budget == Decimal("27.00")


def test_negative_combined_pnl_is_not_clamped() -> None:
    result = make_engine("10").run_with_accounting(
        ["3010", "3000", "3003", "3000", "3003", "3000", "2890"]
    )

    assert result.snapshots[-1].combined_terminal_value_pnl == Decimal("-16.4500")
    assert result.metrics.minimum_combined_pnl == Decimal("-16.4500")
    assert result.metrics.floor_pass is False
    assert result.metrics.locked_levels == (1,)
    assert result.metrics.number_of_entries == 6
    assert result.metrics.number_of_stops == 2
    assert result.metrics.number_of_tps == 4
    assert result.metrics.outstanding_recovery_debt == Decimal("6.4500")


def test_breakeven_floor_policy_prevents_the_unhedged_locked_zone_loss() -> None:
    engine = HedgeEngine(
        CreditSpread("3010", "3000", "2900", "1", "10"),
        level_count=5,
        lock_policy=LockPolicy.BREAKEVEN_FLOOR,
    )
    result = engine.run_with_accounting(
        ["3010", "3000", "3003", "3000", "3003", "3000", "2890"]
    )

    assert LedgerEventType.FLOOR_ENTRY in [event.event_type for event in result.events]
    assert result.metrics.combined_pnl == Decimal("10.0000")
    assert result.metrics.minimum_combined_pnl == Decimal("3.5500")
    assert result.metrics.floor_pass is True
    assert result.metrics.floor_entry_count == 1
    assert result.metrics.locked_levels == ()
    assert result.metrics.outstanding_recovery_debt == Decimal("0")
    assert all(level.state is LevelState.PAID for level in result.levels)


def test_floor_hedge_exits_at_breakeven_and_can_reenter_without_more_debt() -> None:
    engine = HedgeEngine(
        CreditSpread("3010", "3000", "2980", "1", "10"),
        level_count=1,
        lock_policy=LockPolicy.BREAKEVEN_FLOOR,
    )
    result = engine.run_with_accounting(
        [
            "3010",
            "3000",
            "3003",
            "3000",
            "3003",
            "3000",
            "3000.1",
            "3000",
        ]
    )

    assert [event.event_type for event in result.events[-3:]] == [
        LedgerEventType.FLOOR_ENTRY,
        LedgerEventType.BREAKEVEN,
        LedgerEventType.FLOOR_ENTRY,
    ]
    assert result.metrics.floor_entry_count == 2
    assert result.metrics.breakeven_exit_count == 1
    assert result.metrics.premium_budget_consumed == Decimal("6.4500")
    assert result.metrics.outstanding_recovery_debt == Decimal("6.4500")
    assert result.levels[0].active_is_floor is True


def test_coarse_and_tenth_tick_paths_have_the_same_accounting_minimum() -> None:
    anchors = [
        "3010",
        "2984.8",
        "2966.4",
        "2944.7",
        "2918.3",
        "2928.1",
        "2891.3",
        "2959.2",
        "2992.9",
    ]
    expanded = _expand_in_tenths(anchors)
    coarse = make_engine().run_with_accounting(anchors)
    fine = make_engine().run_with_accounting(expanded)

    assert [
        (event.event_type, event.level_id, event.price) for event in coarse.events
    ] == [(event.event_type, event.level_id, event.price) for event in fine.events]
    assert coarse.metrics.minimum_combined_pnl == Decimal("27.00")
    assert coarse.metrics.minimum_combined_pnl == fine.metrics.minimum_combined_pnl
    assert {
        snapshot.event_sequence
        for snapshot in coarse.snapshots
        if snapshot.event_sequence is not None
    } == {event.sequence for event in coarse.events}
    assert coarse.input_prices == tuple(map(Decimal, anchors))


def _expand_in_tenths(anchors: list[str]) -> list[Decimal]:
    prices = [Decimal(anchors[0])]
    for value in anchors[1:]:
        target = Decimal(value)
        step = Decimal("0.1") if target > prices[-1] else Decimal("-0.1")
        while prices[-1] != target:
            prices.append(prices[-1] + step)
    return prices
