# Milestone 1.5 — Independent Golden Tests and Formula Migration

## Objective

Prove the new formulas independently and replace the old formulas without
allowing tests to merely restate production logic.

## 1. Golden fixture policy

Golden expected values must be calculated independently of production code.

Allowed sources:

```text
hand calculation documented in Markdown
fixed CSV/JSON fixtures
a small one-purpose verification spreadsheet committed as values only
```

Not allowed:

```text
calling production sizing to generate expected values
copying formula implementation into the test
updating fixtures automatically when tests fail
```

## 2. Create fixtures

```text
tests/fixtures/strategy_math/
├── price_step_levels.json
├── equal_loss_linear.json
├── equal_loss_curved.json
├── delta_step_synthetic.json
├── entry_percent_stops.json
├── price_step_fraction_stops.json
├── baseline_zero_cost.json
├── baseline_with_fees.json
├── recovery_with_costs.json
├── quantity_rounding.json
└── invalid_cases.json
```

Every fixture includes:

```text
description
units
inputs
calculation steps
expected outputs
independent reviewer field
```

## 3. Mandatory numerical example

Use a standard example:

```text
short put strike = 3000
long put strike = 2900
option quantity = 0.1
level count = 5
entry stop rate = 0.0015
```

For PRICE_STEP:

```text
price step = 20
boundaries = 3000, 2980, 2960, 2940, 2920, 2900
```

For first level:

```text
entry = 3000
TP = 2980
ENTRY_PERCENT stop = 3004.5
```

Add a cost-bearing case with explicit fee rates and compute:

```text
zone budget
net TP profit per ETH
raw quantity
quantized quantity
expected net TP
projected net stop
coverage
```

## 4. Characterization tests for old code

Before replacing old formulas, write tests that capture actual existing behavior:

```text
old generator produces equal USD partitions
old stop is 15% of price step
old sizing ignores costs
```

Mark them:

```python
@pytest.mark.legacy_characterization
```

These tests document what is being replaced.

## 5. Migration order

### Step A

Add new math package without changing callers.

### Step B

Add comparison adapters.

```python
new_result = StrategyMathEngine(...)
legacy_result = legacy_generator(...)
```

For approved PRICE_STEP/no-cost compatibility cases:

```text
level boundaries should match
```

Expected differences:

```text
stop distance differs after restoring ENTRY_PERCENT
cost-aware quantities differ
naming and metadata differ
```

### Step C

Migrate the legacy exact engine.

### Step D

Migrate the simulated coordinator.

### Step E

Migrate the integrated live/demo coordinator.

### Step F

Delete or quarantine old independent formula functions only after all callers
are migrated.

## 6. Search-based migration audit

Search for direct arithmetic:

```text
spread width / level count
option_budget / tp_distance
(option_budget + debt) / tp_distance
0.15 * price_step
0.0015 * entry
```

Every remaining occurrence must be:

```text
test fixture
documentation
the authoritative strategy-math package
```

No application or interface module may retain direct copies.

## 7. Mutation and perturbation tests

Add tests proving:

```text
changing price_step changes boundaries
changing level_count changes normalized price_step
changing delta_step changes solved delta boundaries
changing stop mode changes stop geometry
changing entry fee changes quantity
changing TP fee changes quantity
changing funding changes quantity
changing quantity step changes coverage
```

Also prove unrelated isolation:

```text
changing stop rate does not change level boundaries
changing fee rate does not change option zone budget
changing spacing does not alter option fill credit
```

## 8. Property tests

Useful properties:

```text
entry > TP
stop > entry
level IDs ordered
final TP equals hedge-region end
zone loss nonnegative
submitted quantity nonnegative
expected net TP equals submitted quantity × net TP per unit
undercoverage and overcoverage cannot both be positive
confirmed debt cannot be negative
```

## 9. Coverage requirements

Require for `domain/strategy_math`:

```text
>= 95% line coverage
>= 90% branch coverage
```

Every domain error branch must have a test.

## 10. Review report

Generate:

```text
artifacts/milestone1/MATH_MIGRATION_REPORT.md
```

Include:

```text
old formula
new formula
reason for change
affected files
affected tests
numerical before/after example
remaining legacy callers
```

## Acceptance gate

```text
[ ] Golden fixtures are independent.
[ ] Legacy behavior is documented.
[ ] All callers use the new engine.
[ ] Direct duplicate arithmetic is removed.
[ ] Perturbation tests pass.
[ ] Property tests pass.
[ ] Coverage threshold passes.
[ ] Migration report is generated.
```
