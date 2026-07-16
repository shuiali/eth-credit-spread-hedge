"""Central strategy-math contract validation."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from eth_credit_hedge.domain.strategy_math import (
    CoverageResult,
    DeltaExposure,
    DeltaSpacingUnavailableError,
    DeltaStepSpacingConfig,
    InvalidConfigurationError,
    InvalidUnitsError,
    LevelMath,
    LevelSpacingMode,
    Money,
    NonPositiveNetProfitError,
    OptionSpreadState,
    OptionValuationContext,
    OptionValuationMode,
    Price,
    Quantity,
    QuantizationCoverageError,
    StopMode,
    StrategyMathError,
    UnsupportedValuationError,
    require_valuation_context,
    validate_spacing_configuration_fields,
)


NOW = datetime(2026, 7, 16, 12, tzinfo=timezone.utc)


def test_invalid_mixed_spacing_configuration_is_rejected() -> None:
    with pytest.raises(InvalidConfigurationError, match="unexpected level_count"):
        validate_spacing_configuration_fields(
            LevelSpacingMode.PRICE_STEP,
            {"price_step_usd", "level_count"},
        )


@pytest.mark.parametrize("legacy_name", ["delta_spacing", "delta_grid", "delta_step"])
def test_ambiguous_legacy_price_spacing_fields_are_rejected(
    legacy_name: str,
) -> None:
    with pytest.raises(InvalidUnitsError, match="price_step_usd"):
        validate_spacing_configuration_fields(
            LevelSpacingMode.PRICE_STEP,
            {legacy_name},
        )


def test_delta_step_is_allowed_only_as_real_delta_exposure_configuration() -> None:
    config = DeltaStepSpacingConfig(
        delta_step=DeltaExposure(Decimal("0.01")),
        valuation_mode=OptionValuationMode.MARK_MODEL,
        minimum_price=Price(Decimal("2500")),
        maximum_price=Price(Decimal("3500")),
        solver_tolerance=Decimal("0.000001"),
        maximum_iterations=100,
    )

    assert config.mode is LevelSpacingMode.DELTA_STEP
    with pytest.raises(DeltaSpacingUnavailableError, match="real option-delta"):
        DeltaStepSpacingConfig(
            delta_step=DeltaExposure(Decimal("0.01")),
            valuation_mode=OptionValuationMode.EXPIRATION,
            minimum_price=Price(Decimal("2500")),
            maximum_price=Price(Decimal("3500")),
            solver_tolerance=Decimal("0.000001"),
            maximum_iterations=100,
        )


def test_invalid_spread_strikes_are_rejected() -> None:
    with pytest.raises(InvalidConfigurationError, match="short put strike"):
        OptionSpreadState(
            short_put_strike=Price(Decimal("2900")),
            long_put_strike=Price(Decimal("3000")),
            option_quantity=Quantity(Decimal("0.1")),
        )


def test_absent_stale_and_delta_less_valuation_contexts_are_rejected() -> None:
    with pytest.raises(UnsupportedValuationError, match="cannot be absent"):
        require_valuation_context(None, NOW)

    context = OptionValuationContext(
        valuation_mode=OptionValuationMode.MARK_MODEL,
        observed_at_utc=NOW,
        valid_until_utc=NOW + timedelta(seconds=5),
        delta_source_available=False,
    )
    with pytest.raises(UnsupportedValuationError, match="stale"):
        require_valuation_context(context, NOW + timedelta(seconds=6))
    with pytest.raises(DeltaSpacingUnavailableError, match="option-delta source"):
        require_valuation_context(context, NOW, require_delta=True)


@pytest.mark.parametrize(
    ("tp", "stop", "message"),
    [
        (Decimal("3000"), Decimal("3004"), "take-profit"),
        (Decimal("2980"), Decimal("3000"), "stop price"),
    ],
)
def test_invalid_short_hedge_geometry_is_rejected(
    tp: Decimal,
    stop: Decimal,
    message: str,
) -> None:
    with pytest.raises(InvalidConfigurationError, match=message):
        _level(tp=tp, stop=stop)


def test_level_distances_must_reconcile_in_declared_price_units() -> None:
    with pytest.raises(InvalidUnitsError, match="price distance"):
        _level(price_distance=Decimal("19"))
    with pytest.raises(InvalidUnitsError, match="stop distance"):
        _level(stop_distance=Decimal("3"))


def test_coverage_flags_must_match_reconciled_amounts() -> None:
    with pytest.raises(InvalidConfigurationError, match="reconcile"):
        CoverageResult(
            required_budget=Money(Decimal("1")),
            expected_net_profit=Money(Decimal("0.9")),
            overcoverage=Money(Decimal("0")),
            undercoverage=Money(Decimal("0")),
            fully_covered=True,
        )


@pytest.mark.parametrize(
    "error_type",
    [
        InvalidConfigurationError,
        InvalidUnitsError,
        UnsupportedValuationError,
        DeltaSpacingUnavailableError,
        NonPositiveNetProfitError,
        QuantizationCoverageError,
    ],
)
def test_all_domain_errors_require_actionable_nonempty_messages(
    error_type: type[StrategyMathError],
) -> None:
    with pytest.raises(ValueError, match="actionable message"):
        error_type("")
    assert "correct the configured value" in str(
        error_type("correct the configured value")
    )


def _level(
    *,
    tp: Decimal = Decimal("2980"),
    stop: Decimal = Decimal("3004"),
    price_distance: Decimal = Decimal("20"),
    stop_distance: Decimal = Decimal("4"),
) -> LevelMath:
    return LevelMath(
        level_id=1,
        entry_price=Price(Decimal("3000")),
        tp_price=Price(tp),
        price_distance=Price(price_distance),
        target_delta=None,
        entry_option_value=Money(Decimal("10")),
        tp_option_value=Money(Decimal("8")),
        zone_option_loss_budget=Money(Decimal("2")),
        stop_price=Price(stop),
        stop_distance=Price(stop_distance),
        spacing_mode=LevelSpacingMode.PRICE_STEP,
        stop_mode=StopMode.ENTRY_PERCENT,
        valuation_mode=OptionValuationMode.EXPIRATION,
    )
