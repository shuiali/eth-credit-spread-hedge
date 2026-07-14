# M1 Domain Migration Notes

## Exchange-neutral option models

M1 adds these models under `eth_credit_hedge.domain`:

- `OptionContract`
- `OptionMarketQuote`
- `OptionFill`
- `OptionLegPosition`
- `OptionPositionState`
- `PutCreditSpreadPosition`

The models contain no Bybit imports or raw exchange payloads. Timestamps must
be timezone-aware and are normalized to UTC. Prices, money, and quantities are
stored as finite `Decimal` values.

## Public Bybit boundary

The fixture and public client now return `OptionChainEntry`, which contains a
normalized `contract`, normalized `quote`, and exchange status. Existing
consumers should migrate field access as follows:

```text
entry.instrument.strike -> entry.contract.strike
entry.instrument.expiry -> entry.contract.expiry_time_utc
entry.bid                -> entry.quote.bid_price
entry.ask                -> entry.quote.ask_price
entry.mark               -> entry.quote.mark_price
```

Quote timestamps come from the Bybit response `time` field. The current option
instrument response does not expose a contract-multiplier field, while Bybit
documents ETH option order quantity in ETH units. The adapter therefore
normalizes one quantity unit with `contract_multiplier = Decimal("1")`.

References:

- https://bybit-exchange.github.io/docs/v5/market/instrument
- https://www.bybit.com/en/help-center/article/FAQ-Options-Trading

## Position safety boundary

The position model rejects short filled quantity above confirmed protective
long quantity. `OPEN` requires equal, positive confirmed quantities, and
`PARTIALLY_OPEN` counts only `min(long filled, short filled)`.

M1 does not calculate actual fill-based credit or live P&L. The additional
requirement that `OPEN` have positive actual net credit is intentionally part
of M2.
