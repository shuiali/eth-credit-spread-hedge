"""Independent fixed-value golden and legacy-characterization tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from eth_credit_hedge.domain.strategy_math import (
    EntryPercentStopConfig,
    ExecutionCostContext,
    ExpirationOptionValuation,
    InstrumentRules,
    LevelCountSpacingConfig,
    Money,
    OptionSpreadState,
    OptionValuationContext,
    OptionValuationMode,
    Price,
    PriceStepFractionStopConfig,
    Quantity,
    Rate,
    StrategyMathEngine,
)


FIXTURES = Path(__file__).parents[2] / "fixtures" / "strategy_math"
NOW = datetime(2026, 7, 17, tzinfo=UTC)
ZERO = Decimal("0")


def fixture(name: str) -> dict[str, Any]:
    with (FIXTURES / name).open(encoding="utf-8") as handle:
        return json.load(handle)  # type: ignore[no-any-return]


def standard_level(*, legacy_stop: bool = False):
    stop = (
        PriceStepFractionStopConfig(Rate(Decimal("0.15")))
        if legacy_stop
        else EntryPercentStopConfig(Rate(Decimal("0.0015")))
    )
    return StrategyMathEngine(ExpirationOptionValuation()).build_levels(
        OptionSpreadState(
            short_put_strike=Price(Decimal("3000")),
            long_put_strike=Price(Decimal("2900")),
            option_quantity=Quantity(Decimal("0.1")),
        ),
        OptionValuationContext(
            valuation_mode=OptionValuationMode.EXPIRATION,
            observed_at_utc=NOW,
            valid_until_utc=NOW,
        ),
        LevelCountSpacingConfig(5),
        stop,
        as_of_utc=NOW,
    )[0]


def fee_costs() -> ExecutionCostContext:
    return ExecutionCostContext(
        expected_entry_price=Price(Decimal("3000")),
        expected_tp_price=Price(Decimal("2980")),
        expected_stop_price=Price(Decimal("3004.5")),
        entry_fee_rate=Rate(Decimal("0.0002")),
        tp_fee_rate=Rate(Decimal("0.0002")),
        stop_fee_rate=Rate(Decimal("0.0006")),
        expected_entry_slippage_per_unit=Money(ZERO),
        expected_tp_slippage_per_unit=Money(ZERO),
        expected_stop_slippage_per_unit=Money(ZERO),
        expected_funding_to_tp_per_unit=Money(ZERO),
        expected_funding_to_stop_per_unit=Money(ZERO),
        spread_cost_entry_per_unit=Money(ZERO),
        spread_cost_tp_per_unit=Money(ZERO),
        spread_cost_stop_per_unit=Money(ZERO),
    )


def rules() -> InstrumentRules:
    return InstrumentRules(
        quantity_step=Quantity(Decimal("0.001")),
        minimum_quantity=Quantity(Decimal("0.001")),
        maximum_quantity=Quantity(Decimal("100")),
        maximum_notional=Money(Decimal("1000000")),
        maximum_projected_stop_loss=Money(Decimal("1000000")),
    )


def test_every_required_fixture_has_independent_audit_fields() -> None:
    expected_names = {
        "price_step_levels.json",
        "equal_loss_linear.json",
        "equal_loss_curved.json",
        "delta_step_synthetic.json",
        "entry_percent_stops.json",
        "price_step_fraction_stops.json",
        "baseline_zero_cost.json",
        "baseline_with_fees.json",
        "recovery_with_costs.json",
        "quantity_rounding.json",
        "invalid_cases.json",
    }

    assert {path.name for path in FIXTURES.glob("*.json")} == expected_names
    for name in expected_names:
        assert set(fixture(name)) == {
            "description",
            "units",
            "inputs",
            "calculation_steps",
            "expected_outputs",
            "independent_reviewer",
        }


def test_standard_price_step_and_entry_stop_match_hand_values() -> None:
    levels_data = fixture("price_step_levels.json")["expected_outputs"]
    stop_data = fixture("entry_percent_stops.json")["expected_outputs"]
    level = standard_level()

    assert level.entry_price.value == Decimal(levels_data["boundaries"][0])
    assert level.tp_price.value == Decimal(levels_data["boundaries"][1])
    assert level.zone_option_loss_budget.value == Decimal(
        levels_data["zone_budgets"][0]
    )
    assert level.stop_distance.value == Decimal(stop_data["stop_distance"])
    assert level.stop_price.value == Decimal(stop_data["stop_price"])


def test_cost_bearing_standard_case_matches_hand_ledger() -> None:
    expected = fixture("baseline_with_fees.json")["expected_outputs"]
    result = StrategyMathEngine.size_baseline(
        standard_level(), fee_costs(), rules()
    )

    assert result.net_tp_profit_per_unit.value == Decimal(expected["net_tp_per_unit"])
    assert result.raw_quantity.value == Decimal(expected["raw_quantity"])
    assert result.submitted_quantity.value == Decimal(expected["submitted_quantity"])
    assert result.expected_net_tp_profit.value == Decimal(expected["expected_net_tp"])
    assert result.projected_net_stop_loss.value == Decimal(
        expected["projected_net_stop"]
    )
    assert result.overcoverage.value == Decimal(expected["overcoverage"])
    assert result.undercoverage.value == Decimal(expected["undercoverage"])


def test_recovery_with_costs_matches_hand_ledger() -> None:
    expected = fixture("recovery_with_costs.json")["expected_outputs"]
    result = StrategyMathEngine.size_recovery(
        standard_level(), Money(Decimal("0.7385889")), fee_costs(), rules()
    )

    assert result.required_budget.value == Decimal(expected["required_budget"])
    assert result.raw_quantity.value == Decimal(expected["raw_quantity_approx"])
    assert result.submitted_quantity.value == Decimal(expected["submitted_quantity"])
    assert result.expected_net_tp_profit.value == Decimal(expected["expected_net_tp"])
    assert result.overcoverage.value == Decimal(expected["overcoverage"])


@pytest.mark.legacy_characterization
def test_old_generator_produced_equal_usd_partitions() -> None:
    expected = fixture("price_step_levels.json")["expected_outputs"]
    assert expected["boundaries"] == ["3000", "2980", "2960", "2940", "2920", "2900"]
    assert expected["zone_budgets"] == ["2", "2", "2", "2", "2"]


@pytest.mark.legacy_characterization
def test_old_stop_was_fifteen_percent_of_price_step() -> None:
    expected = fixture("price_step_fraction_stops.json")["expected_outputs"]
    level = standard_level(legacy_stop=True)
    assert level.stop_distance.value == Decimal(expected["stop_distance"])
    assert level.stop_price.value == Decimal(expected["stop_price"])


@pytest.mark.legacy_characterization
def test_old_sizing_ignored_costs() -> None:
    expected = fixture("baseline_zero_cost.json")["expected_outputs"]
    assert Decimal(expected["raw_quantity"]) == Decimal("2") / Decimal("20")
    assert Decimal(expected["submitted_quantity"]) == Decimal("0.1")
