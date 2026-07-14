# M2 Option Position and Valuation Notes

## Fill-derived accounting

`OptionLegPosition.from_fills` is the canonical constructor for confirmed
exchange executions. It rejects duplicate execution IDs, wrong symbols,
wrong sides, and aggregate quantities above the requested quantity. Average
entry price is quantity-weighted and fees are summed from confirmed fills.

The position exposes separate cash components:

```text
short proceeds - long cost - entry fees = actual net credit
```

`OPEN` requires equal positive confirmed quantities, protective long coverage,
and positive actual net credit. A mismatched protected position remains
`PARTIALLY_OPEN`; only `matched_quantity` is eligible for hedge sizing.

## Separate valuation views

- `expiration_pnl` projects intrinsic value at a supplied terminal ETH price.
- `mark_pnl` values both legs at their mark prices.
- `liquidation_pnl` buys the short leg at ask and sells the long leg at bid.

Mark and liquidation valuation enforce quote age, timestamp skew, index-price
tolerance, exact symbols, `Trading` instrument status, and unexpired contracts.
Liquidation additionally requires the executable short ask and long bid.

## Entry and expiry policies

`OptionEntryPolicy` defines the leg timeout, whether matched partial completion
is allowed, minimum matched quantity, maximum credit deviation, and whether an
unmatched protective long is closed or retained. The protective-long-first
position states continue to reject naked short exposure.

`OptionLifecyclePolicy` defines the last new hedge time, last option adjustment
time, forced-close time, and expiry. It emits the approaching, trading-cutoff,
delivery, and settled lifecycle events. New hedge and recovery entries are
blocked at the new-hedge cutoff.

## Persistence and dashboard boundary

`OptionPositionSnapshot` contains the symbols, matched quantity, average entry
prices, actual credit, fees, fill-derived opened time, expiry, and state.

The dashboard renderer remains calculation-free. The backend payload builder
validates and precomputes actual fills, credit, mark P&L, executable liquidation
P&L, expiration projection, time to expiry, quote age, IV, and Greeks. When no
actual position is supplied, the dashboard explicitly says so instead of
presenting theoretical quote values as fills.

## Acceptance evidence

The M2 gate passes:

```text
python -m compileall -q src tests
ruff check .
mypy
python -m pytest -q
python run_scenarios.py
```

The deterministic terminal-payoff tests were not changed.
