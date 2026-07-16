"""Independent golden tests for explicit stop and TP geometry."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from eth_credit_hedge.config import RuntimeConfig
from eth_credit_hedge.core.credit_spread import CreditSpread
from eth_credit_hedge.core.hedge_engine import HedgeEngine
from eth_credit_hedge.domain.strategy_math import (
    EntryPercentStopConfig,
    ExpirationOptionValuation,
    InvalidConfigurationError,
    LevelSpacingEngine,
    OptionSpreadState,
    OptionValuationContext,
    OptionValuationMode,
    Price,
    PriceStepFractionStopConfig,
    PriceStepSpacingConfig,
    Quantity,
    Rate,
    StopMode,
    StopConfig,
    StrategyMathEngine,
)


NOW = datetime(2026, 7, 16, tzinfo=timezone.utc)


def spread() -> OptionSpreadState:
    return OptionSpreadState(
        short_put_strike=Price(Decimal("3000")),
        long_put_strike=Price(Decimal("2900")),
        option_quantity=Quantity(Decimal("1")),
    )


def context() -> OptionValuationContext:
    return OptionValuationContext(
        valuation_mode=OptionValuationMode.EXPIRATION,
        observed_at_utc=NOW,
        valid_until_utc=NOW,
    )


def build(step: str, stop: StopConfig):
    return StrategyMathEngine(ExpirationOptionValuation()).build_levels(
        spread(),
        context(),
        PriceStepSpacingConfig(Price(Decimal(step))),
        stop,
        as_of_utc=NOW,
    )


def test_entry_percent_exact_golden_arithmetic() -> None:
    level = build(
        "20",
        EntryPercentStopConfig(Rate(Decimal("0.0015"))),
    )[0]

    assert level.entry_price.value == Decimal("3000")
    assert level.stop_distance.value == Decimal("4.5000")
    assert level.stop_price.value == Decimal("3004.5000")
    assert level.stop_mode is StopMode.ENTRY_PERCENT


def test_price_step_fraction_exact_golden_arithmetic() -> None:
    level = build(
        "20",
        PriceStepFractionStopConfig(Rate(Decimal("0.15"))),
    )[0]

    assert level.price_distance.value == Decimal("20")
    assert level.stop_distance.value == Decimal("3.00")
    assert level.stop_price.value == Decimal("3003.00")
    assert level.stop_mode is StopMode.PRICE_STEP_FRACTION


def test_same_entry_different_steps_leave_entry_percent_stop_unchanged() -> None:
    stop = EntryPercentStopConfig(Rate(Decimal("0.0015")))
    wide = build("20", stop)[0]
    narrow = build("10", stop)[0]

    assert wide.entry_price == narrow.entry_price
    assert wide.stop_distance == narrow.stop_distance == Price(Decimal("4.5000"))


def test_same_entry_different_steps_change_price_step_fraction_stop() -> None:
    stop = PriceStepFractionStopConfig(Rate(Decimal("0.15")))
    wide = build("20", stop)[0]
    narrow = build("10", stop)[0]

    assert wide.entry_price == narrow.entry_price
    assert wide.stop_distance == Price(Decimal("3.00"))
    assert narrow.stop_distance == Price(Decimal("1.50"))


def test_tp_geometry_is_copied_from_spacing_output_including_narrow_last_level() -> None:
    spacing = PriceStepSpacingConfig(Price(Decimal("30")))
    boundaries = LevelSpacingEngine(ExpirationOptionValuation()).build_levels(
        spread(), context(), spacing, as_of_utc=NOW
    )
    levels = StrategyMathEngine(ExpirationOptionValuation()).build_levels(
        spread(),
        context(),
        spacing,
        PriceStepFractionStopConfig(Rate(Decimal("0.15"))),
        as_of_utc=NOW,
    )

    assert [level.tp_price for level in levels] == [
        level.tp_price for level in boundaries
    ]
    assert levels[-1].tp_price == Price(Decimal("2900"))
    assert levels[-1].price_distance == Price(Decimal("10"))
    assert levels[-1].stop_distance == Price(Decimal("1.50"))


def test_configured_operational_maximum_rejects_stop_above_limit() -> None:
    with pytest.raises(InvalidConfigurationError, match="operational maximum"):
        StrategyMathEngine(ExpirationOptionValuation()).build_levels(
            spread(),
            context(),
            PriceStepSpacingConfig(Price(Decimal("20"))),
            EntryPercentStopConfig(Rate(Decimal("0.0015"))),
            as_of_utc=NOW,
            maximum_stop_price=Price(Decimal("3004")),
        )


def test_runtime_stop_perturbation_changes_stops_only() -> None:
    runtime_spread = CreditSpread("3010", "3000", "2900", "1", "30")
    low_config = RuntimeConfig.from_env(
        {
            "ETH_HEDGE_STOP_MODE": "ENTRY_PERCENT",
            "ETH_HEDGE_ENTRY_STOP_RATE": "0.001",
        }
    )
    high_config = RuntimeConfig.from_env(
        {
            "ETH_HEDGE_STOP_MODE": "ENTRY_PERCENT",
            "ETH_HEDGE_ENTRY_STOP_RATE": "0.002",
        }
    )
    low = HedgeEngine(
        runtime_spread,
        5,
        stop=low_config.strategy.stop,
    )
    high = HedgeEngine(
        runtime_spread,
        5,
        stop=high_config.strategy.stop,
    )

    assert [(item.entry_price, item.tp_price) for item in low.levels] == [
        (item.entry_price, item.tp_price) for item in high.levels
    ]
    assert [item.option_budget for item in low.levels] == [
        item.option_budget for item in high.levels
    ]
    assert [item.stop_price for item in low.levels] != [
        item.stop_price for item in high.levels
    ]
