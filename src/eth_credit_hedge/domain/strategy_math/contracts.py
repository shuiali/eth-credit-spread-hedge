"""Immutable contracts for authoritative strategy mathematics."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Self, TypeAlias

from eth_credit_hedge.domain.strategy_math.errors import (
    DeltaSpacingUnavailableError,
    InvalidConfigurationError,
    InvalidUnitsError,
    UnsupportedValuationError,
)
from eth_credit_hedge.domain.strategy_math.units import (
    DeltaExposure,
    Money,
    Price,
    Quantity,
    Rate,
)


class _ContractEnum(str, Enum):
    @classmethod
    def parse(cls, value: str | Self) -> Self:
        if isinstance(value, cls):
            return value
        try:
            return cls(value.upper())
        except ValueError as exc:
            supported = ", ".join(item.value for item in cls)
            raise InvalidConfigurationError(
                f"unsupported {cls.__name__} {value!r}; expected one of {supported}"
            ) from exc


class LevelSpacingMode(_ContractEnum):
    PRICE_STEP = "PRICE_STEP"
    LEVEL_COUNT = "LEVEL_COUNT"
    EQUAL_OPTION_LOSS = "EQUAL_OPTION_LOSS"
    DELTA_STEP = "DELTA_STEP"


class StopMode(_ContractEnum):
    ENTRY_PERCENT = "ENTRY_PERCENT"
    PRICE_STEP_FRACTION = "PRICE_STEP_FRACTION"


class OptionValuationMode(_ContractEnum):
    EXPIRATION = "EXPIRATION"
    MARK_MODEL = "MARK_MODEL"
    EXECUTABLE_LIQUIDATION = "EXECUTABLE_LIQUIDATION"


class QuantityRoundingMode(_ContractEnum):
    FLOOR = "FLOOR"
    CEIL = "CEIL"
    NEAREST = "NEAREST"


@dataclass(frozen=True, slots=True)
class OptionSpreadState:
    short_put_strike: Price
    long_put_strike: Price
    option_quantity: Quantity

    def __post_init__(self) -> None:
        if self.short_put_strike <= self.long_put_strike:
            raise InvalidConfigurationError(
                "short put strike must be above long put strike"
            )


@dataclass(frozen=True, slots=True)
class OptionValuationContext:
    valuation_mode: OptionValuationMode
    observed_at_utc: datetime
    valid_until_utc: datetime
    delta_source_available: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "valuation_mode",
            OptionValuationMode.parse(self.valuation_mode),
        )
        if self.observed_at_utc.tzinfo is None or self.valid_until_utc.tzinfo is None:
            raise UnsupportedValuationError(
                "valuation timestamps must be timezone-aware UTC instants"
            )
        if self.valid_until_utc < self.observed_at_utc:
            raise UnsupportedValuationError(
                "valuation valid-until timestamp cannot precede observation"
            )

    def require_fresh(self, as_of_utc: datetime, *, require_delta: bool = False) -> None:
        if as_of_utc.tzinfo is None:
            raise UnsupportedValuationError(
                "valuation freshness time must be timezone-aware"
            )
        if not self.observed_at_utc <= as_of_utc <= self.valid_until_utc:
            raise UnsupportedValuationError(
                "valuation context is stale or not yet valid at the requested time"
            )
        if require_delta and not self.delta_source_available:
            raise DeltaSpacingUnavailableError(
                "DELTA_STEP requires a current option-delta source"
            )


def require_valuation_context(
    context: OptionValuationContext | None,
    as_of_utc: datetime,
    *,
    require_delta: bool = False,
) -> OptionValuationContext:
    if context is None:
        raise UnsupportedValuationError(
            "option valuation context is required and cannot be absent"
        )
    context.require_fresh(as_of_utc, require_delta=require_delta)
    return context


@dataclass(frozen=True, slots=True)
class PriceStepSpacingConfig:
    price_step_usd: Price

    @property
    def mode(self) -> LevelSpacingMode:
        return LevelSpacingMode.PRICE_STEP


@dataclass(frozen=True, slots=True)
class LevelCountSpacingConfig:
    level_count: int

    def __post_init__(self) -> None:
        if self.level_count <= 0:
            raise InvalidConfigurationError("level count must be positive")

    @property
    def mode(self) -> LevelSpacingMode:
        return LevelSpacingMode.LEVEL_COUNT


@dataclass(frozen=True, slots=True)
class EqualOptionLossSpacingConfig:
    target_zone_loss_usd: Money
    valuation_mode: OptionValuationMode

    def __post_init__(self) -> None:
        if self.target_zone_loss_usd.value <= 0:
            raise InvalidConfigurationError(
                "target zone option-loss budget must be positive USD"
            )
        object.__setattr__(
            self,
            "valuation_mode",
            OptionValuationMode.parse(self.valuation_mode),
        )

    @property
    def mode(self) -> LevelSpacingMode:
        return LevelSpacingMode.EQUAL_OPTION_LOSS


@dataclass(frozen=True, slots=True)
class DeltaStepSpacingConfig:
    delta_step: DeltaExposure
    valuation_mode: OptionValuationMode
    minimum_price: Price
    maximum_price: Price
    solver_tolerance: Decimal
    maximum_iterations: int

    def __post_init__(self) -> None:
        valuation_mode = OptionValuationMode.parse(self.valuation_mode)
        object.__setattr__(self, "valuation_mode", valuation_mode)
        if self.delta_step.value <= 0:
            raise InvalidConfigurationError(
                "delta step must be positive ETH-equivalent exposure"
            )
        if valuation_mode is OptionValuationMode.EXPIRATION:
            raise DeltaSpacingUnavailableError(
                "DELTA_STEP cannot use terminal EXPIRATION valuation; "
                "a real option-delta source is required"
            )
        if self.minimum_price >= self.maximum_price:
            raise InvalidConfigurationError(
                "delta solver minimum price must be below maximum price"
            )
        if not self.solver_tolerance.is_finite() or self.solver_tolerance <= 0:
            raise InvalidConfigurationError(
                "delta solver tolerance must be a positive finite Decimal"
            )
        if self.maximum_iterations <= 0:
            raise InvalidConfigurationError(
                "delta solver maximum iterations must be positive"
            )

    @property
    def mode(self) -> LevelSpacingMode:
        return LevelSpacingMode.DELTA_STEP


SpacingConfig: TypeAlias = (
    PriceStepSpacingConfig
    | LevelCountSpacingConfig
    | EqualOptionLossSpacingConfig
    | DeltaStepSpacingConfig
)


@dataclass(frozen=True, slots=True)
class EntryPercentStopConfig:
    rate: Rate

    def __post_init__(self) -> None:
        if self.rate.value <= 0:
            raise InvalidConfigurationError("entry-percent stop rate must be positive")

    @property
    def mode(self) -> StopMode:
        return StopMode.ENTRY_PERCENT


@dataclass(frozen=True, slots=True)
class PriceStepFractionStopConfig:
    fraction: Rate

    def __post_init__(self) -> None:
        if self.fraction.value <= 0:
            raise InvalidConfigurationError(
                "price-step stop fraction must be positive"
            )

    @property
    def mode(self) -> StopMode:
        return StopMode.PRICE_STEP_FRACTION


StopConfig: TypeAlias = EntryPercentStopConfig | PriceStepFractionStopConfig


@dataclass(frozen=True, slots=True)
class LevelMath:
    level_id: int
    entry_price: Price
    tp_price: Price
    price_distance: Price
    target_delta: DeltaExposure | None
    entry_option_value: Money
    tp_option_value: Money
    zone_option_loss_budget: Money
    stop_price: Price
    stop_distance: Price
    spacing_mode: LevelSpacingMode
    stop_mode: StopMode
    valuation_mode: OptionValuationMode

    def __post_init__(self) -> None:
        if self.level_id <= 0:
            raise InvalidConfigurationError("level id must be positive")
        if self.tp_price >= self.entry_price:
            raise InvalidConfigurationError(
                "short-hedge take-profit price must be below entry price"
            )
        if self.stop_price <= self.entry_price:
            raise InvalidConfigurationError(
                "short-hedge stop price must be above entry price"
            )
        if self.price_distance.value != self.entry_price.value - self.tp_price.value:
            raise InvalidUnitsError(
                "price distance must equal entry price minus take-profit price"
            )
        if self.stop_distance.value != self.stop_price.value - self.entry_price.value:
            raise InvalidUnitsError(
                "stop distance must equal stop price minus entry price"
            )
        if self.zone_option_loss_budget.value < 0:
            raise InvalidConfigurationError(
                "zone option-loss budget cannot be negative"
            )
        object.__setattr__(
            self, "spacing_mode", LevelSpacingMode.parse(self.spacing_mode)
        )
        object.__setattr__(self, "stop_mode", StopMode.parse(self.stop_mode))
        object.__setattr__(
            self,
            "valuation_mode",
            OptionValuationMode.parse(self.valuation_mode),
        )

    def to_dict(self) -> dict[str, int | str | None]:
        return {
            "level_id": self.level_id,
            "entry_price": str(self.entry_price.value),
            "tp_price": str(self.tp_price.value),
            "price_distance": str(self.price_distance.value),
            "target_delta": (
                None if self.target_delta is None else str(self.target_delta.value)
            ),
            "entry_option_value": str(self.entry_option_value.value),
            "tp_option_value": str(self.tp_option_value.value),
            "zone_option_loss_budget": str(self.zone_option_loss_budget.value),
            "stop_price": str(self.stop_price.value),
            "stop_distance": str(self.stop_distance.value),
            "spacing_mode": self.spacing_mode.value,
            "stop_mode": self.stop_mode.value,
            "valuation_mode": self.valuation_mode.value,
        }


@dataclass(frozen=True, slots=True)
class CoverageResult:
    required_budget: Money
    expected_net_profit: Money
    overcoverage: Money
    undercoverage: Money
    fully_covered: bool

    def __post_init__(self) -> None:
        if self.required_budget.value < 0:
            raise InvalidConfigurationError("required coverage budget cannot be negative")
        if self.overcoverage.value < 0 or self.undercoverage.value < 0:
            raise InvalidConfigurationError(
                "coverage overage and shortage must be nonnegative"
            )
        if self.overcoverage.value > 0 and self.undercoverage.value > 0:
            raise InvalidConfigurationError(
                "coverage cannot be both overcovered and undercovered"
            )
        difference = self.expected_net_profit.value - self.required_budget.value
        if difference != self.overcoverage.value - self.undercoverage.value:
            raise InvalidConfigurationError(
                "coverage amounts must reconcile to expected net profit minus budget"
            )
        if self.fully_covered is not (self.undercoverage.value == 0):
            raise InvalidConfigurationError(
                "fully covered must be false whenever undercoverage is positive"
            )

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "required_budget": str(self.required_budget.value),
            "expected_net_profit": str(self.expected_net_profit.value),
            "overcoverage": str(self.overcoverage.value),
            "undercoverage": str(self.undercoverage.value),
            "fully_covered": self.fully_covered,
        }


_SPACING_FIELDS: dict[LevelSpacingMode, frozenset[str]] = {
    LevelSpacingMode.PRICE_STEP: frozenset({"price_step_usd"}),
    LevelSpacingMode.LEVEL_COUNT: frozenset({"level_count"}),
    LevelSpacingMode.EQUAL_OPTION_LOSS: frozenset(
        {"target_zone_loss_usd", "valuation_mode"}
    ),
    LevelSpacingMode.DELTA_STEP: frozenset(
        {
            "delta_step",
            "valuation_mode",
            "minimum_price",
            "maximum_price",
            "solver_tolerance",
            "maximum_iterations",
        }
    ),
}


def validate_spacing_configuration_fields(
    mode: LevelSpacingMode | str,
    fields: Collection[str],
) -> None:
    """Reject mixed configs and legacy names before values reach formulas."""
    parsed_mode = LevelSpacingMode.parse(mode)
    supplied = set(fields)
    ambiguous = supplied.intersection({"delta_spacing", "delta_grid"})
    if ambiguous:
        names = ", ".join(sorted(ambiguous))
        raise InvalidUnitsError(
            f"ambiguous USD price-spacing field(s) {names}; use price_step_usd"
        )
    if parsed_mode is not LevelSpacingMode.DELTA_STEP and "delta_step" in supplied:
        raise InvalidUnitsError(
            "delta_step is reserved for ETH-equivalent option exposure; "
            "use price_step_usd for USD/ETH spacing"
        )
    expected = _SPACING_FIELDS[parsed_mode]
    missing = expected - supplied
    extra = supplied - expected
    if missing or extra:
        details: list[str] = []
        if missing:
            details.append("missing " + ", ".join(sorted(missing)))
        if extra:
            details.append("unexpected " + ", ".join(sorted(extra)))
        raise InvalidConfigurationError(
            f"{parsed_mode.value} spacing configuration is invalid: "
            + "; ".join(details)
        )


__all__ = [
    "CoverageResult",
    "DeltaStepSpacingConfig",
    "EntryPercentStopConfig",
    "EqualOptionLossSpacingConfig",
    "LevelCountSpacingConfig",
    "LevelMath",
    "LevelSpacingMode",
    "OptionSpreadState",
    "OptionValuationContext",
    "OptionValuationMode",
    "PriceStepFractionStopConfig",
    "PriceStepSpacingConfig",
    "QuantityRoundingMode",
    "SpacingConfig",
    "StopConfig",
    "StopMode",
    "require_valuation_context",
    "validate_spacing_configuration_fields",
]
