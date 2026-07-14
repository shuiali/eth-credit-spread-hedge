# M11 Realistic Simulation and Fault Validation

M11 keeps the deterministic hedge engine as the `EXACT_REFERENCE` oracle and adds
a separately labeled `SIMULATED_EXCHANGE` mode. Simulated results are never merged
with exact results; comparison reports retain the mode and model label for every
run.

## Implemented surface

- Seeded acknowledgement, visibility, and fill delays.
- Seeded partial fills, rejections, uncertain acknowledgements, duplicate private
  events, and reordered delivery.
- Persistence-first crash and database-failure checkpoints.
- Private and public disconnects, stale intervals, REST execution recovery, and
  outage price discontinuities.
- First-available-price entry and stop gap execution.
- Perpetual spread, entry/stop slippage, entry/TP/stop fees, and signed funding
  cash flow as separate values.
- Executable option entry and liquidation at bid/ask with partial fills and fees.
- Exact normalized-capture replay and enriched CSV inputs for trades, mark/index,
  option bid/ask/mark/IV, funding, and instrument status.
- Seeded jump diffusion, volatility clustering, regime switching, historical
  bootstrap, V-shaped, and repeated-oscillation paths.
- Explicit IV-rise, spot-down/IV-down, skew, and near-expiry option scenarios.
- Strategy, execution, and operational reports covering every Plan 6 metric.
- Immutable predeclared safety thresholds and tail-risk-aware baseline comparison.

## Reproducibility and accounting

Every exchange event has a deterministic sequence and canonical JSON form. A
SHA-256 digest identifies a complete simulated event ledger. Replaying only the
ledger reconstructs gross hedge P&L, role-specific fees, funding, and net hedge
P&L; equal seeds and inputs produce equal ledgers and digests.

Funding is stored as a signed cash flow: positive means cash received by the
simulated short and negative means cash paid. Spread and slippage are already
embedded in execution prices and are also exposed separately for attribution;
they are not subtracted a second time from realized P&L.

## Safety boundary

The simulator never contacts Bybit and cannot place real orders. Demo, shadow,
and production deployment gates remain separate Plan 7 work.
