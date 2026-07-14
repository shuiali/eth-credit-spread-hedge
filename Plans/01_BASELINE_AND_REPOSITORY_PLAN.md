# Plan 1 — Freeze the Baseline and Repair the Repository

## Objective

Produce a clean, reproducible repository where the current deterministic strategy is frozen and all tests pass from a fresh environment. Do not add exchange-order code in this phase.

## Step 1 — Record the baseline specification

Create `docs/BASELINE_SPEC.md` with:

- Put-credit-spread expiration payoff.
- Virtual-level generation.
- 0.15% stop formula.
- New downward-cross entry requirement.
- Same-level re-entry rule.
- Full-next-TP recovery formula.
- Premium stop-budget formula.
- Explicit limitation: the premium budget does not guarantee nonnegative combined P&L.

Create the Git tag:

```bash
git tag baseline-v1-deterministic
```

## Step 2 — Move to an installable package

Target:

```text
pyproject.toml
src/
  eth_credit_hedge/
    __init__.py
    core/
    data/
    backtesting/
    visualization/
tests/
  fixtures/
```

Replace imports such as:

```python
from core.credit_spread import CreditSpread
```

with:

```python
from eth_credit_hedge.core.credit_spread import CreditSpread
```

Acceptance:

```bash
pip install -e .
python -c "import eth_credit_hedge"
```

must work from any directory.

## Step 3 — Restore test fixtures

Add:

```text
tests/fixtures/bybit_eth_option_pair.json
```

Include raw response captures for:

```text
GET /v5/market/instruments-info
GET /v5/market/tickers
```

Fixture metadata:

```json
{
  "exchange": "Bybit",
  "captured_at_utc": "...",
  "environment": "mainnet-public",
  "requests": []
}
```

Do not hand-edit quote values after capture without documenting the transformation.

## Step 4 — Create typed configuration

```python
@dataclass(frozen=True)
class StrategyConfig:
    level_count: int
    stop_rate: Decimal
    recovery_mode: RecoveryMode
    lock_policy: LockPolicy
```

Factory:

```python
StrategyConfig.baseline()
StrategyConfig.experimental_floor()
```

Dashboard, historical replay and Monte Carlo must default to baseline.

Until separately approved, demo/shadow/production configuration must enforce:

```text
FULL_NEXT_TP
UNHEDGED
```

## Step 5 — Make pytest the source of truth

Retain `run_scenarios.py` as a readable regression command, but add pytest files:

```text
tests/domain/test_credit_spread.py
tests/domain/test_virtual_levels.py
tests/domain/test_crossing_engine.py
tests/domain/test_recovery.py
tests/domain/test_stop_budget.py
tests/domain/test_accounting.py
tests/backtesting/test_historical.py
tests/backtesting/test_monte_carlo.py
tests/infrastructure/test_bybit_public_fixtures.py
```

Required cases:

```text
start exactly at entry then fall
remain below without duplicate entry
one stop then recovery
two stops then recovery
projected stop budget rejection
locked level can finish below zero
large segment crosses entry/TP/next entry
TP executes before next entry at shared boundary
event-price accounting snapshots
coarse and expanded path minimum equality
invalid premium greater than spread width
no level below long put
empty path rejected
reusing an accounting engine rejected
```

## Step 6 — Add deterministic event JSON

Each event needs stable serialization:

```json
{
  "event_version": 1,
  "sequence": 12,
  "event_type": "STOP",
  "level_id": 1,
  "price": "3004.5",
  "quantity": "1.225",
  "realized_pnl": "-5.5125"
}
```

This becomes the format for replay and persistence.

## Step 7 — Add code-quality gates

Add development dependencies:

```text
pytest
pytest-cov
ruff
mypy
```

Apply strict typing first to:

```text
domain/core
exchange parsers
risk
persistence
reconciliation
```

Visual rendering can be relaxed initially.

## Step 8 — Add continuous integration

CI matrix:

```text
Python 3.11
Python 3.12
```

Jobs:

```text
install
compileall
ruff
mypy
pytest
```

Main branch must not merge while CI fails.

## Step 9 — Add configuration validation

Reject:

```text
level_count <= 0
stop_rate <= 0
recovery_tp_count <= 0
long strike >= short strike
quantity <= 0
credit < 0
credit > spread width × quantity
tick size <= 0
```

Parse environment variables once into a typed config object.

## Acceptance gate

- [ ] Fresh clone installs.
- [ ] Fixture is present.
- [ ] No manual `PYTHONPATH` manipulation.
- [ ] All deterministic scenarios pass.
- [ ] All pytest tests pass.
- [ ] Lint passes.
- [ ] Core type checks pass.
- [ ] Baseline defaults are enforced.
- [ ] Experimental modes cannot activate accidentally.
- [ ] Baseline tag and specification exist.
