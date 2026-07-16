# Milestone 1 — Authoritative Strategy-Math Contract

## Purpose

Milestone 1 corrects the strategy mathematics before any additional Bybit demo,
runtime, dashboard, or mainnet work.

The current repository has three critical formula problems:

1. a USD/ETH price interval is called `delta_step`;
2. the runtime stop is 15% of price spacing instead of the previously approved
   0.15% of entry price;
3. baseline and recovery sizing use gross TP distance and omit fees, funding,
   spread, and slippage.

This milestone creates one authoritative mathematical contract used by the exact
engine, simulated runtime, and eventual live runtime.

## Mandatory freeze

Before implementation:

```powershell
git status
git branch --show-current
git log -1 --oneline
```

The current remediation branch must already contain the Plan 9 and Plan 10 work.

Do not:

- reset to D6;
- amend the snapshot commit;
- add demo credentials;
- run demo mutations;
- modify exchange adapters;
- modify dashboard design;
- implement distributed recovery;
- implement `BREAKEVEN_FLOOR`;
- add mainnet code.

## Deliverables

Milestone 1 consists of six plans:

1. `01_MATH_CONTRACT_AND_UNITS_PLAN.md`
2. `02_SPACING_MODES_PLAN.md`
3. `03_STOP_GEOMETRY_PLAN.md`
4. `04_COST_AWARE_SIZING_AND_RECOVERY_PLAN.md`
5. `05_GOLDEN_TESTS_AND_MIGRATION_PLAN.md`
6. `06_RUNTIME_INTEGRATION_AND_ACCEPTANCE_PLAN.md`

Implement them in order.

## Target architecture

Create a dedicated package:

```text
src/eth_credit_hedge/domain/strategy_math/
├── __init__.py
├── units.py
├── contracts.py
├── valuation.py
├── spacing.py
├── stops.py
├── costs.py
├── sizing.py
├── recovery.py
├── quantization.py
└── errors.py
```

No module outside this package may independently calculate:

```text
level boundaries
zone option-loss budget
stop distance
baseline quantity
recovery quantity
projected TP profit
projected stop loss
coverage or undercoverage
```

Existing implementations must call this package through one public façade.

## Required public façade

```python
class StrategyMathEngine:
    def build_levels(
        self,
        spread: OptionSpreadState,
        market: OptionValuationContext,
        spacing: SpacingConfig,
        stop: StopConfig,
    ) -> tuple[LevelMath, ...]: ...

    def size_baseline(
        self,
        level: LevelMath,
        costs: ExecutionCostContext,
        instrument: InstrumentRules,
    ) -> SizingResult: ...

    def size_recovery(
        self,
        level: LevelMath,
        confirmed_debt: Money,
        costs: ExecutionCostContext,
        instrument: InstrumentRules,
    ) -> SizingResult: ...
```

## Hard invariants

```text
Price spacing is never called delta spacing.
All values declare units.
DELTA_STEP requires a real option-delta source.
Terminal payoff alone cannot satisfy DELTA_STEP.
Stop mode is explicit.
Costs are included before sizing.
Risk uses quantized submitted quantity.
Recovery uses confirmed debt.
Rounding undercoverage is reported.
A rejected recovery is never described as fully recovered.
```

## Commit sequence

Use one commit per plan:

```text
M1.1 Add authoritative math contracts and units
M1.2 Add explicit spacing modes
M1.3 Add explicit stop geometry
M1.4 Add cost-aware sizing and recovery
M1.5 Add golden fixtures and migrate legacy formulas
M1.6 Connect math engine to integrated simulation
```

After every commit:

```powershell
python -m compileall -q src tests
ruff check .
mypy src
pytest -q -m "not live"
git status
git add -A
git commit -m "<message>"
git push
```

## Milestone acceptance

Milestone 1 passes only when:

- all six plans pass;
- the old ambiguous formulas are no longer authoritative;
- exact, simulated, and integrated paths call the same math engine;
- golden tests use independently calculated expected values;
- changing a spacing, stop, or cost setting changes the expected runtime output;
- the full integrated simulation remains disabled from demo but passes offline;
- Plan 10 formula defects D-001 through D-004 are closed or explicitly narrowed.

Do not proceed to the combined ledger milestone until this acceptance gate passes.
