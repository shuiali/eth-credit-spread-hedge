# Plan 2 — Complete Option Position and Mark-to-Market Model

## Objective

Preserve expiration payoff analysis while adding actual option fills, live mark P&L and executable liquidation P&L. The live stop budget must come from confirmed net credit.

## Step 1 — Separate contract, quote, fill and position

```python
@dataclass(frozen=True)
class OptionContract:
    symbol: str
    base_coin: str
    quote_coin: str
    settle_coin: str
    option_type: Literal["Put", "Call"]
    strike: Decimal
    expiry_time_utc: datetime
    contract_multiplier: Decimal

@dataclass(frozen=True)
class OptionMarketQuote:
    symbol: str
    timestamp_utc: datetime
    bid_price: Decimal | None
    bid_size: Decimal | None
    ask_price: Decimal | None
    ask_size: Decimal | None
    mark_price: Decimal
    underlying_price: Decimal
    index_price: Decimal
    bid_iv: Decimal | None
    ask_iv: Decimal | None
    mark_iv: Decimal | None
    delta: Decimal | None
    gamma: Decimal | None
    vega: Decimal | None
    theta: Decimal | None

@dataclass(frozen=True)
class OptionFill:
    order_id: str
    execution_id: str
    symbol: str
    side: Literal["Buy", "Sell"]
    price: Decimal
    quantity: Decimal
    fee: Decimal
    timestamp_utc: datetime
```

## Step 2 — Build an actual option-spread position

```python
@dataclass
class OptionLegPosition:
    contract: OptionContract
    side: Literal["Long", "Short"]
    requested_quantity: Decimal
    filled_quantity: Decimal
    average_entry_price: Decimal
    fees_paid: Decimal

@dataclass
class PutCreditSpreadPosition:
    short_put: OptionLegPosition
    long_put: OptionLegPosition
    state: OptionPositionState
```

States:

```text
PLANNED
LONG_PROTECTION_PENDING
LONG_PROTECTION_FILLED
SHORT_PREMIUM_PENDING
OPEN
PARTIALLY_OPEN
CLOSING
CLOSED
ERROR
```

## Step 3 — Implement three distinct P&L views

### Expiration projection

Keep:

```python
expiration_pnl(underlying_price)
```

### Mark-to-market

```python
mark_pnl(short_quote, long_quote)
```

### Executable liquidation

Use conservative close sides:

```text
Buy short put back at ask.
Sell long put at bid.
```

```python
liquidation_pnl(short_quote, long_quote)
```

Dashboard must not combine these into one number.

## Step 4 — Calculate actual credit from fills

```text
actual short proceeds
- actual long cost
- option entry fees
= actual net credit
```

The option spread becomes `OPEN` only when:

```text
protective long is confirmed
short and long matched quantity is known
actual net credit is positive
```

## Step 5 — Define option entry sequencing

Initial policy:

1. Buy protective long put.
2. Wait for confirmed fill.
3. Sell short put.
4. On timeout, cancel remaining short quantity.
5. Close or retain the unmatched long according to a predefined policy.
6. Never represent an incomplete pair as an open credit spread.

Configuration:

```python
@dataclass(frozen=True)
class OptionEntryPolicy:
    max_leg_wait_seconds: Decimal
    allow_partial_spread: bool
    minimum_matched_quantity: Decimal
    maximum_credit_deviation: Decimal
```

## Step 6 — Handle partial fills

For each leg track:

```text
requested quantity
confirmed filled quantity
remaining quantity
average fill price
fees
```

Matched spread quantity:

```text
min(long filled quantity, short filled quantity)
```

Only matched quantity may create hedge levels.

## Step 7 — Add quote validation

Reject valuation when:

```text
quote is stale
leg timestamps differ beyond tolerance
index values differ beyond tolerance
bid or ask required for liquidation is missing
instrument is not Trading
expiry passed
```

## Step 8 — Define expiry lifecycle

Events:

```text
OPTION_EXPIRY_APPROACHING
OPTION_TRADING_CUTOFF
OPTION_DELIVERY
OPTION_SETTLED
```

Configure:

```text
last new hedge time
last option adjustment time
forced close time
```

No new recovery entry after cutoff unless a separately tested rule allows it.

## Step 9 — Persist option-position snapshots

Persist:

```text
symbols
matched quantity
average entries
actual net credit
fees
opened time
expiry time
state
```

## Step 10 — Tests

Required:

```text
actual credit from multiple fills
different leg fill quantities
partial long fill
partial short fill
fees reduce credit
mark P&L
liquidation P&L uses bid/ask correctly
expiration P&L unchanged
stale quote rejection
expiry mismatch rejection
matched quantity calculation
negative actual credit rejection
```

## Step 11 — Dashboard additions

Show:

```text
actual option fills
actual net credit
mark P&L
liquidation P&L
expiration projection
time to expiry
short/long IV and Greeks
quote freshness
matched quantity
```

## Acceptance gate

- [x] Fill-based credit exists.
- [x] Terminal, mark and liquidation P&L are separate.
- [x] Protective long is confirmed before spread is OPEN.
- [x] Partial fills cannot create silent naked exposure.
- [x] Quote freshness is enforced.
- [x] Expiry lifecycle is defined.
- [x] Tests cover multiple and partial fills.
- [x] Baseline terminal tests remain unchanged.
