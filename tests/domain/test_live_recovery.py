"""Confirmed-debt-only FULL_NEXT_TP live recovery tests."""

from dataclasses import replace
from decimal import Decimal

from eth_credit_hedge.core.virtual_levels import HedgeLevel
from eth_credit_hedge.domain.instruments import (
    InstrumentSpec,
    LotSizeFilter,
    PriceFilter,
)
from eth_credit_hedge.domain.live_recovery import (
    LockedLevelAction,
    RecoveryDebtState,
    SameLevelRecoveryPlanner,
    add_confirmed_stop_debt,
    allocate_confirmed_debt,
    settle_recovery_take_profit,
)
from eth_credit_hedge.domain.risk import RiskEngine, RiskLimits, RiskState


def level() -> HedgeLevel:
    return HedgeLevel(
        level_id=1,
        entry_price=Decimal("3100"),
        tp_price=Decimal("3000"),
        stop_price=Decimal("3104.65"),
        option_budget=Decimal("1"),
    )


def instrument() -> InstrumentSpec:
    return InstrumentSpec(
        symbol="ETHUSDT",
        category="linear",
        status="Trading",
        base_coin="ETH",
        quote_coin="USDT",
        settle_coin="USDT",
        price_filter=PriceFilter(
            tick_size=Decimal("0.1"),
            min_price=Decimal("10"),
            max_price=Decimal("1000000"),
        ),
        lot_size_filter=LotSizeFilter(
            qty_step=Decimal("0.001"),
            min_order_qty=Decimal("0.001"),
            max_order_qty=Decimal("100"),
            max_market_order_qty=Decimal("50"),
            min_notional=Decimal("5"),
        ),
        contract_multiplier=Decimal("1"),
        delivery_time_utc=None,
    )


def limits() -> RiskLimits:
    return RiskLimits(
        maximum_perp_quantity=Decimal("1"),
        maximum_perp_notional=Decimal("5000"),
        maximum_margin_usage=Decimal("0.5"),
        minimum_liquidation_distance=Decimal("0.1"),
        maximum_recovery_debt=Decimal("10"),
        maximum_projected_stop_loss=Decimal("10"),
        maximum_realized_cycle_loss=Decimal("20"),
        maximum_daily_realized_loss=Decimal("50"),
        maximum_entries_per_level=3,
        maximum_active_levels=3,
        maximum_order_requests_per_minute=10,
        maximum_reconciliation_failures=3,
    )


def risk_state() -> RiskState:
    return RiskState(
        current_perp_quantity=Decimal("0"),
        current_perp_notional=Decimal("0"),
        post_trade_margin_usage=Decimal("0.1"),
        post_trade_liquidation_distance=Decimal("0.5"),
        confirmed_recovery_debt=Decimal("0.0338"),
        realized_cycle_loss=Decimal("0.0338"),
        daily_realized_loss=Decimal("0.0338"),
        entries_for_level=1,
        active_levels=0,
        order_requests_last_minute=1,
        consecutive_reconciliation_failures=0,
        market_data_fresh=True,
        reconciliation_succeeded=True,
    )


def debt() -> RecoveryDebtState:
    return RecoveryDebtState(
        projected_debt=Decimal("99"),
        confirmed_debt=Decimal("0.0338"),
        allocated_debt=Decimal("0"),
        remaining_debt=Decimal("0.0338"),
    )


def test_recovery_uses_confirmed_not_projected_debt_and_rounds_up() -> None:
    plan = SameLevelRecoveryPlanner(RiskEngine()).plan(
        level(),
        debt(),
        instrument(),
        risk_state(),
        limits(),
    )

    assert plan.approved
    assert plan.raw_desired_quantity == Decimal("0.010338")
    assert plan.quantity == Decimal("0.011")
    assert plan.expected_take_profit == Decimal("1.1")
    assert plan.allocated_debt == Decimal("0.0338")
    assert plan.locked_action is None


def test_recovery_risk_rejection_is_explicit_and_allocates_nothing() -> None:
    rejected_limits = replace(limits(), maximum_perp_quantity=Decimal("0.010"))

    plan = SameLevelRecoveryPlanner(RiskEngine()).plan(
        level(),
        debt(),
        instrument(),
        risk_state(),
        rejected_limits,
    )

    assert not plan.approved
    assert plan.quantity is None
    assert plan.allocated_debt == Decimal("0")
    assert plan.expected_take_profit == Decimal("0")
    assert plan.locked_action is LockedLevelAction.CLOSE_OPTION_STRATEGY
    assert "maximum perpetual quantity exceeded" in plan.reasons


def test_global_risk_debt_may_exceed_but_not_underreport_local_debt() -> None:
    global_state = replace(
        risk_state(),
        confirmed_recovery_debt=Decimal("0.5"),
    )

    plan = SameLevelRecoveryPlanner(RiskEngine()).plan(
        level(),
        debt(),
        instrument(),
        global_state,
        limits(),
    )

    assert plan.raw_desired_quantity == Decimal("0.010338")


def test_actual_tp_profit_reduces_only_explicitly_allocated_debt() -> None:
    allocated = allocate_confirmed_debt(debt())

    partially_paid = settle_recovery_take_profit(
        allocated,
        realized_take_profit=Decimal("1.02"),
        zone_budget=Decimal("1"),
    )
    fully_paid = settle_recovery_take_profit(
        allocate_confirmed_debt(partially_paid),
        realized_take_profit=Decimal("1.02"),
        zone_budget=Decimal("1"),
    )

    assert partially_paid.confirmed_debt == Decimal("0.0138")
    assert partially_paid.allocated_debt == Decimal("0")
    assert partially_paid.remaining_debt == Decimal("0.0138")
    assert fully_paid.confirmed_debt == Decimal("0")
    assert fully_paid.remaining_debt == Decimal("0")


def test_tp_below_zone_budget_cannot_reduce_debt() -> None:
    allocated = allocate_confirmed_debt(debt())

    unchanged = settle_recovery_take_profit(
        allocated,
        realized_take_profit=Decimal("0.99"),
        zone_budget=Decimal("1"),
    )

    assert unchanged.confirmed_debt == debt().confirmed_debt
    assert unchanged.allocated_debt == Decimal("0")
    assert unchanged.remaining_debt == debt().confirmed_debt


def test_confirmed_stop_loss_adds_debt_without_using_projection() -> None:
    initial = RecoveryDebtState.empty(projected_debt=Decimal("5"))

    stopped = add_confirmed_stop_debt(initial, Decimal("0.0338"))

    assert stopped.projected_debt == Decimal("5")
    assert stopped.confirmed_debt == Decimal("0.0338")
    assert stopped.remaining_debt == Decimal("0.0338")
