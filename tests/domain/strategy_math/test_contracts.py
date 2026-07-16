"""Immutable strategy-math configuration and result contracts."""

from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from eth_credit_hedge.domain.strategy_math import (
    CoverageResult,
    DeltaExposure,
    EntryPercentStopConfig,
    EqualOptionLossSpacingConfig,
    LevelCountSpacingConfig,
    LevelMath,
    LevelSpacingMode,
    Money,
    OptionValuationMode,
    Price,
    PriceStepFractionStopConfig,
    PriceStepSpacingConfig,
    QuantityRoundingMode,
    Rate,
    StopMode,
)


@pytest.mark.parametrize(
    ("enum_type", "raw", "expected"),
    [
        (LevelSpacingMode, "price_step", LevelSpacingMode.PRICE_STEP),
        (StopMode, "ENTRY_PERCENT", StopMode.ENTRY_PERCENT),
        (
            OptionValuationMode,
            "mark_model",
            OptionValuationMode.MARK_MODEL,
        ),
        (QuantityRoundingMode, "nearest", QuantityRoundingMode.NEAREST),
    ],
)
def test_enum_parsing_is_explicit_and_case_normalized(
    enum_type: type[LevelSpacingMode]
    | type[StopMode]
    | type[OptionValuationMode]
    | type[QuantityRoundingMode],
    raw: str,
    expected: object,
) -> None:
    assert enum_type.parse(raw) is expected


def test_spacing_and_stop_configs_are_immutable_and_declare_modes() -> None:
    price_spacing = PriceStepSpacingConfig(Price(Decimal("20")))
    count_spacing = LevelCountSpacingConfig(5)
    loss_spacing = EqualOptionLossSpacingConfig(
        Money(Decimal("2")),
        OptionValuationMode.MARK_MODEL,
    )
    entry_stop = EntryPercentStopConfig(Rate(Decimal("0.0015")))
    step_stop = PriceStepFractionStopConfig(Rate(Decimal("0.15")))

    assert price_spacing.mode is LevelSpacingMode.PRICE_STEP
    assert count_spacing.mode is LevelSpacingMode.LEVEL_COUNT
    assert loss_spacing.mode is LevelSpacingMode.EQUAL_OPTION_LOSS
    assert entry_stop.mode is StopMode.ENTRY_PERCENT
    assert step_stop.mode is StopMode.PRICE_STEP_FRACTION
    with pytest.raises(FrozenInstanceError):
        count_spacing.level_count = 10  # type: ignore[misc]


def test_level_result_serializes_units_and_modes_without_float_conversion() -> None:
    result = LevelMath(
        level_id=1,
        entry_price=Price(Decimal("3000.10")),
        tp_price=Price(Decimal("2980.10")),
        price_distance=Price(Decimal("20.00")),
        target_delta=DeltaExposure(Decimal("-0.25")),
        entry_option_value=Money(Decimal("10.50")),
        tp_option_value=Money(Decimal("8.50")),
        zone_option_loss_budget=Money(Decimal("2.00")),
        stop_price=Price(Decimal("3004.600")),
        stop_distance=Price(Decimal("4.500")),
        spacing_mode=LevelSpacingMode.DELTA_STEP,
        stop_mode=StopMode.ENTRY_PERCENT,
        valuation_mode=OptionValuationMode.MARK_MODEL,
    )

    assert result.to_dict() == {
        "level_id": 1,
        "entry_price": "3000.10",
        "tp_price": "2980.10",
        "price_distance": "20.00",
        "target_delta": "-0.25",
        "entry_option_value": "10.50",
        "tp_option_value": "8.50",
        "zone_option_loss_budget": "2.00",
        "stop_price": "3004.600",
        "stop_distance": "4.500",
        "spacing_mode": "DELTA_STEP",
        "stop_mode": "ENTRY_PERCENT",
        "valuation_mode": "MARK_MODEL",
    }


def test_coverage_result_serializes_explicit_rounding_undercoverage() -> None:
    coverage = CoverageResult(
        required_budget=Money(Decimal("1.00")),
        expected_net_profit=Money(Decimal("0.97")),
        overcoverage=Money(Decimal("0")),
        undercoverage=Money(Decimal("0.03")),
        fully_covered=False,
    )

    assert coverage.to_dict() == {
        "required_budget": "1.00",
        "expected_net_profit": "0.97",
        "overcoverage": "0",
        "undercoverage": "0.03",
        "fully_covered": False,
    }
