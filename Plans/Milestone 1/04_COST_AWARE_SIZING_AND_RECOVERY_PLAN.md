# Milestone 1.4 — Cost-Aware Baseline Sizing and Recovery

## Objective

Replace gross-distance sizing with one cost-aware formula shared by baseline and
recovery entries.

## 1. Cost model

Create:

```python
@dataclass(frozen=True)
class ExecutionCostContext:
    expected_entry_price: Price
    expected_tp_price: Price
    expected_stop_price: Price

    entry_fee_rate: Rate
    tp_fee_rate: Rate
    stop_fee_rate: Rate

    expected_entry_slippage_per_unit: Money
    expected_tp_slippage_per_unit: Money
    expected_stop_slippage_per_unit: Money

    expected_funding_to_tp_per_unit: Money
    expected_funding_to_stop_per_unit: Money

    spread_cost_entry_per_unit: Money
    spread_cost_tp_per_unit: Money
    spread_cost_stop_per_unit: Money
```

Simulation may populate expected costs from `ExecutionModelConfig`.
Live planning may use configured conservative estimates.
Confirmed accounting later replaces estimates with actual fills.

## 2. Net TP profit per unit

For one ETH short:

```text
gross_tp_profit_per_unit =
    expected_entry_price - expected_tp_price
```

Expected entry fee per unit:

```text
entry_fee_per_unit =
    expected_entry_price × entry_fee_rate
```

TP fee per unit:

```text
tp_fee_per_unit =
    expected_tp_price × tp_fee_rate
```

Then:

```text
net_tp_profit_per_unit =
    gross_tp_profit_per_unit
    - entry_fee_per_unit
    - tp_fee_per_unit
    - expected_entry_slippage_per_unit
    - expected_tp_slippage_per_unit
    - expected_funding_to_tp_per_unit
    - spread_cost_entry_per_unit
    - spread_cost_tp_per_unit
```

Reject when:

```text
net_tp_profit_per_unit <= 0
```

## 3. Net stop loss per unit

```text
gross_stop_loss_per_unit =
    expected_stop_price - expected_entry_price
```

```text
net_stop_loss_per_unit =
    gross_stop_loss_per_unit
    + entry_fee_per_unit
    + stop_fee_per_unit
    + expected_entry_slippage_per_unit
    + expected_stop_slippage_per_unit
    + expected_funding_to_stop_per_unit
    + spread_cost_entry_per_unit
    + spread_cost_stop_per_unit
```

## 4. Zone budget

Use:

```text
zone_option_loss_budget =
    option_value(entry) - option_value(TP)
```

The cost model does not change the option-loss budget. It changes the hedge
quantity required to cover it net of costs.

## 5. Baseline quantity

```text
raw_baseline_quantity =
    (
        zone_option_loss_budget
        + configured_baseline_buffer
    )
    / net_tp_profit_per_unit
```

Then quantize.

Do not assume:

```text
baseline quantity = option quantity
```

That equality is merely a special no-cost, terminal-linear case.

## 6. Recovery quantity

Use confirmed debt:

```text
raw_recovery_quantity =
    (
        zone_option_loss_budget
        + confirmed_recovery_debt
        + configured_recovery_buffer
    )
    / net_tp_profit_per_unit
```

`confirmed_recovery_debt` includes only actual confirmed losses from prior
attempts.

## 7. Quantization

Create one quantity function:

```python
def quantize_quantity(
    raw_quantity: Quantity,
    rules: InstrumentRules,
    mode: QuantityRoundingMode,
) -> Quantity:
    ...
```

Initial recommended mode for full coverage:

```text
CEIL
```

But enforce:

```text
maximum quantity
maximum notional
risk limits
```

After quantization:

```text
expected_net_tp_profit =
    submitted_quantity × net_tp_profit_per_unit
projected_net_stop_loss =
    submitted_quantity × net_stop_loss_per_unit
```

Calculate:

```text
overcoverage
undercoverage
```

## 8. Risk rejection

If quantized recovery exceeds a finite risk limit:

```text
status = REJECTED_BY_RISK
recoverable_amount = 0
remaining_debt unchanged
```

Do not floor quantity and claim complete recovery.

## 9. Actual debt after stop

When fills exist:

```text
actual_realized_pnl =
    short sale proceeds
    - buyback cost
    - allocated entry fees
    - stop fees
    + actual funding
```

For a loss:

```text
confirmed_debt_increment = max(-actual_realized_pnl, 0)
```

Explicitly store:

```text
price loss
fees
funding
slippage versus reference
total debt
```

## 10. Funding sign convention

Funding may be received or paid.

Use:

```text
funding_pnl > 0 means income
funding_pnl < 0 means cost
```

Debt calculation:

```text
net realized P&L includes funding P&L
```

Do not blindly add absolute funding.

## 11. Result object

```python
@dataclass(frozen=True)
class SizingResult:
    role: Literal["BASELINE", "RECOVERY"]
    raw_quantity: Quantity
    submitted_quantity: Quantity
    net_tp_profit_per_unit: Money
    net_stop_loss_per_unit: Money
    expected_net_tp_profit: Money
    projected_net_stop_loss: Money
    required_budget: Money
    overcoverage: Money
    undercoverage: Money
    fully_covered: bool
    quantization_mode: QuantityRoundingMode
    cost_breakdown: CostBreakdown
    status: SizingStatus
```

## 12. Tests

Hand-calculate expected values.

Required cases:

- zero costs reproduces ideal baseline;
- entry and TP fees increase required quantity;
- funding cost increases required quantity;
- received funding decreases required quantity;
- spread and slippage increase required quantity;
- stop loss includes entry and stop costs;
- quantity step rounding;
- minimum quantity;
- maximum quantity rejection;
- maximum notional rejection;
- net TP profit nonpositive;
- 0.1 ETH option does not imply 0.1 ETH hedge with costs;
- confirmed actual debt differs from projected debt;
- recovery debt is unchanged when recovery is rejected.

## 13. Configuration

Example:

```toml
[strategy.costs]
baseline_buffer_usd = "0"
recovery_buffer_usd = "0"

[strategy.costs.perpetual]
entry_fee_rate = "0.00055"
tp_fee_rate = "0.00020"
stop_fee_rate = "0.00055"
expected_entry_slippage_bps = "1"
expected_tp_slippage_bps = "1"
expected_stop_slippage_bps = "3"
expected_funding_to_tp_usd_per_eth = "0"
expected_funding_to_stop_usd_per_eth = "0"
```

Every field needs a consumer and perturbation test.

## Acceptance gate

```text
[ ] Baseline and recovery use net profit per unit.
[ ] Fees are included before quantity calculation.
[ ] Funding has a defined sign convention.
[ ] Spread and slippage are represented.
[ ] Quantized risk is recalculated.
[ ] Undercoverage is visible.
[ ] Rejected recovery is explicit.
[ ] Actual stop debt includes actual costs and funding.
[ ] Golden numerical tests pass.
```
