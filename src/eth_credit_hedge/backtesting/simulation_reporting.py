"""Mode-separated Plan 6 simulation metrics and comparisons."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from math import ceil


ZERO = Decimal("0")
ONE = Decimal("1")


class RunMode(str, Enum):
    EXACT_REFERENCE = "EXACT_REFERENCE"
    SIMULATED_EXCHANGE = "SIMULATED_EXCHANGE"


def _rate(value: Decimal, name: str) -> Decimal:
    normalized = Decimal(value)
    if not normalized.is_finite() or not ZERO <= normalized <= ONE:
        raise ValueError(f"{name} must be in [0, 1]")
    return normalized


def _count(value: int, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} cannot be negative")
    return value


@dataclass(frozen=True, slots=True)
class StrategyRunMetrics:
    terminal_net_pnl: Decimal
    minimum_combined_mark_pnl: Decimal
    minimum_executable_pnl: Decimal
    floor_breach_rate: Decimal
    expected_shortfall: Decimal
    maximum_recovery_debt: Decimal
    maximum_quantity: Decimal
    locked_level_rate: Decimal
    option_close_rate: Decimal
    stop_count: int

    def __post_init__(self) -> None:
        for field_name in (
            "terminal_net_pnl",
            "minimum_combined_mark_pnl",
            "minimum_executable_pnl",
            "expected_shortfall",
            "maximum_recovery_debt",
            "maximum_quantity",
        ):
            value = Decimal(getattr(self, field_name))
            if not value.is_finite():
                raise ValueError(f"{field_name.replace('_', ' ')} must be finite")
            if field_name.startswith("maximum_") and value < ZERO:
                raise ValueError(f"{field_name.replace('_', ' ')} cannot be negative")
            object.__setattr__(self, field_name, value)
        for field_name in ("floor_breach_rate", "locked_level_rate", "option_close_rate"):
            object.__setattr__(self, field_name, _rate(getattr(self, field_name), field_name))
        _count(self.stop_count, "stop count")


@dataclass(frozen=True, slots=True)
class ExecutionRunMetrics:
    order_requests: int
    filled_orders: int
    partial_fill_orders: int
    entry_slippage: Decimal
    stop_slippage: Decimal
    maximum_unprotected_ms: int
    rejected_orders: int
    reconciliation_incidents: int
    duplicate_event_count: int

    def __post_init__(self) -> None:
        for field_name in (
            "order_requests",
            "filled_orders",
            "partial_fill_orders",
            "maximum_unprotected_ms",
            "rejected_orders",
            "reconciliation_incidents",
            "duplicate_event_count",
        ):
            _count(getattr(self, field_name), field_name)
        if self.order_requests == 0:
            raise ValueError("execution metrics require at least one order request")
        if self.filled_orders + self.rejected_orders > self.order_requests:
            raise ValueError("filled and rejected orders exceed requests")
        if self.partial_fill_orders > self.filled_orders:
            raise ValueError("partial-fill orders exceed filled orders")
        for field_name in ("entry_slippage", "stop_slippage"):
            value = Decimal(getattr(self, field_name))
            if not value.is_finite() or value < ZERO:
                raise ValueError(f"{field_name.replace('_', ' ')} cannot be negative")
            object.__setattr__(self, field_name, value)

    @property
    def fill_rate(self) -> Decimal:
        return Decimal(self.filled_orders) / Decimal(self.order_requests)

    @property
    def partial_fill_rate(self) -> Decimal:
        return Decimal(self.partial_fill_orders) / Decimal(self.order_requests)

    @property
    def rejection_rate(self) -> Decimal:
        return Decimal(self.rejected_orders) / Decimal(self.order_requests)


@dataclass(frozen=True, slots=True)
class OperationalRunMetrics:
    restart_attempts: int
    restart_successes: int
    maximum_stale_duration_ms: int
    maximum_protection_restore_ms: int
    manual_interventions: int

    def __post_init__(self) -> None:
        for field_name in (
            "restart_attempts",
            "restart_successes",
            "maximum_stale_duration_ms",
            "maximum_protection_restore_ms",
            "manual_interventions",
        ):
            _count(getattr(self, field_name), field_name)
        if self.restart_attempts == 0:
            raise ValueError("operational metrics require at least one restart attempt")
        if self.restart_successes > self.restart_attempts:
            raise ValueError("restart successes exceed attempts")

    @property
    def restart_success_rate(self) -> Decimal:
        return Decimal(self.restart_successes) / Decimal(self.restart_attempts)


@dataclass(frozen=True, slots=True)
class SimulationRunReport:
    name: str
    mode: RunMode
    model_label: str
    seed: int | None
    strategy: StrategyRunMetrics
    execution: ExecutionRunMetrics | None
    operations: OperationalRunMetrics | None
    event_log_digest: str

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.model_label.strip():
            raise ValueError("run name and model label cannot be empty")
        object.__setattr__(self, "mode", RunMode(self.mode))
        if (
            len(self.event_log_digest) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in self.event_log_digest)
        ):
            raise ValueError("event log digest must be a SHA-256 hexadecimal value")
        if self.mode is RunMode.EXACT_REFERENCE:
            if self.execution is not None or self.operations is not None or self.seed is not None:
                raise ValueError("exact reference cannot contain simulated execution data")
        elif self.execution is None or self.operations is None or self.seed is None:
            raise ValueError("simulated run requires seed, execution, and operational metrics")

    @property
    def display_label(self) -> str:
        return f"{self.name} [{self.mode.value}]"


@dataclass(frozen=True, slots=True)
class RunComparison:
    labels: tuple[str, str, str]
    candidate_pnl_change: Decimal
    candidate_tail_deterioration: Decimal
    tail_risk_accepted: bool


def expected_shortfall(
    terminal_pnls: tuple[Decimal, ...],
    *,
    tail_fraction: Decimal,
) -> Decimal:
    if not terminal_pnls:
        raise ValueError("terminal P&L samples cannot be empty")
    fraction = _rate(tail_fraction, "tail fraction")
    if fraction == ZERO:
        raise ValueError("tail fraction must be positive")
    samples = tuple(Decimal(value) for value in terminal_pnls)
    if any(not sample.is_finite() for sample in samples):
        raise ValueError("terminal P&L samples must be finite")
    tail_count = ceil(len(samples) * float(fraction))
    tail = sorted(samples)[:tail_count]
    return sum(tail, ZERO) / Decimal(tail_count)


def compare_baseline_and_candidate(
    *,
    baseline_exact: SimulationRunReport,
    baseline_simulated: SimulationRunReport,
    candidate_simulated: SimulationRunReport,
    maximum_expected_shortfall_deterioration: Decimal,
) -> RunComparison:
    if baseline_exact.mode is not RunMode.EXACT_REFERENCE:
        raise ValueError("baseline exact must use EXACT_REFERENCE mode")
    if baseline_simulated.mode is not RunMode.SIMULATED_EXCHANGE:
        raise ValueError("baseline simulated must use SIMULATED_EXCHANGE mode")
    if candidate_simulated.mode is not RunMode.SIMULATED_EXCHANGE:
        raise ValueError("candidate simulated must use SIMULATED_EXCHANGE mode")
    allowed = Decimal(maximum_expected_shortfall_deterioration)
    if not allowed.is_finite() or allowed < ZERO:
        raise ValueError("maximum expected-shortfall deterioration cannot be negative")
    pnl_change = (
        candidate_simulated.strategy.terminal_net_pnl
        - baseline_simulated.strategy.terminal_net_pnl
    )
    deterioration = max(
        ZERO,
        baseline_simulated.strategy.expected_shortfall
        - candidate_simulated.strategy.expected_shortfall,
    )
    return RunComparison(
        labels=(
            baseline_exact.display_label,
            baseline_simulated.display_label,
            candidate_simulated.display_label,
        ),
        candidate_pnl_change=pnl_change,
        candidate_tail_deterioration=deterioration,
        tail_risk_accepted=deterioration <= allowed,
    )
