"""Single operator configuration path for authoritative strategy mathematics."""

from __future__ import annotations

import hashlib
import json
import tomllib
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path

from eth_credit_hedge.config.schema import StrategyCostConfig
from eth_credit_hedge.domain.strategy_math import (
    InstrumentRules,
    Money,
    OptionValuationMode,
    Quantity,
    QuantityRoundingMode,
    SpacingConfig,
    StopConfig,
    parse_spacing_configuration,
    parse_stop_configuration,
)


@dataclass(frozen=True, slots=True)
class ValuationConfig:
    mode: OptionValuationMode
    model: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", OptionValuationMode.parse(self.mode))
        normalized = self.model.strip().upper()
        if normalized not in {"EXPIRATION", "SYNTHETIC_QUADRATIC"}:
            raise ValueError("valuation model must be EXPIRATION or SYNTHETIC_QUADRATIC")
        if normalized == "EXPIRATION" and self.mode is not OptionValuationMode.EXPIRATION:
            raise ValueError("EXPIRATION model requires EXPIRATION valuation mode")
        if normalized == "SYNTHETIC_QUADRATIC" and self.mode is OptionValuationMode.EXPIRATION:
            raise ValueError("synthetic quadratic model requires a non-terminal mode")
        object.__setattr__(self, "model", normalized)


@dataclass(frozen=True, slots=True)
class QuantityRoundingConfig:
    mode: QuantityRoundingMode
    instrument: InstrumentRules

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", QuantityRoundingMode.parse(self.mode))


@dataclass(frozen=True, slots=True)
class StrategyMathConfig:
    spacing: SpacingConfig
    stop: StopConfig
    valuation: ValuationConfig
    costs: StrategyCostConfig
    rounding: QuantityRoundingConfig

    @property
    def configuration_hash(self) -> str:
        payload = json.dumps(_jsonable(asdict(self)), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class OperatorSimulationConfig:
    math: StrategyMathConfig
    short_put_strike: Decimal
    long_put_strike: Decimal
    option_quantity: Decimal


def load_operator_simulation_config(path: Path) -> OperatorSimulationConfig:
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    math = _mapping(raw, "strategy_math")
    spread = _mapping(raw, "spread")
    spacing = _mapping(math, "spacing")
    stop = _mapping(math, "stop")
    valuation = _mapping(math, "valuation")
    costs = _mapping(math, "costs")
    rounding = _mapping(math, "rounding")
    return OperatorSimulationConfig(
        math=StrategyMathConfig(
            spacing=parse_spacing_configuration(str(spacing["mode"]), _without(spacing, "mode")),
            stop=parse_stop_configuration(str(stop["mode"]), _without(stop, "mode")),
            valuation=ValuationConfig(
                OptionValuationMode.parse(str(valuation["mode"])),
                str(valuation["model"]),
            ),
            costs=StrategyCostConfig(**{key: Decimal(str(value)) for key, value in costs.items()}),
            rounding=QuantityRoundingConfig(
                QuantityRoundingMode.parse(str(rounding["mode"])),
                InstrumentRules(
                    quantity_step=Quantity(_decimal(rounding, "quantity_step")),
                    minimum_quantity=Quantity(_decimal(rounding, "minimum_quantity")),
                    maximum_quantity=Quantity(_decimal(rounding, "maximum_quantity")),
                    maximum_notional=Money(_decimal(rounding, "maximum_notional")),
                    maximum_projected_stop_loss=Money(
                        _decimal(rounding, "maximum_projected_stop_loss")
                    ),
                ),
            ),
        ),
        short_put_strike=_decimal(spread, "short_put_strike"),
        long_put_strike=_decimal(spread, "long_put_strike"),
        option_quantity=_decimal(spread, "option_quantity"),
    )


def _mapping(values: Mapping[str, object], name: str) -> Mapping[str, object]:
    value = values.get(name)
    if not isinstance(value, Mapping):
        raise ValueError(f"missing [{name}] configuration section")
    return value


def _without(values: Mapping[str, object], name: str) -> dict[str, object]:
    return {key: value for key, value in values.items() if key != name}


def _decimal(values: Mapping[str, object], name: str) -> Decimal:
    try:
        value = Decimal(str(values[name]))
    except (KeyError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite decimal") from exc
    if not value.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    return value


def _jsonable(value: object) -> object:
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
    "OperatorSimulationConfig",
    "QuantityRoundingConfig",
    "StrategyMathConfig",
    "ValuationConfig",
    "load_operator_simulation_config",
]
