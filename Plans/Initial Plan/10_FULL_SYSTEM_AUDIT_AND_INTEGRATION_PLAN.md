# Plan 10 — Full-System Audit, Formula Verification, and Integrated Test Runtime

## Status

**Stop further feature development until this audit is complete.**

The objective is to establish, with evidence:

1. what has been implemented;
2. which code is actually used by the runtime;
3. whether every formula matches the intended strategy;
4. whether user-facing settings alter the intended behavior;
5. whether fees and actual fills are included correctly;
6. whether one command exercises the complete system end to end;
7. exactly what remains before Bybit demo and mainnet deployment.

The repository must be treated as untrusted until each requirement is traced to code, tests, runtime coverage, and observable output.

---

# Part I — Immediate conclusions to lock before the audit

## 1. The reported “delta spacing” formula is misnamed

This formula:

```text
delta spacing = spread width / level count
```

is **not delta spacing**. It is underlying-price spacing:

```text
price_step_usd = (short_put_strike - long_put_strike) / level_count
```

Its units are USD per ETH. Delta is not measured in dollars.

The project must distinguish three possible spacing modes:

```python
class LevelSpacingMode(str, Enum):
    PRICE_STEP = "PRICE_STEP"
    EQUAL_OPTION_LOSS = "EQUAL_OPTION_LOSS"
    DELTA_STEP = "DELTA_STEP"
```

### PRICE_STEP

```text
entry_i = short_strike - i × price_step_usd
```

### EQUAL_OPTION_LOSS

Choose boundaries so that:

```text
option_value(entry_i) - option_value(tp_i) = target_zone_loss_usd
```

### DELTA_STEP

Choose prices by solving:

```text
net_option_delta(price_i) = starting_delta - i × delta_step
```

A true `DELTA_STEP` implementation requires a pre-expiry option valuation/Greeks model. An expiration-only put credit-spread payoff is piecewise linear and has essentially constant slope between the strikes, so it cannot honestly generate many different delta levels.

If the code currently divides strike width by level count and calls that `delta_step`, mark it as a **critical terminology and strategy defect**.

## 2. The reported stop formula may be a strategy change

These two rules are different:

```text
A. stop_distance = 0.0015 × entry_price
B. stop_distance = 0.15 × price_step
```

Rule A means 0.15% of the ETH entry price. Rule B means 15% of the distance between virtual levels.

The simplified validated strategy used Rule A. If the current runtime uses Rule B, that is not a refactor; it is a changed trading rule. It must be explicitly approved and revalidated.

Use separate configuration names:

```toml
[hedge.stop]
mode = "ENTRY_PERCENT"
rate = "0.0015"
```

or:

```toml
[hedge.stop]
mode = "PRICE_STEP_FRACTION"
fraction = "0.15"
```

Never use one ambiguous key for both.

## 3. “Hedge quantity equals option quantity” is only conditionally true

It is true only in the idealized terminal-payoff model when:

```text
zone option loss = option quantity × zone width
TP distance = zone width
fees = 0
funding = 0
slippage = 0
```

The general formula is:

```text
net_tp_profit_per_unit =
    expected_entry_price
    - expected_tp_fill_price
    - entry_fee_per_unit
    - tp_fee_per_unit
    - expected_funding_per_unit
    - expected_entry_slippage_per_unit
    - expected_tp_slippage_per_unit
```

```text
desired_quantity =
    (
        zone_option_loss_budget
        + allocated_recovery_debt
        + explicit_cost_buffer
    )
    / net_tp_profit_per_unit
```

Then quantize to Bybit’s quantity step and recalculate actual expected coverage.

Therefore, a 0.1 ETH option spread does **not always** require exactly a 0.1 ETH hedge.

## 4. Fees must be included in both sizing and debt

Required costs:

```text
option long entry fee
option short entry fee
option close fees
perpetual entry fee
perpetual TP fee
perpetual stop fee
funding
spread/slippage
```

Confirmed recovery debt after a stop should be based on actual fills:

```text
confirmed_recovery_debt +=
    absolute(actual realized trading loss)
    + allocated entry fees
    + stop fees
    + funding attributable to the hedge
    + measured slippage cost
```

---

# Part II — Freeze and audit setup

## 5. Freeze the repository

```bash
git status
git add .
git commit -m "Freeze before full-system audit"
git tag pre-audit-freeze
git switch -c audit/full-system
```

Audit rules:

- Do not add new strategy features.
- Do not delete failing tests.
- Do not change expected values until the intended formula is confirmed.
- Every correction must reference a requirement or approved formula.
- Mainnet mutations remain disabled.
- Demo mutations remain disabled until integrated simulation passes.

---

# Part III — One audit command

## 6. Implement a non-mutating audit command

```powershell
python -m eth_credit_hedge.interfaces.project_audit `
  --repo . `
  --plans docs/plans `
  --config config/audit.toml `
  --output artifacts/audit
```

The command must:

1. inventory every tracked source file;
2. record SHA-256 hashes and line counts;
3. map every plan requirement to code and tests;
4. run compilation, linting, typing, and pytest;
5. run branch coverage;
6. identify apparently unused and unreachable code;
7. trace every configuration value to its consumer;
8. execute the complete system against `SimulatedExchange`;
9. generate worked formula examples;
10. produce a go/no-go report.

It must not place orders or use private exchange credentials.

Exit codes:

```text
0 = audit passed
1 = correctness failures found
2 = audit tooling failed
3 = traceability incomplete
4 = integrated runtime missing
```

## 7. Required audit outputs

```text
artifacts/audit/
├── 00_AUDIT_SUMMARY.md
├── 01_REPOSITORY_MANIFEST.csv
├── 02_REQUIREMENT_TRACEABILITY_MATRIX.csv
├── 03_ARCHITECTURE_AND_COMPOSITION.md
├── 04_RUNTIME_REACHABILITY.md
├── 05_CONFIGURATION_USAGE.md
├── 06_FORMULA_AND_UNITS_AUDIT.md
├── 07_ACCOUNTING_AUDIT.md
├── 08_EXCHANGE_INTEGRATION_AUDIT.md
├── 09_TEST_AND_COVERAGE_AUDIT.md
├── 10_DEAD_AND_DUPLICATE_CODE.md
├── 11_INTEGRATED_SCENARIO_RESULTS.md
├── 12_LIVE_READINESS_GAPS.md
├── audit_summary.json
├── runtime_events.jsonl
├── coverage.xml
├── coverage_html/
└── dependency_graph.dot
```

Allowed statuses:

```text
PASS
FAIL
PARTIAL
NOT IMPLEMENTED
NOT REACHABLE
EXPERIMENTAL
```

A requirement cannot be `PASS` merely because a class with a similar name exists.

---

# Part IV — Every-file and every-line audit

## 8. Repository manifest

Include every tracked source file except explicitly listed generated/vendor directories.

For each file record:

```text
path
SHA-256
line count
module/layer
imports
imported by
tests referencing it
integrated runtime coverage
audit status
```

## 9. Function-level inspection

For every class, function, method, constant, branch, exception handler, database mutation, and exchange mutation, record:

```text
purpose
inputs and units
outputs and units
state changed
exceptions
callers
tests
runtime reachability
assumptions
```

This is the practical, checkable equivalent of a line-by-line audit.

## 10. Duplicate and superseded implementations

Search for duplicate implementations of:

```text
CreditSpread
HedgeEngine
CrossingEngine
RiskEngine
level generation
recovery sizing
stop budget
Bybit public client
Bybit private client
option entry
reconciliation
demo runner
strategy runner
```

Report:

- old modules still imported;
- old modules no longer imported;
- two formulas for the same value;
- test-only code accidentally used in runtime;
- configuration defaults that override user values;
- classes implemented but never composed.

Do not delete duplicates until one implementation is selected as authoritative and fully tested.

---

# Part V — Requirement traceability

## 11. Build a matrix for Plans 0–9

One row per requirement:

```text
requirement ID
plan and section
exact requirement
implementation file
class/function
configuration key
unit test
integration test
runtime command
runtime evidence
status
defect ID
```

A requirement is complete only when all four exist:

```text
implementation
test
runtime reachability
observable evidence
```

This matrix answers: “Was this feature only coded, or is the whole bot actually using it?”

---

# Part VI — Architecture and composition audit

## 12. Identify the real composition root

The audit must identify the process owner and answer:

```text
Who owns public market data?
Who owns private streams?
Who owns option entry and close?
Who owns virtual-level crossings?
Who owns perpetual orders?
Who owns protection?
Who owns reconciliation?
Who owns risk state?
Who owns persistence?
Who owns shutdown?
```

If no single CLI/bootstrap composes these services, report:

```text
INTEGRATED RUNTIME NOT IMPLEMENTED
```

## 13. Generate the dependency graph

Show:

```text
CLI
bootstrap
application services
domain services
ports
Bybit adapters
database
health/metrics
read-only dashboard
```

Flag:

- circular dependencies;
- dashboard code inside execution loop;
- backtester used as a live exchange adapter;
- more than one owner of the same order/position;
- infrastructure JSON fields leaking into domain calculations.

## 14. Runtime feature reachability

Run the complete simulation under branch coverage and show whether these features execute:

```text
actual option-fill credit
mark/liquidation P&L
instrument quantization
configured spacing mode
public trigger handling
private execution processing
persistence-first intents
reconciliation
risk veto
stop protection
same-level recovery
fees/funding accounting
health state
kill switch
graceful close
```

Any major feature with zero integrated coverage is `NOT INTEGRATED`.

---

# Part VII — Configuration audit

## 15. Inventory all configuration sources

Search:

```text
environment variables
CLI arguments
TOML/YAML/JSON
hard-coded constants
constructor defaults
test overrides
```

Create one canonical typed configuration model.

For every setting record:

```text
name
type
units
default
source
validation
consumer
runtime evidence
```

## 16. Perturbation tests

For every user-facing setting:

1. run configuration A;
2. run configuration B with only that setting changed;
3. prove the intended output changes;
4. prove unrelated output remains unchanged.

Required tests:

```text
Changing price_step changes level prices.
Changing level_count changes normalized price_step.
Changing delta_step changes solved delta targets and prices.
Changing stop rate changes stops, not levels.
Changing fees changes quantity and P&L.
Changing option quantity changes loss budget and quantity.
Changing recovery mode changes only recovery allocation.
```

A visible setting with no runtime effect is a critical defect.

---

# Part VIII — Formula and units audit

## 17. Declare units for every value

Examples:

```text
entry_price: USD/ETH
price_step: USD/ETH
quantity: ETH
option_loss_budget: USD
recovery_debt: USD
fee_rate: dimensionless
fee: USD
delta: ETH-equivalent exposure
```

Reject calculations that add or compare incompatible units.

## 18. Zone-loss budget

General formula:

```text
zone_loss_budget =
    option_value(entry_boundary)
    - option_value(tp_boundary)
```

The valuation mode must be explicit:

```text
EXPIRATION
MARK
LIQUIDATION
```

Do not assume `option_quantity × price_step` unless the linear terminal region is explicitly being used.

## 19. Quantity after quantization

After Bybit quantity rounding:

```text
submitted_quantity = quantize(desired_quantity)
```

Recalculate:

```text
expected net TP profit
projected net stop loss
undercoverage or overcoverage
margin/notional
```

The code must not claim exact recovery if rounding makes it impossible.

## 20. Stop and TP formulas

The audit must produce one approved specification stating:

```text
spacing mode
TP definition
stop mode
stop parameter
option valuation mode
cost model
```

No runtime may silently substitute a different rule.

---

# Part IX — Accounting audit

## 21. Required ledger fields

```text
option_realized_pnl
option_mark_pnl
option_liquidation_pnl
hedge_gross_realized_pnl
hedge_open_pnl
option_fees
hedge_fees
funding_pnl
slippage_cost
confirmed_recovery_debt
net_combined_pnl
```

Identity:

```text
net_combined_pnl =
    option_pnl
    + hedge_gross_realized_pnl
    + hedge_open_pnl
    - option_fees
    - hedge_fees
    + funding_pnl
    - slippage_cost
```

Build an independent audit function that recomputes P&L from raw fills and compares it with the application ledger.

## 22. Recovery debt

Projected debt may be shown for risk evaluation, but actual live debt must use confirmed executions and fees.

Track separately:

```text
projected_recovery_debt
confirmed_recovery_debt
allocated_recovery_debt
remaining_recovery_debt
```

---

# Part X — One command to test the whole system

## 23. Implement one integrated simulation command

```powershell
python -m eth_credit_hedge.interfaces.strategy_runner simulate `
  --config config/full_audit.toml `
  --scenario stop_reentry_recovery `
  --output artifacts/full_run `
  --dashboard
```

This command must use the same:

```text
bootstrap
configuration
coordinator
risk engine
persistence
order lifecycle
reconciliation
accounting
health
shutdown
```

as the demo runtime. Only the exchange adapter changes:

```text
SimulatedExchange instead of BybitDemoAdapter
```

## 24. Mandatory end-to-end scenario

The default scenario must cause:

```text
option spread opened from fills
levels generated from configured spacing mode
level armed
downward crossing
baseline hedge entry
partial/full fill handling
TP and stop protection
stop execution
fees recorded
actual recovery debt created
same level re-armed
recovery quantity calculated
recovery entry
recovery TP
debt settled
next level activated
graceful shutdown
option spread close
final reconciliation
```

Required outputs:

```text
event ledger
raw orders and fills
positions
level states
configuration snapshot
formula trace
fee trace
P&L charts
final reconciliation
```

---

# Part XI — Golden independent numerical tests

## 25. Hand-calculated fixtures

Expected values must be calculated independently, not by the production formula.

Required scenarios:

```text
zero-cost ideal baseline
fees-only baseline
stop then recovery
quantity-step rounding
delta spacing rejected under terminal-only model
equal-loss spacing under curved MTM
partial fill
gap through stop
insufficient net TP profit
```

Every scenario must show:

```text
inputs
units
hand calculation
expected events
expected net P&L
expected debt
```

---

# Part XII — Static, dynamic, and test-quality audit

## 26. Static tools

```bash
python -m compileall src tests
ruff check .
mypy src
vulture src --min-confidence 80
```

Vulture results are leads, not automatic deletions.

## 27. Coverage

```bash
coverage run --branch -m pytest
coverage html
coverage xml
```

Also run the integrated CLI under coverage.

Flag:

```text
major service with zero integrated coverage
configuration branch never executed
error path untested
code reachable only from tests
dashboard directly mutating strategy state
```

## 28. Detect false-confidence tests

Reject or rewrite tests where:

- expected values are generated by the same formula under test;
- mocks bypass the actual coordinator;
- final P&L is checked without event order;
- tests invoke services that the runtime never invokes;
- configuration is accepted but ignored;
- acknowledgements are treated as fills.

---

# Part XIII — Exchange and live-readiness audit

## 29. Verify every exchange mutation

For option and perpetual orders, prove:

```text
intent persisted before REST call
unique orderLinkId
acknowledgement is not a fill
execution event changes quantity
partial fills aggregate
duplicate executions are ignored
prices and quantities are quantized
exits are reduce-only
position reconciles
uncertain request is queried before retry
```

## 30. Mainnet remains disabled

The audit is allowed to approve:

```text
integrated simulation
demo preflight
bounded demo mutation
```

It must not approve mainnet until:

```text
integrated runtime complete
demo acceptance complete
shadow acceptance complete
finite limits approved
pilot config approved
legal/account eligibility confirmed
```

---

# Part XIV — Final report

## 31. Required concise summary

`00_AUDIT_SUMMARY.md` must begin with:

```text
WHAT IS IMPLEMENTED
WHAT IS ACTUALLY USED
WHAT IS CODED BUT UNUSED
WHAT IS INCORRECT
WHAT IS UNTESTED
WHAT COMMAND WORKS NOW
WHAT EXACTLY TO CODE NEXT
```

## 32. Definition of complete

- [ ] Every tracked source file is inventoried.
- [ ] Every source function has a caller, test, and reachability status.
- [ ] Every Plan 0–9 requirement is traced.
- [ ] The real composition root is identified.
- [ ] Major coded features are proven reachable or marked unused.
- [ ] Price spacing and delta spacing are separated.
- [ ] True delta spacing requires a real delta model.
- [ ] Stop-distance rule is explicit.
- [ ] Fees and funding are included in sizing/accounting.
- [ ] Independent golden scenarios pass.
- [ ] One command runs the complete strategy in simulation.
- [ ] The same command path is used for demo except for the adapter.
- [ ] Final local and exchange state reconcile.
- [ ] The final report names the next single implementation task.

---

# Recommended implementation order after the audit

Only after the audit report exists:

```text
1. Correct formula/name/config defects.
2. Remove or quarantine unused duplicate implementations.
3. Make the integrated simulation command pass.
4. Add fees and actual-fill accounting if absent.
5. Add demo preflight.
6. Run one-level bounded demo.
7. Run stop/re-entry/recovery demo.
8. Run multiple levels.
9. Complete shadow acceptance.
10. Review production approvals and mainnet composition separately.
```
