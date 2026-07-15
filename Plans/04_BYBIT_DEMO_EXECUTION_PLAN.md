# Plan 4 — Authenticated Bybit Adapter and Single-Level Demo Execution

## Objective

Execute exactly one virtual hedge level on Bybit demo. Begin with the narrowest scope: a confirmed option position, one perpetual entry, one TP and one exchange-hosted stop.

Do not enable multiple levels or recovery escalation during the first demo milestone.

## Step 1 — Build authentication

```python
@dataclass(frozen=True)
class ApiCredentials:
    api_key: SecretStr
    api_secret: SecretStr
```

Implement signing in one module. Include:

```text
timestamp
API key
receive window
query/body
HMAC signature
```

Add server-time synchronization. Reject private requests when clock drift exceeds the configured maximum.

Never log secrets or complete signed headers.

## Step 2 — Create private ports

```python
class TradingPort(Protocol):
    async def place_order(self, request: PlaceOrderRequest) -> OrderRequestAck: ...
    async def amend_order(self, request: AmendOrderRequest) -> OrderRequestAck: ...
    async def cancel_order(self, request: CancelOrderRequest) -> OrderRequestAck: ...
    async def cancel_all(self, category: str, symbol: str | None = None) -> None: ...
    async def get_open_orders(self, category: str, symbol: str | None = None) -> tuple[ExchangeOrder, ...]: ...
    async def get_order_history(self, category: str, symbol: str | None = None) -> tuple[ExchangeOrder, ...]: ...

class AccountPort(Protocol):
    async def get_positions(self, category: str, symbol: str | None = None) -> tuple[ExchangePosition, ...]: ...
    async def get_wallet_state(self) -> WalletState: ...
```

## Step 3 — Define idempotent client order IDs

Format:

```text
strategy instance / cycle / level / role / attempt / nonce
```

Example:

```text
ECH-01-C0007-L01-ENTRY-A02-9F3C
```

Roles:

```text
OPTION_LONG
OPTION_SHORT
HEDGE_ENTRY
HEDGE_TP
HEDGE_STOP
EMERGENCY_CLOSE
```

Persist the order-link ID before submitting the request.

If request outcome is uncertain, query by client ID before resending.

## Step 4 — Normalize private events

```python
@dataclass(frozen=True)
class OrderUpdate:
    order_id: str
    order_link_id: str
    symbol: str
    status: str
    side: str
    order_type: str
    price: Decimal | None
    quantity: Decimal
    cumulative_filled_quantity: Decimal
    average_price: Decimal | None
    updated_at: datetime

@dataclass(frozen=True)
class ExecutionUpdate:
    execution_id: str
    order_id: str
    order_link_id: str
    symbol: str
    side: str
    price: Decimal
    quantity: Decimal
    fee: Decimal
    is_maker: bool | None
    executed_at: datetime
```

Only executions change actual filled quantity.

## Step 5 — Add execution deduplication

Store:

```text
execution ID primary key
order ID
received time
payload hash
```

Processing:

```python
if execution_id already exists:
    ignore
else:
    apply accounting and persist atomically
```

Support multiple executions per order and multiple executions in one message.

## Step 6 — Create a live execution state machine

The deterministic level state remains a strategy state. Add a separate exchange state:

```text
READY
TRIGGERED
ENTRY_REQUEST_PERSISTED
ENTRY_SUBMITTED
ENTRY_ACKNOWLEDGED
ENTRY_PARTIALLY_FILLED
ACTIVE_UNPROTECTED
ACTIVE_PROTECTED
EXIT_PARTIALLY_FILLED
CLOSED_TP
CLOSED_STOP
CANCEL_PENDING
RECONCILING
LOCKED
ERROR
```

The domain engine proposes an action. The execution state machine proves whether the exchange completed it.

## Step 7 — Demo milestone A: entry only

1. Confirm option spread is `OPEN`.
2. Load one virtual level.
3. Receive fresh trigger-price event.
4. Detect downward crossing.
5. Calculate baseline quantity without recovery.
6. Quantize quantity.
7. Run risk check.
8. Persist entry intent.
9. Submit entry.
10. Receive acknowledgement.
11. Receive executions.
12. Aggregate actual quantity and average price.
13. Compare local quantity with exchange position.
14. End the test and flatten through a separately tested demo command.

Acceptance:

```text
local quantity equals exchange position
duplicate execution does not double quantity
restart discovers the open position
```

## Step 8 — Demo milestone B: exchange-hosted stop

After confirmed entry:

1. Calculate stop from actual average entry:
   ```text
   actual entry × (1 + stop rate)
   ```
2. Quantize trigger price.
3. Create reduce-only protection.
4. Persist protection intent before request.
5. Confirm stop order exists.
6. Mark level `ACTIVE_PROTECTED`.

If protection cannot be confirmed before deadline:

```text
block all new entries
reconcile
restore protection or emergency close according to policy
```

## Step 9 — Demo milestone C: take profit

After protection is confirmed:

1. Quantize TP.
2. Create reduce-only TP for confirmed open quantity.
3. Confirm TP exists.
4. On execution, reduce local quantity by actual fill.
5. Reconcile position.
6. Cancel/reconcile sibling exit.
7. Mark closed only when exchange position is zero.

For stop execution:

```text
cancel or reconcile TP
calculate actual stop P&L including fees
create confirmed recovery debt
```

## Step 10 — Abstract protective exit management

Test two exchange implementations behind one port:

```python
class ProtectiveExitManager(Protocol):
    async def protect_position(...) -> ProtectionResult: ...
    async def reconcile_protection(...) -> ProtectionState: ...
    async def remove_protection(...) -> None: ...
```

Potential implementations:

```text
explicit conditional reduce-only stop
position trading-stop endpoint
```

Select one after demo testing.

## Step 11 — Treat acknowledgements as asynchronous

Rules:

```text
request acknowledgement != order open
request acknowledgement != fill
cancel acknowledgement != cancelled
```

Private order and execution streams confirm results.

Add deadlines:

```text
ack deadline
order-visible deadline
entry-fill deadline
protection-confirmation deadline
cancel-confirmation deadline
```

## Step 12 — Handle cancel/fill races

Possible event order:

```text
cancel requested
order fills
cancel acknowledgement
duplicate Filled update
```

Rules:

- Executions are authoritative for quantity.
- Late cancelled updates cannot reopen or reduce known fills.
- Reconcile position before replacing exits.
- Processing is idempotent.

## Step 13 — Fake-adapter tests

Replay:

```text
full fill
two partial fills
duplicate execution
out-of-order updates
cancel then fill
stop fill while TP cancellation pending
disconnect after REST acknowledgement
REST timeout but order exists
rejection
insufficient margin
quantity below minimum
```

Use the same application service with:

```text
FakeBybitAdapter
BybitDemoAdapter
```

## Acceptance gate

- [ ] One virtual level triggers on demo.
- [x] Client order IDs are persisted before submission.
- [x] Executions determine actual quantity.
- [x] Position receives exchange-hosted protection.
- [x] TP and stop are reduce-only.
- [x] Duplicate executions are idempotent.
- [x] Partial fills aggregate correctly.
- [x] Cancel/fill races reconcile.
- [x] Local and exchange positions agree.
- [x] Restart discovers and protects an active position.
- [x] Multiple levels and recovery remain disabled.
