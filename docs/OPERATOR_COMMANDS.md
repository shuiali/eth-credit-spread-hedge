# Operator Commands

The command dispatcher exposes the Plan 5 control vocabulary:

```text
SOFT_PAUSE
RESUME_AFTER_RECONCILIATION
CANCEL_PENDING_ENTRY
RESTORE_PROTECTION
CLOSE_HEDGE_POSITION
CLOSE_OPTION_SPREAD
FLATTEN_STRATEGY
ACKNOWLEDGE_INCIDENT
```

Every deployment must register exactly one handler for every command. The
dispatcher authenticates the operator credential, persists the complete
secret-free intent, invokes the handler, persists the result, and writes an
audit record containing command/operator IDs, outcome, timestamp, and detail.
Credentials have redacted string/repr forms and are never persisted or included
in audit models.

A retry of a completed command returns the persisted result without invoking the
handler again. If a process stopped after intent persistence but before result
persistence, the command is `outcome unknown` and will not be repeated blindly;
the operator must reconcile the affected state and issue a new command ID.

Handler implementations remain responsible for persistence-first exchange
mutations. Resume handlers must prove reconciliation complete before performing
the operator-acknowledged kill-switch reset.
