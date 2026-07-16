"""Domain error branches and deterministic strategy-math properties."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from eth_credit_hedge.domain.strategy_math import (
    CoverageResult,
    DeltaExposure,
    DeltaStepSpacingConfig,
    EntryPercentStopConfig,
    EqualOptionLossSpacingConfig,
    ExecutionCostContext,
    ExpirationOptionValuation,
    InstrumentRules,
    InvalidConfigurationError,
    LevelCountSpacingConfig,
    LevelSpacingMode,
    Money,
    OptionLegValuationParameters,
    OptionSpreadState,
    OptionValuationContext,
    OptionValuationMode,
    Price,
    PriceStepFractionStopConfig,
    Quantity,
    QuantityRoundingMode,
    Rate,
    Seconds,
    StopGeometryEngine,
    StopMode,
    StrategyMathEngine,
    UnsupportedValuationError,
    Volatility,
    calculate_actual_stop_debt,
    parse_spacing_configuration,
    parse_stop_configuration,
    quantize_quantity,
    require_valuation_context,
    size_hedge,
    solve_monotonic_price,
    validate_spacing_configuration_fields,
    validate_stop_configuration_fields,
    zero_cost_directional_profit_and_loss_per_unit,
)
from eth_credit_hedge.domain.strategy_math.errors import StrategyMathError
from eth_credit_hedge.domain.strategy_math.spacing import SpacingLevel
from eth_credit_hedge.domain.strategy_math import spacing as spacing_module


NOW = datetime(2026, 7, 17, tzinfo=UTC)
ZERO = Decimal("0")


def spread() -> OptionSpreadState:
    return OptionSpreadState(Price(Decimal("10")), Price(Decimal("4")), Quantity(Decimal("1")))


def context(mode: OptionValuationMode = OptionValuationMode.EXPIRATION) -> OptionValuationContext:
    kwargs: dict[str, object] = {}
    if mode is not OptionValuationMode.EXPIRATION:
        kwargs = {
            "delta_source_available": True,
            "time_to_expiry": Seconds(Decimal("100")),
            "short_leg": OptionLegValuationParameters("S", Price(Decimal("10")), Volatility(Decimal("0.5"))),
            "long_leg": OptionLegValuationParameters("L", Price(Decimal("4")), quote_source="fixed"),
        }
    return OptionValuationContext(mode, NOW, NOW + timedelta(seconds=1), **kwargs)  # type: ignore[arg-type]


def costs(**changes: object) -> ExecutionCostContext:
    values: dict[str, object] = {
        "expected_entry_price": Price(Decimal("10")),
        "expected_tp_price": Price(Decimal("8")),
        "expected_stop_price": Price(Decimal("11")),
        "entry_fee_rate": Rate(ZERO),
        "tp_fee_rate": Rate(ZERO),
        "stop_fee_rate": Rate(ZERO),
        "expected_entry_slippage_per_unit": Money(ZERO),
        "expected_tp_slippage_per_unit": Money(ZERO),
        "expected_stop_slippage_per_unit": Money(ZERO),
        "expected_funding_to_tp_per_unit": Money(ZERO),
        "expected_funding_to_stop_per_unit": Money(ZERO),
        "spread_cost_entry_per_unit": Money(ZERO),
        "spread_cost_tp_per_unit": Money(ZERO),
        "spread_cost_stop_per_unit": Money(ZERO),
    }
    values.update(changes)
    return ExecutionCostContext(**values)  # type: ignore[arg-type]


def rules(**changes: object) -> InstrumentRules:
    values: dict[str, object] = {
        "quantity_step": Quantity(Decimal("0.1")),
        "minimum_quantity": Quantity(Decimal("0.1")),
        "maximum_quantity": Quantity(Decimal("100")),
        "maximum_notional": Money(Decimal("100000")),
        "maximum_projected_stop_loss": Money(Decimal("100000")),
    }
    values.update(changes)
    return InstrumentRules(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("enum", [LevelSpacingMode, StopMode, OptionValuationMode, QuantityRoundingMode])
def test_contract_enums_reject_unknown_values(enum: type[object]) -> None:
    with pytest.raises(InvalidConfigurationError, match="unsupported"):
        enum.parse("unknown")  # type: ignore[attr-defined]


def test_valuation_context_rejects_every_invalid_time_or_input_branch() -> None:
    with pytest.raises(UnsupportedValuationError, match="symbol"):
        OptionLegValuationParameters(" ", Price(Decimal("1")), quote_source="x")
    with pytest.raises(UnsupportedValuationError, match="timezone"):
        OptionValuationContext(OptionValuationMode.EXPIRATION, datetime(2026, 1, 1), NOW)
    with pytest.raises(UnsupportedValuationError, match="precede"):
        OptionValuationContext(OptionValuationMode.EXPIRATION, NOW, NOW - timedelta(seconds=1))
    with pytest.raises(UnsupportedValuationError, match="freshness"):
        context().require_fresh(datetime(2026, 1, 1))
    incomplete = OptionValuationContext(OptionValuationMode.MARK_MODEL, NOW, NOW)
    with pytest.raises(UnsupportedValuationError, match="time to expiry"):
        incomplete.require_fresh(NOW, require_model_inputs=True)
    missing_legs = replace(incomplete, time_to_expiry=Seconds(Decimal("1")))
    with pytest.raises(UnsupportedValuationError, match="leg parameters"):
        missing_legs.require_fresh(NOW, require_model_inputs=True)
    with pytest.raises(UnsupportedValuationError, match="required"):
        require_valuation_context(None, NOW)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: LevelCountSpacingConfig(0),
        lambda: EqualOptionLossSpacingConfig(Money(ZERO), OptionValuationMode.EXPIRATION),
        lambda: EntryPercentStopConfig(Rate(ZERO)),
        lambda: PriceStepFractionStopConfig(Rate(ZERO)),
        lambda: DeltaStepSpacingConfig(DeltaExposure(ZERO), OptionValuationMode.MARK_MODEL, Price(Decimal("4")), Price(Decimal("10")), Decimal("1e-6"), 10),
        lambda: DeltaStepSpacingConfig(DeltaExposure(Decimal("1")), OptionValuationMode.MARK_MODEL, Price(Decimal("10")), Price(Decimal("4")), Decimal("1e-6"), 10),
        lambda: DeltaStepSpacingConfig(DeltaExposure(Decimal("1")), OptionValuationMode.MARK_MODEL, Price(Decimal("4")), Price(Decimal("10")), Decimal("NaN"), 10),
        lambda: DeltaStepSpacingConfig(DeltaExposure(Decimal("1")), OptionValuationMode.MARK_MODEL, Price(Decimal("4")), Price(Decimal("10")), Decimal("1e-6"), 0),
    ],
)
def test_configuration_contract_error_branches(factory: object) -> None:
    with pytest.raises(InvalidConfigurationError):
        factory()  # type: ignore[operator]


def test_strict_configuration_parsers_cover_all_modes_and_bad_scalars() -> None:
    assert parse_spacing_configuration("LEVEL_COUNT", {"level_count": "2"}).level_count == 2  # type: ignore[union-attr]
    assert parse_spacing_configuration("EQUAL_OPTION_LOSS", {"target_zone_loss_usd": "1", "valuation_mode": "EXPIRATION"}).mode is LevelSpacingMode.EQUAL_OPTION_LOSS
    assert parse_spacing_configuration("PRICE_STEP", {"price_step_usd": "2"}).mode is LevelSpacingMode.PRICE_STEP
    delta = parse_spacing_configuration("DELTA_STEP", {"delta_step": "1", "valuation_mode": "MARK_MODEL", "minimum_price": "4", "maximum_price": "10", "solver_tolerance": "0.001", "maximum_iterations": "10"})
    assert delta.mode is LevelSpacingMode.DELTA_STEP
    assert parse_stop_configuration("ENTRY_PERCENT", {"entry_stop_rate": "0.1"}).mode is StopMode.ENTRY_PERCENT
    assert parse_stop_configuration("PRICE_STEP_FRACTION", {"price_step_stop_fraction": "0.1"}).mode is StopMode.PRICE_STEP_FRACTION
    with pytest.raises(InvalidConfigurationError, match="missing.*unexpected"):
        validate_spacing_configuration_fields("PRICE_STEP", {"level_count"})
    with pytest.raises(InvalidConfigurationError, match="missing.*unexpected"):
        validate_stop_configuration_fields("ENTRY_PERCENT", {"price_step_stop_fraction"})
    with pytest.raises(InvalidConfigurationError, match="ambiguous"):
        validate_stop_configuration_fields("ENTRY_PERCENT", {"stop_rate"})
    for value in (True, "1.0", "01", "bad"):
        with pytest.raises(InvalidConfigurationError, match="integer"):
            parse_spacing_configuration("LEVEL_COUNT", {"level_count": value})
    for value in (object(), "Infinity"):
        with pytest.raises(InvalidConfigurationError, match="finite decimal"):
            parse_spacing_configuration("PRICE_STEP", {"price_step_usd": value})


def test_coverage_contract_rejects_each_inconsistent_state() -> None:
    invalid = [
        ("-1", "0", "0", "0", True),
        ("1", "1", "-1", "0", True),
        ("1", "1", "1", "1", True),
        ("1", "2", "0", "0", True),
        ("1", "1", "0", "0", False),
    ]
    for required, profit, over, under, covered in invalid:
        with pytest.raises(InvalidConfigurationError):
            CoverageResult(Money(Decimal(required)), Money(Decimal(profit)), Money(Decimal(over)), Money(Decimal(under)), covered)


def test_spacing_level_and_level_math_reject_corrupt_derived_values() -> None:
    level = StrategyMathEngine(ExpirationOptionValuation()).build_levels(
        spread(), context(), LevelCountSpacingConfig(2), EntryPercentStopConfig(Rate(Decimal("0.01"))), as_of_utc=NOW
    )[0]
    for changes in (
        {"level_id": 0},
        {"zone_option_loss_budget": Money(Decimal("-1"))},
    ):
        with pytest.raises(InvalidConfigurationError):
            replace(level, **changes)
    spacing = SpacingLevel(1, Price(Decimal("10")), Price(Decimal("8")), Price(Decimal("2")), None, Money(Decimal("2")), Money(ZERO), Money(Decimal("2")), LevelSpacingMode.PRICE_STEP, OptionValuationMode.EXPIRATION)
    for changes in ({"level_id": 0}, {"tp_price": Price(Decimal("10"))}, {"price_distance": Price(Decimal("1"))}, {"zone_option_loss_budget": Money(Decimal("1"))}):
        with pytest.raises(InvalidConfigurationError):
            replace(spacing, **changes)


def test_cost_instrument_recovery_and_facade_error_branches() -> None:
    with pytest.raises(InvalidConfigurationError, match="entry price"):
        costs(expected_tp_price=Price(Decimal("10")))
    for field in (
        "expected_entry_slippage_per_unit",
        "expected_tp_slippage_per_unit",
        "spread_cost_stop_per_unit",
    ):
        with pytest.raises(InvalidConfigurationError, match="cannot be negative"):
            costs(**{field: Money(Decimal("-1"))})
    for changes in (
        {"minimum_quantity": Quantity(Decimal("2")), "maximum_quantity": Quantity(Decimal("1"))},
        {"maximum_notional": Money(ZERO)},
        {"maximum_projected_stop_loss": Money(ZERO)},
    ):
        with pytest.raises(InvalidConfigurationError):
            rules(**changes)
    for field in ("allocated_entry_fees", "stop_fees"):
        kwargs = {"entry_price": Price(Decimal("10")), "stop_fill_price": Price(Decimal("11")), "stop_reference_price": Price(Decimal("11")), "quantity": Quantity(Decimal("1")), "allocated_entry_fees": Money(ZERO), "stop_fees": Money(ZERO), "funding_pnl": Money(ZERO), field: Money(Decimal("-1"))}
        with pytest.raises(ValueError, match="cannot be negative"):
            calculate_actual_stop_debt(**kwargs)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="role"):
        StrategyMathEngine.size_budget(role="OTHER", zone_option_loss_budget=Money(Decimal("1")), confirmed_recovery_debt=Money(ZERO), configured_buffer=Money(ZERO), costs=costs(), instrument=rules())  # type: ignore[arg-type]


def test_sizing_and_quantization_properties() -> None:
    sell_profit, sell_loss = zero_cost_directional_profit_and_loss_per_unit(entry_price=Price(Decimal("10")), tp_price=Price(Decimal("8")), stop_price=Price(Decimal("11")), side="Sell")
    assert (sell_profit.value, sell_loss.value) == (Decimal("2"), Decimal("1"))
    buy_profit, buy_loss = zero_cost_directional_profit_and_loss_per_unit(entry_price=Price(Decimal("8")), tp_price=Price(Decimal("10")), stop_price=Price(Decimal("7")), side="Buy")
    assert (buy_profit.value, buy_loss.value) == (Decimal("2"), Decimal("1"))
    assert quantize_quantity(Quantity(Decimal("1.04")), rules(), QuantityRoundingMode.NEAREST).value == Decimal("1.0")
    for field in ("zone_option_loss_budget", "confirmed_recovery_debt", "configured_buffer"):
        kwargs = {"role": "RECOVERY", "zone_option_loss_budget": Money(Decimal("1")), "confirmed_recovery_debt": Money(ZERO), "configured_buffer": Money(ZERO), "costs": costs(), "instrument": rules(), field: Money(Decimal("-1"))}
        with pytest.raises(ValueError, match="cannot be negative"):
            size_hedge(**kwargs)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="baseline"):
        size_hedge(role="BASELINE", zone_option_loss_budget=Money(Decimal("1")), confirmed_recovery_debt=Money(Decimal("1")), configured_buffer=Money(ZERO), costs=costs(), instrument=rules())


def test_root_solver_endpoints_validation_and_nonconvergence() -> None:
    def function(price: Price) -> Decimal:
        return price.value
    assert solve_monotonic_price(function, Decimal("1"), minimum_price=Price(Decimal("1")), maximum_price=Price(Decimal("2")), absolute_tolerance=Decimal("0.1"), relative_tolerance=Decimal("0.1"), maximum_iterations=2) == Price(Decimal("1"))
    assert solve_monotonic_price(function, Decimal("2"), minimum_price=Price(Decimal("1")), maximum_price=Price(Decimal("2")), absolute_tolerance=Decimal("0.1"), relative_tolerance=Decimal("0.1"), maximum_iterations=2) == Price(Decimal("2"))
    invalid = [
        {"minimum_price": Price(Decimal("2")), "maximum_price": Price(Decimal("1")), "absolute_tolerance": Decimal("0.1"), "relative_tolerance": Decimal("0.1"), "maximum_iterations": 2},
        {"minimum_price": Price(Decimal("1")), "maximum_price": Price(Decimal("2")), "absolute_tolerance": ZERO, "relative_tolerance": Decimal("0.1"), "maximum_iterations": 2},
        {"minimum_price": Price(Decimal("1")), "maximum_price": Price(Decimal("2")), "absolute_tolerance": Decimal("0.1"), "relative_tolerance": Decimal("0.1"), "maximum_iterations": 0},
    ]
    for kwargs in invalid:
        with pytest.raises(InvalidConfigurationError):
            solve_monotonic_price(function, Decimal("1.5"), **kwargs)  # type: ignore[arg-type]
    with pytest.raises(StrategyMathError, match="did not converge"):
        solve_monotonic_price(function, Decimal("1.3"), minimum_price=Price(Decimal("1")), maximum_price=Price(Decimal("2")), absolute_tolerance=Decimal("1e-30"), relative_tolerance=Decimal("1e-30"), maximum_iterations=1)


def test_expiration_delta_and_stop_operational_limit_branches() -> None:
    valuation = ExpirationOptionValuation()
    assert valuation.delta_at_price(spread(), context(), Price(Decimal("7"))).value == Decimal("1")
    assert valuation.delta_at_price(spread(), context(), Price(Decimal("11"))).value == ZERO
    with pytest.raises(UnsupportedValuationError, match="EXPIRATION"):
        valuation.delta_at_price(spread(), context(OptionValuationMode.MARK_MODEL), Price(Decimal("7")))
    level = StrategyMathEngine(valuation).build_levels(spread(), context(), LevelCountSpacingConfig(2), EntryPercentStopConfig(Rate(Decimal("0.01"))), as_of_utc=NOW)[0]
    spacing = SpacingLevel(level.level_id, level.entry_price, level.tp_price, level.price_distance, level.target_delta, level.entry_option_value, level.tp_option_value, level.zone_option_loss_budget, level.spacing_mode, level.valuation_mode)
    with pytest.raises(InvalidConfigurationError, match="operational maximum"):
        StopGeometryEngine().build_level(spacing, EntryPercentStopConfig(Rate(Decimal("0.01"))), maximum_stop_price=Price(Decimal("10.05")))
    invalid_stop = object.__new__(EntryPercentStopConfig)
    object.__setattr__(invalid_stop, "rate", Rate(ZERO))
    with pytest.raises(InvalidConfigurationError, match="positive"):
        StopGeometryEngine.stop_distance(Price(Decimal("10")), Price(Decimal("2")), invalid_stop)


class LinearValuation:
    def value_at_price(self, position: OptionSpreadState, market: OptionValuationContext, price: Price) -> Money:
        del position, market
        return Money(price.value)

    def delta_at_price(self, position: OptionSpreadState, market: OptionValuationContext, price: Price) -> DeltaExposure:
        del position, market
        return DeltaExposure(price.value)


def test_spacing_domain_error_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    model = context(OptionValuationMode.MARK_MODEL)
    engine = spacing_module.LevelSpacingEngine(LinearValuation())
    with pytest.raises(InvalidConfigurationError, match="interval"):
        engine.build_levels(
            spread(),
            model,
            DeltaStepSpacingConfig(DeltaExposure(Decimal("1")), OptionValuationMode.MARK_MODEL, Price(Decimal("3")), Price(Decimal("10")), Decimal("1e-6"), 10),
            as_of_utc=NOW,
        )
    with pytest.raises(UnsupportedValuationError, match="must match"):
        engine.build_levels(
            spread(),
            model,
            EqualOptionLossSpacingConfig(Money(Decimal("1")), OptionValuationMode.EXECUTABLE_LIQUIDATION),
            as_of_utc=NOW,
        )
    bad_long_leg = replace(model, long_leg=replace(model.long_leg, strike=Price(Decimal("5"))))  # type: ignore[arg-type]
    with pytest.raises(UnsupportedValuationError, match="long valuation"):
        engine.build_levels(spread(), bad_long_leg, EqualOptionLossSpacingConfig(Money(Decimal("1")), OptionValuationMode.MARK_MODEL), as_of_utc=NOW)
    with pytest.raises(InvalidConfigurationError, match="cannot be negative"):
        SpacingLevel(1, Price(Decimal("10")), Price(Decimal("8")), Price(Decimal("2")), None, Money(Decimal("-1")), Money(ZERO), Money(Decimal("-1")), LevelSpacingMode.PRICE_STEP, OptionValuationMode.EXPIRATION)

    def duplicate(*args: object, **kwargs: object) -> Price:
        del args
        return kwargs["maximum_price"]  # type: ignore[no-any-return]

    monkeypatch.setattr(spacing_module, "solve_monotonic_price", duplicate)
    with pytest.raises(spacing_module.NonMonotonicSpacingError, match="equal-loss"):
        engine.build_levels(spread(), model, EqualOptionLossSpacingConfig(Money(Decimal("1")), OptionValuationMode.MARK_MODEL), as_of_utc=NOW)
    with pytest.raises(spacing_module.NonMonotonicSpacingError, match="delta solver"):
        engine.build_levels(
            spread(),
            model,
            DeltaStepSpacingConfig(DeltaExposure(Decimal("1")), OptionValuationMode.MARK_MODEL, Price(Decimal("4")), Price(Decimal("10")), Decimal("1e-6"), 10),
            as_of_utc=NOW,
        )


@pytest.mark.parametrize("count", [1, 2, 3, 7])
def test_level_and_sizing_properties_hold_for_varied_counts(count: int) -> None:
    levels = StrategyMathEngine(ExpirationOptionValuation()).build_levels(spread(), context(), LevelCountSpacingConfig(count), EntryPercentStopConfig(Rate(Decimal("0.01"))), as_of_utc=NOW)
    assert [level.level_id for level in levels] == list(range(1, count + 1))
    assert levels[-1].tp_price == spread().long_put_strike
    for level in levels:
        result = StrategyMathEngine.size_baseline(level, costs(expected_entry_price=level.entry_price, expected_tp_price=level.tp_price, expected_stop_price=level.stop_price), rules())
        assert level.entry_price > level.tp_price
        assert level.stop_price > level.entry_price
        assert level.zone_option_loss_budget.value >= ZERO
        assert result.submitted_quantity.value >= ZERO
        assert result.expected_net_tp_profit.value == result.submitted_quantity.value * result.net_tp_profit_per_unit.value
        assert not (result.undercoverage.value > ZERO and result.overcoverage.value > ZERO)
