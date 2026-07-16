"""Offline integrated acceptance for the Milestone 1 strategy-math command."""

from __future__ import annotations

import json
from pathlib import Path

from eth_credit_hedge.config import load_operator_simulation_config
from eth_credit_hedge.interfaces.project_audit import run_math_audit
from eth_credit_hedge.interfaces.strategy_runner import SCENARIOS, main, run_simulation


CONFIG = Path(__file__).parents[2] / "config" / "full_strategy.toml"


def test_one_configuration_path_is_stable_and_consumed(tmp_path: Path) -> None:
    first = load_operator_simulation_config(CONFIG)
    second = load_operator_simulation_config(CONFIG)

    assert first.math.configuration_hash == second.math.configuration_hash
    result = run_simulation(first, "stop_reentry_recovery", tmp_path)
    payload = result["results"]["stop_reentry_recovery"]  # type: ignore[index]
    assert first.math.spacing.mode.value == "LEVEL_COUNT"
    assert payload["spacing_mode"] == "PRICE_STEP"  # type: ignore[index]
    assert payload["spacing_parameter"] == "5"  # type: ignore[index]
    assert payload["stop_mode"] == first.math.stop.mode.value  # type: ignore[index]
    assert payload["valuation_mode"] == first.math.valuation.mode.value  # type: ignore[index]


def test_all_nine_integrated_scenarios_and_perturbations_pass(tmp_path: Path) -> None:
    result = run_simulation(load_operator_simulation_config(CONFIG), "all", tmp_path)
    scenarios = result["results"]
    assert isinstance(scenarios, dict)
    assert set(scenarios) == set(SCENARIOS)

    zero = scenarios["price_step_zero_cost"]
    fees = scenarios["price_step_with_fees"]
    assert zero["submitted_quantity"] == "0.1"
    assert fees["submitted_quantity"] != zero["submitted_quantity"]
    assert fees["zone_budget"] == zero["zone_budget"]

    entry_stop = scenarios["entry_percent_stop"]
    step_stop = scenarios["price_step_fraction_stop"]
    assert entry_stop["projected_net_stop"] != step_stop["projected_net_stop"]
    assert entry_stop["perturbed_stop_distance"] == entry_stop["expected_costs"]["gross_stop_loss_per_unit"]["value"]
    assert step_stop["perturbed_stop_distance"] != step_stop["expected_costs"]["gross_stop_loss_per_unit"]["value"]

    recovery = scenarios["stop_reentry_recovery"]
    assert recovery["actual_stop_debt"] != "0"
    assert recovery["recovery_submitted_quantity"] > recovery["submitted_quantity"]
    assert recovery["remaining_confirmed_debt"] in {"0", "0E-11"}

    rounding = scenarios["quantity_rounding"]
    assert rounding["coverage"]["overcoverage"] != "0"
    assert rounding["risk_rejection"] == "REJECTED_BY_RISK"
    rejection_events = (
        tmp_path
        / "quantity_rounding"
        / "risk_rejection"
        / "math_events.jsonl"
    ).read_text(encoding="utf-8")
    assert '"event": "SIZING_REJECTED"' in rejection_events

    curved = scenarios["equal_option_loss_curved"]
    assert len(set(curved["level_distances"])) > 1
    assert scenarios["delta_step_unavailable"]["safe_failure"] is True
    synthetic = scenarios["synthetic_delta_step"]
    assert synthetic["spacing_mode"] == "DELTA_STEP"
    assert len(synthetic["level_entries"]) == 3


def test_math_events_include_required_audit_fields(tmp_path: Path) -> None:
    run_simulation(load_operator_simulation_config(CONFIG), "stop_reentry_recovery", tmp_path)
    path = tmp_path / "stop_reentry_recovery" / "math_events.jsonl"
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    names = {event["event"] for event in events}

    assert {
        "LEVEL_GEOMETRY_CREATED",
        "BASELINE_SIZING_CALCULATED",
        "RECOVERY_SIZING_CALCULATED",
        "COVERAGE_RECALCULATED",
    } <= names
    for event in events:
        assert event["formula_version"] == "M1.6"
        assert len(event["configuration_hash"]) == 64
        assert "inputs" in event
        assert "units" in event
        assert "cost_breakdown" in event
        assert "output_quantity" in event
        assert "coverage" in event


def test_operator_main_prints_required_formula_details(
    tmp_path: Path,
    capsys: object,
) -> None:
    assert main([
        "simulate",
        "--config",
        str(CONFIG),
        "--scenario",
        "stop_reentry_recovery",
        "--output",
        str(tmp_path),
    ]) == 0
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    for field in (
        "spacing_mode",
        "spacing_parameter",
        "valuation_mode",
        "stop_mode",
        "stop_parameter",
        "zone_budget",
        "expected_costs",
        "raw_quantity",
        "submitted_quantity",
        "expected_net_tp",
        "projected_net_stop",
        "coverage",
    ):
        assert f'"{field}"' in output


def test_math_audit_closes_or_narrows_plan10_findings(tmp_path: Path) -> None:
    report = run_math_audit(CONFIG, tmp_path)

    assert report["offline_only"] is True
    assert report["remaining_legacy_formula_callers"] == []
    defects = report["defects"]
    assert isinstance(defects, dict)
    assert all("CLOSED" in defects[name] for name in ("D-001", "D-002", "D-003"))
    assert "NARROWED" in defects["D-004"]
    assert "Milestone 2" in report["milestone2_handoff"]
    assert (tmp_path / "MILESTONE1_DEFECT_CLOSURE.md").exists()
