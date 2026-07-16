"""Authoritative level-boundary generation for every supported spacing mode."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from eth_credit_hedge.domain.strategy_math.contracts import (
    DeltaStepSpacingConfig,
    EqualOptionLossSpacingConfig,
    LevelCountSpacingConfig,
    LevelSpacingMode,
    OptionSpreadState,
    OptionValuationContext,
    OptionValuationMode,
    PriceStepSpacingConfig,
    SpacingConfig,
    require_valuation_context,
)
from eth_credit_hedge.domain.strategy_math.errors import (
    InvalidConfigurationError,
    NonMonotonicSpacingError,
    RootNotBracketedError,
    StrategyMathError,
    UnsupportedValuationError,
)
from eth_credit_hedge.domain.strategy_math.units import (
    DeltaExposure,
    Money,
    Price,
)
from eth_credit_hedge.domain.strategy_math.valuation import OptionValuationPort


_EQUAL_LOSS_TOLERANCE = Decimal("0.000000000001")
_EQUAL_LOSS_MAXIMUM_ITERATIONS = 200
_MONOTONICITY_SEGMENTS = 32


@dataclass(frozen=True, slots=True)
class SpacingLevel:
    level_id: int
    entry_price: Price
    tp_price: Price
    price_distance: Price
    target_delta: DeltaExposure | None
    entry_option_value: Money
    tp_option_value: Money
    zone_option_loss_budget: Money
    spacing_mode: LevelSpacingMode
    valuation_mode: OptionValuationMode

    def __post_init__(self) -> None:
        if self.level_id <= 0:
            raise InvalidConfigurationError("spacing level id must be positive")
        if self.tp_price >= self.entry_price:
            raise InvalidConfigurationError(
                "spacing level TP must be below its short-hedge entry"
            )
        if self.price_distance.value != (
            self.entry_price.value - self.tp_price.value
        ):
            raise InvalidConfigurationError(
                "spacing level price distance must equal entry minus TP"
            )
        expected_loss = (
            self.entry_option_value.value - self.tp_option_value.value
        )
        if expected_loss != self.zone_option_loss_budget.value:
            raise InvalidConfigurationError(
                "zone option-loss budget must equal entry value minus TP value"
            )
        if self.zone_option_loss_budget.value < 0:
            raise InvalidConfigurationError(
                "zone option-loss budget cannot be negative"
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
            "spacing_mode": self.spacing_mode.value,
            "valuation_mode": self.valuation_mode.value,
        }


class LevelSpacingEngine:
    """Build immutable level geometry from real option values or deltas."""

    def __init__(self, valuation: OptionValuationPort) -> None:
        self._valuation = valuation

    def build_levels(
        self,
        spread: OptionSpreadState,
        context: OptionValuationContext,
        spacing: SpacingConfig,
        *,
        as_of_utc: datetime,
    ) -> tuple[SpacingLevel, ...]:
        require_model_inputs = context.valuation_mode is not (
            OptionValuationMode.EXPIRATION
        )
        require_valuation_context(
            context,
            as_of_utc,
            require_delta=isinstance(spacing, DeltaStepSpacingConfig),
            require_model_inputs=require_model_inputs,
        )
        self._validate_context_legs(spread, context)
        if isinstance(spacing, LevelCountSpacingConfig):
            normalized = PriceStepSpacingConfig(
                Price(
                    (
                        spread.short_put_strike.value
                        - spread.long_put_strike.value
                    )
                    / Decimal(spacing.level_count)
                )
            )
            return self._build_price_step(
                spread,
                context,
                normalized,
                normalized_level_count=spacing.level_count,
            )
        if isinstance(spacing, PriceStepSpacingConfig):
            return self._build_price_step(spread, context, spacing)
        if isinstance(spacing, EqualOptionLossSpacingConfig):
            self._require_matching_mode(spacing.valuation_mode, context)
            return self._build_equal_option_loss(spread, context, spacing)
        self._require_matching_mode(spacing.valuation_mode, context)
        return self._build_delta_step(spread, context, spacing)

    def _build_price_step(
        self,
        spread: OptionSpreadState,
        context: OptionValuationContext,
        spacing: PriceStepSpacingConfig,
        *,
        normalized_level_count: int | None = None,
    ) -> tuple[SpacingLevel, ...]:
        if normalized_level_count is not None:
            # Evaluate the algebraically equivalent normalized boundaries in one
            # expression so repeating Decimal quotients preserve legacy results.
            width = (
                spread.short_put_strike.value - spread.long_put_strike.value
            )
            count = Decimal(normalized_level_count)
            boundaries = [
                Price(
                    spread.short_put_strike.value
                    - width * Decimal(index) / count
                )
                for index in range(normalized_level_count)
            ]
            boundaries.append(spread.long_put_strike)
        else:
            boundaries = [spread.short_put_strike]
            current = spread.short_put_strike.value
            end = spread.long_put_strike.value
            while current > end:
                current = max(current - spacing.price_step_usd.value, end)
                boundaries.append(Price(current))
        return self._levels_from_boundaries(
            spread,
            context,
            boundaries,
            spacing_mode=LevelSpacingMode.PRICE_STEP,
            target_deltas=None,
        )

    def _build_equal_option_loss(
        self,
        spread: OptionSpreadState,
        context: OptionValuationContext,
        spacing: EqualOptionLossSpacingConfig,
    ) -> tuple[SpacingLevel, ...]:
        start = spread.short_put_strike
        end = spread.long_put_strike
        def value(price: Price) -> Decimal:
            return self._value(spread, context, price).value

        _validate_strictly_increasing(value, end, start)
        boundaries = [start]
        current = start
        end_value = value(end)
        while current > end:
            current_value = value(current)
            target_value = current_value - spacing.target_zone_loss_usd.value
            if target_value <= end_value:
                next_price = end
            else:
                next_price = solve_monotonic_price(
                    value,
                    target_value,
                    minimum_price=end,
                    maximum_price=current,
                    absolute_tolerance=_EQUAL_LOSS_TOLERANCE,
                    relative_tolerance=_EQUAL_LOSS_TOLERANCE,
                    maximum_iterations=_EQUAL_LOSS_MAXIMUM_ITERATIONS,
                )
            if next_price >= current:
                raise NonMonotonicSpacingError(
                    "equal-loss solver produced a duplicate or ascending price"
                )
            boundaries.append(next_price)
            current = next_price
        return self._levels_from_boundaries(
            spread,
            context,
            boundaries,
            spacing_mode=LevelSpacingMode.EQUAL_OPTION_LOSS,
            target_deltas=None,
        )

    def _build_delta_step(
        self,
        spread: OptionSpreadState,
        context: OptionValuationContext,
        spacing: DeltaStepSpacingConfig,
    ) -> tuple[SpacingLevel, ...]:
        start = spacing.maximum_price
        end = spacing.minimum_price
        if (
            start > spread.short_put_strike
            or end < spread.long_put_strike
        ):
            raise InvalidConfigurationError(
                "delta solver interval must stay within the option spread strikes"
            )
        def delta(price: Price) -> Decimal:
            return self._delta(spread, context, price).value

        _validate_strictly_increasing(delta, end, start)
        start_delta = delta(start)
        end_delta = delta(end)
        boundaries = [start]
        target_deltas = [DeltaExposure(start_delta)]
        next_target = start_delta - spacing.delta_step.value
        while next_target > end_delta:
            next_price = solve_monotonic_price(
                delta,
                next_target,
                minimum_price=end,
                maximum_price=boundaries[-1],
                absolute_tolerance=spacing.solver_tolerance,
                relative_tolerance=spacing.solver_tolerance,
                maximum_iterations=spacing.maximum_iterations,
            )
            if next_price >= boundaries[-1]:
                raise NonMonotonicSpacingError(
                    "delta solver produced a duplicate or ascending price"
                )
            boundaries.append(next_price)
            target_deltas.append(DeltaExposure(next_target))
            next_target -= spacing.delta_step.value
        boundaries.append(end)
        return self._levels_from_boundaries(
            spread,
            context,
            boundaries,
            spacing_mode=LevelSpacingMode.DELTA_STEP,
            target_deltas=target_deltas,
        )

    def _levels_from_boundaries(
        self,
        spread: OptionSpreadState,
        context: OptionValuationContext,
        boundaries: list[Price],
        *,
        spacing_mode: LevelSpacingMode,
        target_deltas: list[DeltaExposure] | None,
    ) -> tuple[SpacingLevel, ...]:
        levels: list[SpacingLevel] = []
        for index, (entry, tp) in enumerate(
            zip(boundaries, boundaries[1:]),
            start=1,
        ):
            entry_value = self._value(spread, context, entry)
            tp_value = self._value(spread, context, tp)
            levels.append(
                SpacingLevel(
                    level_id=index,
                    entry_price=entry,
                    tp_price=tp,
                    price_distance=Price(entry.value - tp.value),
                    target_delta=(
                        None
                        if target_deltas is None
                        else target_deltas[index - 1]
                    ),
                    entry_option_value=entry_value,
                    tp_option_value=tp_value,
                    zone_option_loss_budget=Money(
                        entry_value.value - tp_value.value
                    ),
                    spacing_mode=spacing_mode,
                    valuation_mode=context.valuation_mode,
                )
            )
        return tuple(levels)

    def _value(
        self,
        spread: OptionSpreadState,
        context: OptionValuationContext,
        price: Price,
    ) -> Money:
        return self._valuation.value_at_price(spread, context, price)

    def _delta(
        self,
        spread: OptionSpreadState,
        context: OptionValuationContext,
        price: Price,
    ) -> DeltaExposure:
        return self._valuation.delta_at_price(spread, context, price)

    @staticmethod
    def _require_matching_mode(
        configured: OptionValuationMode,
        context: OptionValuationContext,
    ) -> None:
        if configured is not context.valuation_mode:
            raise UnsupportedValuationError(
                "spacing valuation mode must match the valuation context"
            )

    @staticmethod
    def _validate_context_legs(
        spread: OptionSpreadState,
        context: OptionValuationContext,
    ) -> None:
        if context.valuation_mode is OptionValuationMode.EXPIRATION:
            return
        if context.short_leg is None or context.long_leg is None:
            return
        if context.short_leg.strike != spread.short_put_strike:
            raise UnsupportedValuationError(
                "short valuation leg strike must match the option spread"
            )
        if context.long_leg.strike != spread.long_put_strike:
            raise UnsupportedValuationError(
                "long valuation leg strike must match the option spread"
            )


def solve_monotonic_price(
    function: Callable[[Price], Decimal],
    target: Decimal,
    *,
    minimum_price: Price,
    maximum_price: Price,
    absolute_tolerance: Decimal,
    relative_tolerance: Decimal,
    maximum_iterations: int,
) -> Price:
    """Solve one increasing Decimal curve inside a closed price bracket."""
    if minimum_price >= maximum_price:
        raise InvalidConfigurationError(
            "root solver minimum price must be below maximum price"
        )
    if absolute_tolerance <= 0 or relative_tolerance <= 0:
        raise InvalidConfigurationError("root solver tolerances must be positive")
    if maximum_iterations <= 0:
        raise InvalidConfigurationError(
            "root solver maximum iterations must be positive"
        )
    lower_value = function(minimum_price)
    upper_value = function(maximum_price)
    if not lower_value <= target <= upper_value:
        raise RootNotBracketedError(
            "root target is outside the bounded valuation interval"
        )
    if target == lower_value:
        return minimum_price
    if target == upper_value:
        return maximum_price

    lower = minimum_price.value
    upper = maximum_price.value
    for _ in range(maximum_iterations):
        midpoint = (lower + upper) / Decimal("2")
        midpoint_value = function(Price(midpoint))
        residual = midpoint_value - target
        relative_width = (upper - lower) / max(abs(midpoint), Decimal("1"))
        if abs(residual) <= absolute_tolerance or relative_width <= relative_tolerance:
            return Price(midpoint)
        if residual < 0:
            lower = midpoint
        else:
            upper = midpoint
    raise StrategyMathError(
        "bounded root solver did not converge within maximum_iterations"
    )


def _validate_strictly_increasing(
    function: Callable[[Price], Decimal],
    minimum_price: Price,
    maximum_price: Price,
) -> None:
    width = maximum_price.value - minimum_price.value
    previous = function(minimum_price)
    for index in range(1, _MONOTONICITY_SEGMENTS + 1):
        price = Price(
            minimum_price.value
            + width * Decimal(index) / Decimal(_MONOTONICITY_SEGMENTS)
        )
        current = function(price)
        if current <= previous:
            raise NonMonotonicSpacingError(
                "valuation curve must be strictly increasing with underlying price"
            )
        previous = current


__all__ = ["LevelSpacingEngine", "SpacingLevel", "solve_monotonic_price"]
