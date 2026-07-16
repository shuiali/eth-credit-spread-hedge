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
from eth_credit_hedge.domain.strategy_math import (
    EntryPercentStopConfig,
    PriceStepFractionStopConfig,
    Rate,
    StopConfig,
    StopMode,
    StrategyMathError,
    parse_stop_configuration,
)


class RecoveryMode(str, Enum):
    FULL_NEXT_TP = "FULL_NEXT_TP"
    DISTRIBUTED = "DISTRIBUTED"


class LockPolicy(str, Enum):
    UNHEDGED = "UNHEDGED"
    BREAKEVEN_FLOOR = "BREAKEVEN_FLOOR"


class RuntimeEnvironment(str, Enum):
    LOCAL_EXACT = "LOCAL_EXACT"
    BACKTEST = "LOCAL_EXACT"
    LOCAL_SIMULATED = "LOCAL_SIMULATED"
    DEMO = "DEMO"
    SHADOW_MAINNET = "SHADOW_MAINNET"
    SHADOW = "SHADOW_MAINNET"
    PRODUCTION_PILOT = "PRODUCTION_PILOT"
    PRODUCTION = "PRODUCTION"


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    """Validated inputs that control deterministic strategy behavior."""

    level_count: int
    stop: StopConfig
    recovery_mode: RecoveryMode
    lock_policy: LockPolicy
    recovery_tp_count: int = 3

    def __post_init__(self) -> None:
        if self.level_count <= 0:
            raise ValueError("level count must be positive")
        if not isinstance(
            self.stop, (EntryPercentStopConfig, PriceStepFractionStopConfig)
        ):
            raise ValueError("strategy stop configuration must be explicit")
        if self.recovery_tp_count <= 0:
            raise ValueError("recovery TP count must be positive")

        object.__setattr__(self, "recovery_mode", RecoveryMode(self.recovery_mode))
        object.__setattr__(self, "lock_policy", LockPolicy(self.lock_policy))

    @property
    def stop_mode(self) -> StopMode:
        return self.stop.mode

    @property
    def stop_parameter(self) -> Decimal:
        if isinstance(self.stop, EntryPercentStopConfig):
            return self.stop.rate.value
        return self.stop.fraction.value

    @classmethod
    def baseline(
        cls,
        *,
        level_count: int = 1,
        recovery_tp_count: int = 3,
    ) -> StrategyConfig:
        return cls(
            level_count=level_count,
            stop=EntryPercentStopConfig(Rate(Decimal("0.0015"))),
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
            stop=EntryPercentStopConfig(Rate(Decimal("0.0015"))),
            recovery_mode=RecoveryMode.FULL_NEXT_TP,
            lock_policy=LockPolicy.BREAKEVEN_FLOOR,
            recovery_tp_count=recovery_tp_count,
        )


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    """Environment selection plus its validated strategy configuration."""

    environment: RuntimeEnvironment = RuntimeEnvironment.LOCAL_EXACT
    strategy: StrategyConfig = field(default_factory=StrategyConfig.baseline)
    trigger_price_source: TriggerPriceSource = DEFAULT_TRIGGER_PRICE_SOURCE

    def __post_init__(self) -> None:
        environment = RuntimeEnvironment(self.environment)
        object.__setattr__(self, "environment", environment)
        trigger_source = TriggerPriceSource(self.trigger_price_source)
        object.__setattr__(self, "trigger_price_source", trigger_source)
        local_environments = {
            RuntimeEnvironment.LOCAL_EXACT,
            RuntimeEnvironment.LOCAL_SIMULATED,
        }
        if environment not in local_environments and (
            self.strategy.recovery_mode is not RecoveryMode.FULL_NEXT_TP
            or self.strategy.lock_policy is not LockPolicy.UNHEDGED
        ):
            raise ValueError(
                "demo, shadow, and production require FULL_NEXT_TP and UNHEDGED"
            )
        if (
            environment not in local_environments
            and trigger_source is not TriggerPriceSource.LAST_TRADE
        ):
            raise ValueError(
                "demo, shadow, and production require LAST_TRADE trigger source"
            )

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> RuntimeConfig:
        """Parse strategy environment variables into one immutable object."""
        values = dict(os.environ if environ is None else environ)
        if "ETH_HEDGE_STOP_RATE" in values:
            raise ValueError(
                "ETH_HEDGE_STOP_RATE is ambiguous and no longer supported; "
                "use ETH_HEDGE_STOP_MODE=ENTRY_PERCENT with "
                "ETH_HEDGE_ENTRY_STOP_RATE, or "
                "ETH_HEDGE_STOP_MODE=PRICE_STEP_FRACTION with "
                "ETH_HEDGE_PRICE_STEP_STOP_FRACTION"
            )
        baseline = StrategyConfig.baseline()
        raw_stop_mode = values.get(
            "ETH_HEDGE_STOP_MODE", baseline.stop_mode.value
        ).upper()
        stop_fields: dict[str, object] = {}
        if "ETH_HEDGE_ENTRY_STOP_RATE" in values:
            stop_fields["entry_stop_rate"] = values["ETH_HEDGE_ENTRY_STOP_RATE"]
        if "ETH_HEDGE_PRICE_STEP_STOP_FRACTION" in values:
            stop_fields["price_step_stop_fraction"] = values[
                "ETH_HEDGE_PRICE_STEP_STOP_FRACTION"
            ]
        if not stop_fields and raw_stop_mode == StopMode.ENTRY_PERCENT.value:
            stop_fields["entry_stop_rate"] = baseline.stop_parameter
        try:
            stop = parse_stop_configuration(raw_stop_mode, stop_fields)
        except StrategyMathError as exc:
            raise ValueError(str(exc)) from exc
        strategy = StrategyConfig(
            level_count=int(
                values.get("ETH_HEDGE_LEVEL_COUNT", str(baseline.level_count))
            ),
            stop=stop,
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
        raw_environment = values.get(
            "ETH_HEDGE_ENVIRONMENT",
            RuntimeEnvironment.LOCAL_EXACT.value,
        ).upper()
        legacy_environments = {
            "BACKTEST": RuntimeEnvironment.LOCAL_EXACT.value,
            "SHADOW": RuntimeEnvironment.SHADOW_MAINNET.value,
        }
        environment = RuntimeEnvironment(
            legacy_environments.get(raw_environment, raw_environment)
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
