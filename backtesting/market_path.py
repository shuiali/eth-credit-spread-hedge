"""Deterministic market-like paths made from exact price ticks."""

from __future__ import annotations

import random
from decimal import Decimal
from typing import Iterable

from core.credit_spread import DecimalLike, to_decimal


def expand_price_anchors(
    anchors: Iterable[DecimalLike],
    *,
    ticks_per_segment: int = 3_000,
    tick_size: DecimalLike = "0.1",
    seed: int = 42,
) -> tuple[Decimal, ...]:
    """Join price anchors with a noisy random walk of exact ``tick_size`` moves."""
    exact_anchors = tuple(to_decimal(anchor) for anchor in anchors)
    exact_tick_size = to_decimal(tick_size)
    if len(exact_anchors) < 2:
        raise ValueError("at least two price anchors are required")
    if ticks_per_segment < 2:
        raise ValueError("ticks per segment must be at least two")
    if exact_tick_size <= 0:
        raise ValueError("tick size must be positive")
    if any(anchor <= 0 for anchor in exact_anchors):
        raise ValueError("price anchors must be positive")

    anchor_ticks = tuple(
        _price_to_tick_index(anchor, exact_tick_size) for anchor in exact_anchors
    )
    rng = random.Random(seed)
    path_ticks = [anchor_ticks[0]]
    previous_direction = 0
    for target_tick in anchor_ticks[1:]:
        segment, previous_direction = _bridge_tick_indices(
            path_ticks[-1],
            target_tick,
            minimum_steps=ticks_per_segment,
            previous_direction=previous_direction,
            rng=rng,
        )
        path_ticks.extend(segment)

    return tuple(Decimal(tick) * exact_tick_size for tick in path_ticks)


def _price_to_tick_index(price: Decimal, tick_size: Decimal) -> int:
    tick_index = price / tick_size
    integral_tick = tick_index.to_integral_value()
    if tick_index != integral_tick:
        raise ValueError(f"price {price} is not aligned to tick size {tick_size}")
    return int(integral_tick)


def _bridge_tick_indices(
    start_tick: int,
    target_tick: int,
    *,
    minimum_steps: int,
    previous_direction: int,
    rng: random.Random,
) -> tuple[list[int], int]:
    distance = target_tick - start_tick
    step_count = max(minimum_steps, abs(distance) + 2)
    if (step_count - abs(distance)) % 2:
        step_count += 1

    current_tick = start_tick
    segment: list[int] = []
    direction = previous_direction
    for step_index in range(step_count):
        steps_after_move = step_count - step_index - 1
        feasible_directions = [
            candidate
            for candidate in (-1, 1)
            if abs(target_tick - (current_tick + candidate)) <= steps_after_move
        ]
        if len(feasible_directions) == 1:
            direction = feasible_directions[0]
        else:
            remaining_distance = target_tick - current_tick
            target_direction = 1 if remaining_distance > 0 else -1
            if remaining_distance == 0:
                target_direction = direction or rng.choice((-1, 1))

            probability_up = 0.5
            if direction:
                probability_up = 0.62 if direction > 0 else 0.38
            urgency = min(0.45, abs(remaining_distance) / (steps_after_move + 1))
            target_probability = 1.0 if target_direction > 0 else 0.0
            probability_up = (
                probability_up * (1.0 - urgency) + target_probability * urgency
            )
            direction = 1 if rng.random() < probability_up else -1

        current_tick += direction
        segment.append(current_tick)

    if current_tick != target_tick:
        raise AssertionError("market bridge did not finish at its target anchor")
    return segment, direction
