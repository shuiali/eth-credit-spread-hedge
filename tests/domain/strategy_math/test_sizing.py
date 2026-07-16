"""Independent golden tests for cost-aware sizing and confirmed stop debt."""

from decimal import Decimal

import pytest

from eth_credit_hedge.config import StrategyCostConfig
from eth_credit_hedge.domain.strategy_math import (
    ExecutionCostContext,
    InstrumentRules,
    Money,
    NonPositiveNetProfitError,
    Price,
    Quantity,
    QuantityRoundingMode,
    Rate,
    SizingStatus,
    calculate_actual_stop_debt,
    quantize_quantity,
    size_hedge,
)


ZERO = Decimal("0")


def costs(**changes: object) -> ExecutionCostContext:
    values: dict[str, object] = {
        "expected_entry_price": Price(Decimal("3000")),
        "expected_tp_price": Price(Decimal("2990")),
        "expected_stop_price": Price(Decimal("3005")),
        "entry_fee_rate": Rate(ZERO),
        "tp_fee_rate": Rate(ZERO),
        "stop_fee_rate": Rate(ZERO),
        "expected_entry_slippage_per_unit": Money(ZERO),
        "expected_tp_slippage_per_unit": Money(ZERO),
        "expected_stop_slippage_per_unit": Money(ZERO),
        "expected_funding_to_tp_per_unit": Money(ZERO),
        "expected_funding_to_stop_per_unit": Money(ZERO),
        "spread_cost_entry_per_unit": Money(ZERO),
        "spread_cost_tp_per_unit": Money(ZERO),
        "spread_cost_stop_per_unit": Money(ZERO),
    }
    values.update(changes)
    return ExecutionCostContext(**values)  # type: ignore[arg-type]


def rules(**changes: object) -> InstrumentRules:
    values: dict[str, object] = {
        "quantity_step": Quantity(Decimal("0.001")),
        "minimum_quantity": Quantity(Decimal("0.001")),
        "maximum_quantity": Quantity(Decimal("100")),
        "maximum_notional": Money(Decimal("1000000")),
        "maximum_projected_stop_loss": Money(Decimal("1000000")),
    }
    values.update(changes)
    return InstrumentRules(**values)  # type: ignore[arg-type]


def baseline(
    context: ExecutionCostContext | None = None,
    instrument: InstrumentRules | None = None,
    *,
    budget: str = "1",
):
    return size_hedge(
        role="BASELINE",
        zone_option_loss_budget=Money(Decimal(budget)),
        confirmed_recovery_debt=Money(ZERO),
        configured_buffer=Money(ZERO),
        costs=context or costs(),
        instrument=instrument or rules(),
    )


def test_zero_costs_reproduce_ideal_baseline() -> None:
    result = baseline()

    assert result.raw_quantity.value == Decimal("0.1")  # 1 USD / 10 USD/ETH
    assert result.submitted_quantity.value == Decimal("0.1")
    assert result.net_tp_profit_per_unit.value == Decimal("10")
    assert result.net_stop_loss_per_unit.value == Decimal("5")
    assert result.expected_net_tp_profit.value == Decimal("1.0")
    assert result.projected_net_stop_loss.value == Decimal("0.5")
    assert result.fully_covered


def test_entry_and_tp_fees_increase_quantity() -> None:
    result = baseline(
        costs(
            entry_fee_rate=Rate(Decimal("0.001")),
            tp_fee_rate=Rate(Decimal("0.001")),
        )
    )

    # Net = 10 - 3.000 - 2.990 = 4.010; 1 / 4.010 = 0.249376...
    assert result.net_tp_profit_per_unit.value == Decimal("4.010")
    assert result.raw_quantity.value == Decimal("1") / Decimal("4.010")
    assert result.submitted_quantity.value == Decimal("0.250")


def test_signed_funding_changes_required_quantity_in_the_correct_direction() -> None:
    paid = baseline(costs(expected_funding_to_tp_per_unit=Money(Decimal("-1"))))
    received = baseline(costs(expected_funding_to_tp_per_unit=Money(Decimal("1"))))

    assert paid.net_tp_profit_per_unit.value == Decimal("9")
    assert paid.submitted_quantity.value == Decimal("0.112")
    assert received.net_tp_profit_per_unit.value == Decimal("11")
    assert received.submitted_quantity.value == Decimal("0.091")


def test_spread_and_slippage_increase_quantity_and_stop_costs_increase_loss() -> None:
    result = baseline(
        costs(
            entry_fee_rate=Rate(Decimal("0.001")),
            stop_fee_rate=Rate(Decimal("0.001")),
            expected_entry_slippage_per_unit=Money(Decimal("0.25")),
            expected_tp_slippage_per_unit=Money(Decimal("0.25")),
            expected_stop_slippage_per_unit=Money(Decimal("0.75")),
            spread_cost_entry_per_unit=Money(Decimal("0.10")),
            spread_cost_tp_per_unit=Money(Decimal("0.10")),
            spread_cost_stop_per_unit=Money(Decimal("0.20")),
        )
    )

    assert result.net_tp_profit_per_unit.value == Decimal("6.300")
    assert result.submitted_quantity.value == Decimal("0.159")
    # 5 + 3 entry fee + 3.005 stop fee + 0.25 + 0.75 + 0.10 + 0.20
    assert result.net_stop_loss_per_unit.value == Decimal("12.305")


def test_recovery_uses_confirmed_debt_and_buffer() -> None:
    result = size_hedge(
        role="RECOVERY",
        zone_option_loss_budget=Money(Decimal("1")),
        confirmed_recovery_debt=Money(Decimal("0.35")),
        configured_buffer=Money(Decimal("0.05")),
        costs=costs(),
        instrument=rules(),
    )

    assert result.required_budget.value == Decimal("1.40")
    assert result.raw_quantity.value == Decimal("0.14")
    assert result.submitted_quantity.value == Decimal("0.14")


def test_quantity_step_minimum_and_undercoverage_are_explicit() -> None:
    assert quantize_quantity(
        Quantity(Decimal("0.1001")), rules(), QuantityRoundingMode.CEIL
    ).value == Decimal("0.101")
    minimum = baseline(
        instrument=rules(minimum_quantity=Quantity(Decimal("0.01"))),
        budget="0.001",
    )
    floored = size_hedge(
        role="BASELINE",
        zone_option_loss_budget=Money(Decimal("1.004")),
        confirmed_recovery_debt=Money(ZERO),
        configured_buffer=Money(ZERO),
        costs=costs(),
        instrument=rules(),
        mode=QuantityRoundingMode.FLOOR,
    )

    assert minimum.submitted_quantity.value == Decimal("0.01")
    assert floored.undercoverage.value == Decimal("0.004")
    assert not floored.fully_covered


@pytest.mark.parametrize(
    "limited_rules",
    [
        rules(maximum_quantity=Quantity(Decimal("0.099"))),
        rules(maximum_notional=Money(Decimal("299"))),
        rules(maximum_projected_stop_loss=Money(Decimal("0.49"))),
    ],
)
def test_finite_risk_limits_reject_without_flooring(limited_rules: InstrumentRules) -> None:
    result = baseline(instrument=limited_rules)

    assert result.status is SizingStatus.REJECTED_BY_RISK
    assert result.submitted_quantity.value == Decimal("0.1")
    assert not result.fully_covered


def test_nonpositive_net_tp_profit_is_rejected() -> None:
    with pytest.raises(NonPositiveNetProfitError):
        baseline(costs(expected_entry_slippage_per_unit=Money(Decimal("10"))))


def test_point_one_option_is_not_assumed_to_equal_point_one_hedge() -> None:
    # A 0.1 option over a 10 USD zone has a 1 USD zone budget.
    result = baseline(
        costs(expected_entry_slippage_per_unit=Money(Decimal("1")))
    )

    assert result.submitted_quantity.value == Decimal("0.112")
    assert result.submitted_quantity.value != Decimal("0.1")


def test_actual_stop_debt_uses_fills_fees_signed_funding_and_slippage() -> None:
    result = calculate_actual_stop_debt(
        entry_price=Price(Decimal("3000")),
        stop_fill_price=Price(Decimal("3006")),
        stop_reference_price=Price(Decimal("3005")),
        quantity=Quantity(Decimal("0.1")),
        allocated_entry_fees=Money(Decimal("0.03")),
        stop_fees=Money(Decimal("0.04")),
        funding_pnl=Money(Decimal("0.02")),
    )

    assert result.price_loss.value == Decimal("0.6")
    assert result.slippage_versus_reference.value == Decimal("0.1")
    assert result.actual_realized_pnl.value == Decimal("-0.65")
    assert result.total_debt.value == Decimal("0.65")
    projected = baseline().projected_net_stop_loss.value
    assert result.total_debt.value != projected


@pytest.mark.parametrize(
    ("field", "value", "metric"),
    [
        ("entry_fee_rate", "0.001", "quantity"),
        ("tp_fee_rate", "0.001", "quantity"),
        ("expected_entry_slippage_bps", "1", "quantity"),
        ("expected_tp_slippage_bps", "1", "quantity"),
        ("expected_funding_to_tp_usd_per_eth", "1", "quantity"),
        ("spread_cost_entry_bps", "1", "quantity"),
        ("spread_cost_tp_bps", "1", "quantity"),
        ("stop_fee_rate", "0.001", "stop_loss"),
        ("expected_stop_slippage_bps", "1", "stop_loss"),
        ("expected_funding_to_stop_usd_per_eth", "1", "stop_loss"),
        ("spread_cost_stop_bps", "1", "stop_loss"),
    ],
)
def test_each_cost_field_perturbs_a_runtime_sizing_output(
    field: str,
    value: str,
    metric: str,
) -> None:
    zero_config = StrategyCostConfig()
    changed_config = StrategyCostConfig(**{field: Decimal(value)})
    zero = baseline(
        zero_config.execution_context(
            entry_price=Decimal("3000"),
            tp_price=Decimal("2990"),
            stop_price=Decimal("3005"),
        )
    )
    changed = baseline(
        changed_config.execution_context(
            entry_price=Decimal("3000"),
            tp_price=Decimal("2990"),
            stop_price=Decimal("3005"),
        )
    )

    if metric == "quantity":
        assert changed.raw_quantity != zero.raw_quantity
    else:
        assert changed.projected_net_stop_loss != zero.projected_net_stop_loss


def test_both_configured_buffers_perturb_their_runtime_roles() -> None:
    configured = StrategyCostConfig(
        baseline_buffer_usd=Decimal("0.25"),
        recovery_buffer_usd=Decimal("0.50"),
    )
    context = configured.execution_context(
        entry_price=Decimal("3000"),
        tp_price=Decimal("2990"),
        stop_price=Decimal("3005"),
    )
    baseline_result = size_hedge(
        role="BASELINE",
        zone_option_loss_budget=Money(Decimal("1")),
        confirmed_recovery_debt=Money(ZERO),
        configured_buffer=Money(configured.baseline_buffer_usd),
        costs=context,
        instrument=rules(),
    )
    recovery_result = size_hedge(
        role="RECOVERY",
        zone_option_loss_budget=Money(Decimal("1")),
        confirmed_recovery_debt=Money(Decimal("0.25")),
        configured_buffer=Money(configured.recovery_buffer_usd),
        costs=context,
        instrument=rules(),
    )

    assert baseline_result.required_budget.value == Decimal("1.25")
    assert recovery_result.required_budget.value == Decimal("1.75")
