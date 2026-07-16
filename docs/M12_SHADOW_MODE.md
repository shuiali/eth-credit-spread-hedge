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

## Live mainnet shadow acceptance

Status: LINKED-DELTA REVALIDATION PASSED (2026-07-16).

- The mainnet-bound public runner accepted on attempt 1 and recorded two
  normalized ETHUSDT trade observations with one risk-approved crossing intent.
- Offline replay reproduced exactly one intent. The live decision digest and
  replay digest both equal
  `5ccf1a6c7f034c030553a6e971e7d8ca1977ae26ab28c6abecf24aa63ae25fd9`.
- Contradictory transitions, stale-data approvals, invalid quantization,
  nondeterministic risk decisions and expiry-cutoff violations were all zero.
- The selected spread was long `ETH-31JUL26-1750-P-USDT` and short
  `ETH-31JUL26-1800-P-USDT`, with common expiry
  `2026-07-31T08:00:00+00:00`.
- Capture: `artifacts/shadow-mainnet-20260715T072024553853Z-a1.jsonl` with
  SHA-256
  `3af2d2684fbf64a78e378201946033082ff427e019ce073f578f4b5105f5dfcf`.
- The runner confirmed `external_order_mutations_enabled=false` and
  `trading_adapter_constructed=false`; no private credentials or order authority
  were used.

Review decision: M12 passed for the original captured geometry. The 2026-07-16
linked-delta geometry is revalidated separately below. Production pilot mutation
remains disabled until finite-limit and pilot configuration approval,
legal/account eligibility and signed operator approval are recorded separately.

## Linked-delta shadow revalidation

- The corrected shadow geometry used 0.10 USDT between virtual entries, the same
  0.10 USDT TP distance, and a 0.015 USDT stop distance.
- Attempt 3 recorded one approved intent from 82 observations. Offline replay
  reproduced the same intent and digest
  `ccc76a613e46d54647657e682cfea7acbe5d5e6e19825cd358b412f74c389e1b`.
- Contradictory transitions, stale approvals, invalid quantization,
  nondeterministic decisions, and expiry violations were all zero.
- Capture: `artifacts/shadow-mainnet-20260716T112749152226Z-a3.jsonl`, SHA-256
  `f1e337a983274b5422bdd1d76dcff722b4ee5c29725aa8e31a044f88f410879a`.
- The runner confirmed `external_order_mutations_enabled=false` and
  `trading_adapter_constructed=false`.

Review decision: linked-delta M12 passes. Pilot mutation remains disabled on the
remaining demo burn-in, finite-limit, configuration, eligibility, and signed
operator gates.
