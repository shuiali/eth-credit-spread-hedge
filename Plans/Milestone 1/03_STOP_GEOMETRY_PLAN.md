# Milestone 1.3 — Explicit Stop and TP Geometry

## Objective

Remove the conflict between 0.15% of entry and 15% of price spacing. The selected
rule must be explicit, persisted, testable, and visible in audit output.

## 1. Supported stop modes

### ENTRY_PERCENT

Formula:

```text
stop_distance = entry_price × rate
stop_price = entry_price + stop_distance
```

For:

```text
entry = 3000
rate = 0.0015
```

Expected:

```text
stop distance = 4.5
stop price = 3004.5
```

This is the originally approved simplified stop rule.

### PRICE_STEP_FRACTION

Formula:

```text
stop_distance = level.price_distance × fraction
stop_price = entry_price + stop_distance
```

For:

```text
price distance = 20
fraction = 0.15
```

Expected:

```text
stop distance = 3
stop price = 3003
```

This is a different strategy.

## 2. Default policy

Until explicitly changed by approved research:

```toml
[strategy.stop]
mode = "ENTRY_PERCENT"
rate = "0.0015"
```

The runtime must not silently reinterpret `0.15` as 15%.

## 3. Configuration migration

Deprecate:

```text
ETH_HEDGE_STOP_RATE
```

because its units and reference are ambiguous.

Replace with explicit fields:

```text
ETH_HEDGE_STOP_MODE
ETH_HEDGE_ENTRY_STOP_RATE
ETH_HEDGE_PRICE_STEP_STOP_FRACTION
```

Exactly one mode-specific parameter may be present.

Deprecation behavior:

- old setting causes a startup error;
- error explains both replacement modes;
- do not auto-convert because intent is unknown.

## 4. TP geometry

The TP of a level is normally the next solved level boundary.

Rules:

```text
PRICE_STEP: TP is next USD boundary.
EQUAL_OPTION_LOSS: TP is next option-loss boundary.
DELTA_STEP: TP is next delta-solved boundary.
```

Do not independently hard-code:

```text
TP distance = spread width / level count
```

Store:

```text
entry price
TP price
actual TP distance
source spacing mode
```

## 5. Stop validation

For every short hedge level:

```text
stop_price > entry_price > tp_price
```

Reject:

```text
zero stop distance
stop at or below entry
TP at or above entry
stop outside configured operational maximum
```

## 6. Stop and option-region interaction

The stop may lie:

```text
above the current level entry
above a previous level boundary
above the short strike
```

Document whether this is allowed.

Initial rule:

- stop may cross higher virtual boundaries;
- stopping one level must not mutate another level's state;
- the crossing coordinator must process exit events before eligible entries at
  the same exact price according to explicit priority.

## 7. Worked examples

Create:

```text
docs/specification/STOP_GEOMETRY_EXAMPLES.md
```

Include at least:

```text
entry-percent stop with 5 levels
entry-percent stop with 10 levels
price-step-fraction stop with 5 levels
price-step-fraction stop with 10 levels
unequal spacing levels
last narrow level
```

Demonstrate that ENTRY_PERCENT is independent of level count, while
PRICE_STEP_FRACTION changes when level count changes.

## 8. Tests

Required tests:

- exact ENTRY_PERCENT arithmetic;
- exact PRICE_STEP_FRACTION arithmetic;
- same entry with different price steps produces same ENTRY_PERCENT stop;
- same entry with different price steps produces different
  PRICE_STEP_FRACTION stops;
- invalid mixed config;
- old ambiguous environment key rejected;
- stop mode serialized into level and events;
- runtime perturbation changes stops only;
- TP geometry follows spacing output;
- event ordering at stop/entry shared prices.

## 9. Dashboard and report labels

Later consumers must display:

```text
Stop mode: ENTRY_PERCENT
Stop parameter: 0.15% of entry
Stop distance: $4.50
```

or:

```text
Stop mode: PRICE_STEP_FRACTION
Stop parameter: 15% of zone width
Stop distance: $3.00
```

Never display only `stop rate = 0.15`.

## Acceptance gate

```text
[ ] Stop mode is explicit.
[ ] ENTRY_PERCENT is default.
[ ] Old ambiguous config is rejected.
[ ] TP follows spacing geometry.
[ ] Stop event calculations use the selected mode.
[ ] Level count no longer changes entry-percent stop distance.
[ ] Tests and examples pass.
```
