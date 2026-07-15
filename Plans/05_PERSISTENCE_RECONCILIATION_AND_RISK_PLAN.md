# Plan 5 — Persistence, Reconciliation, Risk and Multi-Level Recovery

## Objective

Make the bot restart-safe, detect mismatched state and allow independent risk controls to veto strategy actions. Only then enable multiple demo levels and recovery sizing.

## Step 1 — Add an event journal

Persist:

```text
StrategyCycleCreated
OptionEntryIntentCreated
OptionExecutionReceived
OptionSpreadOpened
VirtualLevelArmed
HedgeEntryIntentCreated
OrderAcknowledged
ExecutionReceived
ProtectionIntentCreated
ProtectionConfirmed
TakeProfitReceived
StopReceived
RecoveryDebtChanged
LevelPaid
LevelLocked
TradingSuspended
ReconciliationCompleted
```

Columns:

```text
sequence
event ID
event type
event version
cycle ID
level ID
timestamp
payload JSON
causation ID
correlation ID
```

Append event and update derived state in one database transaction.

## Step 2 — Add snapshots

Snapshot:

```text
cycle ID
last event sequence
option position
levels
orders
positions
recovery debt
stop budget
trading permission
```

Restart sequence:

1. Load latest snapshot.
2. Replay later events.
3. Query exchange.
4. Reconcile.
5. Resume only if reconciliation succeeds.

## Step 3 — Build reconciliation

```python
@dataclass(frozen=True)
class ReconciliationReport:
    status: Literal["MATCHED", "REPAIRABLE", "AMBIGUOUS", "DANGEROUS"]
    differences: tuple[StateDifference, ...]
    repair_actions: tuple[RepairAction, ...]
    trading_allowed: bool
```

Inputs:

```text
local orders
local option positions
local perp position
exchange open orders
exchange recent orders
exchange executions
exchange positions
```

## Step 4 — Define reconciliation rules

### Unknown exchange order

Import only when client ID belongs to this strategy and role is unambiguous. Otherwise suspend.

### Missing exchange order

Query history. Determine filled, cancelled, rejected or unknown. Do not resend blindly.

### Position mismatch

Derive expected quantity from executions and compare with exchange. Normalize only documented rounding differences; suspend on material mismatch.

### Missing protection

Restore stop protection. If impossible, use the predefined emergency-close policy.

### Unknown option position

Suspend all hedge entries and require explicit resolution.

## Step 5 — Build an independent risk engine

```python
class RiskEngine:
    def evaluate(self, proposal: TradeProposal, state: RiskState) -> RiskDecision: ...
```

```python
@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    approved_quantity: Decimal | None
    reasons: tuple[str, ...]
    requires_operator_ack: bool
```

The risk engine cannot silently reduce recovery quantity while reporting full recovery.

## Step 6 — Add finite mandatory limits

```text
maximum perp quantity
maximum perp notional
maximum margin usage
minimum liquidation distance
maximum recovery debt
maximum projected stop loss
maximum realized cycle loss
maximum daily realized loss
maximum entries per level
maximum active levels
maximum order request rate
maximum stale-data duration
maximum reconciliation failures
```

Unlimited notional is not a valid deployment configuration.

## Step 7 — Add margin and liquidation checks

Before entry:

```text
fetch account state
estimate post-trade exposure
calculate/obtain margin and liquidation distance
reject when thresholds fail
```

Reconcile after fill.

## Step 8 — Separate projected and confirmed debt

```text
projected recovery debt
confirmed recovery debt
allocated recovery debt
remaining recovery debt
```

Live confirmed debt:

```text
actual realized stop loss
+ stop fees
+ allocated entry fees
+ attributable funding
```

Only confirmed debt affects live recovery sizing.

## Step 9 — Enable multiple levels in two stages

### Stage A

Multiple baseline levels, no increased re-entry quantity.

Validate:

```text
concurrent positions
independent stops
independent TPs
shared boundaries
restart with several levels
```

### Stage B

Enable local `FULL_NEXT_TP` recovery:

```text
desired quantity =
(zone budget + confirmed local debt) / TP distance
```

Then:

```text
quantize
recalculate expected TP
recalculate projected stop
run risk engine
```

If rejected, mark recovery impossible.

Keep distributed cross-level recovery disabled.

## Step 10 — Define locked-level action

Choose exactly one:

```text
accept remaining unhedged option risk
close whole option strategy
reduce option quantity
experimental floor hedge
predefined emergency hedge
```

Recommended initial deployment behavior:

```text
When a required normal hedge is rejected:
suspend new entries and start controlled option-strategy reduction/close.
```

## Step 11 — Add operator commands

```text
pause new entries
resume after reconciliation
cancel pending entry
restore protection
close hedge position
close option spread
flatten strategy
acknowledge incident
```

Commands are authenticated, logged and idempotent.

## Step 12 — Add failure policies

| Failure | Automatic response |
|---|---|
| Trigger data stale | Block entries |
| Private stream disconnected | Block entries; leave exchange stops active |
| Stop missing | Restore or emergency close |
| Unknown position | Suspend and alert |
| Risk limit exceeded | Reject proposal |
| Database unavailable | Block new orders |
| REST uncertain | Query before retry |
| Option quote stale | Block option decisions |
| Reconciliation repeatedly fails | Global suspend |

## Step 13 — Tests

```text
restart with open entry
restart with partial fill
restart with active TP and stop
execution persisted before crash
request sent but acknowledgement missing
unknown exchange order
unknown exchange position
missing stop repaired
quantity mismatch blocks trading
risk veto
recovery rejected
multi-level exits
daily loss lock
database transaction rollback
```

## Acceptance gate

- [x] Restart reconstructs state.
- [x] Executions store idempotently.
- [x] Reconciliation precedes resumed trading.
- [x] Unknown state suspends.
- [x] Risk engine is independent.
- [x] Finite limits exist.
- [ ] Multiple baseline levels pass demo.
- [x] Recovery uses confirmed actual debt.
- [x] Rejected recovery is explicit.
- [x] Locked-level action is tested.
