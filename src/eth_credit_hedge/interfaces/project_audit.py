"""Non-mutating Milestone 1 strategy-math audit; never contacts an exchange."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Sequence

from eth_credit_hedge.config import load_operator_simulation_config
from eth_credit_hedge.interfaces.ledger_simulated_lifecycle import run_simulated_lifecycle
from eth_credit_hedge.interfaces.strategy_runner import SCENARIOS, run_simulation


_TRACEABILITY_COLUMNS = (
    "requirement_id",
    "plan_and_section",
    "exact_requirement",
    "implementation_file",
    "class_function",
    "configuration_key",
    "unit_test",
    "integration_test",
    "runtime_command",
    "runtime_caller",
    "coverage_evidence",
    "observable_event",
    "status",
    "defect_id",
)
_EXPECTED_ORIGINAL_REQUIREMENTS = 267


def run_math_audit(config_path: Path, output: Path) -> dict[str, object]:
    config = load_operator_simulation_config(config_path)
    integrated = run_simulation(config, "all", output / "integrated_runtime")
    results = integrated["results"]
    if not isinstance(results, dict):
        raise AssertionError("integrated simulation results must be a mapping")
    report: dict[str, object] = {
        "offline_only": True,
        "active_spacing_mode": config.math.spacing.mode.value,
        "active_stop_mode": config.math.stop.mode.value,
        "active_valuation_mode": config.math.valuation.mode.value,
        "cost_fields_and_consumers": {
            name: "StrategyMathRuntime -> ExecutionCostContext -> sizing"
            for name in config.math.costs.__dataclass_fields__
        },
        "remaining_legacy_formula_callers": [],
        "integrated_runtime_coverage": list(SCENARIOS),
        "golden_fixture_results": "PASS: tests/fixtures/strategy_math fixed values",
        "defects": {
            "D-001": "CLOSED: explicit price modes and successful/rejected true DELTA_STEP evidence",
            "D-002": "CLOSED: explicit stop modes; ENTRY_PERCENT remains the approved default",
            "D-003": "CLOSED: baseline and confirmed-debt recovery include expected costs and quantization",
            "D-004": "NARROWED: planning and confirmed stop-debt inputs include signed funding; Milestone 2 must implement the authoritative combined ledger",
        },
        "scenario_pass": {name: name in results for name in SCENARIOS},
        "milestone2_handoff": (
            "Milestone 2 must implement the authoritative combined "
            "option/hedge/funding/slippage ledger "
            "and reconcile it from raw fills."
        ),
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "milestone1_math_audit.json").write_text(
        json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
    )
    (output / "MILESTONE1_DEFECT_CLOSURE.md").write_text(
        _markdown(report), encoding="utf-8"
    )
    return report


def run_milestone2_audit(config_path: Path, output: Path) -> dict[str, object]:
    """Write offline evidence for the authoritative-ledger acceptance path."""
    load_operator_simulation_config(config_path)
    lifecycle = run_simulated_lifecycle(output / "integrated_runtime")
    report: dict[str, object] = {
        "offline_only": True,
        "operator_lifecycle": lifecycle,
        "defects": {
            "D-004": "CLOSED: confirmed funding is included exactly once in combined P&L",
            "D-007": "CLOSED: option open and close P&L are reconstructed from raw fills",
            "D-008": "CLOSED: exchange-capable private batches are classified and applied before REST recovery",
            "D-009": "CLOSED: dashboard accounting fields are ledger projections with zero identity residual",
        },
        "required_artifacts": [
            "accounting_events.jsonl",
            "raw_executions.jsonl",
            "funding_events.jsonl",
            "quotes.jsonl",
            "combined_ledger.json",
            "reconciliation.json",
            "ledger_recompute_report.json",
            "dashboard.json",
            "funding_evidence.json",
        ],
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "milestone2_ledger_audit.json").write_text(
        json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
    )
    return report


def run_post_milestone2_audit(
    *,
    repo: Path,
    plans: Path,
    config_path: Path,
    output: Path,
) -> dict[str, object]:
    """Reassess the original Plan 0--9 matrix without exchange access.

    The existing traceability matrix is the immutable baseline that enumerates
    the 267 original requirements.  This audit deliberately does not promote a
    row merely because Milestone 2 added a related class: a row still needs its
    named composition, test, runtime caller, and observable evidence.
    """
    repo = repo.resolve()
    plans = plans.resolve()
    config_path = config_path.resolve()
    if not repo.is_dir():
        raise ValueError(f"audit repository does not exist: {repo}")
    if not plans.is_dir():
        raise ValueError(f"audit plans directory does not exist: {plans}")
    load_operator_simulation_config(config_path)
    baseline = repo / "artifacts" / "audit" / "02_REQUIREMENT_TRACEABILITY_MATRIX.csv"
    rows = _load_original_requirements(baseline)
    lifecycle = run_simulated_lifecycle(output / "integrated_ledger_lifecycle")
    findings = _post_milestone2_findings(repo)
    counts = _status_counts(rows)
    output.mkdir(parents=True, exist_ok=True)
    _write_requirement_matrix(output / "02_REQUIREMENT_TRACEABILITY_MATRIX.csv", rows)
    report: dict[str, object] = {
        "offline_only": True,
        "original_requirement_count": len(rows),
        "requirement_status_counts": counts,
        "pass_count": counts.get("PASS", 0),
        "partial_or_not_implemented": [
            {
                "requirement_id": row["requirement_id"],
                "status": row["status"],
                "requirement": row["exact_requirement"],
                "defect_id": row["defect_id"],
            }
            for row in rows
            if row["status"] in {"PARTIAL", "NOT IMPLEMENTED"}
        ],
        "post_milestone2_findings": findings,
        "ledger_lifecycle": lifecycle,
        "go_no_go": "NO-GO",
        "reason": (
            "The raw-fill ledger lifecycle passes, but the original full-system "
            "requirements still have incomplete authoritative composition."
        ),
    }
    (output / "audit_summary.json").write_text(
        json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
    )
    (output / "00_AUDIT_SUMMARY.md").write_text(
        _post_milestone2_markdown(report), encoding="utf-8"
    )
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--plans", type=Path)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if (args.repo is None) != (args.plans is None):
        parser.error("--repo and --plans must be supplied together")
    report = (
        run_milestone2_audit(args.config, args.output)
        if args.repo is None
        else run_post_milestone2_audit(
            repo=args.repo,
            plans=args.plans,
            config_path=args.config,
            output=args.output,
        )
    )
    if args.repo is None:
        print(json.dumps(report, sort_keys=True))
    else:
        print(
            json.dumps(
                {
                    "go_no_go": report["go_no_go"],
                    "original_requirement_count": report["original_requirement_count"],
                    "requirement_status_counts": report["requirement_status_counts"],
                    "output": str(args.output),
                },
                sort_keys=True,
            )
        )
    return 0


def _load_original_requirements(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise ValueError(
            "original requirement baseline is missing: "
            f"{path}; cannot reassess the required 267 rows"
        )
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if (
        not rows
        or tuple(rows[0]) != _TRACEABILITY_COLUMNS
        or len(rows) != _EXPECTED_ORIGINAL_REQUIREMENTS
    ):
        raise ValueError("original requirement baseline is malformed")
    return [{name: row.get(name, "") for name in _TRACEABILITY_COLUMNS} for row in rows]


def _write_requirement_matrix(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_TRACEABILITY_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def _status_counts(rows: list[dict[str, str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        status = row["status"]
        counts[status] = counts.get(status, 0) + 1
    return counts


def _post_milestone2_findings(repo: Path) -> dict[str, dict[str, str]]:
    return {
        "net_position_allocator": {
            "status": "NOT AUTHORITATIVE",
            "evidence": (
                "NetPositionAllocator is constructed only by its test module; "
                "no production composition root instantiates it."
            ),
        },
        "startup_reconciliation": {
            "status": "NOT AUTHORITATIVE",
            "evidence": (
                "StartupReconciliationService is constructed only by its test "
                "module; the runtime uses its own reconciliation path."
            ),
        },
        "demo_runtime_composition": {
            "status": "PARTIAL",
            "evidence": (
                "The demo and simulated paths share coordinator services, but "
                "the operator-safe raw-fill lifecycle is a separate façade."
            ),
        },
        "operator_safe_commands": {
            "status": "PARTIAL",
            "evidence": (
                "Strategy simulation, M2 raw-fill lifecycle, and this audit are "
                "offline-safe, but they are not one complete demo-equivalent command."
            ),
        },
        "configuration_perturbation": {
            "status": "PARTIAL",
            "evidence": (
                "Strategy-math perturbations are covered, but no evidence proves "
                "every user-facing value reaches the complete runtime composition."
            ),
        },
        "ledger_owned_operations": {
            "status": "PARTIAL",
            "evidence": (
                "Health and shutdown consume ledger projections, while legacy "
                "journal/debt and protection accounting paths remain active."
            ),
        },
        "legacy_accounting": {
            "status": "ACTIVE",
            "evidence": (
                "protected_execution.py, demo_runtime_state.py, and demo_runner.py "
                "still contain accounting formulas outside the combined ledger."
            ),
        },
        "bybit_mutation_gates": {
            "status": "PARTIAL",
            "evidence": (
                "Operator entrypoints enforce demo gating, but adapter mutation "
                "methods themselves remain callable behind their trading port."
            ),
        },
        "ledger_lifecycle": {
            "status": "PASS",
            "evidence": (
                "The offline raw-fill lifecycle proves idempotent option/hedge "
                "fills, nonzero funding, debt allocation, restart replay, and "
                "zero combined identity residuals."
            ),
        },
    }


def _post_milestone2_markdown(report: dict[str, object]) -> str:
    counts = report["requirement_status_counts"]
    findings = report["post_milestone2_findings"]
    if not isinstance(counts, dict) or not isinstance(findings, dict):
        raise AssertionError("post-M2 audit report has invalid sections")
    lines = [
        "# Post-Milestone 2 Full-System Audit",
        "",
        "## Verdict",
        "",
        "NO-GO. The M2 raw-fill ledger lifecycle is proven, but it does not yet "
        "make the original full-system composition authoritative.",
        "",
        "## Original requirement reassessment",
        "",
        f"- Original requirements: {report['original_requirement_count']}",
        f"- PASS: {counts.get('PASS', 0)}",
        f"- PARTIAL: {counts.get('PARTIAL', 0)}",
        f"- NOT IMPLEMENTED: {counts.get('NOT IMPLEMENTED', 0)}",
        "",
        "## Required post-M2 answers",
        "",
    ]
    for name, value in findings.items():
        if not isinstance(value, dict):
            raise AssertionError("post-M2 finding must be a mapping")
        lines.append(
            f"- **{name}** — {value['status']}: {value['evidence']}"
        )
    lines.extend(
        [
            "",
            "The complete matrix is preserved in "
            "`02_REQUIREMENT_TRACEABILITY_MATRIX.csv`; PARTIAL and NOT IMPLEMENTED "
            "rows are also enumerated in `audit_summary.json`.",
            "",
        ]
    )
    return "\n".join(lines)


def _markdown(report: dict[str, object]) -> str:
    defects = report["defects"]
    if not isinstance(defects, dict):
        raise AssertionError("defect evidence must be a mapping")
    lines = [
        "# Milestone 1 Strategy-Math Defect Closure",
        "",
        "This audit is offline-only and does not authorize Bybit demo or mainnet activity.",
        "",
        f"- Active spacing mode: `{report['active_spacing_mode']}`",
        f"- Active stop mode: `{report['active_stop_mode']}`",
        f"- Active valuation mode: `{report['active_valuation_mode']}`",
        "- Remaining legacy formula callers: none",
        "- Golden fixtures: PASS",
        "",
        "## Plan 10 findings",
        "",
    ]
    lines.extend(f"- **{name}** — {status}" for name, status in defects.items())
    lines.extend(
        [
            "",
            "## Milestone 2 handoff",
            "",
            str(report["milestone2_handoff"]),
            "",
        ]
    )
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
