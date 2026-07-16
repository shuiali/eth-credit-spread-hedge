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

- [x] Trigger.
- [x] Persist intent.
- [x] Entry submission.
- [x] Confirm actual fill.
- [x] Aggregate partial fills.
- [x] Reconcile position.
- [x] Create stop.
- [x] Confirm stop.
- [x] Create TP.
- [x] Confirm TP.
- [x] Process TP.
- [x] Process stop.
- [x] Reconcile sibling exit.
- [x] Actual P&L and debt.
- [x] Restart recovery.

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

- [x] Multiple baseline levels on demo.
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
- [x] Incident playbooks.
- [x] Rollback tested.

## Deployment gates

- [x] Public data demo passed.
- [x] Read-only private passed.
- [x] Manual one-level demo passed.
- [x] Automatic one-level demo passed.
- [x] Restart demo passed.
- [x] Multiple levels passed.
- [x] Recovery passed.
- [x] Mainnet shadow passed.
- [ ] Finite limits approved.
- [ ] Pilot config approved.
- [ ] Legal/account eligibility confirmed.
- [x] No zero-loss guarantee represented.

## Integrated demo runtime

- [ ] One command runs the complete approved strategy on Bybit demo.
- [x] A non-mutating `preflight` command proves the exact demo capability set.
- [x] Demo uses REST mutations, private demo streams, and mainnet public streams.
- [x] WebSocket order entry is rejected for demo.
- [x] Exact option symbols and execution bounds are required for a new cycle.
- [x] Internal lots reconcile correctly to the aggregate ETHUSDT exchange position.
- [x] A command-level simulated-exchange test proves the complete composition root.
- [x] Mainnet remains a separate disabled runner until production approvals exist.
- [x] The command is bounded, explicitly mutation-gated, and demo-host sealed.
- [x] Option spread open and close both use confirmed actual fills.
- [x] All configured virtual levels participate in ordered live crossings.
- [x] Multiple baseline levels and same-level recovery share one durable state.
- [x] Every active hedge has confirmed exchange-hosted protection.
- [x] Private reconnect and restart require reconciliation before new entries.
- [x] Risk state is authoritative rather than staged/test constants.
- [x] Kill switches, health, metrics, logs, and alerts are live-wired.
- [x] Timeout, `Ctrl+C`, and failure close and reconcile the whole strategy.
- [x] Simulated runtime fault acceptance passes.
- [ ] Bounded Bybit demo acceptance passes and is documented.
