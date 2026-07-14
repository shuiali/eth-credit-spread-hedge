# Master Completion Checklist

Check an item only when implementation, tests and acceptance criteria pass.

## Repository

- [x] Installable `src` package.
- [x] Missing fixtures restored.
- [x] Baseline tagged.
- [x] Baseline specification committed.
- [x] Baseline defaults enforced.
- [x] Experimental modes separated.
- [x] All tests pass from fresh clone.
- [x] Lint, types and CI pass.

## Option position

- [x] Contract/quote/fill models.
- [x] Leg positions.
- [x] Spread lifecycle.
- [x] Actual credit from fills.
- [x] Mark P&L.
- [x] Liquidation P&L.
- [x] Expiration P&L retained.
- [x] Partial-fill policy.
- [x] Protective long first.
- [x] Quote freshness.
- [x] Expiry cutoff.

## Instruments and public data

- [x] Option filters.
- [x] Perpetual filters.
- [x] Quantization.
- [x] Minimum notional.
- [x] Risk recalculation after rounding.
- [x] Trigger source selected.
- [x] Ticker/trade stream.
- [x] Order-book snapshot/delta.
- [x] Reconnect supervision.
- [x] Stale-data gate.
- [x] Historical recorder.

## Private adapter

- [x] Authentication.
- [x] Time sync.
- [x] Place/amend/cancel.
- [x] Open orders/history.
- [x] Positions/wallet.
- [x] Private order stream.
- [x] Execution stream.
- [x] Position stream.
- [x] Idempotent client IDs.
- [x] Execution deduplication.

## One-level demo

- [ ] Trigger.
- [ ] Persist intent.
- [ ] Entry submission.
- [ ] Confirm actual fill.
- [ ] Aggregate partial fills.
- [ ] Reconcile position.
- [ ] Create stop.
- [ ] Confirm stop.
- [ ] Create TP.
- [ ] Confirm TP.
- [ ] Process TP.
- [ ] Process stop.
- [ ] Reconcile sibling exit.
- [ ] Actual P&L and debt.
- [ ] Restart recovery.

## Persistence and reconciliation

- [x] Event journal.
- [x] Event versions.
- [x] Snapshots.
- [x] Migrations.
- [x] Intent persisted before request.
- [x] Execution persisted atomically.
- [x] Startup replay.
- [x] Exchange query.
- [x] Order comparison.
- [x] Position comparison.
- [x] Missing protection repair.
- [x] Unknown state suspension.

## Risk

- [x] Independent risk engine.
- [x] Maximum quantity.
- [x] Maximum notional.
- [x] Maximum margin use.
- [x] Minimum liquidation distance.
- [x] Maximum debt.
- [x] Maximum stop.
- [x] Cycle loss.
- [x] Daily loss.
- [x] Entries per level.
- [x] Active-level limit.
- [x] Stale-data veto.
- [x] Reconciliation veto.
- [x] Explicit locked-level policy.

## Multiple levels and recovery

- [ ] Multiple baseline levels on demo.
- [x] Concurrent exits.
- [x] Shared boundaries.
- [x] Local full-next-TP recovery.
- [x] Actual debt.
- [x] Quantized recovery.
- [x] Risk rejection handled.
- [x] Distributed recovery remains disabled.

## Realistic simulation

- [x] Simulated exchange.
- [x] Delays.
- [x] Partial fills.
- [x] Duplicates.
- [x] Reordering.
- [x] Rejections.
- [x] Spread.
- [x] Fees.
- [x] Funding.
- [x] Slippage.
- [x] Gaps.
- [x] Option bid/ask.
- [x] Crash injection.
- [x] Database failure.
- [x] Network failure.
- [x] Jump/regime stress.
- [x] Predeclared thresholds.

## Operations

- [x] Environment separation.
- [x] Shadow mode.
- [x] Health endpoints.
- [x] Structured logs.
- [x] Metrics.
- [x] Alerts.
- [x] Soft pause.
- [x] Strategy close.
- [x] Emergency flatten.
- [ ] Incident playbooks.
- [ ] Rollback tested.

## Deployment gates

- [ ] Public data demo passed.
- [ ] Read-only private passed.
- [ ] Manual one-level demo passed.
- [ ] Automatic one-level demo passed.
- [ ] Restart demo passed.
- [ ] Multiple levels passed.
- [ ] Recovery passed.
- [ ] Mainnet shadow passed.
- [ ] Finite limits approved.
- [ ] Pilot config approved.
- [ ] Legal/account eligibility confirmed.
- [ ] No zero-loss guarantee represented.
