# M5 Authenticated Private Adapter and Read-Only Reconciliation

## Environment isolation

Private access is sealed to Bybit demo:

```text
REST       https://api-demo.bybit.com
private WS wss://stream-demo.bybit.com/v5/private
```

Only `BYBIT_API_KEY_DEMO` and `BYBIT_API_SECRET_DEMO` are loaded. URL overrides
and mainnet-named credentials are ignored. Credential, signature, prepared
request, and environment-profile representations redact secret values.

The local `.env` file is loaded without overriding already exported variables.
It remains ignored by Git.

## Authentication and clock safety

GET requests sign the exact transmitted query string. POST requests sign the
exact compact JSON body bytes that are sent. The adapter uses HMAC-SHA256 and
the required `X-BAPI-*` headers without logging complete headers.

Before any private request, an unsigned demo `/v5/market/time` observation is
measured around the local request midpoint. Excessive clock offset, network
uncertainty, or sample age blocks private access.

## Read-only reconciliation boundary

The startup reader captures:

- linear and option open orders;
- recent order history;
- linear executions;
- `ETHUSDT` and option positions;
- Unified wallet state.

Every open order must have a known persisted client ID. Every nonzero exchange
position must match an expected category, symbol, side, and exact quantity.
Unknown, missing, or mismatched state blocks trading.

The private WebSocket authenticates before subscribing once to `order`,
`execution`, and `position`. It preserves batched executions, fences connection
generations, emits authenticated/disconnected connection events, and remains
blocked on initial connection and reconnect until the REST snapshot is
reconciled for that generation.

## Mutation semantics prepared for later demo gates

Place, amend, cancel, and symbol/category-scoped cancel-all requests are
implemented but are not invoked by the M5 reader. Acknowledgements never change
filled quantity. Any transport loss during a mutation is an uncertain outcome;
the exact client ID is queried through realtime orders and durable history
before a later service may retry.

Client IDs are parseable, use only Bybit-safe characters, and never exceed 36
characters. Persistence-before-submission and execution deduplication are M6/M8
application gates and remain disabled here.

## Opt-in demo read-only check

After placing rotated demo credentials in `.env`:

```powershell
$env:RUN_BYBIT_DEMO_READ_ONLY="1"
python -m pytest -q tests/test_live_bybit_demo.py
```

This command synchronizes time, reads private state, and authenticates the demo
private WebSocket. It does not submit, amend, cancel, or flatten orders.

## Official references

- https://bybit-exchange.github.io/docs/v5/demo
- https://bybit-exchange.github.io/docs/v5/guide
- https://bybit-exchange.github.io/docs/v5/market/time
- https://bybit-exchange.github.io/docs/v5/order/open-order
- https://bybit-exchange.github.io/docs/v5/order/order-list
- https://bybit-exchange.github.io/docs/v5/order/execution
- https://bybit-exchange.github.io/docs/v5/position
- https://bybit-exchange.github.io/docs/v5/account/wallet-balance
- https://bybit-exchange.github.io/docs/v5/ws/connect
- https://bybit-exchange.github.io/docs/v5/websocket/private/order
- https://bybit-exchange.github.io/docs/v5/websocket/private/execution
- https://bybit-exchange.github.io/docs/v5/websocket/private/position
