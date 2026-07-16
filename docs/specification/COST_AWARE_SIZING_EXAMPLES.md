# Cost-Aware Sizing Examples

Milestone 1.4 sizes both baseline and recovery hedges from expected net TP
profit per ETH. All values use exact decimal arithmetic.

For an expected short entry at 3000, TP at 2990, and stop at 3005 with no costs:

```text
net TP profit per ETH = 3000 - 2990 = 10 USD
net stop loss per ETH = 3005 - 3000 = 5 USD
zone budget = 1 USD
raw baseline quantity = 1 / 10 = 0.1 ETH
```

With 0.1% entry and TP fees:

```text
entry fee per ETH = 3000 × 0.001 = 3.000 USD
TP fee per ETH = 2990 × 0.001 = 2.990 USD
net TP profit per ETH = 10 - 3.000 - 2.990 = 4.010 USD
raw quantity = 1 / 4.010 = 0.249376... ETH
submitted quantity at a 0.001 step with CEIL = 0.250 ETH
```

Funding uses the accounting sign convention. Positive funding is income and is
added to net P&L; negative funding is a cost. Thus +1 USD/ETH funding raises net
TP profit from 10 to 11 and lowers raw quantity, while -1 lowers net profit to 9
and increases quantity.

Recovery adds confirmed debt and its explicit buffer to the unchanged zone
budget. A risk rejection does not cap or floor the recovery order: no order is
submitted, recoverable amount is zero, and confirmed debt remains unchanged.

Actual stop debt is calculated from confirmed fill prices and quantities,
allocated entry fees, stop fees, and signed funding. Slippage versus the stop
reference is stored separately for audit without double-counting it, because the
actual fill price already includes that price effect.
