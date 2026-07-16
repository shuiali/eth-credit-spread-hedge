"""Independent synthetic fixtures for authoritative spacing modes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from eth_credit_hedge.domain.strategy_math import (
    DeltaExposure,
    DeltaSpacingUnavailableError,
    DeltaStepSpacingConfig,
    EqualOptionLossSpacingConfig,
    ExpirationOptionValuation,
    LevelCountSpacingConfig,
    LevelSpacingEngine,
    Money,
    NonMonotonicSpacingError,
    OptionLegValuationParameters,
    OptionSpreadState,
    OptionValuationContext,
    OptionValuationMode,
    Price,
    PriceStepSpacingConfig,
    Quantity,
    RootNotBracketedError,
    Seconds,
    StrategyMathError,
    UnsupportedValuationError,
    parse_spacing_configuration,
    solve_monotonic_price,
)


NOW = datetime(2026, 7, 16, 12, tzinfo=timezone.utc)


class LinearValuation:
    """Independent value(price) = 7 + 3*price synthetic curve."""

    def value_at_price(
        self,
        position: OptionSpreadState,
        context: OptionValuationContext,
        underlying_price: Price,
    ) -> Money:
        del position, context
        return Money(Decimal("7") + Decimal("3") * underlying_price.value)

    def delta_at_price(
        self,
        position: OptionSpreadState,
        context: OptionValuationContext,
        underlying_price: Price,
    ) -> DeltaExposure:
        del position, context, underlying_price
        return DeltaExposure(Decimal("3"))


class QuadraticValuation:
    """Independent value(price) = price^2 and delta(price) = 2*price."""

    def __init__(self) -> None:
        self.delta_calls = 0

    def value_at_price(
        self,
        position: OptionSpreadState,
        context: OptionValuationContext,
        underlying_price: Price,
    ) -> Money:
        del position, context
        return Money(underlying_price.value * underlying_price.value)

    def delta_at_price(
        self,
        position: OptionSpreadState,
        context: OptionValuationContext,
        underlying_price: Price,
    ) -> DeltaExposure:
        del position, context
        self.delta_calls += 1
        return DeltaExposure(Decimal("2") * underlying_price.value)


class NonMonotonicValuation(QuadraticValuation):
    def value_at_price(
        self,
        position: OptionSpreadState,
        context: OptionValuationContext,
        underlying_price: Price,
    ) -> Money:
        del position, context
        distance = underlying_price.value - Decimal("7")
        return Money(-(distance * distance))


class NonMonotonicDeltaValuation(QuadraticValuation):
    def delta_at_price(
        self,
        position: OptionSpreadState,
        context: OptionValuationContext,
        underlying_price: Price,
    ) -> DeltaExposure:
        del position, context
        distance = underlying_price.value - Decimal("7")
        return DeltaExposure(-(distance * distance))


def spread() -> OptionSpreadState:
    return OptionSpreadState(
        short_put_strike=Price(Decimal("10")),
        long_put_strike=Price(Decimal("4")),
        option_quantity=Quantity(Decimal("1")),
    )


def expiration_context() -> OptionValuationContext:
    return OptionValuationContext(
        valuation_mode=OptionValuationMode.EXPIRATION,
        observed_at_utc=NOW,
        valid_until_utc=NOW,
    )


def model_context(
    *,
    observed_at: datetime = NOW,
    valid_until: datetime = NOW + timedelta(seconds=5),
    delta_available: bool = True,
) -> OptionValuationContext:
    return OptionValuationContext(
        valuation_mode=OptionValuationMode.MARK_MODEL,
        observed_at_utc=observed_at,
        valid_until_utc=valid_until,
        delta_source_available=delta_available,
        time_to_expiry=Seconds(Decimal("86400")),
        short_leg=OptionLegValuationParameters(
            symbol="SHORT",
            strike=Price(Decimal("10")),
            quote_source="SYNTHETIC",
        ),
        long_leg=OptionLegValuationParameters(
            symbol="LONG",
            strike=Price(Decimal("4")),
            quote_source="SYNTHETIC",
        ),
    )


def test_price_step_boundaries_and_narrow_final_zone() -> None:
    levels = LevelSpacingEngine(ExpirationOptionValuation()).build_levels(
        spread(),
        expiration_context(),
        PriceStepSpacingConfig(Price(Decimal("2.5"))),
        as_of_utc=NOW,
    )

    assert [(item.entry_price.value, item.tp_price.value) for item in levels] == [
        (Decimal("10"), Decimal("7.5")),
        (Decimal("7.5"), Decimal("5.0")),
        (Decimal("5.0"), Decimal("4")),
    ]
    assert levels[-1].price_distance.value == Decimal("1.0")
    assert levels[-1].tp_price == spread().long_put_strike
    assert all(item.target_delta is None for item in levels)


def test_level_count_is_byte_equivalent_to_normalized_price_step() -> None:
    engine = LevelSpacingEngine(ExpirationOptionValuation())

    by_count = engine.build_levels(
        spread(), expiration_context(), LevelCountSpacingConfig(3), as_of_utc=NOW
    )
    by_step = engine.build_levels(
        spread(),
        expiration_context(),
        PriceStepSpacingConfig(Price(Decimal("2"))),
        as_of_utc=NOW,
    )

    assert tuple(level.to_dict() for level in by_count) == tuple(
        level.to_dict() for level in by_step
    )


def test_equal_option_loss_uses_linear_curve_values() -> None:
    levels = LevelSpacingEngine(LinearValuation()).build_levels(
        spread(),
        model_context(),
        EqualOptionLossSpacingConfig(
            Money(Decimal("6")), OptionValuationMode.MARK_MODEL
        ),
        as_of_utc=NOW,
    )

    assert [level.entry_price.value for level in levels] == pytest.approx(
        [Decimal("10"), Decimal("8"), Decimal("6")],
        abs=Decimal("1e-10"),
    )
    assert [level.zone_option_loss_budget.value for level in levels] == (
        pytest.approx(
            [Decimal("6"), Decimal("6"), Decimal("6")],
            abs=Decimal("1e-9"),
        )
    )


def test_expiration_equal_loss_reduces_to_equal_price_spacing() -> None:
    engine = LevelSpacingEngine(ExpirationOptionValuation())
    equal_loss = engine.build_levels(
        spread(),
        expiration_context(),
        EqualOptionLossSpacingConfig(
            Money(Decimal("2")), OptionValuationMode.EXPIRATION
        ),
        as_of_utc=NOW,
    )
    price_step = engine.build_levels(
        spread(),
        expiration_context(),
        PriceStepSpacingConfig(Price(Decimal("2"))),
        as_of_utc=NOW,
    )

    assert [item.entry_price.value for item in equal_loss] == pytest.approx(
        [item.entry_price.value for item in price_step], abs=Decimal("1e-10")
    )
    assert [item.zone_option_loss_budget.value for item in equal_loss] == pytest.approx(
        [Decimal("2"), Decimal("2"), Decimal("2")],
        abs=Decimal("1e-9"),
    )


def test_equal_option_loss_curved_roots_are_unequal_and_final_loss_is_actual() -> None:
    levels = LevelSpacingEngine(QuadraticValuation()).build_levels(
        spread(),
        model_context(),
        EqualOptionLossSpacingConfig(
            Money(Decimal("36")), OptionValuationMode.MARK_MODEL
        ),
        as_of_utc=NOW,
    )

    assert levels[0].tp_price.value == pytest.approx(Decimal("8"), abs=Decimal("1e-10"))
    assert levels[1].tp_price.value == pytest.approx(
        Decimal("5.291502622129181"), abs=Decimal("1e-10")
    )
    assert levels[0].price_distance.value != levels[1].price_distance.value
    assert levels[0].zone_option_loss_budget.value == pytest.approx(
        Decimal("36"), abs=Decimal("1e-9")
    )
    assert levels[-1].zone_option_loss_budget.value == pytest.approx(
        Decimal("12"), abs=Decimal("1e-9")
    )


def test_delta_step_solves_known_real_delta_roots() -> None:
    valuation = QuadraticValuation()
    levels = LevelSpacingEngine(valuation).build_levels(
        spread(),
        model_context(),
        delta_config(Decimal("4")),
        as_of_utc=NOW,
    )

    assert [level.entry_price.value for level in levels] == pytest.approx(
        [Decimal("10"), Decimal("8"), Decimal("6")],
        abs=Decimal("1e-10"),
    )
    assert [level.target_delta.value for level in levels if level.target_delta] == [
        Decimal("20"),
        Decimal("16"),
        Decimal("12"),
    ]
    assert valuation.delta_calls > len(levels)


def test_delta_step_perturbation_changes_targets_and_prices() -> None:
    engine = LevelSpacingEngine(QuadraticValuation())

    wide = engine.build_levels(
        spread(), model_context(), delta_config(Decimal("4")), as_of_utc=NOW
    )
    narrow = engine.build_levels(
        spread(), model_context(), delta_config(Decimal("3")), as_of_utc=NOW
    )

    assert [item.entry_price.value for item in wide] != [
        item.entry_price.value for item in narrow
    ]
    assert [item.target_delta for item in wide] != [
        item.target_delta for item in narrow
    ]


def test_price_step_perturbation_never_creates_delta_targets() -> None:
    engine = LevelSpacingEngine(ExpirationOptionValuation())

    wide = engine.build_levels(
        spread(),
        expiration_context(),
        PriceStepSpacingConfig(Price(Decimal("3"))),
        as_of_utc=NOW,
    )
    narrow = engine.build_levels(
        spread(),
        expiration_context(),
        PriceStepSpacingConfig(Price(Decimal("2"))),
        as_of_utc=NOW,
    )

    assert [item.entry_price for item in wide] != [item.entry_price for item in narrow]
    assert all(item.target_delta is None for item in (*wide, *narrow))


def test_stale_or_incomplete_mark_context_is_rejected_before_valuation() -> None:
    engine = LevelSpacingEngine(QuadraticValuation())
    config = EqualOptionLossSpacingConfig(
        Money(Decimal("10")), OptionValuationMode.MARK_MODEL
    )

    with pytest.raises(UnsupportedValuationError, match="stale"):
        engine.build_levels(
            spread(), model_context(), config, as_of_utc=NOW + timedelta(seconds=6)
        )
    incomplete = OptionValuationContext(
        valuation_mode=OptionValuationMode.MARK_MODEL,
        observed_at_utc=NOW,
        valid_until_utc=NOW + timedelta(seconds=5),
    )
    with pytest.raises(UnsupportedValuationError, match="time to expiry"):
        engine.build_levels(spread(), incomplete, config, as_of_utc=NOW)


def test_model_legs_require_a_source_and_matching_spread_strikes() -> None:
    with pytest.raises(UnsupportedValuationError, match="IV or.*quote source"):
        OptionLegValuationParameters(
            symbol="SHORT",
            strike=Price(Decimal("10")),
        )

    context = OptionValuationContext(
        valuation_mode=OptionValuationMode.MARK_MODEL,
        observed_at_utc=NOW,
        valid_until_utc=NOW + timedelta(seconds=5),
        time_to_expiry=Seconds(Decimal("86400")),
        short_leg=OptionLegValuationParameters(
            symbol="SHORT",
            strike=Price(Decimal("11")),
            quote_source="SYNTHETIC",
        ),
        long_leg=OptionLegValuationParameters(
            symbol="LONG",
            strike=Price(Decimal("4")),
            quote_source="SYNTHETIC",
        ),
    )
    with pytest.raises(UnsupportedValuationError, match="short.*strike"):
        LevelSpacingEngine(QuadraticValuation()).build_levels(
            spread(),
            context,
            EqualOptionLossSpacingConfig(
                Money(Decimal("10")), OptionValuationMode.MARK_MODEL
            ),
            as_of_utc=NOW,
        )


def test_terminal_delta_step_is_rejected_explicitly() -> None:
    with pytest.raises(DeltaSpacingUnavailableError, match="real option-delta"):
        DeltaStepSpacingConfig(
            delta_step=DeltaExposure(Decimal("1")),
            valuation_mode=OptionValuationMode.EXPIRATION,
            minimum_price=Price(Decimal("4")),
            maximum_price=Price(Decimal("10")),
            solver_tolerance=Decimal("1e-12"),
            maximum_iterations=100,
        )


def test_non_monotonic_curve_is_rejected() -> None:
    with pytest.raises(NonMonotonicSpacingError, match="strictly increasing"):
        LevelSpacingEngine(NonMonotonicValuation()).build_levels(
            spread(),
            model_context(),
            EqualOptionLossSpacingConfig(
                Money(Decimal("1")), OptionValuationMode.MARK_MODEL
            ),
            as_of_utc=NOW,
        )

    with pytest.raises(NonMonotonicSpacingError, match="strictly increasing"):
        LevelSpacingEngine(NonMonotonicDeltaValuation()).build_levels(
            spread(),
            model_context(),
            delta_config(Decimal("1")),
            as_of_utc=NOW,
        )


def test_root_solver_rejects_unbracketed_target_and_fails_deterministically() -> None:
    def linear(price: Price) -> Decimal:
        return price.value

    with pytest.raises(RootNotBracketedError, match="outside"):
        solve_monotonic_price(
            linear,
            Decimal("11"),
            minimum_price=Price(Decimal("4")),
            maximum_price=Price(Decimal("10")),
            absolute_tolerance=Decimal("1e-12"),
            relative_tolerance=Decimal("1e-12"),
            maximum_iterations=100,
        )
    with pytest.raises(StrategyMathError, match="maximum_iterations"):
        solve_monotonic_price(
            lambda price: price.value * price.value,
            Decimal("2"),
            minimum_price=Price(Decimal("1")),
            maximum_price=Price(Decimal("2")),
            absolute_tolerance=Decimal("1e-30"),
            relative_tolerance=Decimal("1e-30"),
            maximum_iterations=1,
        )


def test_strict_configuration_parser_outputs_are_consumed_by_engine() -> None:
    parsed = parse_spacing_configuration(
        "PRICE_STEP", {"price_step_usd": "2.5"}
    )
    levels = LevelSpacingEngine(ExpirationOptionValuation()).build_levels(
        spread(), expiration_context(), parsed, as_of_utc=NOW
    )

    assert levels[0].tp_price == Price(Decimal("7.5"))


def delta_config(step: Decimal) -> DeltaStepSpacingConfig:
    return DeltaStepSpacingConfig(
        delta_step=DeltaExposure(step),
        valuation_mode=OptionValuationMode.MARK_MODEL,
        minimum_price=Price(Decimal("4")),
        maximum_price=Price(Decimal("10")),
        solver_tolerance=Decimal("1e-12"),
        maximum_iterations=100,
    )
