"""Command contract for the integrated demo strategy runner."""

from __future__ import annotations

import pytest

from eth_credit_hedge.interfaces.demo_strategy_runner import (
    DemoCycleMode,
    DemoShutdownPolicy,
    parse_command,
)


BASE_ENV = {
    "ETH_HEDGE_ENVIRONMENT": "DEMO",
    "ETH_HEDGE_LEVEL_COUNT": "10",
    "ETH_HEDGE_STOP_RATE": "0.15",
    "ETH_HEDGE_RECOVERY_MODE": "FULL_NEXT_TP",
    "ETH_HEDGE_LOCK_POLICY": "UNHEDGED",
}


def open_arguments(action: str = "preflight") -> list[str]:
    return [
        action,
        "--cycle-mode",
        "OPEN_NEW",
        "--short-symbol",
        "ETH-31JUL26-1800-P-USDT",
        "--long-symbol",
        "ETH-31JUL26-1750-P-USDT",
        "--option-quantity",
        "0.1",
        "--min-net-credit",
        "1",
        "--max-entry-deviation-bps",
        "100",
    ]


def test_preflight_parses_explicit_new_cycle_without_mutation_token() -> None:
    command = parse_command(open_arguments(), environ=BASE_ENV)

    assert command.action == "preflight"
    assert command.cycle_mode is DemoCycleMode.OPEN_NEW
    assert command.short_symbol == "ETH-31JUL26-1800-P-USDT"
    assert command.run_seconds == 3600
    assert command.shutdown_policy is DemoShutdownPolicy.CLOSE_ALL


def test_run_requires_the_exact_full_strategy_demo_token() -> None:
    with pytest.raises(ValueError, match="FULL_STRATEGY_DEMO"):
        parse_command(open_arguments("run"), environ=BASE_ENV)

    command = parse_command(
        open_arguments("run"),
        environ={
            **BASE_ENV,
            "RUN_BYBIT_DEMO_MUTATIONS": "FULL_STRATEGY_DEMO",
        },
    )
    assert command.action == "run"


def test_runner_rejects_non_demo_or_experimental_strategy_before_execution() -> None:
    with pytest.raises(ValueError, match="ETH_HEDGE_ENVIRONMENT=DEMO"):
        parse_command(
            open_arguments(),
            environ={**BASE_ENV, "ETH_HEDGE_ENVIRONMENT": "SHADOW_MAINNET"},
        )
    with pytest.raises(ValueError, match="demo, shadow, and production require"):
        parse_command(
            open_arguments(),
            environ={**BASE_ENV, "ETH_HEDGE_RECOVERY_MODE": "DISTRIBUTED"},
        )


def test_open_new_requires_every_explicit_execution_bound() -> None:
    arguments = open_arguments()
    arguments = arguments[: arguments.index("--max-entry-deviation-bps")]
    with pytest.raises(ValueError, match="maximum entry deviation"):
        parse_command(arguments, environ=BASE_ENV)


def test_restore_only_requires_cycle_id_and_forbids_new_leg_fields() -> None:
    restored = parse_command(
        [
            "preflight",
            "--cycle-mode",
            "RESTORE_ONLY",
            "--cycle-id",
            "DEMO-20260715-001",
        ],
        environ=BASE_ENV,
    )
    assert restored.cycle_id == "DEMO-20260715-001"

    with pytest.raises(ValueError, match="cannot specify"):
        parse_command(
            [
                "preflight",
                "--cycle-mode",
                "RESTORE_ONLY",
                "--cycle-id",
                "DEMO-20260715-001",
                "--short-symbol",
                "ETH-31JUL26-1800-P-USDT",
            ],
            environ=BASE_ENV,
        )


@pytest.mark.parametrize("seconds", ["0", "-1"])
def test_duration_must_be_positive_and_bounded(seconds: str) -> None:
    with pytest.raises(ValueError, match="positive and bounded"):
        parse_command(
            [*open_arguments(), "--run-seconds", seconds],
            environ=BASE_ENV,
        )
