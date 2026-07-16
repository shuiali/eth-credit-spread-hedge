# Milestone 1.2 — Explicit Level Spacing Modes

## Objective

Implement three genuinely different level-generation models and prevent one from
masquerading as another.

## 1. PRICE_STEP mode

Inputs:

```text
short strike
long strike
price_step_usd
option valuation context
```

Generate:

```text
entry_1 = short strike
tp_1 = max(entry_1 - price_step_usd, long strike)
entry_2 = tp_1
...
```

Rules:

- final TP equals long strike;
- no level below long strike;
- last zone may be narrower than configured step;
- store actual zone width;
- never call this delta spacing.

## 2. LEVEL_COUNT normalization

Input:

```text
level_count
```

Normalize:

```text
price_step_usd =
    (short strike - long strike) / level_count
```

Then call the exact same `PRICE_STEP` implementation.

Test that both methods generate byte-equivalent level results when mathematically
equivalent.

## 3. EQUAL_OPTION_LOSS mode

Inputs:

```text
target_zone_loss_usd
valuation mode
option position
valuation context
```

Goal:

```text
option_value(entry_i) - option_value(tp_i)
    = target_zone_loss_usd
```

Algorithm:

1. Start at the short-strike boundary or configured hedge start.
2. Evaluate option position value at current entry.
3. Define target TP option value:
   ```text
   entry value - target zone loss
   ```
4. Solve for the next lower price.
5. Clamp the final boundary to the long strike or configured hedge end.
6. Record actual final-zone loss if smaller.
7. Stop when hedge region is exhausted.

Required valuation interfaces:

```python
class OptionValuationPort(Protocol):
    def value_at_price(
        self,
        position: OptionSpreadState,
        context: OptionValuationContext,
        underlying_price: Price,
    ) -> Money: ...

    def delta_at_price(...) -> DeltaExposure: ...
```

For `EXPIRATION`, the credit-spread loss curve is linear between strikes.
Equal-loss spacing therefore reduces to equal price spacing in that region.
This equivalence must be a test, not an assumption.

For `MARK_MODEL`, the solver may produce unequal USD distances.

## 4. DELTA_STEP mode

Goal:

```text
net_option_delta(price_i) = target_delta_i
```

Inputs:

```text
initial target delta
delta step
minimum and maximum underlying price
fresh option valuation context
```

Algorithm:

1. Evaluate the spread delta at the hedge-region start.
2. Build monotonically ordered target deltas.
3. For every target, solve for the underlying price.
4. Verify solved prices are strictly descending.
5. Reject duplicate or non-monotonic solutions.
6. Stop at the hedge-region boundary.
7. Derive TP from the next solved level.
8. Calculate actual option-loss budget from valuation, not from delta alone.

Root solver requirements:

```text
bounded bracket
maximum iteration count
absolute and relative tolerances
monotonicity validation
deterministic failure
```

## 5. Terminal-model rejection

A terminal put credit spread has no useful smooth set of distinct deltas between
the strikes. Therefore:

```python
if spacing_mode is DELTA_STEP and valuation_mode is EXPIRATION:
    raise DeltaSpacingUnavailableError
```

Do not synthesize delta values from price distance.

## 6. Data freshness requirements

`MARK_MODEL` and `DELTA_STEP` require:

```text
valuation timestamp
time to expiry
IV or option quote source
short and long leg parameters
freshness limit
```

Reject stale input before level generation.

## 7. Configuration surface

Example:

```toml
[strategy.spacing]
mode = "PRICE_STEP"
price_step_usd = "20"
```

or:

```toml
[strategy.spacing]
mode = "EQUAL_OPTION_LOSS"
target_zone_loss_usd = "2"
valuation_mode = "MARK_MODEL"
```

or:

```toml
[strategy.spacing]
mode = "DELTA_STEP"
delta_step = "0.01"
valuation_mode = "MARK_MODEL"
solver_tolerance = "0.000001"
maximum_iterations = 100
```

Mutually exclusive fields must be rejected.

## 8. Independent tests

Create fixtures with simple synthetic curves independent of production code:

### Linear value fixture

```text
value(price) = constant + slope × price
delta(price) = constant slope
```

Use it to prove equal-loss spacing.

### Curved value fixture

```text
value(price) = a + b×price + c×price²
delta(price) = b + 2c×price
```

Calculate expected roots manually or with fixed external fixture values.

Required tests:

- PRICE_STEP boundaries;
- LEVEL_COUNT equivalence;
- narrower final zone;
- EQUAL_OPTION_LOSS on linear curve;
- EQUAL_OPTION_LOSS on curved curve;
- DELTA_STEP known roots;
- stale valuation rejection;
- terminal DELTA_STEP rejection;
- monotonicity failure;
- root not bracketed;
- perturbing delta step changes solved prices;
- perturbing price step does not change target deltas because none exist.

## 9. Migration

The current generator becomes:

```python
LegacyPriceStepLevelGenerator
```

temporarily.

Add comparison tests:

```text
legacy output == new PRICE_STEP output
```

Only after equivalence passes should current runtime callers migrate to the new
engine.

## Acceptance gate

```text
[ ] Existing behavior is correctly labelled PRICE_STEP.
[ ] LEVEL_COUNT normalizes to PRICE_STEP.
[ ] EQUAL_OPTION_LOSS works with a curved model.
[ ] DELTA_STEP uses real option delta.
[ ] Terminal DELTA_STEP fails explicitly.
[ ] Configuration perturbation tests pass.
[ ] No runtime caller still calls USD spacing delta spacing.
```
