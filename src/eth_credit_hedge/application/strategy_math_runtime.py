"""Offline composition root and evidence recorder for authoritative strategy math."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from eth_credit_hedge.config import OperatorSimulationConfig
from eth_credit_hedge.domain.strategy_math import (
    DeltaExposure,
    ExpirationOptionValuation,
    LevelMath,
    Money,
    OptionLegValuationParameters,
    OptionSpreadState,
    OptionValuationContext,
    Price,
    Quantity,
    Seconds,
    SizingResult,
    StrategyMathEngine,
)


FORMULA_VERSION = "M1.6"
NOW = datetime(2026, 7, 17, tzinfo=UTC)


class MathEventType(str, Enum):
    LEVEL_GEOMETRY_CREATED = "LEVEL_GEOMETRY_CREATED"
    BASELINE_SIZING_CALCULATED = "BASELINE_SIZING_CALCULATED"
    RECOVERY_SIZING_CALCULATED = "RECOVERY_SIZING_CALCULATED"
    SIZING_REJECTED = "SIZING_REJECTED"
    COVERAGE_RECALCULATED = "COVERAGE_RECALCULATED"


class SyntheticQuadraticValuation:
    """Deterministic integration model: value=P² and delta=2P."""

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
        return DeltaExposure(Decimal("2") * underlying_price.value)


class StrategyMathRuntime:
    """Own one engine and expose calculation-free evidence consumers."""

    def __init__(self, config: OperatorSimulationConfig, output: Path) -> None:
        self.config = config
        self.output = output
        self.output.mkdir(parents=True, exist_ok=True)
        valuation = (
            ExpirationOptionValuation()
            if config.math.valuation.model == "EXPIRATION"
            else SyntheticQuadraticValuation()
        )
        self.engine = StrategyMathEngine(valuation)
        self._events: list[dict[str, object]] = []

    def build_levels(self) -> tuple[LevelMath, ...]:
        spread = OptionSpreadState(
            Price(self.config.short_put_strike),
            Price(self.config.long_put_strike),
            Quantity(self.config.option_quantity),
        )
        levels = self.engine.build_levels(
            spread,
            self._context(spread),
            self.config.math.spacing,
            self.config.math.stop,
            as_of_utc=NOW,
        )
        for level in levels:
            self._record(
                MathEventType.LEVEL_GEOMETRY_CREATED,
                inputs={"spread": asdict(spread)},
                units={"price": "USD/ETH", "option_budget": "USD"},
                output={"level": level.to_dict()},
            )
        return levels

    def size_baseline(self, level: LevelMath) -> SizingResult:
        result = self.engine.size_baseline(
            level,
            self.config.math.costs.execution_context(
                entry_price=level.entry_price.value,
                tp_price=level.tp_price.value,
                stop_price=level.stop_price.value,
            ),
            self.config.math.rounding.instrument,
            configured_buffer=Money(self.config.math.costs.baseline_buffer_usd),
            mode=self.config.math.rounding.mode,
        )
        self._record_sizing(MathEventType.BASELINE_SIZING_CALCULATED, level, result)
        return result

    def size_recovery(self, level: LevelMath, confirmed_debt: Money) -> SizingResult:
        result = self.engine.size_recovery(
            level,
            confirmed_debt,
            self.config.math.costs.execution_context(
                entry_price=level.entry_price.value,
                tp_price=level.tp_price.value,
                stop_price=level.stop_price.value,
            ),
            self.config.math.rounding.instrument,
            configured_buffer=Money(self.config.math.costs.recovery_buffer_usd),
            mode=self.config.math.rounding.mode,
        )
        self._record_sizing(MathEventType.RECOVERY_SIZING_CALCULATED, level, result)
        return result

    def dashboard_payload(self, level: LevelMath, sizing: SizingResult) -> dict[str, object]:
        """Return stored outputs only; no dashboard formula is evaluated here."""
        return {
            "spacing_mode": level.spacing_mode.value,
            "spacing_parameter": _spacing_parameter(self.config),
            "valuation_mode": level.valuation_mode.value,
            "stop_mode": level.stop_mode.value,
            "stop_parameter": _stop_parameter(self.config),
            "zone_budget": str(level.zone_option_loss_budget.value),
            "expected_costs": _jsonable(asdict(sizing.cost_breakdown)),
            "raw_quantity": str(sizing.raw_quantity.value),
            "submitted_quantity": str(sizing.submitted_quantity.value),
            "expected_net_tp": str(sizing.expected_net_tp_profit.value),
            "projected_net_stop": str(sizing.projected_net_stop_loss.value),
            "coverage": {
                "fully_covered": sizing.fully_covered,
                "overcoverage": str(sizing.overcoverage.value),
                "undercoverage": str(sizing.undercoverage.value),
            },
            "risk_projection": {
                "quantity": str(sizing.submitted_quantity.value),
                "projected_stop_loss": str(sizing.projected_net_stop_loss.value),
                "status": sizing.status.value,
            },
        }

    def persist(self) -> Path:
        path = self.output / "math_events.jsonl"
        path.write_text(
            "".join(json.dumps(event, sort_keys=True) + "\n" for event in self._events),
            encoding="utf-8",
        )
        return path

    @property
    def events(self) -> tuple[dict[str, object], ...]:
        return tuple(self._events)

    def _record_sizing(
        self,
        event_type: MathEventType,
        level: LevelMath,
        result: SizingResult,
    ) -> None:
        output = self.dashboard_payload(level, result)
        self._record(
            event_type,
            inputs={"level": level.to_dict(), "required_budget": str(result.required_budget.value)},
            units={"quantity": "ETH", "money": "USD", "price": "USD/ETH"},
            cost_breakdown=asdict(result.cost_breakdown),
            output=output,
        )
        self._record(
            MathEventType.COVERAGE_RECALCULATED,
            inputs={"submitted_quantity": str(result.submitted_quantity.value)},
            units={"quantity": "ETH", "coverage": "USD"},
            output=output["coverage"],
        )
        if result.status.value != "APPROVED":
            self._record(
                MathEventType.SIZING_REJECTED,
                inputs={"required_budget": str(result.required_budget.value)},
                units={"money": "USD"},
                output={"status": result.status.value},
            )

    def _record(
        self,
        event_type: MathEventType,
        *,
        inputs: object,
        units: object,
        output: object,
        cost_breakdown: object | None = None,
    ) -> None:
        self._events.append(
            {
                "event": event_type.value,
                "formula_version": FORMULA_VERSION,
                "configuration_hash": self.config.math.configuration_hash,
                "inputs": _jsonable(inputs),
                "units": units,
                "cost_breakdown": _jsonable(cost_breakdown or {}),
                "output_quantity": (
                    output.get("submitted_quantity")
                    if isinstance(output, dict)
                    else None
                ),
                "coverage": output.get("coverage") if isinstance(output, dict) else output,
                "output": _jsonable(output),
            }
        )

    def _context(self, spread: OptionSpreadState) -> OptionValuationContext:
        if self.config.math.valuation.model == "EXPIRATION":
            return OptionValuationContext(
                self.config.math.valuation.mode,
                NOW,
                NOW,
            )
        return OptionValuationContext(
            self.config.math.valuation.mode,
            NOW,
            NOW + timedelta(minutes=1),
            delta_source_available=True,
            time_to_expiry=Seconds(Decimal("86400")),
            short_leg=OptionLegValuationParameters(
                "SYNTHETIC-SHORT", spread.short_put_strike, quote_source="DETERMINISTIC"
            ),
            long_leg=OptionLegValuationParameters(
                "SYNTHETIC-LONG", spread.long_put_strike, quote_source="DETERMINISTIC"
            ),
        )


def _spacing_parameter(config: OperatorSimulationConfig) -> str:
    spacing = config.math.spacing
    for name in ("price_step_usd", "level_count", "target_zone_loss_usd", "delta_step"):
        if hasattr(spacing, name):
            value = getattr(spacing, name)
            return str(getattr(value, "value", value))
    raise AssertionError("known spacing configuration has no parameter")


def _stop_parameter(config: OperatorSimulationConfig) -> str:
    stop = config.math.stop
    value = stop.rate if hasattr(stop, "rate") else stop.fraction
    return str(value.value)


def _jsonable(value: object) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "value"):
        return _jsonable(getattr(value, "value"))
    return value


__all__ = [
    "FORMULA_VERSION",
    "MathEventType",
    "StrategyMathRuntime",
    "SyntheticQuadraticValuation",
]
