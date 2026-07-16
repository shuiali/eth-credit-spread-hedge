"""Preflight or run the explicitly selected integrated Bybit demo strategy."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum

from eth_credit_hedge.config.schema import (
    LockPolicy,
    RecoveryMode,
    RuntimeConfig,
    RuntimeEnvironment,
)
from eth_credit_hedge.domain.strategy_math import StopMode


MUTATION_GATE_ENV = "RUN_BYBIT_DEMO_MUTATIONS"
FULL_STRATEGY_DEMO_TOKEN = "FULL_STRATEGY_DEMO"


class DemoCycleMode(str, Enum):
    OPEN_NEW = "OPEN_NEW"
    RESTORE_ONLY = "RESTORE_ONLY"


class DemoShutdownPolicy(str, Enum):
    CLOSE_ALL = "CLOSE_ALL"
    LEAVE_OPTION_PROTECTED = "LEAVE_OPTION_PROTECTED"


@dataclass(frozen=True, slots=True)
class DemoStrategyCommand:
    action: str
    cycle_mode: DemoCycleMode
    cycle_id: str | None
    short_symbol: str | None
    long_symbol: str | None
    option_quantity: Decimal | None
    minimum_net_credit: Decimal | None
    maximum_entry_deviation_bps: Decimal | None
    run_seconds: int
    shutdown_policy: DemoShutdownPolicy
    health_host: str
    health_port: int


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    for action in ("preflight", "run"):
        command = subparsers.add_parser(action)
        command.add_argument(
            "--cycle-mode",
            required=True,
            choices=[mode.value for mode in DemoCycleMode],
        )
        command.add_argument("--cycle-id")
        command.add_argument("--short-symbol")
        command.add_argument("--long-symbol")
        command.add_argument("--option-quantity")
        command.add_argument("--min-net-credit")
        command.add_argument("--max-entry-deviation-bps")
        command.add_argument("--run-seconds", type=int, default=3600)
        command.add_argument(
            "--shutdown-policy",
            choices=[policy.value for policy in DemoShutdownPolicy],
            default=DemoShutdownPolicy.CLOSE_ALL.value,
        )
        command.add_argument("--health-host", default="127.0.0.1")
        command.add_argument("--health-port", type=int, default=8080)
    return parser


def parse_command(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> DemoStrategyCommand:
    args = build_parser().parse_args(argv)
    values = dict(os.environ if environ is None else environ)
    runtime = RuntimeConfig.from_env(values)
    if runtime.environment is not RuntimeEnvironment.DEMO:
        raise ValueError("demo strategy runner requires ETH_HEDGE_ENVIRONMENT=DEMO")
    if runtime.strategy.recovery_mode is not RecoveryMode.FULL_NEXT_TP:
        raise ValueError("demo strategy requires FULL_NEXT_TP recovery")
    if runtime.strategy.lock_policy is not LockPolicy.UNHEDGED:
        raise ValueError("demo strategy requires UNHEDGED lock policy")
    if (
        runtime.strategy.stop_mode is not StopMode.ENTRY_PERCENT
        or runtime.strategy.stop_parameter != Decimal("0.0015")
    ):
        raise ValueError("demo strategy requires stop distance 0.15% of entry")
    if args.action == "run" and values.get(MUTATION_GATE_ENV) != FULL_STRATEGY_DEMO_TOKEN:
        raise ValueError(
            f"run requires {MUTATION_GATE_ENV}={FULL_STRATEGY_DEMO_TOKEN}"
        )
    if args.run_seconds <= 0:
        raise ValueError("run seconds must be positive and bounded")
    if not 1 <= args.health_port <= 65535:
        raise ValueError("health port must be between 1 and 65535")
    if not args.health_host.strip():
        raise ValueError("health host cannot be empty")

    mode = DemoCycleMode(args.cycle_mode)
    cycle_id = _optional_text(args.cycle_id)
    short_symbol = _optional_text(args.short_symbol)
    long_symbol = _optional_text(args.long_symbol)
    quantity = _optional_decimal(args.option_quantity, "option quantity")
    minimum_credit = _optional_decimal(
        args.min_net_credit,
        "minimum net credit",
    )
    maximum_deviation = _optional_decimal(
        args.max_entry_deviation_bps,
        "maximum entry deviation bps",
    )
    if mode is DemoCycleMode.OPEN_NEW:
        missing = tuple(
            name
            for name, value in (
                ("short symbol", short_symbol),
                ("long symbol", long_symbol),
                ("option quantity", quantity),
                ("minimum net credit", minimum_credit),
                ("maximum entry deviation bps", maximum_deviation),
            )
            if value is None
        )
        if missing:
            raise ValueError("OPEN_NEW requires " + ", ".join(missing))
        if cycle_id is not None:
            raise ValueError("OPEN_NEW generates its cycle ID; do not provide one")
        if short_symbol == long_symbol:
            raise ValueError("short and long option symbols must differ")
    else:
        if cycle_id is None:
            raise ValueError("RESTORE_ONLY requires cycle ID")
        if any(
            value is not None
            for value in (
                short_symbol,
                long_symbol,
                quantity,
                minimum_credit,
                maximum_deviation,
            )
        ):
            raise ValueError("RESTORE_ONLY cannot specify new option entry fields")

    for value, name in (
        (quantity, "option quantity"),
        (minimum_credit, "minimum net credit"),
        (maximum_deviation, "maximum entry deviation bps"),
    ):
        if value is not None and value <= Decimal("0"):
            raise ValueError(f"{name} must be positive")

    return DemoStrategyCommand(
        action=args.action,
        cycle_mode=mode,
        cycle_id=cycle_id,
        short_symbol=short_symbol,
        long_symbol=long_symbol,
        option_quantity=quantity,
        minimum_net_credit=minimum_credit,
        maximum_entry_deviation_bps=maximum_deviation,
        run_seconds=args.run_seconds,
        shutdown_policy=DemoShutdownPolicy(args.shutdown_policy),
        health_host=args.health_host,
        health_port=args.health_port,
    )


async def execute(command: DemoStrategyCommand) -> dict[str, object]:
    if command.action == "preflight":
        from eth_credit_hedge.interfaces.demo_bootstrap import run_demo_preflight

        return await run_demo_preflight(command)
    from eth_credit_hedge.application.demo_strategy_runtime import (
        run_demo_strategy,
    )

    return await run_demo_strategy(command)


def main(argv: Sequence[str] | None = None) -> int:
    command = parse_command(argv)
    result = asyncio.run(execute(command))
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _optional_decimal(value: object, name: str) -> Decimal | None:
    if value is None:
        return None
    try:
        normalized = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a finite decimal") from exc
    if not normalized.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    return normalized


if __name__ == "__main__":
    raise SystemExit(main())
