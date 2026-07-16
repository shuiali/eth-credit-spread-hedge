# Milestone 1.6 — Integrated Runtime Wiring and Acceptance

## Objective

Connect the authoritative strategy-math contract to the integrated simulated
runtime without yet contacting Bybit demo.

## 1. One configuration path

The operator configuration must parse into:

```python
@dataclass(frozen=True)
class StrategyMathConfig:
    spacing: SpacingConfig
    stop: StopConfig
    valuation: ValuationConfig
    costs: CostConfig
    rounding: QuantityRoundingConfig
```

Remove separate formula settings from:

```text
legacy exact engine config
simulated exchange config
demo runner defaults
test helpers
```

Execution behavior may have separate simulated fill parameters, but sizing cost
assumptions must map explicitly into the shared cost context.

## 2. One strategy-math owner

Construct one `StrategyMathEngine` in the composition root.

Pass it into:

```text
level creation
baseline proposal creation
recovery proposal creation
risk projection
audit output
dashboard payload
```

Do not instantiate independent formula helpers inside services.

## 3. Integrated simulation command

Implement or update:

```powershell
python -m eth_credit_hedge.interfaces.strategy_runner simulate `
  --config config/full_strategy.toml `
  --scenario stop_reentry_recovery `
  --output artifacts/full_run
```

The command must print:

```text
spacing mode
spacing parameter
valuation mode
stop mode
stop parameter
zone budget
expected costs
raw quantity
submitted quantity
expected net TP
projected net stop
coverage
```

## 4. Runtime event evidence

Persist math events:

```text
LEVEL_GEOMETRY_CREATED
BASELINE_SIZING_CALCULATED
RECOVERY_SIZING_CALCULATED
SIZING_REJECTED
COVERAGE_RECALCULATED
```

Each event includes:

```text
formula version
configuration hash
inputs
units
cost breakdown
output quantity
coverage
```

## 5. Integrated scenarios

### Scenario A — PRICE_STEP, zero cost

Proves compatibility with idealized baseline.

### Scenario B — PRICE_STEP with fees

Proves hedge quantity changes.

### Scenario C — ENTRY_PERCENT stop

Proves level count does not change stop distance for a fixed entry.

### Scenario D — PRICE_STEP_FRACTION stop

Proves level count does change stop distance.

### Scenario E — stop then recovery with costs

Proves:

```text
actual stop debt includes costs
recovery sizing includes future expected costs
debt settles only from actual net TP
```

### Scenario F — quantity rounding

Proves coverage output.

### Scenario G — EQUAL_OPTION_LOSS with curved model

Proves unequal USD level distances.

### Scenario H — DELTA_STEP unavailable

Terminal model must fail safely.

### Scenario I — synthetic DELTA_STEP

Uses deterministic synthetic valuation and proves solved levels.

## 6. Integrated branch coverage

Run isolated coverage for the operator command.

Required math paths reached:

```text
PRICE_STEP
LEVEL_COUNT normalization
EQUAL_OPTION_LOSS
DELTA_STEP success
DELTA_STEP rejection
ENTRY_PERCENT
PRICE_STEP_FRACTION
baseline sizing
recovery sizing
fee cost
funding cost
slippage cost
quantity quantization
risk rejection
```

## 7. Plan 10 defect closure

Map evidence:

```text
D-001 closed:
real delta mode and corrected terminology

D-002 closed:
explicit stop modes and approved default

D-003 closed:
cost-aware baseline/recovery sizing

D-004 narrowed or closed:
funding represented in planning and confirmed debt/accounting inputs
```

Do not claim D-004 fully closed if the authoritative combined ledger has not yet
been implemented. State exactly what remains for Milestone 2.

## 8. Audit command update

The audit must report:

```text
active spacing mode
active stop mode
active valuation mode
cost fields and consumers
remaining legacy formula callers
integrated runtime coverage
golden fixture results
```

## 9. No demo gate

Milestone 1 completion authorizes only:

```text
continued offline integration work
```

It does not authorize:

```text
Bybit credentials
demo preflight
demo mutation
mainnet
```

## 10. Final acceptance checklist

```text
[ ] One math engine is constructed by the composition root.
[ ] All level geometry uses it.
[ ] All baseline sizing uses it.
[ ] All recovery sizing uses it.
[ ] Risk projection uses quantized outputs.
[ ] Operator simulation displays formula details.
[ ] Cost perturbations affect runtime quantities.
[ ] Spacing perturbations affect runtime levels.
[ ] Stop perturbations affect runtime stops.
[ ] Golden integrated scenarios pass.
[ ] Plan 10 audit is rerun for D-001 through D-004.
[ ] Remaining accounting work is explicitly handed to Milestone 2.
```

## Final commit

```powershell
git add -A
git commit -m "Milestone 1: authoritative strategy math contract"
git push
```

Tag only after acceptance:

```powershell
git tag milestone-1-strategy-math-passed
git push origin milestone-1-strategy-math-passed
```
