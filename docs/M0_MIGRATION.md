# M0 Migration Notes

## Package imports

The implementation now uses a `src` layout and is installed as
`eth_credit_hedge`. Replace legacy imports with the package namespace:

```text
core.*          -> eth_credit_hedge.core.*
data.*          -> eth_credit_hedge.data.*
backtesting.*   -> eth_credit_hedge.backtesting.*
visualization.* -> eth_credit_hedge.visualization.*
```

Install the project before running launchers or tests:

```powershell
python -m pip install -e ".[dev]"
```

No manual `PYTHONPATH` setting is required.

## Configuration

`StrategyConfig.baseline()` freezes `FULL_NEXT_TP`, `UNHEDGED`, and the
explicit `ENTRY_PERCENT` stop at `0.0015` of entry.
`StrategyConfig.experimental_floor()` is deliberately separate. `RuntimeConfig`
rejects experimental recovery or lock policies for demo, shadow, and production
environments.

`RuntimeConfig.from_env()` reads the `ETH_HEDGE_*` variables documented in
`.env.example` into one immutable object. M0 does not read or use private Bybit
credentials.

## Event persistence contract

`LedgerEvent.to_dict()` and `LedgerEvent.to_json()` emit version-two events.
All `Decimal` values are serialized as strings, enum values are explicit, and
recovery-allocation keys are emitted in stable order. Version two adds spacing
mode, stop mode, stop parameter, and actual stop distance.

## Baseline history

The pre-migration deterministic implementation is preserved by the local Git
tag `baseline-v1-deterministic`. The `.env` file, generated artifacts, caches,
and Graphify outputs are excluded from version control.
