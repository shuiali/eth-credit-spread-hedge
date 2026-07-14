# M12 Environment Isolation and Shadow Mode

The six deployment environments are explicit, package-owned TOML profiles:

```text
LOCAL_EXACT
LOCAL_SIMULATED
DEMO
SHADOW_MAINNET
PRODUCTION_PILOT
PRODUCTION
```

Each profile uses a separate SQLite path and, when external access is required,
a separate credential-variable scope. Profiles contain variable names only and
never load or serialize secret values. Demo is sealed to demo hosts; shadow,
pilot, and production are sealed to mainnet hosts. `SHADOW_MAINNET` cannot enable
external order mutations.

Startup fails closed when migrations do not match, the database or kill switch
is unavailable, clock drift exceeds the profile limit, reconciliation is
incomplete, or the required credential scope is unavailable. All refusal reasons
are returned together.

`ShadowModeService` has no trading-port dependency. It consumes only normalized
last-trade triggers, health state, instrument rules, virtual levels, and the
independent risk engine. Each crossing records desired and quantized quantity,
risk approval or veto, hypothetical entry/TP/stop, expected TP P&L, and projected
stop loss as canonical JSONL. Replaying the same observations through a fresh
service must produce identical intent objects and SHA-256 decision digests.

The market-data-only shadow runner records the normalized trigger plus the exact
health and risk snapshots used for each decision. Loading that JSONL capture and
replaying it through a fresh service must reproduce the complete ordered intent
set and aggregate digest. The runner has no trading-port field or constructor
argument.

This milestone proves local shadow determinism and the zero-order architecture.
The live mainnet shadow burn-in gate remains open until rotated, separately
permissioned shadow credentials and an operator-approved observation window are
available.
