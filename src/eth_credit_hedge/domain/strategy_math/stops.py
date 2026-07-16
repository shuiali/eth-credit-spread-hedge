"""Authoritative short-hedge stop and take-profit geometry."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from eth_credit_hedge.domain.strategy_math.contracts import (
    EntryPercentStopConfig,
    LevelMath,
    OptionSpreadState,
    OptionValuationContext,
    QuantityRoundingMode,
    PriceStepFractionStopConfig,
    SpacingConfig,
    StopConfig,
)
from eth_credit_hedge.domain.strategy_math.errors import InvalidConfigurationError
from eth_credit_hedge.domain.strategy_math.costs import ExecutionCostContext
from eth_credit_hedge.domain.strategy_math.quantization import InstrumentRules
from eth_credit_hedge.domain.strategy_math.sizing import SizingResult, size_hedge
from eth_credit_hedge.domain.strategy_math.spacing import (
    LevelSpacingEngine,
    SpacingLevel,
)
from eth_credit_hedge.domain.strategy_math.units import Money, Price
from eth_credit_hedge.domain.strategy_math.valuation import OptionValuationPort


class StopGeometryEngine:
    """Decorate solved spacing boundaries without recomputing their TP geometry."""

    def build_levels(
        self,
        levels: tuple[SpacingLevel, ...],
        stop: StopConfig,
        *,
        maximum_stop_price: Price | None = None,
    ) -> tuple[LevelMath, ...]:
        return tuple(
            self.build_level(
                level,
                stop,
                maximum_stop_price=maximum_stop_price,
            )
            for level in levels
        )

    def build_level(
        self,
        level: SpacingLevel,
        stop: StopConfig,
        *,
        maximum_stop_price: Price | None = None,
    ) -> LevelMath:
        stop_price = self.stop_price(level.entry_price, level.price_distance, stop)
        distance = Price(stop_price.value - level.entry_price.value)
        if maximum_stop_price is not None and stop_price > maximum_stop_price:
            raise InvalidConfigurationError(
                "stop price exceeds the configured operational maximum"
            )
        return LevelMath(
            level_id=level.level_id,
            entry_price=level.entry_price,
            tp_price=level.tp_price,
            price_distance=level.price_distance,
            target_delta=level.target_delta,
            entry_option_value=level.entry_option_value,
            tp_option_value=level.tp_option_value,
            zone_option_loss_budget=level.zone_option_loss_budget,
            stop_price=stop_price,
            stop_distance=distance,
            spacing_mode=level.spacing_mode,
            stop_mode=stop.mode,
            valuation_mode=level.valuation_mode,
        )

    @staticmethod
    def stop_distance(
        entry_price: Price,
        price_distance: Price,
        stop: StopConfig,
    ) -> Price:
        if isinstance(stop, EntryPercentStopConfig):
            distance = entry_price.value * stop.rate.value
        elif isinstance(stop, PriceStepFractionStopConfig):
            distance = price_distance.value * stop.fraction.value
        else:  # pragma: no cover - closed union guarded for runtime misuse
            raise InvalidConfigurationError("unsupported stop configuration")
        if distance <= 0:
            raise InvalidConfigurationError("stop distance must be positive")
        return Price(distance)

    @classmethod
    def stop_price(
        cls,
        entry_price: Price,
        price_distance: Price,
        stop: StopConfig,
    ) -> Price:
        distance = cls.stop_distance(entry_price, price_distance, stop)
        return Price(entry_price.value + distance.value)


class StrategyMathEngine:
    """Single public façade for level spacing and stop geometry."""

    def __init__(self, valuation: OptionValuationPort) -> None:
        self._spacing = LevelSpacingEngine(valuation)
        self._stops = StopGeometryEngine()

    def build_levels(
        self,
        spread: OptionSpreadState,
        market: OptionValuationContext,
        spacing: SpacingConfig,
        stop: StopConfig,
        *,
        as_of_utc: datetime,
        maximum_stop_price: Price | None = None,
    ) -> tuple[LevelMath, ...]:
        spacing_levels = self._spacing.build_levels(
            spread,
            market,
            spacing,
            as_of_utc=as_of_utc,
        )
        return self._stops.build_levels(
            spacing_levels,
            stop,
            maximum_stop_price=maximum_stop_price,
        )

    @staticmethod
    def size_baseline(
        level: LevelMath,
        costs: ExecutionCostContext,
        instrument: InstrumentRules,
        *,
        configured_buffer: Money = Money(Decimal("0")),
        mode: QuantityRoundingMode = QuantityRoundingMode.CEIL,
    ) -> SizingResult:
        return size_hedge(
            role="BASELINE",
            zone_option_loss_budget=level.zone_option_loss_budget,
            confirmed_recovery_debt=Money(Decimal("0")),
            configured_buffer=configured_buffer,
            costs=costs,
            instrument=instrument,
            mode=mode,
        )

    @staticmethod
    def size_recovery(
        level: LevelMath,
        confirmed_debt: Money,
        costs: ExecutionCostContext,
        instrument: InstrumentRules,
        *,
        configured_buffer: Money = Money(Decimal("0")),
        mode: QuantityRoundingMode = QuantityRoundingMode.CEIL,
    ) -> SizingResult:
        return size_hedge(
            role="RECOVERY",
            zone_option_loss_budget=level.zone_option_loss_budget,
            confirmed_recovery_debt=confirmed_debt,
            configured_buffer=configured_buffer,
            costs=costs,
            instrument=instrument,
            mode=mode,
        )

    @staticmethod
    def size_budget(
        *,
        role: Literal["BASELINE", "RECOVERY"],
        zone_option_loss_budget: Money,
        confirmed_recovery_debt: Money,
        configured_buffer: Money,
        costs: ExecutionCostContext,
        instrument: InstrumentRules,
        mode: QuantityRoundingMode = QuantityRoundingMode.CEIL,
    ) -> SizingResult:
        if role not in ("BASELINE", "RECOVERY"):
            raise ValueError("sizing role must be BASELINE or RECOVERY")
        return size_hedge(
            role=role,
            zone_option_loss_budget=zone_option_loss_budget,
            confirmed_recovery_debt=confirmed_recovery_debt,
            configured_buffer=configured_buffer,
            costs=costs,
            instrument=instrument,
            mode=mode,
        )


__all__ = ["StopGeometryEngine", "StrategyMathEngine"]
