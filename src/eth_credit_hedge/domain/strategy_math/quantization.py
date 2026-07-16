"""Coverage-aware hedge quantity quantization."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP

from eth_credit_hedge.domain.strategy_math.contracts import QuantityRoundingMode
from eth_credit_hedge.domain.strategy_math.errors import InvalidConfigurationError
from eth_credit_hedge.domain.strategy_math.units import Money, Quantity


@dataclass(frozen=True, slots=True)
class InstrumentRules:
    quantity_step: Quantity
    minimum_quantity: Quantity
    maximum_quantity: Quantity
    maximum_notional: Money
    maximum_projected_stop_loss: Money

    def __post_init__(self) -> None:
        if self.minimum_quantity > self.maximum_quantity:
            raise InvalidConfigurationError(
                "minimum quantity cannot exceed maximum quantity"
            )
        if self.maximum_notional.value <= 0:
            raise InvalidConfigurationError("maximum notional must be positive")
        if self.maximum_projected_stop_loss.value <= 0:
            raise InvalidConfigurationError(
                "maximum projected stop loss must be positive"
            )

    @classmethod
    def exact(cls) -> InstrumentRules:
        """Finite rules for deterministic arithmetic without an exchange adapter."""
        return cls(
            quantity_step=Quantity(Decimal("1e-18")),
            minimum_quantity=Quantity(Decimal("1e-18")),
            maximum_quantity=Quantity(Decimal("1e30")),
            maximum_notional=Money(Decimal("1e50")),
            maximum_projected_stop_loss=Money(Decimal("1e50")),
        )


def quantize_quantity(
    raw_quantity: Quantity,
    rules: InstrumentRules,
    mode: QuantityRoundingMode,
) -> Quantity:
    selected = QuantityRoundingMode.parse(mode)
    rounding = {
        QuantityRoundingMode.CEIL: ROUND_CEILING,
        QuantityRoundingMode.FLOOR: ROUND_FLOOR,
        QuantityRoundingMode.NEAREST: ROUND_HALF_UP,
    }[selected]
    units = (raw_quantity.value / rules.quantity_step.value).to_integral_value(
        rounding=rounding
    )
    quantized = units * rules.quantity_step.value
    return Quantity(max(quantized, rules.minimum_quantity.value))


__all__ = ["InstrumentRules", "quantize_quantity"]
