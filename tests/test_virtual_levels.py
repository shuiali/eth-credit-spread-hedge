"""Virtual-level generation tests."""

from decimal import Decimal

from core.credit_spread import CreditSpread
from core.virtual_levels import LevelState, generate_virtual_levels


def make_spread() -> CreditSpread:
    return CreditSpread("3010", "3000", "2900", "1", "30")


def test_five_levels_match_the_locked_specification() -> None:
    levels = generate_virtual_levels(make_spread(), 5)

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
    assert levels[0].stop_price == Decimal("3004.5000")


def test_levels_cover_the_spread_without_overlap_or_gaps() -> None:
    spread = make_spread()
    levels = generate_virtual_levels(spread, 7)

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
