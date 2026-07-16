"""Run authoritative strategy mathematics entirely offline."""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Sequence

from eth_credit_hedge.application.strategy_math_runtime import StrategyMathRuntime
from eth_credit_hedge.config import (
    OperatorSimulationConfig,
    QuantityRoundingConfig,
    StrategyCostConfig,
    StrategyMathConfig,
    ValuationConfig,
    load_operator_simulation_config,
)
from eth_credit_hedge.domain.live_recovery import (
    RecoveryDebtState,
    add_confirmed_stop_debt,
    allocate_confirmed_debt,
    settle_recovery_take_profit,
)
from eth_credit_hedge.domain.strategy_math import (
    DeltaExposure,
    DeltaSpacingUnavailableError,
    DeltaStepSpacingConfig,
    EntryPercentStopConfig,
    EqualOptionLossSpacingConfig,
    InstrumentRules,
    LevelCountSpacingConfig,
    Money,
    OptionValuationMode,
    Price,
    PriceStepFractionStopConfig,
    PriceStepSpacingConfig,
    Quantity,
    Rate,
    calculate_actual_stop_debt,
)


SCENARIOS = (
    "price_step_zero_cost",
    "price_step_with_fees",
    "entry_percent_stop",
    "price_step_fraction_stop",
    "stop_reentry_recovery",
    "quantity_rounding",
    "equal_option_loss_curved",
    "delta_step_unavailable",
    "synthetic_delta_step",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    simulate = subparsers.add_parser("simulate")
    simulate.add_argument("--config", type=Path, required=True)
    simulate.add_argument("--scenario", choices=(*SCENARIOS, "all"), required=True)
    simulate.add_argument("--output", type=Path, required=True)
    return parser


def run_simulation(
    config: OperatorSimulationConfig,
    scenario: str,
    output: Path,
) -> dict[str, object]:
    names = SCENARIOS if scenario == "all" else (scenario,)
    results = {
        name: _run_named_scenario(config, name, output / name) for name in names
    }
    payload: dict[str, object] = {
        "offline_only": True,
        "scenario": scenario,
        "results": results,
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "summary.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_operator_simulation_config(args.config)
    payload = run_simulation(config, args.scenario, args.output)
    print(json.dumps(payload, sort_keys=True))
    return 0


def _run_named_scenario(
    base: OperatorSimulationConfig,
    name: str,
    output: Path,
) -> dict[str, object]:
    if name == "delta_step_unavailable":
        return _delta_unavailable(base, output)
    config = _scenario_config(base, name)
    runtime = StrategyMathRuntime(config, output)
    levels = runtime.build_levels()
    baseline = runtime.size_baseline(levels[0])
    payload = runtime.dashboard_payload(levels[0], baseline)
    payload["level_distances"] = [str(level.price_distance.value) for level in levels]
    payload["level_entries"] = [str(level.entry_price.value) for level in levels]
    if name == "entry_percent_stop":
        payload["perturbed_stop_distance"] = _perturbed_stop(config, output, False)
    elif name == "price_step_fraction_stop":
        payload["perturbed_stop_distance"] = _perturbed_stop(config, output, True)
    elif name == "stop_reentry_recovery":
        actual = calculate_actual_stop_debt(
            entry_price=levels[0].entry_price,
            stop_fill_price=levels[0].stop_price,
            stop_reference_price=levels[0].stop_price,
            quantity=baseline.submitted_quantity,
            allocated_entry_fees=Money(
                baseline.cost_breakdown.entry_fee_per_unit.value
                * baseline.submitted_quantity.value
            ),
            stop_fees=Money(
                baseline.cost_breakdown.stop_fee_per_unit.value
                * baseline.submitted_quantity.value
            ),
            funding_pnl=Money(
                baseline.cost_breakdown.funding_to_stop_per_unit.value
                * baseline.submitted_quantity.value
            ),
        )
        recovery = runtime.size_recovery(levels[0], actual.total_debt)
        debt = allocate_confirmed_debt(
            add_confirmed_stop_debt(RecoveryDebtState.empty(), actual.total_debt.value)
        )
        settled = settle_recovery_take_profit(
            debt,
            realized_take_profit=recovery.expected_net_tp_profit.value,
            zone_budget=levels[0].zone_option_loss_budget.value,
        )
        payload.update(
            {
                "actual_stop_debt": str(actual.total_debt.value),
                "recovery_raw_quantity": str(recovery.raw_quantity.value),
                "recovery_submitted_quantity": str(recovery.submitted_quantity.value),
                "recovery_expected_net_tp": str(recovery.expected_net_tp_profit.value),
                "remaining_confirmed_debt": str(settled.confirmed_debt),
            }
        )
    elif name == "quantity_rounding":
        rejected_config = replace(
            config,
            math=replace(
                config.math,
                rounding=QuantityRoundingConfig(
                    config.math.rounding.mode,
                    replace(
                        config.math.rounding.instrument,
                        maximum_quantity=Quantity(Decimal("0.001")),
                    ),
                ),
            ),
        )
        rejected = StrategyMathRuntime(rejected_config, output / "risk_rejection")
        rejected_level = rejected.build_levels()[0]
        rejected_result = rejected.size_baseline(rejected_level)
        rejected.persist()
        payload["risk_rejection"] = rejected_result.status.value
    runtime.persist()
    return payload


def _scenario_config(base: OperatorSimulationConfig, name: str) -> OperatorSimulationConfig:
    zero = StrategyCostConfig()
    if name == "price_step_zero_cost":
        return replace(base, math=replace(base.math, spacing=PriceStepSpacingConfig(Price(Decimal("20"))), costs=zero))
    if name == "price_step_with_fees":
        return replace(base, math=replace(base.math, spacing=PriceStepSpacingConfig(Price(Decimal("20")))))
    if name == "entry_percent_stop":
        return replace(base, math=replace(base.math, spacing=LevelCountSpacingConfig(5), stop=EntryPercentStopConfig(Rate(Decimal("0.0015")))))
    if name == "price_step_fraction_stop":
        return replace(base, math=replace(base.math, spacing=LevelCountSpacingConfig(5), stop=PriceStepFractionStopConfig(Rate(Decimal("0.15")))))
    if name == "quantity_rounding":
        return replace(base, math=replace(base.math, costs=zero, rounding=replace(base.math.rounding, instrument=replace(base.math.rounding.instrument, quantity_step=Quantity(Decimal("0.03"))))))
    if name == "equal_option_loss_curved":
        return _synthetic(base, EqualOptionLossSpacingConfig(Money(Decimal("36")), OptionValuationMode.MARK_MODEL))
    if name == "synthetic_delta_step":
        return _synthetic(base, DeltaStepSpacingConfig(DeltaExposure(Decimal("4")), OptionValuationMode.MARK_MODEL, Price(Decimal("4")), Price(Decimal("10")), Decimal("1e-12"), 200))
    return base


def _synthetic(base: OperatorSimulationConfig, spacing: object) -> OperatorSimulationConfig:
    return OperatorSimulationConfig(
        StrategyMathConfig(
            spacing=spacing,  # type: ignore[arg-type]
            stop=EntryPercentStopConfig(Rate(Decimal("0.01"))),
            valuation=ValuationConfig(OptionValuationMode.MARK_MODEL, "SYNTHETIC_QUADRATIC"),
            costs=StrategyCostConfig(),
            rounding=QuantityRoundingConfig(
                base.math.rounding.mode,
                InstrumentRules.exact(),
            ),
        ),
        Decimal("10"),
        Decimal("4"),
        Decimal("1"),
    )


def _perturbed_stop(config: OperatorSimulationConfig, output: Path, fraction: bool) -> str:
    changed = replace(config, math=replace(config.math, spacing=LevelCountSpacingConfig(10)))
    runtime = StrategyMathRuntime(changed, output / "level_count_10")
    level = runtime.build_levels()[0]
    runtime.persist()
    if fraction:
        assert level.stop_mode.value == "PRICE_STEP_FRACTION"
    return str(level.stop_distance.value)


def _delta_unavailable(base: OperatorSimulationConfig, output: Path) -> dict[str, object]:
    try:
        invalid = replace(
            base,
            math=replace(
                base.math,
                spacing=DeltaStepSpacingConfig(
                    DeltaExposure(Decimal("1")),
                    OptionValuationMode.EXPIRATION,
                    Price(base.long_put_strike),
                    Price(base.short_put_strike),
                    Decimal("1e-12"),
                    100,
                ),
            ),
        )
        StrategyMathRuntime(invalid, output).build_levels()
    except DeltaSpacingUnavailableError as exc:
        return {"safe_failure": True, "error": str(exc)}
    raise AssertionError("terminal DELTA_STEP must fail safely")


if __name__ == "__main__":
    raise SystemExit(main())
