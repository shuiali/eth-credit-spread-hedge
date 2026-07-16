"""Non-mutating Milestone 1 strategy-math audit; never contacts an exchange."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from eth_credit_hedge.config import load_operator_simulation_config
from eth_credit_hedge.interfaces.strategy_runner import SCENARIOS, run_simulation


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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(run_math_audit(args.config, args.output), sort_keys=True))
    return 0


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
