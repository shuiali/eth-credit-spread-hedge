# Authoritative Runtime Composition V1

## Status

This is the Plan 3.1 ownership contract for the offline-only Milestone 3
runtime. It defines the target graph and construction boundary. Plan 3.2
connects allocator state; lifecycle accounting and startup reconciliation
remain migrations for Plans 3.3 through 3.7.

The target is one `AuthoritativeStrategyRuntime` assembled through dependency
injection. Simulated and future demo execution must supply the same graph; only
the environment ports may differ. No component in this contract authorizes
network access or exchange mutations.

## Authoritative owners

| Responsibility | Sole runtime-façade owner and delegate | Current status |
| --- | --- | --- |
| Configuration | `AuthoritativeStrategyRuntime.configuration` -> `RuntimeConfig`/`EnvironmentProfile` | Existing input owner; complete runtime consumption deferred to Plan 3.5. |
| Clock | `AuthoritativeStrategyRuntime.clock` -> `ClockPort` | Explicit port; `ServerClock` and simulated virtual time are adapters. |
| Public market data | `AuthoritativeStrategyRuntime.market_data` -> `MarketDataPort` | Explicit port; current adapters are `BybitPublicMarketData` and `SimulatedExchange`. |
| Private executions | `AuthoritativeStrategyRuntime.private_executions` -> `PrivateExecutionPort` | Explicit port; stream-to-ledger lifecycle migration is Plan 3.4. |
| Option entry and close | `AuthoritativeStrategyRuntime.coordinator` -> lifecycle services | Existing services; common runtime ownership is deferred. |
| Level generation | `AuthoritativeStrategyRuntime.strategy_math` -> `StrategyMathEngine` | Accepted Milestone 1 authority; no formula is duplicated. |
| Crossings | `AuthoritativeStrategyRuntime.coordinator` -> `LiveStrategyCoordinator` | Existing coordinator; it is the target crossing owner. |
| Hedge entry and exits | `AuthoritativeStrategyRuntime.coordinator` -> `LiveStrategyCoordinator` | Existing coordinator/service split; allocator integration is Plan 3.2. |
| Protection | `AuthoritativeStrategyRuntime.coordinator` -> `ProtectiveExitService` | Existing service; protection-state migration is Plan 3.4. |
| Internal lots | `HedgeLotAllocationService` -> `NetPositionAllocator` | Production-connected through the demo and simulated composition roots; durable allocation state is replayed before use. |
| Accounting | `AuthoritativeStrategyRuntime.accounting` -> `AccountingRuntime` | Accepted Milestone 2 authority. |
| Recovery debt | `AuthoritativeStrategyRuntime.accounting` -> combined-ledger projection | Target authority; journal-side debt remains legacy until Plan 3.4. |
| Risk | `AuthoritativeStrategyRuntime.risk` -> risk service pair | Existing owner pair; complete configuration wiring is Plan 3.5. |
| Persistence | `AuthoritativeStrategyRuntime.coordinator` -> persistence ports | Existing durable stores; ownership convergence is deferred. |
| Reconciliation | `AuthoritativeStrategyRuntime.reconciliation` -> `StartupReconciliationService` | Accepted unit authority, not yet production-connected; Plan 3.3. |
| Health | `AuthoritativeStrategyRuntime.health` -> `MutableOperationalState` | Existing health projection; legacy fallbacks are removed in Plan 3.5. |
| Kill switch | `AuthoritativeStrategyRuntime.shutdown` -> `KillSwitchController` | Existing owner. |
| Shutdown | `AuthoritativeStrategyRuntime.shutdown` -> `StrategyCloseService` | Existing owner; authoritative final reconciliation is Plan 3.5. |
| Metrics and logging | `AuthoritativeStrategyRuntime.health` -> projections | Existing projection/event owners; unified events are Plan 3.7. |

Operational consumers may read the accounting, health, and journal projections.
They must not independently calculate level geometry, combined P&L, fees,
funding, slippage, recovery debt, or hedge-lot allocation.

## Runtime façade and injection boundary

`eth_credit_hedge.application.authoritative_runtime.AuthoritativeStrategyRuntime`
is the sole façade declaration. Its constructor is intentionally dependency-only:
it receives configuration, six environment ports, strategy math, the coordinator,
accounting, allocator, reconciliation, risk, health, and shutdown authorities.
`assemble_authoritative_runtime` creates one façade from those preconstructed
dependencies and does not secretly create another authority.

The approved future composition root will construct the runtime façade once and
inject its members into consumers. The current `demo_strategy_runtime` has not
yet switched to the façade because startup reconciliation remains unused. The
allocator is now constructed once by each current environment composition root,
then injected into lifecycle, private execution, recovery, reconciliation, and
shutdown. The checked-in manifest records the remaining façade migration as
deferred to Plans 3.3-3.7.

## Environment ports

The contract exposes these ports in `eth_credit_hedge.ports.runtime`:

| Port | Boundary |
| --- | --- |
| `MarketDataPort` | public instruments, option quotes, order books, and public streams |
| `PrivateExecutionPort` | authenticated execution batches and reconciliation gating |
| `TradingMutationPort` | place, amend, cancel, and bounded cancel-all mutations |
| `ExchangeQueryPort` | orders, executions, positions, and wallet reads |
| `FundingPort` | confirmed funding facts for the accounting ledger |
| `ClockPort` | UTC time supplied by the chosen environment |

The current `TradingPort`, `AccountPort`, and `PrivateEventPort` remain
compatibility ports until Plan 3.6 seals capability boundaries. `SimulatedExchange`
is the only adapter eligible for Plan 3 offline runtime execution. Existing Bybit
adapters are inventory only and must not be contacted during Milestone 3.

## Existing-root inventory

| Root or coordinator | Classification | Current caller and persistence | Disposition |
| --- | --- | --- | --- |
| `demo_strategy_runtime` | scheduled for migration | `demo_strategy_runner`; execution/journal/accounting SQLite stores, health snapshot, structured logs | Target entrypoint shape, but currently contains separate accounting initialization and `_reconcile_runtime`; Plans 3.2-3.7 migrate it. |
| `demo_runner` | legacy | direct D3-D6 CLI and live-only tests; execution SQLite store | Remains outside the offline runtime and is not a Milestone 3 authority. |
| `strategy_math_runtime` | compatibility adapter | `strategy_runner`; JSON math evidence | Retained as isolated M1 evidence; not a lifecycle composition root. |
| `accounting_runtime` | authoritative | `ledger_simulated_lifecycle` and helper initialization in `demo_strategy_runtime`; accounting SQLite store | Sole combined-ledger façade. |
| `ledger_simulated_lifecycle` | test fixture | accounting runtime tests and project audit; deterministic JSON/SQLite artifacts | Retained as M2 replay evidence; it does not represent the complete strategy runtime. |
| `one_level_coordinator` | compatibility adapter | one-level tests and legacy D4 path | Not a multi-level authoritative coordinator. |
| `multi_level_execution` | compatibility adapter | legacy D5 path and tests | Not the shared runtime coordinator. |
| `live_strategy_coordinator` | authoritative target | `demo_strategy_runtime` and simulated command; `DemoRuntimeJournal`, execution store, and allocation service | Owns crossing orchestration and receives the allocator; ledger-derived debt and startup reconciliation remain deferred. |
| `mainnet_shadow_runner` | legacy, out of Milestone 3 scope | shadow CLI; JSONL capture | Not an offline strategy runtime and must not be invoked in this milestone. |

## Current ownership divergence

The following remain deliberately visible until their named migration plans:

- `demo_strategy_runtime._reconcile_runtime` uses `evaluate_private_snapshot`
  rather than `StartupReconciliationService` (Plan 3.3).
- `DemoRuntimeState` retains `realized_pnl`, `confirmed_debt`, and
  `daily_realized_pnl`; `RuntimeRiskStateBuilder`, health, and the live
  coordinator can read these legacy projections (Plans 3.4 and 3.5).
- `demo_runner` and `protected_execution.py` contain legacy realized-P&L/debt
  paths (Plan 3.4).
- `HedgeLotAllocationService` persists allocator state in the execution store
  and is injected by both current environment composition roots. The remaining
  façade consolidation is deferred to Plans 3.3-3.7.
- `ledger_simulated_lifecycle` proves the raw-fill ledger separately from the
  simulated strategy command (Plan 3.7).
- `TradingPort` exposes mutation and query methods together; capability sealing
  is Plan 3.6.

## Persistence and observable facts

Current durable facts are split across `SqliteExecutionStore` (intents,
acknowledgements, fills, protection, option snapshots, and legacy recovery
snapshots), `SqliteJournalStore` (`DemoRuntimeJournal` event/snapshot replay),
and `SqliteAccountingStore` (raw accounting events, snapshots, digests, and
reconciliation records). The authoritative runtime must eventually reproduce
the same final digest from persisted facts.

Current observable events include journal events such as
`RECONCILIATION_COMPLETED`, `TRADING_SUSPENDED`, `HEDGE_ENTRY_INTENT_CREATED`,
`PROTECTION_CONFIRMED`, `STOP_RECEIVED`, and `TAKE_PROFIT_RECEIVED`; combined
ledger events and digests; health snapshots; simulated exchange event logs; and
structured runtime logs. Plan 3.1 records the ownership contract only and adds
no new lifecycle event.

## Constructor guard

`artifacts/runtime_composition.json` records every currently allowed production
constructor occurrence for `StrategyMathEngine`, `AccountingRuntime`,
`NetPositionAllocator`, and `StartupReconciliationService`. Architecture tests
fail if a new production occurrence appears outside that explicit inventory.
Tests may create isolated fixtures. The listed legacy and compatibility sites
are not endorsed as target authorities: later plans must remove them from this
allowlist as their consumers migrate.

## Plan 3.1 acceptance mapping

| Acceptance item | Evidence |
| --- | --- |
| Ownership specification exists | This document and `artifacts/runtime_composition.json`. |
| Every responsibility has one declared owner | `Authoritative owners` table. |
| One runtime façade exists | `AuthoritativeStrategyRuntime` and `assemble_authoritative_runtime`. |
| Environment ports are explicit | `eth_credit_hedge.ports.runtime`. |
| Composition manifest exists | `artifacts/runtime_composition.json`. |
| Architecture tests prevent duplicate authorities | `tests/architecture/test_authoritative_runtime_composition.py`. |
| Legacy owners are documented | `Existing-root inventory` and `Current ownership divergence`. |
