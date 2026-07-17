# Combined Ledger Contract V1

## Scope

This contract defines Milestone 2.1 accounting facts and snapshots. It does not
define lot allocation, valuation, reconstruction, persistence, or runtime use.
Only a confirmed execution may later change a filled quantity.

## Exact values and signs

All accounting values are exact `Decimal`-backed values. Prices and quantities
are positive. A fee is a nonnegative, positive-cost field. No binary floating
point is accepted for accounting contracts.

- cash received is positive and cash paid is negative;
- realized profit is positive and realized loss is negative;
- funding income is positive and funding cost is negative;
- option and hedge fees are positive cost fields, subtracted once;
- actual-fill slippage is attribution only when actual fills determine P&L.

Long option buys and short-perpetual buybacks are cash paid. Short option and
short-perpetual sales are cash received. Mark and executable-liquidation values
are distinct snapshot fields. A later lot policy will be FIFO. A later recovery
debt implementation must use the actual stopped-attempt net result:

```text
gross realized price P&L - allocated entry fees - exit fees + allocated funding
```

The debt increment is `max(-attempt_net_result, 0)`.

## Immutable facts

Each event carries an ID, version, cycle, UTC timestamp, source, correlation,
and applicable level, execution, order, order-link, and symbol identifiers.
Duplicate identifiers with different canonical content are conflicts. Fee
ownership is explicitly `OPTION` or `HEDGE`.

`ConfirmedExecution` is the sole fill fact. It rejects nonpositive price or
quantity, negative fees, unsupported fee currencies, and non-UTC timestamps.

## Serialization

Canonical JSON uses stable sorted keys, UTC ISO-8601 timestamps, enum strings,
and Decimal strings. SHA-256 digests are calculated from that canonical JSON.

## Future snapshot identity

Future reconstruction must expose separate gross P&L and costs and satisfy:

```text
net_combined_mark_pnl =
    option_realized_pnl + option_open_mark_pnl
    + hedge_realized_pnl + hedge_open_mark_pnl
    + funding_pnl - option_fees - hedge_fees
```

```text
net_combined_liquidation_pnl =
    option_realized_pnl + option_open_liquidation_pnl
    + hedge_realized_pnl + hedge_open_liquidation_pnl
    + funding_pnl - option_fees - hedge_fees
```
