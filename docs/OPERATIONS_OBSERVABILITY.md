# Operations: Health, Logs, Metrics, and Alerts

All operational views consume one immutable `OperationalSnapshot`, preventing a
ready endpoint from disagreeing with status or metrics for the same observation.

## HTTP endpoints

```text
/health/live
/health/ready
/status/strategy
/status/exchange
/status/risk
```

Readiness returns HTTP 503 for stale market data, disconnected public or private
streams, database failure, incomplete reconciliation, missing protection,
unprotected quantity, or an active risk lock. The dependency-free server binds
to `127.0.0.1` by default.

## Logs and metrics

Structured logs use only the fixed Plan 7 fields. Runtime credential values can
be supplied to the logger redactor; values of four or more characters are
removed from every string field before JSON serialization. Callers must not add
arbitrary payload dictionaries to log records.

Prometheus text output exposes market age, connection and reconciliation state,
cycle/level counts, hedge and unprotected quantities, recovery debt, stop budget,
daily P&L, order rejections, duplicate executions, and restart reconciliations.

## Alerts

Immediate alerts cover unprotected or unknown positions, risk violations,
database/authentication failures, dangerous reconciliation, and kill-switch
activation. Warnings cover stale data/quotes, pending orders, large slippage,
debt near its finite limit, and approaching expiry. The dispatcher emits only a
newly active alert code and re-arms it after the condition resolves.
