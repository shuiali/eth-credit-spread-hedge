# Plan 7 — Shadow Mode, Demo Burn-In and Controlled Deployment

## Objective

Turn tested code into an operational service with environment isolation, health checks, monitoring, kill switches and gradual deployment.

## Step 1 — Define environments

```text
LOCAL_EXACT
LOCAL_SIMULATED
DEMO
SHADOW_MAINNET
PRODUCTION_PILOT
PRODUCTION
```

Use different credentials, databases and configuration files.

Refuse startup when:

```text
environment and URL conflict
required limits are missing
migrations are pending
kill switch unavailable
clock drift too high
reconciliation incomplete
```

## Step 2 — Build shadow mode

Consume live data and create intents without orders.

Record:

```text
virtual crossing
desired quantity
quantized quantity
risk decision
hypothetical entry
hypothetical TP/stop
expected P&L
```

Replay captured data offline and confirm identical decisions.

## Step 3 — Add health endpoints

```text
/health/live
/health/ready
/status/strategy
/status/exchange
/status/risk
```

Readiness false when:

```text
data stale
private stream disconnected
database unavailable
reconciliation incomplete
protection missing
risk lock active
```

## Step 4 — Add structured logs

Fields:

```text
timestamp
service
cycle ID
level ID
client order ID
exchange order ID
execution ID
correlation ID
event
message
```

Do not log secrets.

## Step 5 — Add metrics

```text
market data age
public/private connection state
reconciliation state
open cycles
active levels
open hedge quantity
unprotected quantity
recovery debt
remaining stop budget
daily P&L
order rejections
duplicate executions
restart reconciliations
```

## Step 6 — Add alerts

Immediate:

```text
unprotected position
unknown position
risk violation
database failure
authentication failure
dangerous reconciliation
kill switch
```

Warning:

```text
stale data
stale option quote
order pending too long
large slippage
debt near limit
expiry approaching
```

## Step 7 — Add kill switches

### Soft pause

```text
block new entries
keep protection
continue reconciliation
```

### Strategy close

```text
cancel pending entries
close hedges
close/reduce option spread
verify state
```

### Emergency flatten

```text
persist intent
cancel relevant orders
submit reduce-only closes
verify positions
repeat reconciliation
alert
```

## Step 8 — Demo burn-in stages

### D1 Public data

Require stable reconnect and complete capture.

### D2 Read-only private state

Parse and reconcile positions, orders and wallet.

### D3 Manual one-level hedge

Entry, protection, exit and restart.

### D4 Automatic one-level hedge

Virtual crossing through complete lifecycle.

### D5 Multiple baseline levels

No recovery escalation.

### D6 Full-next-TP recovery

Actual realized debt plus hard limits.

Write a review after every stage.

## Step 9 — Mainnet shadow acceptance

Require:

```text
all intents reproduce offline
no contradictory transitions
no stale data would trigger an order
quantization always valid
risk decisions deterministic
expiry cutoff works
```

## Step 10 — Production pilot conditions

Proceed only when:

```text
account and user are legally eligible and exchange-approved
all test/demo/shadow gates pass
finite limits exist
operator monitoring exists
smallest practical quantity is used
automatic scaling is disabled
```

Pilot:

```text
one option spread
one virtual level
no distributed recovery
strict notional and daily loss limits
manual intervention available
```

## Step 11 — Gradual rollout

Order:

```text
one level
multiple baseline levels
same-level full-next-TP recovery
carefully increased capped quantity
additional cycles
```

Never increase complexity and notional in the same release.

## Step 12 — Release process

Every release includes:

```text
versioned migration
changelog
configuration diff
risk review
rollback procedure
demo smoke test
shadow replay
operator approval
```

## Step 13 — Incident playbooks

Create:

```text
private stream outage
public data outage
missing stop
partial option spread
unknown position
database outage
authentication failure
large stop slippage
expiry anomaly
liquidation-distance deterioration
```

Each includes detection, automatic action, manual action, verification and review.

## Final acceptance gate

- [x] Shadow mode is consistent.
- [ ] One-level demo lifecycle repeatedly passes.
- [x] Restart/reconciliation passes.
- [ ] Multiple levels pass demo.
- [ ] Recovery passes with actual fills.
- [x] Kill switches pass.
- [x] Alerts pass.
- [x] Production limits are finite.
- [x] Expiry handling passes.
- [x] Active state is explainable.
- [ ] Small pilot approval is documented.
