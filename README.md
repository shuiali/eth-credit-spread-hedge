# ETH Credit-Spread Dynamic Hedge

A deterministic ETH put-credit-spread hedge with virtual short-perpetual
levels, actual-fill accounting, restart reconciliation, realistic exchange
simulation, Bybit demo execution gates, and an order-free mainnet shadow mode.

As of 2026-07-15, the integrated runtime and its complete simulated acceptance
suite pass. The bounded integrated Bybit demo acceptance is not complete, and
the production pilot is **not approved**. Finite-limit approval, pilot
configuration approval, legal/account eligibility, and a signed operator
approval are still required.

## Safety boundary

Read this before running anything that contacts Bybit.

| Test tier | Network | Credentials | Can place orders? |
|---|---:|---:|---:|
| Local unit, integration, simulation, scenarios, dashboard | No | No | No |
| Public option chain, market data, and public live tests | Yes | No | No |
| Bybit demo read-only reconciliation | Yes | Demo keys | No |
| Integrated Bybit demo preflight | Yes | Demo keys | No |
| Integrated bounded demo runtime | Yes | Demo keys | **Yes, on Bybit demo** |
| Mainnet shadow acceptance | Yes | No | No |
| D3-D6 demo burn-in | Yes | Demo keys | **Yes, on Bybit demo** |
| Production pilot | Not available | Not approved | **Do not run** |

The mainnet shadow runner has no trading adapter and refuses profiles that allow
external mutations. The D3-D6 runner does the opposite: it requires an exact,
stage-specific environment token before it can place demo orders.

There is no production-order command in this repository.

## Requirements and installation

- Python 3.11 or newer.
- PowerShell examples below assume the repository root as the current directory.
- Network access is required only for sections explicitly labeled live.

Create an isolated environment and install the package plus development tools:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
python -c "import eth_credit_hedge; print('package import: OK')"
```

Do not put credentials in source files. `.env` is ignored by Git; `.env.example`
contains variable names and non-secret defaults only.

## Fast, safe local verification

This is the recommended first run. It has no network access and explicitly
excludes every test marked `live`, even if a live opt-in environment variable
was accidentally left set:

```powershell
python -m compileall -q src tests
ruff check .
mypy
python -m pytest -q -m "not live"
python run_scenarios.py
```

Expected result:

- compilation, Ruff, and mypy exit successfully;
- pytest reports no failures;
- `run_scenarios.py` prints `PASS` for all ten deterministic scenarios.

Use configured `mypy`, not `mypy src`. The project deliberately limits strict
typing to the package surfaces listed in `pyproject.toml`; the calculation-free
visualization renderer is validated separately by render and boundary tests.

Useful focused suites:

```powershell
python -m pytest -q tests\domain -m "not live"
python -m pytest -q tests\application -m "not live"
python -m pytest -q tests\infrastructure -m "not live"
python -m pytest -q tests\interfaces -m "not live"
python -m pytest -q tests\test_backtesting.py tests\test_simulated_exchange.py
```

## Deterministic strategy and dashboard

The reference strategy uses exact `Decimal` arithmetic, reconstructs each price
segment into ordered ticks, and keeps option, realized hedge, open hedge, fees,
funding, and recovery debt separate.

Run the deterministic scenarios:

```powershell
python run_scenarios.py
```

Render the default dashboard or save it without opening a window:

```powershell
python dashboard_app.py
python dashboard_app.py --save artifacts\dashboard.png
python dashboard_app.py --mc-paths 0 --save artifacts\dashboard-no-mc.png
python dashboard_app.py `
  --path 3010,2990,3005,2970,2985,2940,2890 `
  --mc-paths 100 `
  --save artifacts\dashboard-custom.png
```

Use the committed option fixture for a network-free option-driven dashboard:

```powershell
python dashboard_app.py `
  --option-fixture tests\fixtures\bybit_eth_option_pair.json `
  --short-symbol ETH-31JUL26-1750-P-USDT `
  --long-symbol ETH-31JUL26-1650-P-USDT `
  --mc-paths 0 `
  --save artifacts\dashboard-fixture.png
```

The dashboard consumes a completed payload and contains no trading logic.
Generated files under `artifacts/` are ignored by Git.

## Public Bybit checks — no credentials, no orders

These commands contact public Bybit endpoints only.

### Option chain

List current ETH puts first. Symbols expire, so do not blindly reuse the sample
symbols in this README:

```powershell
python option_chain_app.py
python option_chain_app.py `
  --short-symbol <CURRENT_SHORT_PUT_SYMBOL> `
  --long-symbol <CURRENT_LONG_PUT_SYMBOL> `
  --quantity 1
```

The second command accepts only the exact pair selected by the user. It reports
natural bid-minus-ask and mark credit separately.

### Public live integration tests

```powershell
$env:RUN_LIVE_BYBIT_TESTS="1"
python -m pytest -q tests\test_live_bybit.py
Remove-Item Env:RUN_LIVE_BYBIT_TESTS
```

These tests normalize the ETH option chain, perpetual specification, order book,
trade stream, and snapshot/delta sequence. They do not load private credentials.

### Bounded market-data capture

Choose a current option symbol from `option_chain_app.py`, then run:

```powershell
python capture_market_data.py `
  --output artifacts\public_market_data.jsonl `
  --seconds 60 `
  --depth 50 `
  --option-symbol <CURRENT_OPTION_SYMBOL>
```

The JSONL capture includes normalized ETHUSDT trades, tickers, order-book
snapshots/deltas, and the selected option ticker. The deployment trigger source
is locked to ETHUSDT last trades.

## Bybit demo read-only gate — credentials, no orders

Create `.env` once and insert credentials for a dedicated Bybit demo account:

```powershell
Copy-Item .env.example .env
```

Required values:

```text
BYBIT_API_KEY_DEMO=...
BYBIT_API_SECRET_DEMO=...
```

Run the read-only private-state gate:

```powershell
$env:RUN_BYBIT_DEMO_READ_ONLY="1"
python -m pytest -q tests\test_live_bybit_demo.py
Remove-Item Env:RUN_BYBIT_DEMO_READ_ONLY
```

This synchronizes the server clock, reads orders, executions, positions, and
wallet state, authenticates the private WebSocket, and reconciles an empty local
state. It does not submit, amend, or cancel orders.

Do not continue to D3 if reconciliation is not exactly `MATCHED`.

## Integrated Bybit demo strategy

For the currently selected demo spread, run the complete integrated strategy
with one PowerShell command:

```powershell
.\run_demo.ps1
```

The default is 20 virtual levels over the 100 USDT spread (5 USDT TP distance,
0.75 USDT SL distance) for one hour. For the extreme 1 USDT spacing test, run
`.\run_demo.ps1 -LevelCount 100`. The script loads demo credentials from `.env`,
uses the sealed mutation gate, and requests bounded `CLOSE_ALL` shutdown.

This is the production-style composition root described by
[`Plans/09_INTEGRATED_DEMO_STRATEGY_RUNTIME_PLAN_REVISED.md`](Plans/09_INTEGRATED_DEMO_STRATEGY_RUNTIME_PLAN_REVISED.md).
It owns the selected option spread, every configured virtual hedge level,
same-level recovery, durable replay, reconnect reconciliation, finite risk
checks, health endpoints, and bounded safe closure.

### 1. Select current option legs

List the current chain and explicitly choose one common-expiry ETH USDT put
pair. The short strike must be above the long strike and expiry must be 14–90
days away. The runtime never selects legs automatically.

```powershell
python option_chain_app.py
```

Record these operator-approved values before continuing:

```text
<SHORT_SYMBOL>
<LONG_SYMBOL>
<OPTION_QUANTITY>
<MINIMUM_NET_CREDIT>
<MAX_ENTRY_DEVIATION_BPS>
```

### 2. Run the non-mutating preflight

Preflight authenticates and reads the Bybit demo account, probes the exact REST
and WebSocket capabilities, validates the selected instruments and finite
configuration, migrates/opens the local stores, and reconciles durable state.
It submits, amends, and cancels zero exchange orders.

```powershell
$env:ETH_HEDGE_ENVIRONMENT="DEMO"
$env:ETH_HEDGE_LEVEL_COUNT="1"

python -m eth_credit_hedge.interfaces.demo_strategy_runner preflight `
  --cycle-mode OPEN_NEW `
  --short-symbol "<SHORT_SYMBOL>" `
  --long-symbol "<LONG_SYMBOL>" `
  --option-quantity "<OPTION_QUANTITY>" `
  --min-net-credit "<MINIMUM_NET_CREDIT>" `
  --max-entry-deviation-bps "<MAX_ENTRY_DEVIATION_BPS>" `
  --run-seconds 900 `
  --shutdown-policy CLOSE_ALL
```

Continue only when the canonical JSON reports `"accepted":true`,
`"external_order_mutations":0`, `"reconciliation_complete":true`, and a
capability status of `SUPPORTED`. Preserve the evidence file and SHA-256 printed
by the command.

### 3. Run one bounded demo session

This command can place demo orders. The exact mutation token authorizes only the
integrated demo runner; remove it as soon as the process exits.

```powershell
$env:RUN_BYBIT_DEMO_MUTATIONS="FULL_STRATEGY_DEMO"

python -m eth_credit_hedge.interfaces.demo_strategy_runner run `
  --cycle-mode OPEN_NEW `
  --short-symbol "<SHORT_SYMBOL>" `
  --long-symbol "<LONG_SYMBOL>" `
  --option-quantity "<OPTION_QUANTITY>" `
  --min-net-credit "<MINIMUM_NET_CREDIT>" `
  --max-entry-deviation-bps "<MAX_ENTRY_DEVIATION_BPS>" `
  --run-seconds 900 `
  --shutdown-policy CLOSE_ALL

Remove-Item Env:RUN_BYBIT_DEMO_MUTATIONS
```

Start with `ETH_HEDGE_LEVEL_COUNT=1`. Increase it only after the one-level TP,
stop, restart, and same-level recovery gates are evidenced. `CLOSE_ALL` is the
mandatory bounded-acceptance policy: timeout, `Ctrl+C`, and internal failure all
use the same close service and success requires a proven final flat state.

Option entry is deliberately fail-closed. An IOC leg that is unfilled or only
partially filled aborts the cycle; any filled protective quantity is closed and
the short leg is never treated as a valid spread unless both quantities match.
After any non-zero exit, run a `RESTORE_ONLY` preflight for the printed cycle ID
and do not retry until it reports exact reconciliation, zero option/linear
positions, and zero open orders. Do not loosen the entry bounds merely to turn a
demo-liquidity rejection into a passing test.

### Health and status while running

The operator interface binds to loopback port 8080 by default:

```powershell
Invoke-RestMethod http://127.0.0.1:8080/health/live
Invoke-RestMethod http://127.0.0.1:8080/health/ready
Invoke-RestMethod http://127.0.0.1:8080/status/strategy
Invoke-RestMethod http://127.0.0.1:8080/status/exchange
Invoke-RestMethod http://127.0.0.1:8080/status/risk
Invoke-WebRequest http://127.0.0.1:8080/metrics | Select-Object -Expand Content
```

Readiness is false for stale data, a disconnected stream, incomplete
reconciliation, unavailable persistence, missing protection, or an active risk
lock.

### Restart an interrupted integrated cycle

Use `RESTORE_ONLY` only for a cycle originally created by this integrated
runner. Keep the same `state/demo.sqlite3`, journal database, and kill-switch
file. Never delete or replace state to bypass reconciliation.

```powershell
$env:ETH_HEDGE_ENVIRONMENT="DEMO"
$env:RUN_BYBIT_DEMO_MUTATIONS="FULL_STRATEGY_DEMO"

python -m eth_credit_hedge.interfaces.demo_strategy_runner preflight `
  --cycle-mode RESTORE_ONLY `
  --cycle-id "<INTEGRATED_CYCLE_ID>" `
  --run-seconds 900 `
  --shutdown-policy CLOSE_ALL

python -m eth_credit_hedge.interfaces.demo_strategy_runner run `
  --cycle-mode RESTORE_ONLY `
  --cycle-id "<INTEGRATED_CYCLE_ID>" `
  --run-seconds 900 `
  --shutdown-policy CLOSE_ALL

Remove-Item Env:RUN_BYBIT_DEMO_MUTATIONS
```

After shutdown, require `close_verified=true`, no final linear or option
positions owned by the cycle, exact reconciliation, and a saved evidence hash.
An acknowledgement is never proof of a fill or a flat account.

## Mainnet shadow acceptance — public data, zero orders

This is the highest safe live tier. It watches public mainnet ETH data, records
hypothetical decisions, replays the capture offline, and rejects any mismatch:

```powershell
python -m eth_credit_hedge.interfaces.mainnet_shadow_runner `
  --attempt-seconds 15 `
  --maximum-attempts 12
```

A successful run prints JSON containing:

- `"accepted": true`;
- equal `decision_digest` and `replay_digest`;
- equal nonzero `recorded_intents` and `reproduced_intents`;
- zero values for all acceptance metrics;
- `"external_order_mutations_enabled": false`;
- `"trading_adapter_constructed": false`.

The raw observations are written to `artifacts/shadow-mainnet-*.jsonl`. A timeout
means no approved downward crossing occurred within the bounded attempts; it is
not permission to weaken freshness or risk gates.

## D3-D6 Bybit demo burn-in — places demo orders

> **Warning:** The commands in this section open and close positions on Bybit
> demo. They may change the demo account margin mode, persist exchange-linked
> state in `state/demo.sqlite3`, and leave the demo option spread open for later
> stages. Never use mainnet credentials. Never run stages concurrently.

Before every stage:

1. Use a dedicated demo account and the demo-only variables shown above.
2. Confirm the exchange account and local `state/demo.sqlite3` describe the same
   orders and positions. Do not delete the database to hide a mismatch.
3. Confirm the sealed limits in
   `src/eth_credit_hedge/config/environments/demo.toml` are acceptable.
4. Ensure an operator is watching the account and can intervene.
5. Stop after any error and reconcile exchange state before retrying.

Run the stages in order. Each exact token authorizes only its matching stage:

### D3 — manual one-level protected lifecycle

```powershell
$env:RUN_BYBIT_DEMO_MUTATIONS="D3_MANUAL_ONE_LEVEL"
python -m eth_credit_hedge.interfaces.demo_runner d3-manual
Remove-Item Env:RUN_BYBIT_DEMO_MUTATIONS
```

### D4 — automatic last-trade crossing

```powershell
$env:RUN_BYBIT_DEMO_MUTATIONS="D4_AUTOMATIC_ONE_LEVEL"
python -m eth_credit_hedge.interfaces.demo_runner d4-automatic
Remove-Item Env:RUN_BYBIT_DEMO_MUTATIONS
```

### D5 — two independent baseline levels

```powershell
$env:RUN_BYBIT_DEMO_MUTATIONS="D5_MULTIPLE_BASELINE_LEVELS"
python -m eth_credit_hedge.interfaces.demo_runner d5-multiple
Remove-Item Env:RUN_BYBIT_DEMO_MUTATIONS
```

### D6 — actual-stop-debt full-next-TP recovery

```powershell
$env:RUN_BYBIT_DEMO_MUTATIONS="D6_FULL_NEXT_TP_RECOVERY"
python -m eth_credit_hedge.interfaces.demo_runner d6-recovery
Remove-Item Env:RUN_BYBIT_DEMO_MUTATIONS
```

Successful runners print canonical JSON evidence. Review exchange state as well
as local output; acknowledgement alone is never treated as a fill, and a close
acknowledgement alone never proves the position is flat.

The accepted evidence and failure history are recorded in
[`docs/DEMO_BURN_IN_REVIEWS.md`](docs/DEMO_BURN_IN_REVIEWS.md). Recovery in D6 is
same-level only; distributed recovery remains disabled for deployment.

## Troubleshooting and fail-closed behavior

- **Tests are skipped:** live tests are opt-in. Local `-m "not live"` skips them
  deliberately; use only the matching live section above.
- **Mainnet shadow times out:** no qualifying crossing was observed. Retry later
  without changing the safety profile.
- **An option symbol is missing:** list the current chain again; expired symbols
  are expected to disappear.
- **Clock drift, stale data, or reconciliation fails:** the system blocks new
  entries. Fix the external condition; do not bypass the gate.
- **Integrated `OPEN_NEW` reports durable exposure:** do not delete the database.
  Use `RESTORE_ONLY` only if the cycle was created by the integrated runner. A
  legacy D3–D6 spread has no integrated runtime journal and must be reviewed and
  deliberately closed or migrated before a new integrated cycle can start.
- **A demo stage stops after an uncertain acknowledgement:** inspect exchange
  orders/executions and reconcile using the same database. Do not resubmit with
  a new identity blindly.
- **Protection is missing or the position is unknown:** keep the kill switch
  active and follow [`docs/INCIDENT_PLAYBOOKS.md`](docs/INCIDENT_PLAYBOOKS.md).

## Project map

```text
src/eth_credit_hedge/domain/          exchange-neutral models and invariants
src/eth_credit_hedge/application/     coordinators, reconciliation, risk, shadow
src/eth_credit_hedge/infrastructure/  Bybit, persistence, recording, monitoring
src/eth_credit_hedge/backtesting/     exact and realistic simulated exchanges
src/eth_credit_hedge/interfaces/      demo and shadow runners, health interface
tests/                                local, integration, fault, and opt-in live tests
Plans/                                milestone plans and completion checklist
docs/                                 evidence, operations, incidents, and releases
```

Start with [`Plans/00_MASTER_ROADMAP.md`](Plans/00_MASTER_ROADMAP.md) for the
architecture and milestone order. Operational references:

- [`docs/M12_SHADOW_MODE.md`](docs/M12_SHADOW_MODE.md)
- [`docs/DEMO_BURN_IN_REVIEWS.md`](docs/DEMO_BURN_IN_REVIEWS.md)
- [`docs/KILL_SWITCHES.md`](docs/KILL_SWITCHES.md)
- [`docs/OPERATOR_COMMANDS.md`](docs/OPERATOR_COMMANDS.md)
- [`docs/INCIDENT_PLAYBOOKS.md`](docs/INCIDENT_PLAYBOOKS.md)
- [`docs/ROLLBACK_PROCEDURE.md`](docs/ROLLBACK_PROCEDURE.md)
- [`docs/PILOT_APPROVAL.md`](docs/PILOT_APPROVAL.md)

## Production status

The production pilot remains `NOT APPROVED`, and legal/account eligibility is
`UNCONFIRMED`. Local tests, simulation, demo passes, and shadow acceptance do not
grant production order authority. See
[`docs/PILOT_APPROVAL.md`](docs/PILOT_APPROVAL.md) for the external approvals that
must be attached before a pilot can exist.
