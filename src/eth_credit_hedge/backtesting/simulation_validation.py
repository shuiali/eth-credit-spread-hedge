"""Predeclared acceptance thresholds for simulator safety validation."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


PREDECLARED_THRESHOLDS: Mapping[str, float] = MappingProxyType(
    {
        "maximum_unprotected_ms": 1000.0,
        "duplicate_executions_counted": 0.0,
        "unknown_state_order_count": 0.0,
        "restart_success_rate": 1.0,
        "risk_limit_bypass_count": 0.0,
        "pnl_reproducibility_rate": 1.0,
    }
)


@dataclass(frozen=True, slots=True)
class SimulationValidationMetrics:
    maximum_unprotected_ms: int
    duplicate_executions_counted: int
    unknown_state_order_count: int
    restart_attempts: int
    restart_successes: int
    risk_limit_bypass_count: int
    reproducible_pnl_runs: int
    total_pnl_runs: int


@dataclass(frozen=True, slots=True)
class SimulationAcceptanceReport:
    accepted: bool
    failures: tuple[str, ...]


def _rate(numerator: int, denominator: int) -> float:
    if denominator <= 0 or numerator < 0 or numerator > denominator:
        raise ValueError("metric counts must describe a non-empty valid rate")
    return numerator / denominator


def evaluate_simulation_acceptance(
    metrics: SimulationValidationMetrics,
    thresholds: Mapping[str, float],
) -> SimulationAcceptanceReport:
    """Evaluate fixed gates without deriving limits from the observed run."""

    required = set(PREDECLARED_THRESHOLDS)
    if set(thresholds) != required:
        raise ValueError("thresholds must contain exactly the predeclared gate names")
    observed = {
        "maximum_unprotected_ms": float(metrics.maximum_unprotected_ms),
        "duplicate_executions_counted": float(metrics.duplicate_executions_counted),
        "unknown_state_order_count": float(metrics.unknown_state_order_count),
        "restart_success_rate": _rate(metrics.restart_successes, metrics.restart_attempts),
        "risk_limit_bypass_count": float(metrics.risk_limit_bypass_count),
        "pnl_reproducibility_rate": _rate(
            metrics.reproducible_pnl_runs,
            metrics.total_pnl_runs,
        ),
    }
    minimum_gates = {"restart_success_rate", "pnl_reproducibility_rate"}
    failures = tuple(
        name
        for name, threshold in thresholds.items()
        if (
            observed[name] < threshold
            if name in minimum_gates
            else observed[name] > threshold
        )
    )
    return SimulationAcceptanceReport(accepted=not failures, failures=failures)
