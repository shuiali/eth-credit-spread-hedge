"""Exact unit-wrapper behavior."""

from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from eth_credit_hedge.domain.strategy_math import (
    DeltaExposure,
    InvalidUnitsError,
    Money,
    Price,
    Quantity,
    Rate,
    Seconds,
    Volatility,
)


def test_unit_arithmetic_preserves_type_and_decimal_exactness() -> None:
    total = Price(Decimal("0.1")) + Price(Decimal("0.2"))
    difference = Money(Decimal("1.00")) - Money(Decimal("1.25"))

    assert total == Price(Decimal("0.3"))
    assert isinstance(total, Price)
    assert difference == Money(Decimal("-0.25"))
    assert total.as_decimal() == Decimal("0.3")


def test_mixed_unit_arithmetic_is_rejected_with_both_units_named() -> None:
    with pytest.raises(InvalidUnitsError, match="price.*money"):
        Price(Decimal("3000")) + Money(Decimal("1"))  # type: ignore[operator]


@pytest.mark.parametrize(
    ("factory", "value", "message"),
    [
        (Price, Decimal("0"), "price.*positive"),
        (Quantity, Decimal("-0.1"), "quantity.*positive"),
        (Rate, Decimal("-0.1"), "rate.*negative"),
        (Volatility, Decimal("-0.1"), "volatility.*negative"),
        (Seconds, Decimal("-1"), "seconds.*negative"),
        (Money, Decimal("NaN"), "money.*finite"),
    ],
)
def test_units_reject_values_invalid_for_their_declared_dimension(
    factory: type[Price]
    | type[Quantity]
    | type[Rate]
    | type[Volatility]
    | type[Seconds]
    | type[Money],
    value: Decimal,
    message: str,
) -> None:
    with pytest.raises(InvalidUnitsError, match=message):
        factory(value)


def test_units_require_decimal_inputs_and_are_immutable() -> None:
    with pytest.raises(InvalidUnitsError, match="Decimal"):
        Price("3000")  # type: ignore[arg-type]

    price = Price(Decimal("3000"))
    with pytest.raises(FrozenInstanceError):
        price.value = Decimal("3001")  # type: ignore[misc]


def test_signed_delta_exposure_and_money_are_valid_contract_values() -> None:
    assert DeltaExposure(Decimal("-0.25")).value == Decimal("-0.25")
    assert Money(Decimal("-12.50")).value == Decimal("-12.50")
