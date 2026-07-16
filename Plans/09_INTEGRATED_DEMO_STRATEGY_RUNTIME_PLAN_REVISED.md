# Plan 9 — Integrated Bybit Demo Strategy Runtime (Reviewed and Revised)

## Status

**Implemented locally; linked-delta TP/SL behavior now passes local verification
and a bounded Bybit demo run. Protected restart reconciliation and bounded flat
closure pass. Full order-bearing acceptance under the revised geometry is still
pending.**

The missing **composition and orchestration milestone** now exists as the sealed
`demo_strategy_runner` preflight/run command, durable runtime journal, live
coordinator, account-wide reconciliation, bounded close service, operational
health surface, and command-level simulated runtime test.

As of 2026-07-15, the complete simulated-runtime gate and official non-mutating
Bybit demo preflight pass. Integrated cycles `DEMO-C0015` and `DEMO-C0017`
proved protected restart reconciliation, REST recovery of an actual hosted stop,
two baseline levels in one cycle, live health/status, bounded `CLOSE_ALL`, and
final flat reconciliation. `DEMO-C0018` then completed the one-hour configured
multi-level burn-in with an actual hosted stop, durable debt, verified close, and
fresh flat reconciliation. Those runs also exposed and fixed flat-position
parsing and transient reconciliation races. A successful integrated same-level
recovery TP and concurrent protected baselines remain pending. Production
approvals remain external gates. See
`docs/DEMO_BURN_IN_REVIEWS.md` for evidence.

On 2026-07-16 the strategy geometry changed by operator instruction: TP distance
is exactly one delta step and SL distance is exactly 15% of that step. The prior
one-hour burn-in remains historical evidence for the previous geometry and must
be revalidated before production approval. Linked-delta shadow capture and exact
offline replay passed on 2026-07-16. `DEMO-C0021`
validated three protected `$1`-delta baselines, two `$0.15` hosted stops, one
hosted `$1` TP, and verified flat close. `DEMO-C0022` proved fail-closed behavior
when price crossed an extremely tight stop before Bybit could host it.
`DEMO-C0023` then ran the linked geometry for about 38 minutes with a `$5` TP
and `$0.75` SL. It received one baseline TP and one baseline stop, persisted
`0.28179945` recovery debt, and re-armed the stopped level, but no recovery
recross occurred before an unrelated runtime task failure invoked verified
`CLOSE_ALL`. The post-close preflight proved zero positions and orders with
exact reconciliation. This is useful fail-closed and recovery-arming evidence,
not a passed one-hour burn-in or successful recovery TP.

## Objective

Create one explicitly gated application with two commands:

1. `preflight` — performs capability, credential, account, endpoint, database,
   reconciliation, instrument, and configuration checks without placing orders.
2. `run` — opens or restores one explicitly selected ETH put credit spread and
   runs the approved hedge strategy against live Bybit demo data for a bounded
   session.

The runtime must never automatically choose option legs. A new cycle requires
the exact short-put symbol, long-put symbol, option quantity, minimum acceptable
net credit, and maximum permitted leg-price deviation.

Create one explicitly gated command that opens or restores one ETH put credit
spread and runs the approved deployable hedge strategy against live Bybit demo
data for a bounded session:

```powershell
$env:ETH_HEDGE_ENVIRONMENT="DEMO"
$env:ETH_HEDGE_LEVEL_COUNT="10"

# Mandatory non-mutating check.
python -m eth_credit_hedge.interfaces.demo_strategy_runner preflight `
  --cycle-mode OPEN_NEW `
  --short-symbol "<EXACT_ETH_SHORT_PUT_SYMBOL>" `
  --long-symbol "<EXACT_ETH_LONG_PUT_SYMBOL>" `
  --option-quantity "1" `
  --min-net-credit "<MINIMUM_ACCEPTABLE_CREDIT>" `
  --health-port 8080

# Explicitly gated mutation run.
$env:RUN_BYBIT_DEMO_MUTATIONS="FULL_STRATEGY_DEMO"
python -m eth_credit_hedge.interfaces.demo_strategy_runner run `
  --cycle-mode OPEN_NEW `
  --short-symbol "<EXACT_ETH_SHORT_PUT_SYMBOL>" `
  --long-symbol "<EXACT_ETH_LONG_PUT_SYMBOL>" `
  --option-quantity "1" `
  --min-net-credit "<MINIMUM_ACCEPTABLE_CREDIT>" `
  --max-entry-deviation-bps "<MAX_ALLOWED_DEVIATION>" `
  --run-seconds 3600 `
  --shutdown-policy CLOSE_ALL `
  --health-port 8080
Remove-Item Env:RUN_BYBIT_DEMO_MUTATIONS
```

The command must:

- bind only the sealed Bybit demo REST and private WebSocket endpoints;
- use one durable strategy cycle and one option spread;
- generate every configured virtual level between the confirmed option strikes;
- consume normalized `ETHUSDT` `LAST_TRADE` events continuously;
- support multiple baseline levels within the finite demo limits;
- use same-level `FULL_NEXT_TP` recovery from actual confirmed stop debt;
- install and verify exchange-hosted reduce-only TP and stop protection;
- rebuild exact state after restart and reconcile it with the exchange;
- expose health, status, metrics, structured logs, alerts, and kill switches;
- close hedges and both option legs on timeout or `Ctrl+C` and verify flat state;
- exit nonzero if safe closure cannot be proven.

## Locked strategy boundary

The integrated demo runtime uses:

```text
RuntimeEnvironment.DEMO
TriggerPriceSource.LAST_TRADE
RecoveryMode.FULL_NEXT_TP
LockPolicy.UNHEDGED
stop_rate = 0.15 of one delta step
```

`DISTRIBUTED` recovery and `BREAKEVEN_FLOOR` remain local experiments. The
runtime must reject them before credentials or exchange adapters are created.

Backtesting and simulation remain reference oracles. Dashboard code remains a
read-only consumer. Neither is imported into the live execution loop.

## Mandatory Bybit demo transport boundary

The demo profile must encode and test the following transport split:

```text
REST trading/account mutations: https://api-demo.bybit.com
Private order/execution/position WebSocket: wss://stream-demo.bybit.com
Public market data WebSocket: wss://stream.bybit.com
WebSocket order entry: forbidden in demo
```

All demo order create/amend/cancel operations must use REST. Private WebSocket
streams are used to confirm order, execution, and position changes. The runtime
must fail startup if a WebSocket trade endpoint is configured for demo.

Because Bybit demo does not expose every mainnet API, startup must execute a
versioned capability probe and compare the result with the exact endpoint set
required by this runtime. An unavailable required capability is a hard preflight
failure, not a reason to silently use a different endpoint.

## Current integration gaps

| Existing component | What is already proven | Missing runtime work |
|---|---|---|
| `MultiLevelCoordinator` | Ordered baseline crossings and finite risk vetoes | It is baseline-only, keeps submitted levels forever, and does not drive exits or re-entry. |
| `SameLevelRecoveryService` | Durable actual-debt allocation, rejection, settlement, and stop rollover | No continuous trigger/lifecycle coordinator invokes it after arbitrary live exits. |
| `OneLevelLifecycleService` | Fill confirmation, stop/TP installation, and authoritative exit polling | No supervisor owns one lifecycle task per active level or restores those tasks after restart. |
| `SqliteExecutionStore` | Durable intents, executions, protection, option state, and recovery debt | Virtual level arming/state and the active strategy cycle are not reconstructed into a live coordinator. |
| `SqliteJournalStore` and `StartupReplayService` | Versioned events, snapshots, and ordered replay | No live strategy reducer/snapshot codec currently uses them. |
| Startup reconciliation | Exact local/exchange comparison and fail-closed reports | It is called only at bounded runner checkpoints, not startup and reconnect boundaries of a service. |
| Public/private WebSockets | Reconnect generations, normalized events, and stale-event fencing | The private stream is not routed into live execution services; reconnect does not start a supervised reconciliation barrier. |
| `RiskEngine` | Independent finite vetoes | Staged runners construct bounded risk states; a runtime must calculate state from wallet, positions, debt, P&L, request windows, and reconciliation health. |
| Kill switches | Durable escalation and abstract strategy close | No concrete Bybit demo `StrategyCloseOperationsPort` closes hedges and option legs. |
| Health, metrics, logs, alerts | Models, renderers, policies, and HTTP interface | No mutable operational-state owner or server/task lifecycle is wired to live state. |
| Option entry | Protective-long-first open/recovery from partial state | There is no persistence-first live option-spread close service. |

## Step 0 — Create the integration inventory and capability matrix

Before writing the runtime loop, create
`docs/INTEGRATION_COMPONENT_MATRIX.md`. For every existing component, record:

```text
component/class
constructor dependencies
owner
startup order
shutdown order
persistent state used
events consumed
events emitted
whether it is authoritative or derived
```

Add `BybitDemoCapabilityProbe`, which checks without mutation:

```text
demo REST connectivity
public-mainnet and private-demo WebSocket connectivity
server clock
account information and account mode
wallet and margin state
option and ETHUSDT instrument metadata
option position queries
linear position queries
open-order and execution-history queries
configured create/amend/cancel endpoint availability
required private WebSocket topics
```

The capability result must be stored with the runtime evidence. No mainnet URL,
testnet URL, or WebSocket trade URL may pass the demo profile.

Verification:

- Every object in the final composition root appears in the component matrix.
- A missing required endpoint blocks `run`.
- `preflight` performs no mutations.
- Capability fixtures cover supported, unsupported, and partially supported
  demo accounts.

## Runtime architecture

```text
demo_strategy_runner CLI
        |
        v
DemoRuntimeBootstrap
  profile + credentials + clock + migrations + stores + kill switch
        |
        v
startup replay -----> startup reconciliation -----> reconnect barrier
        |                         |
        v                         v
LiveStrategyCoordinator <---- OperationalState
        |
        +-- public LAST_TRADE supervisor
        +-- private order/execution/position supervisor
        +-- periodic REST reconciliation supervisor
        +-- option-quote/freshness supervisor
        +-- per-level entry/protection/exit tasks
        +-- health/metrics/alert supervisor
        |
        v
DemoStrategyCloseOperations
  cancel entries -> close hedges -> close short option -> close long option
  -> reconcile -> prove no positions/orders remain
```

One task failure must cancel the task group, activate the appropriate durable
kill-switch state, run bounded safe closure, emit an alert, and exit nonzero.
No background task may fail silently.

## Step 1 — Freeze the command and startup contract

Add `src/eth_credit_hedge/interfaces/demo_strategy_runner.py` with parser-only
tests before exchange wiring.

Required commands and arguments:

```text
preflight
run

--cycle-mode OPEN_NEW | RESTORE_ONLY
--cycle-id <required for RESTORE_ONLY>
--short-symbol <required for OPEN_NEW>
--long-symbol <required for OPEN_NEW>
--option-quantity <required for OPEN_NEW>
--min-net-credit <required for OPEN_NEW>
--max-entry-deviation-bps <required for OPEN_NEW>
--run-seconds 3600
--shutdown-policy CLOSE_ALL | LEAVE_OPTION_PROTECTED
--health-host 127.0.0.1
--health-port 8080
```

`OPEN_NEW` must never rank or automatically select option legs.
`RESTORE_ONLY` must never open a new option spread when the requested durable
cycle cannot be restored exactly.

Startup must refuse unless:

- `RUN_BYBIT_DEMO_MUTATIONS=FULL_STRATEGY_DEMO` exactly;
- runtime environment is `DEMO`;
- recovery and lock policies match the locked boundary;
- demo profile endpoints and credential scopes match;
- execution/journal migrations are current;
- the independent kill-switch store is available and `RUNNING`;
- clock drift is inside the sealed limit;
- the database is writable;
- startup replay succeeds;
- local and exchange state reconcile exactly;
- the account is one-way and option-capable;
- finite risk limits are present.

Verification:

- CLI tests prove wrong/missing tokens fail before adapter construction.
- Mainnet endpoint injection is rejected.
- Zero, negative, or unbounded duration is rejected.

## Step 2 — Extract reusable demo bootstrap

Move shared setup from the 100k-line staged `demo_runner.py` into small package
components without changing D3-D6 behavior:

```text
interfaces/demo_bootstrap.py
application/demo_runtime_state.py
application/demo_strategy_runtime.py
infrastructure/bybit/demo_strategy_close.py
```

Bootstrap owns profile loading, credentials, server clock, store initialization,
late-fill recovery, option-spread open/restore, private-state capture, startup
reconciliation, and service construction.

Verification:

- Existing D3-D6 unit and opt-in interfaces retain the same mutation tokens.
- Existing D3-D6 tests pass unchanged.
- Bootstrap fixture tests cover fresh, restarted, mismatched, and partial-option
  states.

## Step 3 — Add durable live strategy state

Define one versioned strategy-cycle state containing:

```text
cycle ID and option-spread ID
configured strategy and level geometry
per-level READY / ACTIVE / PAID / LOCKED state
entry arming and connection generation
attempt counters and active order-link IDs
baseline versus recovery role
confirmed and allocated recovery debt
realized per-level and daily P&L
last processed public/private event identity
last successful reconciliation
kill-switch mode and suspension reason
```

Use `SqliteJournalStore` for strategy events/snapshots and
`SqliteExecutionStore` for exchange intents/executions. Implement a pure reducer
and canonical snapshot codec so replay produces byte-equivalent state.

Persist every state transition before any corresponding exchange mutation.

Verification:

- Reducer tests cover every journal event.
- Snapshot plus later events equals full replay.
- Duplicate/reordered events do not change state twice.
- Crash checkpoints reconstruct the same level, debt, and order ownership.

## Step 4 — Implement the live strategy coordinator

Create `LiveStrategyCoordinator`; do not stretch the baseline-only M9 coordinator
into unrelated responsibilities.

For each fresh `LAST_TRADE` segment:

1. Fence stale/reconnected generations.
2. Refresh market-health and option-quote health.
3. Arm eligible levels at or above their entry boundary.
4. Detect all ordered downward crossings.
5. Build the current risk state from authoritative runtime data.
6. Submit eligible baseline or recovery entries persistence-first.
7. Start one lifecycle task for each confirmed entry.
8. Persist protection confirmation before reporting the level protected.
9. Apply actual TP/stop executions idempotently.
10. Mark baseline TP paid, or add actual stop debt and re-arm same-level recovery.
11. Settle recovery debt only from actual TP P&L.
12. Lock/suspend exactly as the existing recovery and risk policies require.

Shared boundaries must preserve TP-before-next-entry ordering. A level may have at
most one active hedge, and aggregate confirmed quantity must reconcile to the
single ETHUSDT short position.

Verification:

- Deterministic event tests trace multiple levels, shared boundaries, stop,
  same-level recovery, recovery TP, and debt settlement.
- Large price jumps preserve crossing order.
- Stale data, reconnects, soft pause, risk rejection, and option expiry block
  entries without losing the crossing state.

## Step 4A — Add an internal hedge-lot allocator for the net ETHUSDT position

Bybit exposes one aggregate ETHUSDT position in one-way mode, while the strategy
tracks separate virtual levels. Add a durable internal lot ledger:

```text
lot ID
level ID
attempt
entry order-link ID
confirmed entry executions
average entry price
confirmed open quantity
reserved TP quantity
reserved stop quantity
realized exit allocations
recovery role
```

The `NetPositionAllocator` must enforce:

```text
sum(internal open lot quantity) == reconciled exchange short quantity
sum(all reserved reduce-only leaves quantity) does not exceed the exchange position
one execution can be allocated to one or more owned lots deterministically
a level exit cannot realize P&L against another level silently
exchange auto-reduction or cancellation of reduce-only orders triggers reconciliation
```

Define deterministic exit-allocation priority, preferably by owned order-link ID
and then oldest execution. Do not infer a level from price alone.

Verification:

- Two or more active levels reconcile to one aggregate short position.
- Concurrent TP/stop orders never over-reserve close quantity.
- A partial reduce-only execution is allocated exactly once.
- Exchange cancellation or resizing of reduce-only orders is detected and repaired.
- Recovery debt is attributed to the correct level.

## Step 5 — Supervise public, private, and reconciliation flows

Run these tasks under one `asyncio.TaskGroup`:

- public last-trade stream from the mainnet public-data WebSocket;
- private order/execution/position stream;
- periodic REST reconciliation;
- option quote/freshness refresh;
- active lifecycle tasks;
- health/metrics/alerts;
- bounded runtime timer and signal handling.

Private reconnect immediately blocks new entries. REST capture and reconciliation
must pass before marking that connection generation reconciled. REST history is
the recovery source for missed private executions.

Verification:

- Simulated disconnect/reconnect never infers a crossing across missing data.
- Missed, duplicated, delayed, and reordered private executions converge to one
  durable state.
- Any unknown position, order, or protection state suspends new entries.
- A task exception cannot leave sibling tasks running silently.

## Step 6 — Build authoritative runtime risk state

Replace staged constants with a `RuntimeRiskStateBuilder` calculated from:

- confirmed exchange position quantity/notional;
- wallet margin use;
- actual liquidation distance;
- durable confirmed recovery debt;
- realized cycle and UTC-day P&L;
- per-level entry count and active-level count;
- rolling one-minute order-request timestamps;
- market-data freshness;
- consecutive reconciliation failures.

The risk engine still vetoes rather than resizes approved quantity.

Verification:

- Every `RiskLimits` field has an independent failing test.
- Quantization is applied before risk evaluation.
- Risk state survives restart without resetting debt, P&L, or request history.

## Step 7 — Implement concrete strategy close

Add persistence-first option exit models and storage migration support. Safe close
order is:

1. Activate `STRATEGY_CLOSE` and block new entries.
2. Cancel pending ETHUSDT entries.
3. Keep existing protection until each hedge close is submitted.
4. Close confirmed ETHUSDT short quantity reduce-only and verify flat.
5. Buy back the short option leg from actual executable quotes.
6. Sell the protective long only after the short leg is confirmed closed.
7. Aggregate partial fills and fees idempotently.
8. Cancel remaining owned orders.
9. Reconcile local/exchange orders and positions.
10. Prove no strategy-owned exposure remains.

If graceful close cannot be proven, escalate to `EMERGENCY_FLATTEN`, preserve the
protective long when its paired short remains open, alert, and exit nonzero.

Verification:

- Option short always closes before protective long.
- Partial fill, rejection, uncertain acknowledgement, timeout, and restart tests.
- `Ctrl+C`, timer expiry, and internal failure all use the same close service.
- Success is reported only after confirmed executions and final reconciliation.

## Step 7A — Define shutdown and option-expiry policies explicitly

`CLOSE_ALL` is the mandatory acceptance policy for bounded demo tests, but it
must not be assumed to be the only eventual production policy.

Policies:

```text
CLOSE_ALL
  close all perp hedges, then short option, then protective long, and prove flat

LEAVE_OPTION_PROTECTED
  close all perp hedges, cancel strategy-owned pending orders, retain the matched
  option spread, persist a suspended cycle, and prove that the protective long
  still covers the short quantity
```

Add option-expiry controls:

```text
no new cycle after the entry cutoff
no new normal hedge after the hedge cutoff
forced reconciliation before delivery
defined behavior if option markets are closed or illiquid
settlement confirmation before cycle archival
```

The runtime must never report a clean shutdown merely because the process exited.

## Step 8 — Wire operations

Use the existing components in the runtime:

- `FileKillSwitchStore` outside the trading database;
- `KillSwitchController`, `StrategyCloseService`, and emergency executor;
- `OperationalSnapshot` as the single health/metrics/alert source;
- `HealthApi` on the configured loopback address;
- Prometheus rendering;
- secret-safe structured JSON logs;
- immediate/warning alert evaluation and dispatch.

At minimum, expose:

```text
/health/live
/health/ready
/status/strategy
/status/exchange
/status/risk
```

Verification:

- Readiness changes with stale data, disconnect, reconciliation, protection, and
  risk lock state.
- No credential value appears in logs, alerts, exceptions, or JSON output.
- Health server starts and stops with the runtime.

## Step 9 — Full simulated acceptance

Before Bybit demo, run the same runtime against `SimulatedExchange` rather than a
separate toy loop.

Required scenarios:

```text
multi-level baseline lifecycle
baseline stop -> same-level recovery -> TP settlement
multiple concurrent protected levels
restart with live entries and exits
public/private disconnect and REST recovery
uncertain acknowledgements and partial fills
database failure before and after intent persistence
gap through stop and TP
risk and kill-switch vetoes
timer and Ctrl+C graceful close
option close partial fill and restart
```

Acceptance: deterministic equal-seed event digests, final flat exchange state,
zero unprotected quantity, exact local/exchange reconciliation, and no lost debt.

## Step 9A — Add a command-level end-to-end acceptance test

CI must launch the real composition root against `SimulatedExchange` using the
same CLI parser, bootstrap, stores, coordinator, risk builder, health server,
and close service used by demo.

The test must:

```text
start the command
open or restore the fixture option spread
inject a deterministic public price path
cause baseline entry, stop, same-level re-entry, and recovery TP
interrupt or expire the runtime
close according to policy
restart from the same database
prove flat or intentionally protected final state
compare the canonical event digest
```

This is the test that proves the components are connected, rather than merely
tested separately.

## Step 10 — Bounded Bybit demo acceptance

Run only after all local gates pass:

1. Read-only startup and reconciliation.
2. Five-minute one-level session with verified close.
3. One-hour configured multi-level session.
4. Restart during a protected live level.
5. Same-level recovery observed from an actual stop when market conditions allow.
6. `Ctrl+C` strategy close and final flat reconciliation.

Record canonical JSON evidence, capture hashes, exact release commit, risk-limit
snapshot, operator, UTC timestamps, and exchange final state. A simulated stop is
not demo recovery evidence.

## Step 11 — Documentation and command handoff

Update `README.md` only after the integrated runtime passes simulated acceptance.
The final runbook must include:

- exact installation and `.env` setup;
- the one command shown in this plan;
- preflight and expected startup output;
- health/status queries;
- how to stop safely;
- how to restart using the same database;
- how to inspect final reconciliation;
- incident and rollback links;
- an explicit warning never to delete state to bypass reconciliation.

## Step 12 — Keep mainnet as a separate, disabled composition root

`demo_strategy_runner` must be physically incapable of using mainnet private
trading endpoints. Mainnet must use a separate future command, for example:

```text
python -m eth_credit_hedge.interfaces.production_strategy_runner
```

That command must not be implemented or enabled until all of the following are
approved and stored as versioned evidence:

```text
finite risk limits
pilot configuration
legal/account eligibility
mainnet API credential scope
production incident owner
production rollback and flatten procedure
demo and shadow acceptance evidence
```

Copying a demo configuration and changing URLs is forbidden. Production has its
own profile, mutation token, database, kill-switch file, alert routing, and
release approval.

## Definition of complete

- [ ] One command runs the complete approved strategy on Bybit demo.
- [x] A non-mutating `preflight` command proves the exact demo capability set.
- [x] Demo uses REST mutations, private demo streams, and mainnet public streams.
- [x] WebSocket order entry is rejected for demo.
- [x] Exact option symbols and execution bounds are required for a new cycle.
- [x] Internal lots reconcile correctly to the aggregate ETHUSDT exchange position.
- [x] A command-level simulated-exchange test proves the complete composition root.
- [x] Mainnet remains a separate disabled runner until production approvals exist.
- [x] The command is bounded, explicitly mutation-gated, and demo-host sealed.
- [x] Option spread open and close both use confirmed actual fills.
- [x] All configured virtual levels participate in ordered live crossings.
- [x] Multiple baseline levels and same-level recovery share one durable state.
- [x] Every active hedge has confirmed exchange-hosted protection.
- [x] Private reconnect and restart require reconciliation before new entries.
- [x] Risk state is authoritative rather than staged/test constants.
- [x] Kill switches, health, metrics, logs, and alerts are live-wired.
- [x] Timeout, `Ctrl+C`, and failure close and reconcile the whole strategy.
- [x] Simulated runtime fault acceptance passes.
- [ ] Bounded Bybit demo acceptance passes and is documented.
