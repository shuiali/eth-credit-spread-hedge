# M3 Instrument and Public Market-Data Notes

## Normalized instrument constraints

M3 adds exchange-neutral `PriceFilter`, `LotSizeFilter`, and `InstrumentSpec`
models for Bybit option and linear instruments. The public REST adapter follows
every instrument pagination cursor and retains optional constraints as `None`
when Bybit does not publish them. Both ETH option quantity and `ETHUSDT`
perpetual quantity are normalized in ETH units with a multiplier of one.

M3 exposes one Decimal-exact order-normalization boundary for quantity,
side-aware price quantization, and filter validation. The private adapter added
in M4 must call this boundary for every submission. Minimum notional, TP profit,
projected stop loss, recovery quantity, notional, and the risk result are
recalculated from normalized values rather than requested values.

## Authoritative trigger and public streams

The declared deployment trigger is the last trade from the `ETHUSDT` perpetual:

```text
symbol = ETHUSDT
source = LAST_TRADE
```

The runtime configuration rejects mark- or index-based deployment triggers so
one running strategy cannot mix price sources. Normalized ticker, trade, and
order-book events contain their observed time, source data, sequence, raw
payload hash, and connection generation; raw Bybit JSON does not enter the
domain engine.

## Book synchronization and connection safety

Snapshots replace the complete local book. Sequential deltas insert or update
levels, and a zero-size delta removes a level. A new snapshot resets state.
Sequence faults and new connection generations require another snapshot, while
late events from older generations are ignored.

The WebSocket client sends heartbeat ping messages, enforces pong timeouts,
uses bounded exponential reconnect delays, restores subscriptions after a
disconnect, and waits for a new order-book snapshot before accepting deltas.

## Health gate and historical capture

The market-data health gate blocks trading unless the trigger, instruments,
option quotes, public connection, synchronized book (when required), and clock
are all healthy and fresh. The JSON Lines recorder stores normalized events
with timestamp, symbol, event type, sequence, update ID, price, size, generation,
and a SHA-256 hash of the source payload. Book snapshots and deltas emit one row
per side and level, including zero-size deletions, so the captured book can be
reconstructed. A bounded capture fails if any configured stream emits no data.

Use the bounded capture launcher without credentials:

```powershell
python capture_market_data.py `
  --output artifacts/public_market_data.jsonl `
  --seconds 60 `
  --option-symbol ETH-31JUL26-1750-P-USDT
```

## Bybit boundary behavior

Bybit may publish a top-of-book price and size as zero when that side is absent.
The adapter normalizes a zero-size level to `None`; it rejects inconsistent zero
price with nonzero size. This keeps an unavailable ask from appearing as a
crossed executable market while preserving the domain's crossed-quote guard.

The implementation follows the official Bybit documentation:

- https://bybit-exchange.github.io/docs/v5/market/instrument
- https://bybit-exchange.github.io/docs/v5/market/orderbook
- https://bybit-exchange.github.io/docs/v5/websocket/public/orderbook
- https://bybit-exchange.github.io/docs/v5/websocket/public/ticker
- https://bybit-exchange.github.io/docs/v5/websocket/public/trade
- https://bybit-exchange.github.io/docs/v5/ws/connect

## Acceptance evidence

The compile, Ruff, strict mypy, pytest, deterministic-scenario, forced-reconnect,
and JSONL fixture tests pass. Opt-in public smoke tests also passed for the live
ETH option chain, `ETHUSDT` instrument metadata, REST order book, and WebSocket
trade stream. A bounded live capture produced perpetual ticker/trade/book and
selected-option ticker records. No private endpoint or credential was used.
