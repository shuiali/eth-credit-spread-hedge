# Plan 3 — Instrument Metadata, Quantization and Public Market Data

## Objective

Create a normalized public-data layer for ETH options and the ETH perpetual, including instrument constraints, one authoritative trigger source, order-book synchronization and freshness gates.

## Step 1 — Expand instrument metadata

```python
@dataclass(frozen=True)
class PriceFilter:
    tick_size: Decimal
    min_price: Decimal | None
    max_price: Decimal | None

@dataclass(frozen=True)
class LotSizeFilter:
    qty_step: Decimal
    min_order_qty: Decimal
    max_order_qty: Decimal
    max_market_order_qty: Decimal | None
    min_notional: Decimal | None

@dataclass(frozen=True)
class InstrumentSpec:
    symbol: str
    category: Literal["option", "linear"]
    status: str
    base_coin: str
    quote_coin: str
    settle_coin: str
    price_filter: PriceFilter
    lot_size_filter: LotSizeFilter
    contract_multiplier: Decimal
    delivery_time_utc: datetime | None
```

## Step 2 — Add quantization utilities

```python
floor_to_step(value, step)
ceil_to_step(value, step)
nearest_to_step(value, step)
```

Use explicit side-aware policies.

After quantity or price is quantized, recalculate:

```text
TP profit
projected stop loss
recovery amount
notional
risk decision
```

## Step 3 — Add order validation

```python
@dataclass(frozen=True)
class OrderValidationResult:
    accepted: bool
    normalized_price: Decimal | None
    normalized_quantity: Decimal
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
```

Validate:

```text
min/max quantity
quantity step
price tick
min notional
instrument Trading status
```

## Step 4 — Define a market-data port

```python
class MarketDataPort(Protocol):
    async def get_instrument(self, symbol: str) -> InstrumentSpec: ...
    async def get_option_chain(self, base_coin: str) -> tuple[OptionMarketQuote, ...]: ...
    async def get_orderbook_snapshot(self, symbol: str, depth: int) -> OrderBookSnapshot: ...
    async def stream_ticker(self, symbol: str) -> AsyncIterator[TickerEvent]: ...
    async def stream_orderbook(self, symbol: str, depth: int) -> AsyncIterator[OrderBookEvent]: ...
    async def stream_trades(self, symbol: str) -> AsyncIterator[TradeEvent]: ...
```

The domain engine never receives raw JSON.

## Step 5 — Choose one trigger source

```python
class TriggerPriceSource(str, Enum):
    LAST_TRADE = "LAST_TRADE"
    MARK_PRICE = "MARK_PRICE"
    INDEX_PRICE = "INDEX_PRICE"
```

Use one source consistently. For the first demo milestone, use ETH perpetual last trades unless testing shows a better defined alternative.

Every trigger event records:

```text
source
observed price
observed timestamp
connection generation
```

## Step 6 — Build local order-book state

```python
@dataclass
class LocalOrderBook:
    bids: ...
    asks: ...
    update_id: int
    timestamp_utc: datetime
    synchronized: bool
```

Rules:

1. Snapshot replaces the entire book.
2. Deltas update levels.
3. Zero size removes a level.
4. New snapshot resets the book.
5. Sequence or connection fault marks it unsynchronized.
6. Stale book blocks execution policies that require depth.

## Step 7 — Add connection supervision

Implement:

```text
heartbeat
ping/pong
bounded exponential reconnect
subscription restore
snapshot restore
stale-data timer
connection generation ID
```

Ignore late events from old generations.

## Step 8 — Normalize events

```text
TickerUpdated
TradeObserved
OrderBookSnapshotApplied
OrderBookDeltaApplied
MarketDataStale
MarketDataRecovered
InstrumentChanged
InstrumentDisabled
```

## Step 9 — Add data-health gates

Trading requires:

```text
trigger price fresh
instrument loaded
public websocket connected
option quotes fresh
order book synchronized when needed
clock synchronized
```

## Step 10 — Record normalized historical data

Store as JSON Lines or Parquet:

```text
timestamp
symbol
event type
sequence
price
size
raw payload hash
```

Capture ETH perpetual data and selected option ticker data.

## Step 11 — Tests

```text
instrument pagination
filter parsing
price and quantity quantization
minimum notional rejection
snapshot then deltas
new snapshot reset
zero-size deletion
stale-data detection
old connection event rejection
consistent trigger source
missing optional Greeks
```

## Acceptance gate

- [x] Option and perpetual constraints parse.
- [x] All requests pass quantization.
- [x] Risk recalculates after quantization.
- [x] One trigger source is declared.
- [x] WebSocket reconnect restores state.
- [x] New snapshots reset local books.
- [x] Stale data blocks new trading.
- [x] Normalized capture works.
