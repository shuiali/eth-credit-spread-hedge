"""Confirmed stop-debt accounting from actual fills and costs."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from eth_credit_hedge.domain.strategy_math.units import Money, Price, Quantity


@dataclass(frozen=True, slots=True)
class ActualStopDebt:
    price_loss: Money
    allocated_entry_fees: Money
    stop_fees: Money
    funding_pnl: Money
    slippage_versus_reference: Money
    actual_realized_pnl: Money
    total_debt: Money


def calculate_actual_stop_debt(
    *,
    entry_price: Price,
    stop_fill_price: Price,
    stop_reference_price: Price,
    quantity: Quantity,
    allocated_entry_fees: Money,
    stop_fees: Money,
    funding_pnl: Money,
) -> ActualStopDebt:
    for name, amount in (
        ("allocated entry fees", allocated_entry_fees),
        ("stop fees", stop_fees),
    ):
        if amount.value < 0:
            raise ValueError(f"{name} cannot be negative")
    price_loss = Money(
        (stop_fill_price.value - entry_price.value) * quantity.value
    )
    slippage = Money(
        (stop_fill_price.value - stop_reference_price.value) * quantity.value
    )
    realized = Money(
        -price_loss.value
        - allocated_entry_fees.value
        - stop_fees.value
        + funding_pnl.value
    )
    return ActualStopDebt(
        price_loss=price_loss,
        allocated_entry_fees=allocated_entry_fees,
        stop_fees=stop_fees,
        funding_pnl=funding_pnl,
        slippage_versus_reference=slippage,
        actual_realized_pnl=realized,
        total_debt=Money(
            -realized.value if realized.value < 0 else Decimal("0")
        ),
    )


__all__ = ["ActualStopDebt", "calculate_actual_stop_debt"]
