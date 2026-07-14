"""Typed strategy configuration tests."""

from decimal import Decimal

import pytest

from eth_credit_hedge.config import (
    LockPolicy,
    RecoveryMode,
    RuntimeConfig,
    RuntimeEnvironment,
    StrategyConfig,
)
from eth_credit_hedge.domain.market_data import TriggerPriceSource


def test_baseline_factory_freezes_validated_strategy_defaults() -> None:
    config = StrategyConfig.baseline(level_count=5)

    assert config == StrategyConfig(
        level_count=5,
        stop_rate=Decimal("0.0015"),
        recovery_mode=RecoveryMode.FULL_NEXT_TP,
        lock_policy=LockPolicy.UNHEDGED,
        recovery_tp_count=3,
    )


def test_experimental_floor_requires_an_explicit_factory() -> None:
    baseline = StrategyConfig.baseline()
    experimental = StrategyConfig.experimental_floor()

    assert baseline.lock_policy is LockPolicy.UNHEDGED
    assert experimental.lock_policy is LockPolicy.BREAKEVEN_FLOOR
    assert experimental.recovery_mode is RecoveryMode.FULL_NEXT_TP


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"level_count": 0}, "level count"),
        ({"stop_rate": "0"}, "stop rate"),
        ({"recovery_tp_count": 0}, "recovery TP count"),
    ],
)
def test_strategy_config_rejects_invalid_values(
    overrides: dict[str, object], message: str
) -> None:
    values: dict[str, object] = {
        "level_count": 1,
        "stop_rate": "0.0015",
        "recovery_mode": RecoveryMode.FULL_NEXT_TP,
        "lock_policy": LockPolicy.UNHEDGED,
        "recovery_tp_count": 3,
    }
    values.update(overrides)

    with pytest.raises(ValueError, match=message):
        StrategyConfig(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "environment",
    [
        RuntimeEnvironment.DEMO,
        RuntimeEnvironment.SHADOW,
        RuntimeEnvironment.PRODUCTION,
    ],
)
def test_deployment_environments_reject_experimental_strategy_modes(
    environment: RuntimeEnvironment,
) -> None:
    with pytest.raises(ValueError, match="FULL_NEXT_TP and UNHEDGED"):
        RuntimeConfig(environment, StrategyConfig.experimental_floor())


def test_runtime_config_parses_environment_mapping_once() -> None:
    config = RuntimeConfig.from_env(
        {
            "ETH_HEDGE_ENVIRONMENT": "demo",
            "ETH_HEDGE_LEVEL_COUNT": "4",
            "ETH_HEDGE_STOP_RATE": "0.0015",
            "ETH_HEDGE_RECOVERY_MODE": "FULL_NEXT_TP",
            "ETH_HEDGE_LOCK_POLICY": "UNHEDGED",
            "ETH_HEDGE_RECOVERY_TP_COUNT": "3",
        }
    )

    assert config.environment is RuntimeEnvironment.DEMO
    assert config.strategy == StrategyConfig.baseline(level_count=4)
    assert config.trigger_price_source is TriggerPriceSource.LAST_TRADE


def test_deployment_config_rejects_mixed_trigger_source() -> None:
    with pytest.raises(ValueError, match="LAST_TRADE"):
        RuntimeConfig(
            environment=RuntimeEnvironment.DEMO,
            strategy=StrategyConfig.baseline(),
            trigger_price_source=TriggerPriceSource.MARK_PRICE,
        )


def test_plan_seven_environment_names_and_legacy_aliases_are_stable() -> None:
    assert RuntimeEnvironment.BACKTEST is RuntimeEnvironment.LOCAL_EXACT
    assert RuntimeEnvironment.SHADOW is RuntimeEnvironment.SHADOW_MAINNET
    assert RuntimeConfig.from_env(
        {"ETH_HEDGE_ENVIRONMENT": "LOCAL_SIMULATED"}
    ).environment is RuntimeEnvironment.LOCAL_SIMULATED
