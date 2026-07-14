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
- [ ] Spread lifecycle.
- [ ] Actual credit from fills.
- [ ] Mark P&L.
- [ ] Liquidation P&L.
- [x] Expiration P&L retained.
- [ ] Partial-fill policy.
- [ ] Protective long first.
- [ ] Quote freshness.
- [ ] Expiry cutoff.

## Instruments and public data

- [ ] Option filters.
- [ ] Perpetual filters.
- [ ] Quantization.
- [ ] Minimum notional.
- [ ] Risk recalculation after rounding.
- [ ] Trigger source selected.
- [ ] Ticker/trade stream.
- [ ] Order-book snapshot/delta.
- [ ] Reconnect supervision.
- [ ] Stale-data gate.
- [ ] Historical recorder.

## Private adapter

- [ ] Authentication.
- [ ] Time sync.
- [ ] Place/amend/cancel.
- [ ] Open orders/history.
- [ ] Positions/wallet.
- [ ] Private order stream.
- [ ] Execution stream.
- [ ] Position stream.
- [ ] Idempotent client IDs.
- [ ] Execution deduplication.

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

- [ ] Event journal.
- [ ] Event versions.
- [ ] Snapshots.
- [ ] Migrations.
- [ ] Intent persisted before request.
- [ ] Execution persisted atomically.
- [ ] Startup replay.
- [ ] Exchange query.
- [ ] Order comparison.
- [ ] Position comparison.
- [ ] Missing protection repair.
- [ ] Unknown state suspension.

## Risk

- [ ] Independent risk engine.
- [ ] Maximum quantity.
- [ ] Maximum notional.
- [ ] Maximum margin use.
- [ ] Minimum liquidation distance.
- [ ] Maximum debt.
- [ ] Maximum stop.
- [ ] Cycle loss.
- [ ] Daily loss.
- [ ] Entries per level.
- [ ] Active-level limit.
- [ ] Stale-data veto.
- [ ] Reconciliation veto.
- [ ] Explicit locked-level policy.

## Multiple levels and recovery

- [ ] Multiple baseline levels on demo.
- [ ] Concurrent exits.
- [ ] Shared boundaries.
- [ ] Local full-next-TP recovery.
- [ ] Actual debt.
- [ ] Quantized recovery.
- [ ] Risk rejection handled.
- [ ] Distributed recovery remains disabled.

## Realistic simulation

- [ ] Simulated exchange.
- [ ] Delays.
- [ ] Partial fills.
- [ ] Duplicates.
- [ ] Reordering.
- [ ] Rejections.
- [ ] Spread.
- [ ] Fees.
- [ ] Funding.
- [ ] Slippage.
- [ ] Gaps.
- [ ] Option bid/ask.
- [ ] Crash injection.
- [ ] Database failure.
- [ ] Network failure.
- [ ] Jump/regime stress.
- [ ] Predeclared thresholds.

## Operations

- [ ] Environment separation.
- [ ] Shadow mode.
- [ ] Health endpoints.
- [ ] Structured logs.
- [ ] Metrics.
- [ ] Alerts.
- [ ] Soft pause.
- [ ] Strategy close.
- [ ] Emergency flatten.
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
