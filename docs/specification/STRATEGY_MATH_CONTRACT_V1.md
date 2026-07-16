# Strategy Math Contract V1

## Status and authority

This document is the Milestone 1 mathematical contract. It separates units,
modes, validation, and reporting from historical variable names. Milestone 1.1
defines the contracts only; later Milestone 1 plans implement and migrate the
formulas through the single `StrategyMathEngine` façade.

No exchange adapter, demo mutation, or mainnet behavior is authorized here.

## Units

| Value | Unit |
|---|---|
| `Price` | USD per ETH |
| `Quantity` | ETH |
| `Money` | USD |
| `Rate` | dimensionless decimal |
| `DeltaExposure` | ETH-equivalent option delta exposure |
| `Volatility` | annualized decimal |
| `Seconds` | seconds |
| level count / iteration count | dimensionless integer |

All formula inputs are exact `Decimal` values. Binary floating-point inputs are
not part of the contract. Prices and quantities are positive; rates, volatility,
and elapsed seconds are nonnegative. Signed USD and delta exposure are allowed
where they represent P&L or directional exposure.

## Supported spacing modes

`PRICE_STEP` uses an explicit `price_step_usd` in USD/ETH. For a short hedge,
successive entry and TP boundaries decrease in underlying price. This value must
never be called delta spacing.

`LEVEL_COUNT` is a user-facing convenience. It normalizes to `PRICE_STEP` using
the spread width divided by the positive level count, then uses the same price
boundary implementation. It is not a separate runtime formula.

`EQUAL_OPTION_LOSS` solves boundaries so each zone has the configured option
value loss:

```text
zone_option_loss_budget = option_value(entry) - option_value(tp)
```

The valuation mode is explicit and the target budget is positive USD.

`DELTA_STEP` solves boundaries from net option delta targets measured in
ETH-equivalent exposure. It requires a current real delta source and a bounded
solver interval. Terminal expiration payoff alone is rejected because it cannot
honestly supply a multi-level delta grid.

## Supported stop modes

`ENTRY_PERCENT` defines:

```text
stop_distance = entry_price * rate
stop_price = entry_price + stop_distance
```

`PRICE_STEP_FRACTION` defines:

```text
stop_distance = price_distance * fraction
stop_price = entry_price + stop_distance
```

The two rules are distinct and must not share an ambiguous configuration key.
For a short hedge, the stop is strictly above entry. Milestone 1.3 owns runtime
migration and the approved deployment default.

## Supported option valuation modes

`EXPIRATION` uses terminal spread value. It may support linear price-step zones,
but it cannot support true `DELTA_STEP`.

`MARK_MODEL` uses a pre-expiry mark-to-market model with an explicit observation
time, validity interval, and delta capability when required.

`EXECUTABLE_LIQUIDATION` uses executable close prices for both legs, including
the declared close-cost treatment. It also needs a fresh valuation context.

Absent, stale, future-dated, or unsupported contexts are rejected.

## Level and zone-loss definition

Each immutable level records its entry, TP, price distance, optional target
delta, option values at both boundaries, zone option-loss budget, stop geometry,
spacing mode, stop mode, and valuation mode.

```text
price_distance = entry_price - tp_price
zone_option_loss_budget = entry_option_value - tp_option_value
stop_distance = stop_price - entry_price
```

The zone budget is USD and is nonnegative. The shortcut
`option_quantity * price_distance` is permitted only for an explicitly selected
linear terminal-payoff region; it is not the general definition.

## Cost components

Sizing and coverage must account for all applicable components before quantity
is selected:

- option long and short entry fees;
- option close fees;
- perpetual entry fee;
- perpetual TP or stop fee;
- expected funding;
- bid/ask spread and entry/exit slippage;
- any explicitly approved cost buffer.

Every rate is dimensionless and nonnegative; every resulting fee, funding,
slippage, or buffer amount is USD. Milestone 1.4 defines the immutable execution
cost context and exact allocation rules.

## Baseline sizing definition

For one ETH of submitted short hedge, calculate expected entry and TP fills and
subtract all expected costs to obtain `net_tp_profit_per_unit` in USD/ETH.

```text
raw_baseline_quantity =
    (zone_option_loss_budget + explicit_cost_buffer)
    / net_tp_profit_per_unit
```

Nonpositive net TP profit is rejected. The ideal zero-cost linear terminal case
may reduce to hedge quantity equalling option quantity, but that is a consequence,
not a general rule.

## Recovery sizing definition

Recovery uses confirmed debt only. Projected debt may inform risk reporting but
must not increase live recovery quantity.

```text
raw_recovery_quantity =
    (zone_option_loss_budget + confirmed_recovery_debt + explicit_cost_buffer)
    / net_tp_profit_per_unit
```

Rejected, unsubmitted, or unfilled recovery orders do not settle debt and must
never be described as fully recovered.

## Rounding and coverage behavior

Raw quantity is quantized using explicit `FLOOR`, `CEIL`, or `NEAREST` behavior
and the instrument quantity step. Exchange minimum quantity, maximum quantity,
minimum notional, tick size, and contract multiplier are validated before an
order can be submitted.

After quantization, all expected TP profit, stop loss, costs, notional, margin,
overcoverage, and undercoverage are recalculated from the submitted quantity.
Coverage is exact only when undercoverage is zero. Rounding undercoverage is an
observable result, not a hidden assertion failure.

## Rejection behavior

The contract rejects nonpositive prices or quantities, invalid spread strikes,
negative cost rates, invalid mode-specific configuration, stale or absent
valuation context, unavailable delta sources, invalid solver bounds, invalid
short-hedge TP/stop geometry, nonpositive net profit, and unreportable
post-quantization coverage.

Failures use `StrategyMathError` subclasses with messages that name the invalid
field, its expected unit or relationship, and the corrective action where one is
unambiguous.

## Explicitly unresolved decisions

Milestone 1.1 does not choose the following silently:

1. The approved deployment default between `ENTRY_PERCENT` and
   `PRICE_STEP_FRACTION`; Milestone 1.3 must resolve and migrate it.
2. The concrete pre-expiry mark model, volatility source, interpolation policy,
   and executable-liquidation quote policy; Milestone 1.2 owns them.
3. Cost rates, funding horizon, slippage convention, option-cost allocation, and
   explicit buffer policy; Milestone 1.4 owns them.
4. The deployment rounding mode and whether undercoverage is rejected or
   accepted within an explicit tolerance; Milestone 1.4 owns them.
5. Runtime migration, legacy deprecation, and compatibility evidence; Milestones
   1.5 and 1.6 own them.

Until those decisions are implemented and accepted, the existing formulas remain
legacy behavior and are not silently reinterpreted by this contract layer.
