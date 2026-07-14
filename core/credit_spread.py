"""ETH put-credit-spread terminal payoff model."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TypeAlias


DecimalLike: TypeAlias = Decimal | int | str | float
ZERO = Decimal("0")


def to_decimal(value: DecimalLike) -> Decimal:
    """Convert external numeric inputs without preserving float binary noise."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


@dataclass(frozen=True, slots=True)
class CreditSpread:
    """Same-expiry ETH put credit spread evaluated at terminal value."""

    spot: DecimalLike
    short_put_strike: DecimalLike
    long_put_strike: DecimalLike
    option_quantity: DecimalLike
    premium_credit: DecimalLike

    def __post_init__(self) -> None:
        for field_name in (
            "spot",
            "short_put_strike",
            "long_put_strike",
            "option_quantity",
            "premium_credit",
        ):
            object.__setattr__(self, field_name, to_decimal(getattr(self, field_name)))

        if self.spot <= ZERO:
            raise ValueError("spot must be positive")
        if self.short_put_strike <= self.long_put_strike:
            raise ValueError("short put strike must be above long put strike")
        if self.long_put_strike <= ZERO:
            raise ValueError("long put strike must be positive")
        if self.option_quantity <= ZERO:
            raise ValueError("option quantity must be positive")
        if self.premium_credit < ZERO:
            raise ValueError("premium credit cannot be negative")
        maximum_valid_credit = (
            self.short_put_strike - self.long_put_strike
        ) * self.option_quantity
        if self.premium_credit > maximum_valid_credit:
            raise ValueError("premium credit cannot exceed total spread width")

    def expiry_pnl(self, price: DecimalLike) -> Decimal:
        """Return signed terminal-value P&L at ``price``."""
        terminal_price = to_decimal(price)
        short_intrinsic = max(self.short_put_strike - terminal_price, ZERO)
        long_intrinsic = max(self.long_put_strike - terminal_price, ZERO)
        return (
            self.premium_credit
            - self.option_quantity * short_intrinsic
            + self.option_quantity * long_intrinsic
        )

    def max_profit(self) -> Decimal:
        """Return the premium retained above the short strike."""
        return self.premium_credit

    def max_loss(self) -> Decimal:
        """Return the positive maximum-loss magnitude."""
        spread_width = self.short_put_strike - self.long_put_strike
        return self.option_quantity * spread_width - self.premium_credit

    def loss_region(self) -> tuple[Decimal, Decimal]:
        """Return the lower and upper boundaries of the linear loss region."""
        return self.long_put_strike, self.short_put_strike

    def loss_slope(self, price: DecimalLike) -> Decimal:
        """Return d(P&L)/d(price), excluding the non-differentiable strikes."""
        terminal_price = to_decimal(price)
        if self.long_put_strike < terminal_price < self.short_put_strike:
            return self.option_quantity
        return ZERO
