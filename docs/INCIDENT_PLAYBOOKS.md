# Incident Playbooks

Activate at least `SOFT_PAUSE` before investigation whenever exchange state is
not fully known. Never infer safety from an acknowledgement alone.

## Private stream outage

### Detection

Private connection metric is zero, heartbeat expires, or REST contains events
not observed on the stream.

### Automatic action

Soft-pause entries, retain exchange-hosted protection, query orders, executions,
positions, and wallet by REST, and alert.

### Manual action

Confirm Bybit status and credentials; escalate to strategy close if state remains
unknown beyond the incident limit.

### Verification

Require a new connection generation, REST/stream agreement, and successful
startup reconciliation before reset.

### Review

Record outage duration, missed events, recovery latency, and whether thresholds
or reconnect policy need revision.

## Public data outage

### Detection

Market-data age breaches its finite limit, public heartbeat fails, or the local
book becomes unsynchronized.

### Automatic action

Block entries, discard cross-gap trigger inference, reconnect, fetch a fresh book
snapshot, and keep private reconciliation active.

### Manual action

Check provider status and alternate read-only observations; do not override the
freshness veto.

### Verification

Require fresh LAST_TRADE events in a new generation and a synchronized book.

### Review

Archive the discontinuity and replay it as a gap scenario.

## Missing stop

### Detection

A confirmed short quantity has no matching visible reduce-only exchange stop.

### Automatic action

Block entries, persist a replacement-stop intent, submit and verify it; emergency
flatten if protection cannot be restored within the deadline.

### Manual action

Inspect unknown orders and position mode before approving repair or flatten.

### Verification

Compare exact quantity, trigger, trigger source, reduce-only flag, time-in-force,
and position index by REST.

### Review

Measure time unprotected and identify the initiating order/event race.

## Partial option spread

### Detection

Confirmed long and short option quantities differ or protective-long-first entry
does not complete within its deadline.

### Automatic action

Block hedge activation, cancel remaining option intents, and follow the declared
partial-fill policy without assuming the requested credit.

### Manual action

Choose an approved completion or reduction using current executable bid/ask.

### Verification

Reconcile both legs and recompute actual credit, fees, liquidation P&L, and expiry
cutoff from executions.

### Review

Record leg liquidity, spread, fees, and time exposed to directional option risk.

## Unknown position

### Detection

Exchange positions contain a symbol, side, or quantity absent from the journal.

### Automatic action

Activate soft pause, classify reconciliation as ambiguous/dangerous, and alert;
never import or close silently.

### Manual action

Identify ownership from account history and approve import, strategy close, or
emergency flatten.

### Verification

Require local/exchange position equality and an explainable execution chain.

### Review

Document origin, missing causation, and preventive reconciliation changes.

## Database outage

### Detection

Health probe, transaction, migration, or kill-switch persistence fails.

### Automatic action

Do not send a request whose intent is not durable; block entries and alert while
exchange-hosted protection continues.

### Manual action

Restore storage from a verified backup or repair availability without deleting
the event ledger.

### Verification

Run integrity checks, migrations, replay, and full exchange reconciliation.

### Review

Record lost availability, failed operations, backup age, and recovery point.

## Authentication failure

### Detection

REST or private WebSocket authentication fails or the configured credential
scope is unavailable.

### Automatic action

Refuse startup or soft-pause, redact credential material, and issue an immediate
alert.

### Manual action

Rotate the scoped key, verify permissions/IP restrictions, and never paste values
into logs or review artifacts.

### Verification

Run time synchronization and read-only private reconciliation before any reset.

### Review

Record only key identifiers/rotation time, never secret values.

## Large stop slippage

### Detection

Actual stop fill minus first-available price exceeds the predeclared warning or
risk threshold.

### Automatic action

Record gap, spread, slippage, fee, realized loss, and confirmed recovery debt;
veto recovery if finite limits fail.

### Manual action

Review liquidity/outage context and consider strategy close without increasing
size to chase loss.

### Verification

Recompute net stop loss from executions and reconcile debt allocation exactly.

### Review

Add the path to stress validation and revisit—not post-hoc loosen—thresholds.

## Expiry anomaly

### Detection

Instrument delivery metadata changes, expiry cutoff is violated, or option state
does not transition as scheduled.

### Automatic action

Block new entries, refresh instrument metadata, and trigger the configured forced
close decision before delivery.

### Manual action

Confirm contract settlement rules and approve option strategy close if required.

### Verification

Reconcile option legs and separately report liquidation versus expiration P&L.

### Review

Archive metadata changes, timestamps, and exchange notices.

## Liquidation-distance deterioration

### Detection

Post-trade or current liquidation distance breaches the finite minimum.

### Automatic action

Risk-veto entries and recovery, alert immediately, and escalate to strategy close
or emergency flatten according to confirmed exposure.

### Manual action

Review margin, wallet, position, and mark/index sources; do not override the limit
to preserve strategy intent.

### Verification

Require restored distance, reconciled wallet/positions, and operator-acknowledged
kill-switch reset.

### Review

Record distance trajectory, margin usage, and the proposal that was vetoed.
