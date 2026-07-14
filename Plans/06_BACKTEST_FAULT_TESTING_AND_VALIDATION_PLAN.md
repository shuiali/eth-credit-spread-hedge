# Plan 6 — Realistic Simulation, Historical Validation and Fault Injection

## Objective

Keep exact-fill mode as a mathematical oracle, but add a simulated exchange that reproduces live order-state behavior and operational failures.

## Step 1 — Maintain two explicit modes

### Exact reference

```text
cross -> exact fill
```

### Simulated exchange

```text
request
acknowledgement
visibility delay
partial fill
fees
spread
slippage
stop behavior
rejections
disconnects
duplicates
reordering
```

Never combine results without labeling the mode.

## Step 2 — Implement a simulated exchange adapter

```python
class SimulatedExchange(TradingPort, AccountPort, MarketDataPort):
    ...
```

Application code must run unchanged against simulated and Bybit adapters.

## Step 3 — Add execution configuration

```python
@dataclass(frozen=True)
class ExecutionModelConfig:
    acknowledgement_delay_ms: int
    visibility_delay_ms: int
    fill_delay_ms: int
    partial_fill_probability: Decimal
    rejection_probability: Decimal
    maker_fee_rate: Decimal
    taker_fee_rate: Decimal
    stop_slippage_bps: Decimal
    entry_slippage_bps: Decimal
```

All randomness is seeded.

## Step 4 — Define gap execution

Downward gap through entry:

```text
trigger detected
entry fills at first available simulated price
```

Upward gap through stop:

```text
stop fills at first available price, not theoretical stop
```

Record:

```text
trigger price
first available price
fill price
gap slippage
```

## Step 5 — Add option execution costs

Entry:

```text
long bought at ask
short sold at bid
option fees
partial fills
```

Liquidation:

```text
short bought at ask
long sold at bid
```

## Step 6 — Add perpetual costs

Track separately:

```text
entry fee
TP fee
stop fee
funding
slippage
spread cost
```

Net hedge P&L:

```text
gross trading P&L - fees - funding - slippage
```

Recovery debt uses net realized stop loss.

## Step 7 — Ingest historical data

Minimum:

```text
ETH perpetual trades/high-frequency bars
mark/index
selected option bid/ask/mark/IV
funding rates
instrument metadata changes
```

Record live public data when historical option data is not available.

## Step 8 — Scenario families

### Price behavior

```text
smooth decline
fast decline
slow recovery
repeated entry oscillation
near-TP reversal
V reversal
multi-level decline
```

### Gaps

```text
gap below entries
gap above stops
outage discontinuity
```

### Volatility

```text
spot unchanged and IV rises
spot falls and IV falls
skew steepens
near-expiry decay
```

### Operations

```text
disconnect at trigger
timeout after submission
duplicate execution
rejection
partial fill then disconnect
database failure
restart
stale order book
```

## Step 9 — Improve stochastic paths

Add separate models:

```text
jump diffusion
volatility clustering
regime switching
historical bootstrap
V-shaped stress paths
```

Report each model separately.

## Step 10 — Required metrics

### Strategy

```text
terminal net P&L
minimum combined mark P&L
minimum executable P&L
floor breach rate
expected shortfall
maximum recovery debt
maximum quantity
locked-level rate
option-close rate
```

### Execution

```text
fill rate
partial-fill rate
entry slippage
stop slippage
time unprotected
rejection rate
reconciliation incidents
duplicate event count
```

### Operations

```text
restart success
stale duration
time to restore protection
manual interventions
```

## Step 11 — Predeclare acceptance thresholds

Examples:

```text
No unprotected position beyond allowed deadline.
No execution counted twice.
No unknown state permits new orders.
All restart scenarios reconcile.
Risk limits are never bypassed.
All P&L is reproducible from events.
```

Choose thresholds before final results are observed.

## Step 12 — Compare each improvement with baseline

Compare:

```text
baseline exact
baseline simulated
candidate simulated
```

Metrics:

```text
P&L
drawdown
stop count
maximum size
debt
execution reliability
```

Higher average P&L does not justify materially worse tail risk.

## Acceptance gate

- [ ] Simulated exchange implements live ports.
- [ ] Exact and realistic modes are separated.
- [ ] Gap behavior is explicit.
- [ ] Fees, funding, spread and slippage are accounted.
- [ ] Duplicates and reordered events are tested.
- [ ] Historical replay uses normalized captures.
- [ ] Jump and oscillation stress models exist.
- [ ] Runs reproduce by seed and event log.
- [ ] Predeclared thresholds pass.
