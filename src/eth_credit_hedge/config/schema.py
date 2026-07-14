"""Typed configuration for deterministic and deployment strategy modes."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from eth_credit_hedge.domain.market_data import (
    DEFAULT_TRIGGER_PRICE_SOURCE,
    TriggerPriceSource,
)


class RecoveryMode(str, Enum):
    FULL_NEXT_TP = "FULL_NEXT_TP"
    DISTRIBUTED = "DISTRIBUTED"


class LockPolicy(str, Enum):
    UNHEDGED = "UNHEDGED"
    BREAKEVEN_FLOOR = "BREAKEVEN_FLOOR"


class RuntimeEnvironment(str, Enum):
    BACKTEST = "backtest"
    DEMO = "demo"
    SHADOW = "shadow"
    PRODUCTION = "production"


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    """Validated inputs that control deterministic strategy behavior."""

    level_count: int
    stop_rate: Decimal
    recovery_mode: RecoveryMode
    lock_policy: LockPolicy
    recovery_tp_count: int = 3

    def __post_init__(self) -> None:
        if self.level_count <= 0:
            raise ValueError("level count must be positive")
        stop_rate = Decimal(str(self.stop_rate))
        if not stop_rate.is_finite() or stop_rate <= 0:
            raise ValueError("stop rate must be positive")
        if self.recovery_tp_count <= 0:
            raise ValueError("recovery TP count must be positive")

        object.__setattr__(self, "stop_rate", stop_rate)
        object.__setattr__(self, "recovery_mode", RecoveryMode(self.recovery_mode))
        object.__setattr__(self, "lock_policy", LockPolicy(self.lock_policy))

    @classmethod
    def baseline(
        cls,
        *,
        level_count: int = 1,
        recovery_tp_count: int = 3,
    ) -> StrategyConfig:
        return cls(
            level_count=level_count,
            stop_rate=Decimal("0.0015"),
            recovery_mode=RecoveryMode.FULL_NEXT_TP,
            lock_policy=LockPolicy.UNHEDGED,
            recovery_tp_count=recovery_tp_count,
        )

    @classmethod
    def experimental_floor(
        cls,
        *,
        level_count: int = 1,
        recovery_tp_count: int = 3,
    ) -> StrategyConfig:
        return cls(
            level_count=level_count,
            stop_rate=Decimal("0.0015"),
            recovery_mode=RecoveryMode.FULL_NEXT_TP,
            lock_policy=LockPolicy.BREAKEVEN_FLOOR,
            recovery_tp_count=recovery_tp_count,
        )


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    """Environment selection plus its validated strategy configuration."""

    environment: RuntimeEnvironment = RuntimeEnvironment.BACKTEST
    strategy: StrategyConfig = field(default_factory=StrategyConfig.baseline)
    trigger_price_source: TriggerPriceSource = DEFAULT_TRIGGER_PRICE_SOURCE

    def __post_init__(self) -> None:
        environment = RuntimeEnvironment(self.environment)
        object.__setattr__(self, "environment", environment)
        trigger_source = TriggerPriceSource(self.trigger_price_source)
        object.__setattr__(self, "trigger_price_source", trigger_source)
        if environment is not RuntimeEnvironment.BACKTEST and (
            self.strategy.recovery_mode is not RecoveryMode.FULL_NEXT_TP
            or self.strategy.lock_policy is not LockPolicy.UNHEDGED
        ):
            raise ValueError(
                "demo, shadow, and production require FULL_NEXT_TP and UNHEDGED"
            )
        if (
            environment is not RuntimeEnvironment.BACKTEST
            and trigger_source is not TriggerPriceSource.LAST_TRADE
        ):
            raise ValueError(
                "demo, shadow, and production require LAST_TRADE trigger source"
            )

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> RuntimeConfig:
        """Parse strategy environment variables into one immutable object."""
        values = dict(os.environ if environ is None else environ)
        baseline = StrategyConfig.baseline()
        strategy = StrategyConfig(
            level_count=int(
                values.get("ETH_HEDGE_LEVEL_COUNT", str(baseline.level_count))
            ),
            stop_rate=Decimal(
                values.get("ETH_HEDGE_STOP_RATE", str(baseline.stop_rate))
            ),
            recovery_mode=RecoveryMode(
                values.get(
                    "ETH_HEDGE_RECOVERY_MODE", baseline.recovery_mode.value
                ).upper()
            ),
            lock_policy=LockPolicy(
                values.get("ETH_HEDGE_LOCK_POLICY", baseline.lock_policy.value).upper()
            ),
            recovery_tp_count=int(
                values.get(
                    "ETH_HEDGE_RECOVERY_TP_COUNT",
                    str(baseline.recovery_tp_count),
                )
            ),
        )
        environment = RuntimeEnvironment(
            values.get("ETH_HEDGE_ENVIRONMENT", RuntimeEnvironment.BACKTEST.value).lower()
        )
        trigger_price_source = TriggerPriceSource(
            values.get(
                "ETH_HEDGE_TRIGGER_PRICE_SOURCE",
                DEFAULT_TRIGGER_PRICE_SOURCE.value,
            ).upper()
        )
        return cls(
            environment=environment,
            strategy=strategy,
            trigger_price_source=trigger_price_source,
        )
