# Integrated Demo Runtime Component Matrix

This is the ownership and lifecycle inventory for the Plan 9 composition root.
The demo runner uses public mainnet market data, demo private account streams,
and demo REST for every order mutation.

## Transport capability matrix

| Capability | Required transport | Authority | Mutation |
|---|---|---|---|
| ETHUSDT trades, ticker and order book | `wss://stream.bybit.com/v5/public/*` | Public market data | No |
| ETH option quotes and instruments | Mainnet public REST/WebSocket | Public market data | No |
| Server clock and account mode | `https://api-demo.bybit.com` | Demo REST | No |
| Wallet, positions, orders and executions | `https://api-demo.bybit.com` | Demo REST | No |
| Order, execution and position events | `wss://stream-demo.bybit.com/v5/private` | Demo private stream | No |
| Create, amend, cancel and cancel-all | `https://api-demo.bybit.com` | Demo REST | Yes |
| WebSocket order entry | Forbidden | None | Forbidden |
| Mainnet private trading | Forbidden | None | Forbidden |

The versioned `BybitDemoCapabilityProbe` records the read capabilities before a
runtime can request mutation authority. Missing required capability is a startup
failure.

## Composition ownership

| Component | Constructor dependencies | Owner | Startup / shutdown | Persistent state | Consumes / emits | Authority |
|---|---|---|---|---|---|---|
| `DemoStrategyCommand` | CLI and environment | CLI | Parse first / discarded | None | User arguments | Derived configuration |
| `EnvironmentProfile` | Sealed demo TOML | Bootstrap | Load before credentials / immutable | Package TOML | Environment selection | Authoritative boundary |
| `BybitDemoProfile` | Demo credential variables | Bootstrap | Load after command validation / redact | None | Credential scope | Authoritative secret binding |
| `ServerClock` | Demo server-time samples | Bootstrap | Synchronize before signed reads / stop refresh | Memory | Clock samples | Authoritative while fresh |
| `BybitPublicRestClient` | Mainnet public REST | Bootstrap | Create after boundary checks / release | None | Instruments and quotes | Authoritative public snapshot |
| `BybitPublicMarketData` | Public REST and WS factories | Runtime task group | Start after reconciliation / cancel stream | Memory generation | Normalized public events | Authoritative trigger source |
| `BybitPrivateRestClient` | Demo profile and clock | Bootstrap/runtime | Create after token check / release | None | Account reads and REST mutations | Authoritative requests/snapshots |
| `BybitPrivateWebSocketClient` | Demo signer | Runtime task group | Authenticate after REST capture / close stream | Memory generation | Orders, executions, positions | Authoritative event delivery |
| `BybitDemoCapabilityProbe` | Public/private read clients | Preflight | Run before mutation token is used / store result | Evidence JSON | Capability observations | Authoritative preflight evidence |
| `SqliteExecutionStore` | Demo database path | Bootstrap | Initialize before reconciliation / flush | Intents, fills, protection, debt | Execution state | Authoritative local execution ledger |
| `SqliteJournalStore` | Demo journal path | Bootstrap | Initialize before replay / flush | Strategy events and snapshots | Domain transitions | Authoritative strategy ledger |
| `StartupReplayService` | Journal store and reducer | Bootstrap | Replay before reconciliation / none | Reads journal | Rebuilt cycle state | Derived deterministically |
| `StartupReconciliationService` | Local state and private reader | Bootstrap/reconnect task | Run after replay and reconnect / none | Reconciliation events | Local/exchange comparison | Authoritative entry barrier |
| `FileKillSwitchStore` | Demo-specific file path | Bootstrap | Load before entries / persist final state | Kill-switch file | Operator/runtime escalation | Authoritative control state |
| `KillSwitchController` | Kill-switch store | Runtime | Initialize before coordinator / persist shutdown | Kill-switch file | Entry vetoes | Authoritative control gate |
| `OptionSpreadEntryService` | Demo REST and execution store | Runtime coordinator | Restore/open after reconciliation / none | Option intents/fills | Option executions | Authoritative confirmed fills |
| `LiveStrategyCoordinator` | State, services, levels, risk and gates | Runtime task group | Restore before streams / suspend before close | Journal plus execution store | Public/private events; intents | Authoritative orchestration |
| `NetPositionAllocator` | Durable hedge lots | Live coordinator | Restore before position reconciliation / prove zero | Journal snapshots | Entry/exit allocation | Authoritative internal lot ownership |
| `RuntimeRiskStateBuilder` | Account, state, health and request window | Live coordinator | Build before each proposal / none | Journal P&L/request events | Risk state | Derived from authoritative inputs |
| `RiskEngine` | Proposal, state and finite limits | Live coordinator | Stateless / none | None | Approval or veto | Authoritative veto |
| `OneLevelEntryService` | REST, store and clock | Live coordinator | Construct after bootstrap / none | Entry intents/fills | Entry execution | Authoritative local transition |
| `ProtectiveExitService` | REST, account, store and clock | Live coordinator | Construct after bootstrap / reconcile on close | Protection intents/fills | TP/stop execution | Authoritative local transition |
| `SameLevelRecoveryService` | Entry service, debt store, planner and gate | Live coordinator | Restore debt before triggers / settle on close | Recovery debt | Stop debt and recovery allocation | Authoritative debt transition |
| `DemoStrategyCloseOperations` | REST, stores, account, quote source | Runtime shutdown | Construct before tasks / run exactly once | Close intents/fills | Close/reconciliation | Authoritative shutdown executor |
| `OperationalState` | Runtime event updates | Runtime | Initialize not-ready / mark stopped last | Optional evidence snapshot | Health/metric fields | Derived status source |
| `HealthApi` | Operational snapshot provider | Health server | Start after bootstrap / stop before process exit | None | HTTP status | Derived observer |
| `SecretSafeJsonLogger` | Output stream and secret redactions | Runtime | Create after credentials / flush last | Log files/stdout | Structured events | Derived audit view |
| `AlertDispatcher` | Notification port | Runtime monitor | Start with operational state / drain | External notifications | Alert observations | Derived safety notification |

## Startup order

```text
parse and validate command
validate sealed strategy/environment boundary
load demo credentials
initialize stores and kill switch
synchronize server clock
run capability probe
replay durable strategy cycle
capture private REST state
reconcile local and exchange state
validate explicit option selection or exact restore
construct coordinator and close operations
start health server not-ready
authenticate private stream
mark connection reconciled
start public/private/reconciliation/quote/lifecycle task group
mark ready
```

## Shutdown order

```text
persist and activate entry pause
stop accepting public triggers
cancel/await supervised tasks
execute selected shutdown policy
run final private REST capture and reconciliation
persist final strategy snapshot and evidence
mark health not-ready and not-live
stop health server
flush logs and exit (nonzero unless the selected final state is proven)
```

