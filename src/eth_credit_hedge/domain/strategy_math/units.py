"""Immutable Decimal-backed units used by authoritative strategy math."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from functools import total_ordering
from typing import ClassVar, Self

from eth_credit_hedge.domain.strategy_math.errors import InvalidUnitsError


@total_ordering
@dataclass(frozen=True, slots=True)
class _DecimalUnit:
    """Common exact behavior for a value with one declared unit."""

    value: Decimal

    _unit_name: ClassVar[str] = "value"
    _positive: ClassVar[bool] = False
    _nonnegative: ClassVar[bool] = False

    def __post_init__(self) -> None:
        if not isinstance(self.value, Decimal):
            raise InvalidUnitsError(
                f"{self._unit_name} must be provided as Decimal, not "
                f"{type(self.value).__name__}"
            )
        if not self.value.is_finite():
            raise InvalidUnitsError(f"{self._unit_name} must be finite")
        if self._positive and self.value <= 0:
            raise InvalidUnitsError(f"{self._unit_name} must be positive")
        if self._nonnegative and self.value < 0:
            raise InvalidUnitsError(f"{self._unit_name} cannot be negative")

    def __add__(self, other: object) -> Self:
        compatible = self._compatible(other, "add")
        return type(self)(self.value + compatible.value)

    def __sub__(self, other: object) -> Self:
        compatible = self._compatible(other, "subtract")
        return type(self)(self.value - compatible.value)

    def __eq__(self, other: object) -> bool:
        if type(self) is not type(other):
            return False
        return self.value == other.value

    def __lt__(self, other: object) -> bool:
        compatible = self._compatible(other, "compare")
        return self.value < compatible.value

    def _compatible(self, other: object, operation: str) -> _DecimalUnit:
        if type(self) is not type(other):
            other_name = (
                other._unit_name
                if isinstance(other, _DecimalUnit)
                else type(other).__name__
            )
            raise InvalidUnitsError(
                f"cannot {operation} {self._unit_name} and {other_name}"
            )
        return other

    def as_decimal(self) -> Decimal:
        """Return the exact Decimal for an explicit formula boundary."""
        return self.value

    def __str__(self) -> str:
        return str(self.value)


class Price(_DecimalUnit):
    """USD per ETH."""

    _unit_name = "price (USD/ETH)"
    _positive = True


class Quantity(_DecimalUnit):
    """ETH."""

    _unit_name = "quantity (ETH)"
    _positive = True


class Money(_DecimalUnit):
    """USD; signed values are allowed for P&L."""

    _unit_name = "money (USD)"


class Rate(_DecimalUnit):
    """Dimensionless decimal rate."""

    _unit_name = "rate (dimensionless)"
    _nonnegative = True


class DeltaExposure(_DecimalUnit):
    """ETH-equivalent option delta exposure; signed values are allowed."""

    _unit_name = "delta exposure (ETH-equivalent)"


class Volatility(_DecimalUnit):
    """Annualized decimal volatility."""

    _unit_name = "volatility (annualized decimal)"
    _nonnegative = True


class Seconds(_DecimalUnit):
    """Elapsed seconds."""

    _unit_name = "seconds"
    _nonnegative = True


__all__ = [
    "DeltaExposure",
    "Money",
    "Price",
    "Quantity",
    "Rate",
    "Seconds",
    "Volatility",
]
