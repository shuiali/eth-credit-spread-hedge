# Milestone 1 Runtime Integration Acceptance

## Result

Milestone 1 strategy mathematics is accepted for continued offline integration
work only. This result does not authorize Bybit credentials, demo preflight,
demo mutation, mainnet access, or order placement.

## Composition and configuration

- `config/full_strategy.toml` is parsed into one immutable
  `StrategyMathConfig` containing spacing, stop, valuation, costs, and quantity
  rounding/instrument rules.
- `StrategyMathRuntime` constructs one `StrategyMathEngine` and passes its
  results to level evidence, baseline/recovery sizing, quantized risk
  projection, audit output, and dashboard-shaped payloads.
- The existing integrated `run_simulated_strategy_command` composition now
  constructs one engine and injects the same instance into level creation,
  baseline coordination, and confirmed-debt recovery planning.
- Simulated fill behavior remains separate from expected sizing costs; the
  mapping from simulated fee/slippage fields into `ExecutionCostContext` is
  explicit.

## Operator command

```powershell
python -m eth_credit_hedge.interfaces.strategy_runner simulate `
  --config config/full_strategy.toml `
  --scenario stop_reentry_recovery `
  --output artifacts/full_run
```

The command prints spacing/valuation/stop configuration, zone budget, expected
costs, raw and submitted quantity, expected net TP, projected net stop, and
coverage. It persists `math_events.jsonl` and `summary.json` without reading
credentials or contacting an exchange.

## Integrated scenarios

| Scenario | Evidence | Result |
|---|---|---|
| PRICE_STEP, zero cost | ideal 0.1 ETH baseline and exact coverage | PASS |
| PRICE_STEP with fees | submitted quantity changes to 0.110 ETH | PASS |
| ENTRY_PERCENT stop | fixed-entry stop distance survives level-count perturbation | PASS |
| PRICE_STEP_FRACTION stop | stop distance changes with normalized spacing | PASS |
| Stop/re-entry/recovery | cost-bearing actual debt, future costs, net-TP settlement | PASS |
| Quantity rounding | overcoverage and finite-risk rejection are explicit | PASS |
| Curved equal-loss | unequal USD distances | PASS |
| Terminal DELTA_STEP | safe explicit rejection | PASS |
| Synthetic DELTA_STEP | deterministic solved delta levels | PASS |

## Event evidence

The offline runtime persists:

- `LEVEL_GEOMETRY_CREATED`
- `BASELINE_SIZING_CALCULATED`
- `RECOVERY_SIZING_CALCULATED`
- `SIZING_REJECTED`
- `COVERAGE_RECALCULATED`

Every event contains formula version `M1.6`, configuration SHA-256, inputs,
units, cost breakdown, output quantity, and coverage.

## Coverage and quality gates

- Full non-live repository suite: PASS.
- Compilation, Ruff, and strict mypy: PASS.
- Authoritative domain: 99.49% line and 97.25% branch coverage.
- Isolated operator tests reach PRICE_STEP, LEVEL_COUNT normalization,
  EQUAL_OPTION_LOSS, DELTA_STEP success/rejection, both stop modes, baseline and
  recovery sizing, fees, signed funding, slippage, quantization, and finite-risk
  rejection.
- Operator command module: 97% combined branch/line report.
- Offline composition runtime module: 94% combined branch/line report.

## Plan 10 defect disposition

- D-001 — CLOSED: correct price terminology and real delta success/rejection.
- D-002 — CLOSED: explicit stop modes and approved ENTRY_PERCENT default.
- D-003 — CLOSED: cost-aware baseline and confirmed-debt recovery sizing.
- D-004 — NARROWED: expected and confirmed-debt inputs represent signed
  funding. Milestone 2 must implement the authoritative combined ledger from
  raw option and hedge fills, fees, funding, and slippage.

## Acceptance checklist

- [x] One math engine is constructed by the integrated composition root.
- [x] Integrated level geometry uses it.
- [x] Integrated baseline sizing uses it.
- [x] Integrated recovery sizing uses it.
- [x] Risk projection uses submitted quantized quantity.
- [x] Operator simulation displays formula details.
- [x] Cost perturbations affect runtime quantities.
- [x] Spacing perturbations affect runtime levels.
- [x] Stop perturbations affect runtime stops.
- [x] Golden integrated scenarios pass.
- [x] Plan 10 D-001 through D-004 audit evidence is regenerated.
- [x] Remaining combined-ledger work is handed to Milestone 2.
