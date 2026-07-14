"""Seeded macro-GBM paths expanded into exact market microticks."""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from backtesting.market_path import expand_price_anchors
from core.credit_spread import CreditSpread, ZERO, to_decimal
from core.hedge_engine import HedgeEngine, LockPolicy, RecoveryMode
from core.ledger import StrategyMetrics, StrategyResult


@dataclass(frozen=True, slots=True)
class MonteCarloConfig:
    path_count: int = 100
    tick_count: int = 20_001
    macro_step_count: int = 40
    horizon_years: float = 1.0 / 365.0
    annual_volatility: float = 0.80
    annual_drift: float = 0.0
    tick_size: Decimal = Decimal("0.1")
    seed: int = 42

    def __post_init__(self) -> None:
        if self.path_count <= 0:
            raise ValueError("path count must be positive")
        if self.tick_count < 2:
            raise ValueError("tick count must be at least two")
        if self.macro_step_count <= 0:
            raise ValueError("macro step count must be positive")
        if self.horizon_years <= 0:
            raise ValueError("horizon must be positive")
        if self.annual_volatility < 0:
            raise ValueError("volatility cannot be negative")
        object.__setattr__(self, "tick_size", to_decimal(self.tick_size))
        if self.tick_size <= ZERO:
            raise ValueError("tick size must be positive")


@dataclass(frozen=True, slots=True)
class MonteCarloSummary:
    path_count: int
    floor_pass_rate: Decimal
    terminal_pnl_distribution: tuple[Decimal, ...]
    minimum_combined_pnl: Decimal
    number_of_entries: int
    number_of_stops: int
    number_of_tps: int
    reentry_count: int
    maximum_quantity: Decimal
    premium_budget_consumed: Decimal
    locked_levels: int
    outstanding_recovery_debt: Decimal


@dataclass(frozen=True, slots=True)
class MonteCarloResult:
    tick_paths: tuple[tuple[Decimal, ...], ...]
    strategy_results: tuple[StrategyResult, ...]
    path_metrics: tuple[StrategyMetrics, ...]
    combined_pnl_paths: tuple[tuple[Decimal, ...], ...]
    summary: MonteCarloSummary
    saved_path: Path


def generate_gbm_tick_path(
    initial_price: Decimal,
    config: MonteCarloConfig,
    seed: int,
) -> tuple[Decimal, ...]:
    """Generate GBM anchors and join them with exact one-tick market moves."""
    rng = random.Random(seed)
    price = float(initial_price)
    anchors = [_quantize_to_tick(initial_price, config.tick_size)]
    dt_years = config.horizon_years / config.macro_step_count
    drift = (config.annual_drift - 0.5 * config.annual_volatility**2) * dt_years
    diffusion_scale = config.annual_volatility * math.sqrt(dt_years)
    for _ in range(config.macro_step_count):
        shock = rng.gauss(0.0, 1.0)
        price *= math.exp(drift + diffusion_scale * shock)
        anchors.append(_quantize_to_tick(to_decimal(price), config.tick_size))

    ticks_per_segment = math.ceil((config.tick_count - 1) / config.macro_step_count)
    return expand_price_anchors(
        anchors,
        ticks_per_segment=ticks_per_segment,
        tick_size=config.tick_size,
        seed=seed + 10_000_019,
    )


def run_monte_carlo(
    spread: CreditSpread,
    level_count: int,
    config: MonteCarloConfig,
    output_path: str | Path,
    *,
    recovery_mode: RecoveryMode | str = RecoveryMode.FULL_NEXT_TP,
    recovery_tp_count: int = 3,
    lock_policy: LockPolicy | str = LockPolicy.UNHEDGED,
) -> MonteCarloResult:
    """Generate, persist, and replay every seeded Monte Carlo tick path."""
    paths: list[tuple[Decimal, ...]] = []
    results: list[StrategyResult] = []
    for index in range(config.path_count):
        path = generate_gbm_tick_path(spread.spot, config, config.seed + index)
        engine = HedgeEngine(
            spread,
            level_count=level_count,
            recovery_mode=recovery_mode,
            recovery_tp_count=recovery_tp_count,
            lock_policy=lock_policy,
        )
        paths.append(path)
        results.append(engine.run_with_accounting(list(path)))

    saved_path = Path(output_path)
    saved_path.parent.mkdir(parents=True, exist_ok=True)
    saved_path.write_text(
        json.dumps(
            {
                "model": "macro GBM with exact random-walk microticks",
                "config": {
                    "path_count": config.path_count,
                    "tick_count": config.tick_count,
                    "macro_step_count": config.macro_step_count,
                    "horizon_years": config.horizon_years,
                    "annual_volatility": config.annual_volatility,
                    "annual_drift": config.annual_drift,
                    "tick_size": str(config.tick_size),
                    "seed": config.seed,
                    "lock_policy": LockPolicy(lock_policy).value,
                },
                "paths": [[str(tick) for tick in path] for path in paths],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    metrics = tuple(result.metrics for result in results)
    combined_paths = tuple(
        tuple(snapshot.combined_terminal_value_pnl for snapshot in result.snapshots)
        for result in results
    )
    terminal = tuple(metric.combined_pnl for metric in metrics)
    summary = MonteCarloSummary(
        path_count=config.path_count,
        floor_pass_rate=Decimal(sum(metric.floor_pass for metric in metrics))
        / Decimal(config.path_count),
        terminal_pnl_distribution=terminal,
        minimum_combined_pnl=min(metric.minimum_combined_pnl for metric in metrics),
        number_of_entries=sum(metric.number_of_entries for metric in metrics),
        number_of_stops=sum(metric.number_of_stops for metric in metrics),
        number_of_tps=sum(metric.number_of_tps for metric in metrics),
        reentry_count=sum(metric.reentry_count for metric in metrics),
        maximum_quantity=max(
            (metric.maximum_quantity for metric in metrics), default=ZERO
        ),
        premium_budget_consumed=max(
            (metric.premium_budget_consumed for metric in metrics), default=ZERO
        ),
        locked_levels=sum(len(metric.locked_levels) for metric in metrics),
        outstanding_recovery_debt=sum(
            (metric.outstanding_recovery_debt for metric in metrics), ZERO
        ),
    )
    return MonteCarloResult(
        tick_paths=tuple(paths),
        strategy_results=tuple(results),
        path_metrics=metrics,
        combined_pnl_paths=combined_paths,
        summary=summary,
        saved_path=saved_path,
    )


def _quantize_to_tick(price: Decimal, tick_size: Decimal) -> Decimal:
    return (price / tick_size).to_integral_value() * tick_size
