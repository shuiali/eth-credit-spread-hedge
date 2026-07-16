"""Expected per-unit execution costs for hedge sizing."""

from __future__ import annotations

from dataclasses import dataclass

from eth_credit_hedge.domain.strategy_math.errors import InvalidConfigurationError
from eth_credit_hedge.domain.strategy_math.units import Money, Price, Rate


@dataclass(frozen=True, slots=True)
class ExecutionCostContext:
    expected_entry_price: Price
    expected_tp_price: Price
    expected_stop_price: Price
    entry_fee_rate: Rate
    tp_fee_rate: Rate
    stop_fee_rate: Rate
    expected_entry_slippage_per_unit: Money
    expected_tp_slippage_per_unit: Money
    expected_stop_slippage_per_unit: Money
    expected_funding_to_tp_per_unit: Money
    expected_funding_to_stop_per_unit: Money
    spread_cost_entry_per_unit: Money
    spread_cost_tp_per_unit: Money
    spread_cost_stop_per_unit: Money

    def __post_init__(self) -> None:
        if self.expected_entry_price <= self.expected_tp_price:
            raise InvalidConfigurationError(
                "short-hedge entry price must exceed expected TP price"
            )
        if self.expected_stop_price <= self.expected_entry_price:
            raise InvalidConfigurationError(
                "expected stop price must exceed short-hedge entry price"
            )
        for name in (
            "expected_entry_slippage_per_unit",
            "expected_tp_slippage_per_unit",
            "expected_stop_slippage_per_unit",
            "spread_cost_entry_per_unit",
            "spread_cost_tp_per_unit",
            "spread_cost_stop_per_unit",
        ):
            if getattr(self, name).value < 0:
                raise InvalidConfigurationError(
                    f"{name.replace('_', ' ')} cannot be negative"
                )


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    gross_tp_profit_per_unit: Money
    gross_stop_loss_per_unit: Money
    entry_fee_per_unit: Money
    tp_fee_per_unit: Money
    stop_fee_per_unit: Money
    entry_slippage_per_unit: Money
    tp_slippage_per_unit: Money
    stop_slippage_per_unit: Money
    funding_to_tp_per_unit: Money
    funding_to_stop_per_unit: Money
    spread_entry_per_unit: Money
    spread_tp_per_unit: Money
    spread_stop_per_unit: Money


def calculate_cost_breakdown(costs: ExecutionCostContext) -> CostBreakdown:
    return CostBreakdown(
        gross_tp_profit_per_unit=Money(
            costs.expected_entry_price.value - costs.expected_tp_price.value
        ),
        gross_stop_loss_per_unit=Money(
            costs.expected_stop_price.value - costs.expected_entry_price.value
        ),
        entry_fee_per_unit=Money(
            costs.expected_entry_price.value * costs.entry_fee_rate.value
        ),
        tp_fee_per_unit=Money(
            costs.expected_tp_price.value * costs.tp_fee_rate.value
        ),
        stop_fee_per_unit=Money(
            costs.expected_stop_price.value * costs.stop_fee_rate.value
        ),
        entry_slippage_per_unit=costs.expected_entry_slippage_per_unit,
        tp_slippage_per_unit=costs.expected_tp_slippage_per_unit,
        stop_slippage_per_unit=costs.expected_stop_slippage_per_unit,
        funding_to_tp_per_unit=costs.expected_funding_to_tp_per_unit,
        funding_to_stop_per_unit=costs.expected_funding_to_stop_per_unit,
        spread_entry_per_unit=costs.spread_cost_entry_per_unit,
        spread_tp_per_unit=costs.spread_cost_tp_per_unit,
        spread_stop_per_unit=costs.spread_cost_stop_per_unit,
    )


__all__ = ["CostBreakdown", "ExecutionCostContext", "calculate_cost_breakdown"]
