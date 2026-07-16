# Milestone 1 Strategy-Math Defect Closure

This audit is offline-only and does not authorize Bybit demo or mainnet activity.

- Active spacing mode: `LEVEL_COUNT`
- Active stop mode: `ENTRY_PERCENT`
- Active valuation mode: `EXPIRATION`
- Remaining legacy formula callers: none
- Golden fixtures: PASS

## Plan 10 findings

- **D-001** — CLOSED: explicit price modes and successful/rejected true DELTA_STEP evidence
- **D-002** — CLOSED: explicit stop modes; ENTRY_PERCENT remains the approved default
- **D-003** — CLOSED: baseline and confirmed-debt recovery include expected costs and quantization
- **D-004** — NARROWED: planning and confirmed stop-debt inputs include signed funding; Milestone 2 must implement the authoritative combined ledger

## Milestone 2 handoff

Milestone 2 must implement the authoritative combined option/hedge/funding/slippage ledger and reconcile it from raw fills.
