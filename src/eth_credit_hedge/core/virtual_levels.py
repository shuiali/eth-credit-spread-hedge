"""Deterministic virtual hedge-level generation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum

from eth_credit_hedge.core.credit_spread import (
    CreditSpread,
    DecimalLike,
    ZERO,
    to_decimal,
)
from eth_credit_hedge.domain.strategy_math import (
    EntryPercentStopConfig,
    ExpirationOptionValuation,
    LevelCountSpacingConfig,
    LevelSpacingMode,
    OptionSpreadState,
    OptionValuationContext,
    OptionValuationMode,
    Price,
    PriceStepSpacingConfig,
    Quantity,
    InstrumentRules,
    Money,
    Rate,
    SpacingConfig,
    StopConfig,
    StopMode,
    StrategyMathEngine,
)
from eth_credit_hedge.config import StrategyCostConfig


class LevelState(str, Enum):
    READY = "READY"
    ACTIVE = "ACTIVE"
    PAID = "PAID"
    LOCKED = "LOCKED"


@dataclass(slots=True)
class HedgeLevel:
    level_id: int
    entry_price: Decimal
    tp_price: Decimal
    stop_price: Decimal
    option_budget: Decimal
    spacing_mode: LevelSpacingMode = LevelSpacingMode.PRICE_STEP
    stop_mode: StopMode = StopMode.ENTRY_PERCENT
    stop_parameter: Decimal = Decimal("0.0015")
    state: LevelState = LevelState.READY
    attempts: int = 0
    active_quantity: Decimal = ZERO
    active_is_floor: bool = False
    entry_armed: bool = False
    recovery_debt: Decimal = ZERO
    recovery_tps_remaining: int = 0
    realized_stop_losses: Decimal = ZERO
    realized_tp_profit: Decimal = ZERO
    stop_loss_history: list[Decimal] = field(default_factory=list)
    active_recovery_allocations: dict[int, Decimal] = field(default_factory=dict)
    active_net_tp_profit_per_unit: Decimal = ZERO
    active_net_stop_loss_per_unit: Decimal = ZERO

    def __post_init__(self) -> None:
        if self.entry_price <= self.tp_price:
            raise ValueError("short-hedge entry must exceed take profit")
        if self.stop_price <= self.entry_price:
            raise ValueError("short-hedge stop must exceed entry")
        if self.option_budget <= ZERO:
            raise ValueError("level option budget must be positive")
        if not self.stop_parameter.is_finite() or self.stop_parameter <= ZERO:
            raise ValueError("stop parameter must be positive and finite")
        self.spacing_mode = LevelSpacingMode.parse(self.spacing_mode)
        self.stop_mode = StopMode.parse(self.stop_mode)

    @property
    def tp_distance(self) -> Decimal:
        return self.entry_price - self.tp_price

    @property
    def stop_distance(self) -> Decimal:
        return self.stop_price - self.entry_price

    @property
    def initial_quantity(self) -> Decimal:
        costs = StrategyCostConfig()
        return StrategyMathEngine.size_budget(
            role="BASELINE",
            zone_option_loss_budget=Money(self.option_budget),
            confirmed_recovery_debt=Money(ZERO),
            configured_buffer=Money(ZERO),
            costs=costs.execution_context(
                entry_price=self.entry_price,
                tp_price=self.tp_price,
                stop_price=self.stop_price,
            ),
            instrument=InstrumentRules.exact(),
        ).submitted_quantity.value


class LegacyPriceStepLevelGenerator:
    """Characterized pre-M1 generator retained only for migration comparison."""

    @staticmethod
    def generate(
        spread: CreditSpread,
        level_count: int,
        stop_rate: DecimalLike = "0.15",
    ) -> list[HedgeLevel]:
        if level_count <= 0:
            raise ValueError("level count must be positive")
        rate = to_decimal(stop_rate)
        if rate <= ZERO:
            raise ValueError("stop rate must be positive")

        width = spread.short_put_strike - spread.long_put_strike
        count = Decimal(level_count)
        boundaries = [
            spread.short_put_strike - width * Decimal(index) / count
            for index in range(level_count)
        ]
        boundaries.append(spread.long_put_strike)

        levels: list[HedgeLevel] = []
        for index in range(level_count):
            entry_price = boundaries[index]
            tp_price = boundaries[index + 1]
            price_step_usd = entry_price - tp_price
            levels.append(
                HedgeLevel(
                    level_id=index + 1,
                    entry_price=entry_price,
                    tp_price=tp_price,
                    stop_price=entry_price + price_step_usd * rate,
                    option_budget=spread.option_quantity * price_step_usd,
                    spacing_mode=LevelSpacingMode.PRICE_STEP,
                    stop_mode=StopMode.PRICE_STEP_FRACTION,
                    stop_parameter=rate,
                )
            )
        return levels


def generate_virtual_levels(
    spread: CreditSpread,
    level_count: int,
    stop_rate: DecimalLike = "0.15",
) -> list[HedgeLevel]:
    """Compatibility entry point for the explicitly named legacy generator."""
    return LegacyPriceStepLevelGenerator.generate(spread, level_count, stop_rate)


def build_virtual_levels(
    spread: CreditSpread,
    level_count: int,
    stop: StopConfig | None = None,
) -> list[HedgeLevel]:
    """Build runtime boundaries and stops through authoritative strategy math."""
    return _build_stop_levels(
        OptionSpreadState(
            short_put_strike=Price(spread.short_put_strike),
            long_put_strike=Price(spread.long_put_strike),
            option_quantity=Quantity(spread.option_quantity),
        ),
        LevelCountSpacingConfig(level_count),
        stop or EntryPercentStopConfig(Rate(Decimal("0.0015"))),
    )


def build_price_step_virtual_levels(
    *,
    short_put_strike: Decimal,
    long_put_strike: Decimal,
    option_quantity: Decimal,
    price_step_usd: Decimal,
    stop: StopConfig | None = None,
) -> list[HedgeLevel]:
    """Build explicit PRICE_STEP boundaries and authoritative stops."""
    return _build_stop_levels(
        OptionSpreadState(
            short_put_strike=Price(short_put_strike),
            long_put_strike=Price(long_put_strike),
            option_quantity=Quantity(option_quantity),
        ),
        PriceStepSpacingConfig(Price(price_step_usd)),
        stop or EntryPercentStopConfig(Rate(Decimal("0.0015"))),
    )


def _build_stop_levels(
    spread: OptionSpreadState,
    spacing: SpacingConfig,
    stop: StopConfig,
) -> list[HedgeLevel]:
    as_of = datetime(1970, 1, 1, tzinfo=UTC)
    context = OptionValuationContext(
        valuation_mode=OptionValuationMode.EXPIRATION,
        observed_at_utc=as_of,
        valid_until_utc=as_of,
    )
    levels = StrategyMathEngine(ExpirationOptionValuation()).build_levels(
        spread,
        context,
        spacing,
        stop,
        as_of_utc=as_of,
    )
    parameter = (
        stop.rate.value
        if isinstance(stop, EntryPercentStopConfig)
        else stop.fraction.value
    )
    return [
        HedgeLevel(
            level_id=level.level_id,
            entry_price=level.entry_price.value,
            tp_price=level.tp_price.value,
            stop_price=level.stop_price.value,
            option_budget=level.zone_option_loss_budget.value,
            spacing_mode=level.spacing_mode,
            stop_mode=level.stop_mode,
            stop_parameter=parameter,
        )
        for level in levels
    ]
