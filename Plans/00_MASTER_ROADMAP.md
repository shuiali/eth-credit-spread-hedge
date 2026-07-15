# ETH Credit-Spread Dynamic Hedge — Complete Implementation Roadmap

**Status date:** 2026-07-15  
**Current baseline:** deterministic ETH put-credit-spread hedge with virtual levels, exact crossing fills, fixed 0.15% stops, same-level re-entry, full-next-TP recovery, premium stop budget, accounting snapshots, historical replay, Monte Carlo, dashboard, and public Bybit option-chain parsing.

## 1. Purpose

This roadmap defines the exact order in which the strategy should be completed. The objective is to preserve the already validated strategy logic while replacing idealized fills with a reliable exchange-execution system.

Every phase is a hard gate:

- Implement only one phase at a time.
- Add tests before connecting the phase to the next layer.
- Do not begin a later phase while an earlier acceptance criterion fails.
- Keep the deterministic backtester as the reference oracle.
- Keep dashboard code free of trading logic.
- Keep exchange-specific code outside the domain strategy engine.

The completed system must answer four questions independently:

1. What should the strategy do?
2. What did the exchange actually do?
3. Does local state match exchange state?
4. Is further trading still allowed by risk controls?

## 2. Freeze the current baseline

Preserve these modules as the reference implementation:

- `core.credit_spread.CreditSpread`
- `core.virtual_levels.generate_virtual_levels`
- `core.crossing_engine.CrossingEngine`
- `core.hedge_engine.HedgeEngine`
- `core.ledger.Ledger`
- `backtesting.historical`
- `backtesting.monte_carlo`
- `data.bybit_options.BybitOptionClient`
- deterministic scenarios and pytest tests

Create a tag:

```text
baseline-v1-deterministic
```

Locked baseline configuration:

```text
RecoveryMode.FULL_NEXT_TP
LockPolicy.UNHEDGED
stop_rate = 0.0015
```

## 3. Target architecture

```text
src/eth_credit_hedge/
├── domain/
│   ├── instruments.py
│   ├── option_position.py
│   ├── virtual_levels.py
│   ├── strategy_state.py
│   ├── recovery.py
│   ├── risk.py
│   └── events.py
├── application/
│   ├── strategy_coordinator.py
│   ├── option_entry_service.py
│   ├── hedge_execution_service.py
│   ├── reconciliation_service.py
│   └── lifecycle_service.py
├── ports/
│   ├── market_data.py
│   ├── trading.py
│   ├── account.py
│   ├── persistence.py
│   └── notifications.py
├── infrastructure/
│   ├── bybit/
│   │   ├── public_rest.py
│   │   ├── private_rest.py
│   │   ├── public_ws.py
│   │   ├── private_ws.py
│   │   ├── auth.py
│   │   ├── parsers.py
│   │   ├── quantization.py
│   │   └── error_mapping.py
│   ├── persistence/
│   │   ├── sqlite_store.py
│   │   └── migrations/
│   └── monitoring/
│       ├── metrics.py
│       ├── logging.py
│       └── alerts.py
├── backtesting/
│   ├── exact_fill_adapter.py
│   ├── simulated_exchange.py
│   ├── historical.py
│   ├── monte_carlo.py
│   └── fault_injection.py
├── interfaces/
│   ├── cli.py
│   ├── dashboard.py
│   └── health_api.py
└── config/
    ├── schema.py
    ├── demo.toml
    ├── shadow.toml
    └── production.toml
```

Do not rewrite validated formulas during migration.

## 4. Milestone order

| Milestone | Output | Trading allowed |
|---|---|---|
| M0 | Repository repaired, baseline frozen, all tests green | No |
| M1 | Domain model separated from backtest adapter | No |
| M2 | Option mark-to-market and actual-fill credit | No |
| M3 | Instrument filters and quantization | No |
| M4 | Public market-data service and freshness gates | No |
| M5 | Authenticated private adapter, read-only reconciliation | No |
| M6 | One virtual level: demo entry only | Demo only |
| M7 | One level: entry + TP + exchange-hosted stop | Demo only |
| M8 | Persistence, restart and reconciliation | Demo only |
| M9 | Multiple levels without recovery escalation | Demo only |
| M10 | Full-next-TP recovery from actual fills | Demo only |
| M11 | Realistic simulated exchange and fault testing | No real orders |
| M12 | Mainnet shadow mode, no orders | No |
| M13 | Controlled minimal live pilot | Only when account/legal eligibility and all gates are satisfied |
| M14 | Gradual production hardening | Controlled |

## 5. Non-negotiable invariants

### Strategy invariants

```text
One level has at most one active hedge.
PAID or LOCKED levels cannot open normal hedges.
Entry requires a new downward crossing.
TP and stop accounting use actual closed quantity and actual fill prices.
Stop debt uses actual realized loss.
Recovery quantity uses confirmed debt.
Debt can decrease only through explicit recovery allocation.
```

### Exchange invariants

```text
Acknowledgement is not a fill.
Only executions change filled quantity.
Executions are processed idempotently.
Exit quantity cannot exceed confirmed open quantity.
TP and stop orders are reduce-only.
Local and exchange positions must reconcile.
Unknown state blocks new trading.
```

### Accounting invariants

```text
Actual option credit comes from confirmed fills.
Mark-to-market and expiration P&L remain separate.
Projected values never overwrite realized values.
Fees, funding and slippage are separate components.
Recovery debt cannot become negative.
Decimal is used for money, price and quantity.
```

## 6. Recommended development rhythm

Each numbered task group should be one branch or pull request and contain:

1. Interface or data model.
2. Implementation.
3. Unit tests.
4. Fixture integration tests.
5. Failure tests.
6. Migration notes.
7. Acceptance checklist.

Core checks:

```bash
python -m compileall src tests
pytest -q
ruff check .
mypy src
```

## 7. Definition of complete

The system is complete only when:

- The option spread is opened from confirmed fills.
- A single declared trigger source drives virtual crossings.
- Perpetual entries are reconciled from executions.
- Exchange-hosted protection is confirmed.
- Restarts reconstruct the exact active state.
- Unknown exchange state blocks further orders.
- Duplicate, delayed and reordered events are handled.
- All prices and quantities comply with exchange filters.
- Independent risk controls can veto strategy requests.
- Historical, simulated-exchange, demo and shadow gates pass.
- A kill switch can cancel and flatten according to policy.
- Every live position is explainable from the event ledger.
- No zero-loss guarantee is represented unless actually proven.

## 8. Follow the plans in this order

1. `01_BASELINE_AND_REPOSITORY_PLAN.md`
2. `02_OPTION_MODEL_AND_MARK_TO_MARKET_PLAN.md`
3. `03_MARKET_DATA_AND_INSTRUMENT_PLAN.md`
4. `04_BYBIT_DEMO_EXECUTION_PLAN.md`
5. `05_PERSISTENCE_RECONCILIATION_AND_RISK_PLAN.md`
6. `06_BACKTEST_FAULT_TESTING_AND_VALIDATION_PLAN.md`
7. `07_DEPLOYMENT_OPERATIONS_AND_ACCEPTANCE_PLAN.md`
8. `08_MASTER_CHECKLIST.md`
