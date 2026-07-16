"""Option valuation boundary used by authoritative spacing modes."""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from eth_credit_hedge.domain.strategy_math.contracts import (
    OptionSpreadState,
    OptionValuationContext,
    OptionValuationMode,
)
from eth_credit_hedge.domain.strategy_math.errors import UnsupportedValuationError
from eth_credit_hedge.domain.strategy_math.units import DeltaExposure, Money, Price


class OptionValuationPort(Protocol):
    def value_at_price(
        self,
        position: OptionSpreadState,
        context: OptionValuationContext,
        underlying_price: Price,
    ) -> Money: ...

    def delta_at_price(
        self,
        position: OptionSpreadState,
        context: OptionValuationContext,
        underlying_price: Price,
    ) -> DeltaExposure: ...


class ExpirationOptionValuation:
    """Terminal short-put-spread value, excluding the constant entry credit."""

    def value_at_price(
        self,
        position: OptionSpreadState,
        context: OptionValuationContext,
        underlying_price: Price,
    ) -> Money:
        self._require_expiration(context)
        short_intrinsic = max(
            position.short_put_strike.value - underlying_price.value,
            Decimal("0"),
        )
        long_intrinsic = max(
            position.long_put_strike.value - underlying_price.value,
            Decimal("0"),
        )
        liability = (
            short_intrinsic - long_intrinsic
        ) * position.option_quantity.value
        return Money(-liability)

    def delta_at_price(
        self,
        position: OptionSpreadState,
        context: OptionValuationContext,
        underlying_price: Price,
    ) -> DeltaExposure:
        self._require_expiration(context)
        if (
            position.long_put_strike.value
            < underlying_price.value
            < position.short_put_strike.value
        ):
            return DeltaExposure(position.option_quantity.value)
        return DeltaExposure(Decimal("0"))

    @staticmethod
    def _require_expiration(context: OptionValuationContext) -> None:
        if context.valuation_mode is not OptionValuationMode.EXPIRATION:
            raise UnsupportedValuationError(
                "expiration valuation requires an EXPIRATION context"
            )


__all__ = ["ExpirationOptionValuation", "OptionValuationPort"]
