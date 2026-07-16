# Stop Geometry Examples

## Authority and units

All values are exact decimals. Prices and distances are USD per ETH. The
approved default is `ENTRY_PERCENT` with `entry_stop_rate = 0.0015` (0.15% of
entry). The alternate `PRICE_STEP_FRACTION` examples use
`price_step_stop_fraction = 0.15` (15% of the solved zone width).

The TP is always copied from the next boundary produced by the selected spacing
mode. Stop geometry never creates or replaces a TP boundary.

## Equal price spacing over 3000 to 2900

| Example | Level count | First entry | First TP | Actual TP distance | Stop mode | Stop distance | Stop price |
|---|---:|---:|---:|---:|---|---:|---:|
| Entry percent | 5 | 3000 | 2980 | 20 | `ENTRY_PERCENT` | 3000 x 0.0015 = 4.5 | 3004.5 |
| Entry percent | 10 | 3000 | 2990 | 10 | `ENTRY_PERCENT` | 3000 x 0.0015 = 4.5 | 3004.5 |
| Price-step fraction | 5 | 3000 | 2980 | 20 | `PRICE_STEP_FRACTION` | 20 x 0.15 = 3 | 3003 |
| Price-step fraction | 10 | 3000 | 2990 | 10 | `PRICE_STEP_FRACTION` | 10 x 0.15 = 1.5 | 3001.5 |

At the same entry, changing level count does not change an `ENTRY_PERCENT`
stop. It changes a `PRICE_STEP_FRACTION` stop because the solved zone width
changes.

## Unequal solved spacing

Suppose `EQUAL_OPTION_LOSS` or `DELTA_STEP` produces boundaries 3000, 2970,
2955, and 2900. The runtime stores these boundaries exactly:

| Level | Entry | TP | Actual TP distance | ENTRY_PERCENT stop | PRICE_STEP_FRACTION stop |
|---:|---:|---:|---:|---:|---:|
| 1 | 3000 | 2970 | 30 | 3004.5 | 3004.5 |
| 2 | 2970 | 2955 | 15 | 2974.455 | 2972.25 |
| 3 | 2955 | 2900 | 55 | 2959.4325 | 2963.25 |

No independent `spread width / level count` TP calculation is used for these
levels.

## Last narrow price-step level

With a 30 USD `PRICE_STEP` over 3000 to 2900, the boundaries are 3000, 2970,
2940, 2910, and 2900. The final zone is only 10 USD wide. Its TP is 2900 and
its actual TP distance is 10. For the final entry at 2910:

- `ENTRY_PERCENT`: distance = 2910 x 0.0015 = 4.365; stop = 2914.365.
- `PRICE_STEP_FRACTION`: distance = 10 x 0.15 = 1.5; stop = 2911.5.

The fraction mode therefore uses the actual narrow distance, not the requested
30 USD step.

## Boundary and option-region policy

A stop may cross higher virtual boundaries, a prior level boundary, or the
short strike. This is allowed. Stopping one level changes only that level's
state. If an exit and an eligible entry share the exact same price, the crossing
coordinator processes the exit first and then recalculates entry eligibility.

An approved caller may provide an operational maximum stop price. A stop above
that explicit maximum is rejected. Plan 03 defines no deployment-wide value for
this limit, so no unapproved maximum is inferred.

## Audit labels

Examples must be reported as complete geometry, never as an ambiguous rate:

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
