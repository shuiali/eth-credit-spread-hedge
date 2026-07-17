"""Exact, deterministic funding allocation across open hedge lots."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from eth_credit_hedge.domain.accounting.errors import AccountingContractError
from eth_credit_hedge.domain.accounting.events import FundingAllocation
from eth_credit_hedge.domain.strategy_math.units import Money


ZERO = Decimal("0")


def allocate_funding(
    amount: Money,
    open_quantities: Mapping[str, Decimal],
) -> tuple[FundingAllocation, ...]:
    """Allocate one funding payment pro rata without losing Decimal remainder."""
    if not isinstance(amount, Money):
        raise AccountingContractError("funding amount must be Money")
    quantities = tuple(sorted(open_quantities.items()))
    if not quantities:
        raise AccountingContractError("funding requires at least one open hedge lot")
    total = ZERO
    for lot_id, quantity in quantities:
        if not lot_id.strip() or not isinstance(quantity, Decimal) or quantity <= ZERO:
            raise AccountingContractError("funding open quantities must be positive Decimals")
        total += quantity
    allocated = ZERO
    output: list[FundingAllocation] = []
    for index, (lot_id, quantity) in enumerate(quantities):
        share = (
            amount.value - allocated
            if index == len(quantities) - 1
            else amount.value * quantity / total
        )
        allocated += share
        output.append(FundingAllocation(lot_id=lot_id, amount=Money(share)))
    return tuple(output)
