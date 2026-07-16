"""Cost-aware baseline and confirmed-debt recovery sizing."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal

from eth_credit_hedge.domain.strategy_math.contracts import QuantityRoundingMode
from eth_credit_hedge.domain.strategy_math.costs import (
    CostBreakdown,
    ExecutionCostContext,
    calculate_cost_breakdown,
)
from eth_credit_hedge.domain.strategy_math.errors import NonPositiveNetProfitError
from eth_credit_hedge.domain.strategy_math.quantization import (
    InstrumentRules,
    quantize_quantity,
)
from eth_credit_hedge.domain.strategy_math.units import Money, Quantity
from eth_credit_hedge.domain.strategy_math.units import Price


class SizingStatus(str, Enum):
    APPROVED = "APPROVED"
    REJECTED_BY_RISK = "REJECTED_BY_RISK"


@dataclass(frozen=True, slots=True)
class SizingResult:
    role: Literal["BASELINE", "RECOVERY"]
    raw_quantity: Quantity
    submitted_quantity: Quantity
    net_tp_profit_per_unit: Money
    net_stop_loss_per_unit: Money
    expected_net_tp_profit: Money
    projected_net_stop_loss: Money
    required_budget: Money
    overcoverage: Money
    undercoverage: Money
    fully_covered: bool
    quantization_mode: QuantityRoundingMode
    cost_breakdown: CostBreakdown
    status: SizingStatus


def zero_cost_directional_profit_and_loss_per_unit(
    *,
    entry_price: Price,
    tp_price: Price,
    stop_price: Price,
    side: Literal["Buy", "Sell"],
) -> tuple[Money, Money]:
    if side == "Sell":
        profit = entry_price.value - tp_price.value
        loss = stop_price.value - entry_price.value
    else:
        profit = tp_price.value - entry_price.value
        loss = entry_price.value - stop_price.value
    return Money(profit), Money(loss)


def net_profit_and_loss_per_unit(
    costs: ExecutionCostContext,
) -> tuple[Money, Money, CostBreakdown]:
    breakdown = calculate_cost_breakdown(costs)
    net_tp = Money(
        breakdown.gross_tp_profit_per_unit.value
        - breakdown.entry_fee_per_unit.value
        - breakdown.tp_fee_per_unit.value
        - breakdown.entry_slippage_per_unit.value
        - breakdown.tp_slippage_per_unit.value
        - breakdown.spread_entry_per_unit.value
        - breakdown.spread_tp_per_unit.value
        + breakdown.funding_to_tp_per_unit.value
    )
    net_stop = Money(
        breakdown.gross_stop_loss_per_unit.value
        + breakdown.entry_fee_per_unit.value
        + breakdown.stop_fee_per_unit.value
        + breakdown.entry_slippage_per_unit.value
        + breakdown.stop_slippage_per_unit.value
        + breakdown.spread_entry_per_unit.value
        + breakdown.spread_stop_per_unit.value
        - breakdown.funding_to_stop_per_unit.value
    )
    if net_tp.value <= 0:
        raise NonPositiveNetProfitError(
            "expected net TP profit per unit must be positive"
        )
    return net_tp, net_stop, breakdown


def size_hedge(
    *,
    role: Literal["BASELINE", "RECOVERY"],
    zone_option_loss_budget: Money,
    confirmed_recovery_debt: Money,
    configured_buffer: Money,
    costs: ExecutionCostContext,
    instrument: InstrumentRules,
    mode: QuantityRoundingMode = QuantityRoundingMode.CEIL,
) -> SizingResult:
    for name, value in (
        ("zone option-loss budget", zone_option_loss_budget),
        ("confirmed recovery debt", confirmed_recovery_debt),
        ("configured buffer", configured_buffer),
    ):
        if value.value < 0:
            raise ValueError(f"{name} cannot be negative")
    if role == "BASELINE" and confirmed_recovery_debt.value != 0:
        raise ValueError("baseline sizing cannot include recovery debt")

    net_tp, net_stop, breakdown = net_profit_and_loss_per_unit(costs)
    required = Money(
        zone_option_loss_budget.value
        + confirmed_recovery_debt.value
        + configured_buffer.value
    )
    raw = Quantity(required.value / net_tp.value)
    submitted = quantize_quantity(raw, instrument, mode)
    expected_profit = Money(submitted.value * net_tp.value)
    projected_loss = Money(submitted.value * net_stop.value)
    difference = expected_profit.value - required.value
    zero = required.value - required.value
    overcoverage = Money(difference if difference > zero else zero)
    undercoverage = Money(-difference if difference < zero else zero)
    notional = submitted.value * costs.expected_entry_price.value
    rejected = (
        submitted > instrument.maximum_quantity
        or notional > instrument.maximum_notional.value
        or projected_loss.value > instrument.maximum_projected_stop_loss.value
    )
    status = (
        SizingStatus.REJECTED_BY_RISK if rejected else SizingStatus.APPROVED
    )
    return SizingResult(
        role=role,
        raw_quantity=raw,
        submitted_quantity=submitted,
        net_tp_profit_per_unit=net_tp,
        net_stop_loss_per_unit=net_stop,
        expected_net_tp_profit=expected_profit,
        projected_net_stop_loss=projected_loss,
        required_budget=required,
        overcoverage=overcoverage,
        undercoverage=undercoverage,
        fully_covered=undercoverage.value == 0 and not rejected,
        quantization_mode=QuantityRoundingMode.parse(mode),
        cost_breakdown=breakdown,
        status=status,
    )


__all__ = [
    "SizingResult",
    "SizingStatus",
    "net_profit_and_loss_per_unit",
    "size_hedge",
    "zero_cost_directional_profit_and_loss_per_unit",
]
