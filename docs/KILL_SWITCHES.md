# Kill Switches

The kill switch is persisted atomically outside the trading database so database
failure does not silently re-enable entries. State can only escalate without an
explicit reset:

```text
RUNNING -> SOFT_PAUSE -> STRATEGY_CLOSE -> EMERGENCY_FLATTEN
```

Only an operator-acknowledged reset returns to `RUNNING`. Restart reloads the
persisted mode before the entry gate is available.

## Soft pause

Soft pause blocks baseline and recovery entries while leaving protection
management and reconciliation enabled. A crossing observed during the pause is
consumed and is not submitted later as a catch-up order.

## Strategy close

Strategy close activates the durable switch, cancels pending ETH perpetual and
option orders, invokes persistence-first hedge and option-spread closers, then
verifies the complete strategy is closed. Failed verification leaves the switch
active.

## Emergency flatten

Emergency flatten activates and alerts first, cancels relevant orders, queries
the confirmed position, persists a unique reduce-only close intent before every
submission, verifies the exchange position, and runs reconciliation. Attempts
are finite; failure to prove both flat and reconciled raises an incident while
the emergency state remains durable.

Close acknowledgement alone never means a position is flat. Operators must not
reset the switch until exchange state, local event state, and protection/order
state reconcile.
