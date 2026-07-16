"""Virtual-level generation tests."""

from decimal import Decimal

from eth_credit_hedge.core.credit_spread import CreditSpread
from eth_credit_hedge.core.virtual_levels import (
    LegacyPriceStepLevelGenerator,
    LevelState,
    build_virtual_levels,
)
from eth_credit_hedge.domain.strategy_math import PriceStepFractionStopConfig, Rate


def make_spread() -> CreditSpread:
    return CreditSpread("3010", "3000", "2900", "1", "30")


def test_five_levels_match_the_locked_specification() -> None:
    levels = build_virtual_levels(make_spread(), 5)

    assert [(level.entry_price, level.tp_price) for level in levels] == [
        (Decimal("3000"), Decimal("2980")),
        (Decimal("2980"), Decimal("2960")),
        (Decimal("2960"), Decimal("2940")),
        (Decimal("2940"), Decimal("2920")),
        (Decimal("2920"), Decimal("2900")),
    ]
    assert all(level.option_budget == Decimal("20") for level in levels)
    assert all(level.initial_quantity == Decimal("1") for level in levels)
    assert all(level.state is LevelState.READY for level in levels)
    assert levels[0].tp_distance == Decimal("20")
    assert levels[0].stop_distance == Decimal("4.5000")
    assert levels[0].stop_price == Decimal("3004.5000")


def test_entry_percent_stop_is_independent_of_level_count() -> None:
    wide = build_virtual_levels(make_spread(), 5)[0]
    tight = build_virtual_levels(make_spread(), 100)[0]

    assert wide.tp_distance == Decimal("20")
    assert wide.stop_distance == Decimal("4.5000")
    assert tight.tp_distance == Decimal("1")
    assert tight.stop_distance == Decimal("4.5000")
    assert wide.initial_quantity == tight.initial_quantity == Decimal("1")


def test_baseline_quantity_can_be_point_zero_one_at_any_price_spacing() -> None:
    spread = CreditSpread("3010", "3000", "2900", "0.01", "0.3")

    wide = build_virtual_levels(spread, 5)[0]
    tight = build_virtual_levels(spread, 100)[0]

    assert wide.initial_quantity == Decimal("0.01")
    assert tight.initial_quantity == Decimal("0.01")


def test_levels_cover_the_spread_without_overlap_or_gaps() -> None:
    spread = make_spread()
    levels = build_virtual_levels(spread, 7)

    assert levels[0].entry_price == spread.short_put_strike
    assert levels[-1].tp_price == spread.long_put_strike
    assert all(
        current.tp_price == following.entry_price
        for current, following in zip(levels, levels[1:])
    )
    assert sum((level.tp_distance for level in levels), Decimal("0")) == (
        spread.short_put_strike - spread.long_put_strike
    )
    assert min(level.tp_price for level in levels) == spread.long_put_strike


def test_legacy_generator_matches_new_level_count_runtime_adapter() -> None:
    spread = make_spread()

    legacy = LegacyPriceStepLevelGenerator.generate(spread, 7)
    current = build_virtual_levels(
        spread,
        7,
        PriceStepFractionStopConfig(Rate(Decimal("0.15"))),
    )

    assert [
        (
            level.level_id,
            level.entry_price,
            level.tp_price,
            level.stop_price,
            level.option_budget,
        )
        for level in current
    ] == [
        (
            level.level_id,
            level.entry_price,
            level.tp_price,
            level.stop_price,
            level.option_budget,
        )
        for level in legacy
    ]
