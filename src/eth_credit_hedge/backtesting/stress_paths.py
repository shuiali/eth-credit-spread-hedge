"""Deterministic seeded price paths for separately labeled stress models."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class StressPath:
    model: str
    seed: int | None
    prices: tuple[Decimal, ...]


def _price(value: Decimal) -> Decimal:
    result = Decimal(value)
    if not result.is_finite() or result <= 0:
        raise ValueError("price must be positive")
    return result


def _steps(value: int) -> int:
    if value <= 0:
        raise ValueError("steps must be positive")
    return value


def _decimal_price(value: float) -> Decimal:
    return Decimal(str(max(value, 0.00000001)))


def generate_jump_diffusion_path(
    start_price: Decimal,
    *,
    steps: int,
    seed: int,
    jump_probability: Decimal = Decimal("0.05"),
) -> StressPath:
    current = float(_price(start_price))
    count = _steps(steps)
    probability = float(Decimal(jump_probability))
    if not 0 <= probability <= 1:
        raise ValueError("jump probability must be in [0, 1]")
    generator = random.Random(seed)
    prices = [Decimal(str(current))]
    for _ in range(count):
        diffusion = generator.gauss(0.0, 0.012)
        jump = generator.gauss(-0.08, 0.04) if generator.random() < probability else 0.0
        current *= math.exp(diffusion + jump)
        prices.append(_decimal_price(current))
    return StressPath("JUMP_DIFFUSION", seed, tuple(prices))


def generate_volatility_clustered_path(
    start_price: Decimal,
    *,
    steps: int,
    seed: int,
) -> StressPath:
    current = float(_price(start_price))
    generator = random.Random(seed)
    variance = 0.0001
    prices = [Decimal(str(current))]
    for _ in range(_steps(steps)):
        shock = generator.gauss(0.0, math.sqrt(variance))
        current *= math.exp(shock)
        variance = 0.00001 + 0.12 * shock * shock + 0.82 * variance
        prices.append(_decimal_price(current))
    return StressPath("VOLATILITY_CLUSTERING", seed, tuple(prices))


def generate_regime_switching_path(
    start_price: Decimal,
    *,
    steps: int,
    seed: int,
) -> StressPath:
    current = float(_price(start_price))
    generator = random.Random(seed)
    stressed = False
    prices = [Decimal(str(current))]
    for _ in range(_steps(steps)):
        if generator.random() < (0.08 if not stressed else 0.2):
            stressed = not stressed
        volatility = 0.035 if stressed else 0.008
        drift = -0.004 if stressed else 0.0002
        current *= math.exp(drift + generator.gauss(0.0, volatility))
        prices.append(_decimal_price(current))
    return StressPath("REGIME_SWITCHING", seed, tuple(prices))


def generate_historical_bootstrap_path(
    start_price: Decimal,
    *,
    returns: tuple[Decimal, ...],
    steps: int,
    seed: int,
) -> StressPath:
    if not returns:
        raise ValueError("historical returns cannot be empty")
    normalized_returns = tuple(Decimal(value) for value in returns)
    if any(not value.is_finite() or value <= Decimal("-1") for value in normalized_returns):
        raise ValueError("historical returns must be finite and greater than -1")
    current = _price(start_price)
    generator = random.Random(seed)
    prices = [current]
    for _ in range(_steps(steps)):
        current *= Decimal("1") + generator.choice(normalized_returns)
        prices.append(current)
    return StressPath("HISTORICAL_BOOTSTRAP", seed, tuple(prices))


def generate_v_shaped_path(
    start_price: Decimal,
    *,
    bottom_price: Decimal,
    recovery_price: Decimal,
    steps_per_leg: int,
) -> StressPath:
    start = _price(start_price)
    bottom = _price(bottom_price)
    recovery = _price(recovery_price)
    count = _steps(steps_per_leg)
    down_step = (bottom - start) / count
    up_step = (recovery - bottom) / count
    prices = [start]
    prices.extend(start + down_step * index for index in range(1, count + 1))
    prices.extend(bottom + up_step * index for index in range(1, count + 1))
    return StressPath("V_SHAPED_STRESS", None, tuple(prices))


def generate_repeated_oscillation_path(
    *,
    upper_price: Decimal,
    lower_price: Decimal,
    cycles: int,
) -> StressPath:
    upper = _price(upper_price)
    lower = _price(lower_price)
    if lower >= upper:
        raise ValueError("lower oscillation price must be below upper price")
    count = _steps(cycles)
    prices = [upper]
    for _ in range(count):
        prices.extend((lower, upper))
    return StressPath("REPEATED_ENTRY_OSCILLATION", None, tuple(prices))
