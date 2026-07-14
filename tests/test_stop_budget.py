"""Premium stop-budget behavior and its non-guaranteed floor."""

from decimal import Decimal

from eth_credit_hedge.core.credit_spread import CreditSpread
from eth_credit_hedge.core.hedge_engine import HedgeEngine
from eth_credit_hedge.core.virtual_levels import LevelState


def test_locked_level_can_still_finish_below_zero_at_max_option_loss() -> None:
    result = HedgeEngine(
        CreditSpread("3010", "3000", "2980", "1", "15"),
        level_count=1,
    ).run_with_accounting(["3010", "3000", "3004.5", "3000", "3004.5", "3000", "2980"])

    assert result.metrics.premium_budget_consumed == Decimal("10.01250000")
    assert result.metrics.combined_pnl == Decimal("-15.01250000")
    assert result.metrics.floor_pass is False
    assert result.levels[0].state is LevelState.LOCKED
