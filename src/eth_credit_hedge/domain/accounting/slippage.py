"""Reference-price slippage attribution that never changes actual-fill P&L."""

from __future__ import annotations

from decimal import Decimal

from eth_credit_hedge.domain.accounting.errors import AccountingContractError
from eth_credit_hedge.domain.accounting.fills import Side
from eth_credit_hedge.domain.strategy_math.units import Money, Price, Quantity


ZERO = Decimal("0")


def adverse_slippage(
    *,
    side: Side,
    actual_price: Price,
    reference_price: Price,
    quantity: Quantity,
) -> Money:
    """Return adverse USD attribution only; favorable execution is zero."""
    if not all(isinstance(value, (Price, Quantity)) for value in (actual_price, reference_price, quantity)):
        raise AccountingContractError("slippage requires exact price and quantity units")
    adverse_per_unit = (
        reference_price.value - actual_price.value
        if side is Side.SELL
        else actual_price.value - reference_price.value
    )
    return Money(max(adverse_per_unit, ZERO) * quantity.value)
