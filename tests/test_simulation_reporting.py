"""Separated exact/simulated reports and all Plan 6 required metrics."""

from __future__ import annotations

from decimal import Decimal

import pytest

from eth_credit_hedge.backtesting.simulation_reporting import (
    ExecutionRunMetrics,
    OperationalRunMetrics,
    RunMode,
    SimulationRunReport,
    StrategyRunMetrics,
    compare_baseline_and_candidate,
    expected_shortfall,
)


def strategy(*, pnl: str, shortfall: str) -> StrategyRunMetrics:
    return StrategyRunMetrics(
        terminal_net_pnl=Decimal(pnl),
        minimum_combined_mark_pnl=Decimal("-2"),
        minimum_executable_pnl=Decimal("-3"),
        floor_breach_rate=Decimal("0.01"),
        expected_shortfall=Decimal(shortfall),
        maximum_recovery_debt=Decimal("4"),
        maximum_quantity=Decimal("0.2"),
        locked_level_rate=Decimal("0.02"),
        option_close_rate=Decimal("0.01"),
        stop_count=2,
    )


def execution() -> ExecutionRunMetrics:
    return ExecutionRunMetrics(
        order_requests=10,
        filled_orders=9,
        partial_fill_orders=2,
        entry_slippage=Decimal("1.2"),
        stop_slippage=Decimal("2.3"),
        maximum_unprotected_ms=500,
        rejected_orders=1,
        reconciliation_incidents=0,
        duplicate_event_count=3,
    )


def operations() -> OperationalRunMetrics:
    return OperationalRunMetrics(
        restart_attempts=5,
        restart_successes=5,
        maximum_stale_duration_ms=100,
        maximum_protection_restore_ms=200,
        manual_interventions=0,
    )


def report(name: str, mode: RunMode, pnl: str, shortfall: str) -> SimulationRunReport:
    return SimulationRunReport(
        name=name,
        mode=mode,
        model_label="ORDERED_CAPTURE" if mode is RunMode.EXACT_REFERENCE else "JUMP_DIFFUSION",
        seed=None if mode is RunMode.EXACT_REFERENCE else 7,
        strategy=strategy(pnl=pnl, shortfall=shortfall),
        execution=None if mode is RunMode.EXACT_REFERENCE else execution(),
        operations=None if mode is RunMode.EXACT_REFERENCE else operations(),
        event_log_digest="a" * 64,
    )


def test_required_rates_and_expected_shortfall_are_explicit() -> None:
    metrics = execution()

    assert metrics.fill_rate == Decimal("0.9")
    assert metrics.partial_fill_rate == Decimal("0.2")
    assert metrics.rejection_rate == Decimal("0.1")
    assert operations().restart_success_rate == Decimal("1")
    assert expected_shortfall(
        (Decimal("5"), Decimal("-2"), Decimal("1"), Decimal("-6")),
        tail_fraction=Decimal("0.5"),
    ) == Decimal("-4")


def test_comparison_preserves_modes_and_flags_worse_tail_risk() -> None:
    exact = report("baseline exact", RunMode.EXACT_REFERENCE, "10", "-5")
    baseline = report("baseline simulated", RunMode.SIMULATED_EXCHANGE, "8", "-7")
    candidate = report("candidate simulated", RunMode.SIMULATED_EXCHANGE, "12", "-20")

    comparison = compare_baseline_and_candidate(
        baseline_exact=exact,
        baseline_simulated=baseline,
        candidate_simulated=candidate,
        maximum_expected_shortfall_deterioration=Decimal("2"),
    )

    assert comparison.labels == (
        "baseline exact [EXACT_REFERENCE]",
        "baseline simulated [SIMULATED_EXCHANGE]",
        "candidate simulated [SIMULATED_EXCHANGE]",
    )
    assert comparison.candidate_pnl_change == Decimal("4")
    assert comparison.candidate_tail_deterioration == Decimal("13")
    assert not comparison.tail_risk_accepted


def test_comparison_rejects_unlabeled_or_misclassified_runs() -> None:
    simulated = report("simulated", RunMode.SIMULATED_EXCHANGE, "1", "-1")

    with pytest.raises(ValueError, match="baseline exact"):
        compare_baseline_and_candidate(
            baseline_exact=simulated,
            baseline_simulated=simulated,
            candidate_simulated=simulated,
            maximum_expected_shortfall_deterioration=Decimal("0"),
        )
