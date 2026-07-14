# ETH Credit-Spread Hedge

Deterministic proof-of-concept for hedging the downside loss of a same-expiry
ETH put credit spread with virtual short-perpetual levels.

## What is implemented

- Exact `Decimal` credit-spread expiry payoff.
- Gap-free virtual levels between the short and long strikes only.
- Ordered tick-segment event processing with TP-before-entry handoffs.
- Explicit entry arming at exact boundaries and after stop/breakeven exits.
- Same-level re-entry, full-next-TP recovery, and distributed recovery.
- Premium-credit stop budget with deterministic `LOCKED` transitions.
- Experimental breakeven-floor entries, disabled in all baseline runners.
- Complete option, realized hedge, open hedge, combined, and incremental P&L.
- Accounting samples at every exact entry, TP, stop, breakeven, and lock trigger.
- Ten named scenarios with complete expected-ledger comparisons.
- Explicit OHLC intrabar reconstruction and persisted historical replay paths.
- Seeded macro-GBM paths expanded into 20,001+ exact $0.10 microticks.
- A calculation-free original-style dashboard with complete risk-detail panels.
- Fixture-first Bybit ETH option parsing, IV/Greeks, and live-chain integration.

Fees, slippage, funding, partial fills, gaps, automatic spread selection, IV
repricing, and live trading remain intentionally excluded.

## Install

The project uses an installable `src` package:

```powershell
python -m pip install -e ".[dev]"
python -c "import eth_credit_hedge"
```

No manual `PYTHONPATH` setting is required.

## Run the gates

```powershell
python -m compileall -q src tests
ruff check .
mypy
python -m pytest -q
python run_scenarios.py
```

## Dashboard

The launcher performs calculations in the backend and passes one completed
payload to the renderer:

```powershell
python dashboard_app.py
python dashboard_app.py --path 3010,2990,3005,2970,2985,2940,2890 --mc-paths 100
python dashboard_app.py --save artifacts/dashboard.png
python dashboard_app.py --mc-paths 0  # disable the Monte Carlo batch
python dashboard_app.py `
  --option-fixture tests/fixtures/bybit_eth_option_pair.json `
  --short-symbol ETH-31JUL26-1750-P-USDT `
  --long-symbol ETH-31JUL26-1650-P-USDT `
  --mc-paths 0
```

The default selected path contains 33,001 ticks. Comma-separated `--path`
values are macro anchors, not direct jumps: every interval is filled with a
seeded, reversing random walk whose adjacent price move is exactly $0.10.

The locked Plan.md behavior remains the `HedgeEngine`, dashboard, and Monte
Carlo default. Once the premium cannot fund another fixed stop, that level locks
and may leave option loss unhedged. The dashboard therefore reports observed
floor failures honestly; the stop budget is not presented as a guaranteed
non-negative combined-P&L floor. `BREAKEVEN_FLOOR` remains an explicit
experimental policy with its own deterministic regression tests.

The compact dashboard now includes price/events, expiration payoff, all five
P&L views, Monte Carlo paths, terminal distribution, recovery debt and stop
budget, per-level quantity/state/attempts/debt, KPI summary, and event log. The
Monte Carlo slider selects one path for emphasis without moving calculations
into the renderer.

## ETH option chain

List the current ETH puts, then rerun with the exact two symbols selected by
the user:

```powershell
python option_chain_app.py
python option_chain_app.py `
  --short-symbol ETH-31JUL26-1750-P-USDT `
  --long-symbol ETH-31JUL26-1650-P-USDT `
  --quantity 1
```

The engine uses mark-to-mark credit because bid/ask execution is postponed.
Natural bid-minus-ask credit is displayed for transparency but is not fed into
the strategy. The parser retains bid/ask/mark IV and delta, gamma, vega and
theta. A 0.1% relative tolerance allows tiny leg-snapshot index differences;
the engine spot is their average. Larger discrepancies are rejected.

## Distributed recovery convention

Debt remains attributed to the level that incurred it. In `DISTRIBUTED` mode,
the source level's successful recovery TP and the next eligible downstream TPs
repay that claim in equal remaining portions. Claims are not pooled, and only
the oldest outstanding claim can augment one TP. This preserves the rule that
a paid zone cannot reopen and earn its option-loss budget twice.
TP-before-next-entry ordering prevents the same debt allocation from being
reserved by two active hedges; deterministic tests enforce one source claim per
TP and oldest-source priority.
