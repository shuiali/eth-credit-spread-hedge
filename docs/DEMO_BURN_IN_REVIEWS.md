# Demo Burn-In Review Templates

Every status below remains `NOT RUN` until attached evidence comes from the
separately scoped demo environment. A code or simulator test is not demo evidence.

## D1 Public data

Status: PASSED (2026-07-14, automated operator run).

- Live public smoke: option chain, ETHUSDT instrument/order book, trade stream,
  and a synchronized 20-event order-book stream passed.
- Burn-in window: 60 seconds with ETHUSDT trades, ticker, depth-50 order book,
  and selected option `ETH-15JUL26-1775-P-USDT` ticker.
- Capture: 31,479 normalized records; all four streams produced data and every
  source hash had 64 hexadecimal characters.
- Counts: 2,918 trades, 506 perpetual tickers, 100 book snapshots, 27,878 book
  deltas, and 77 option tickers.
- Local reconnect evidence: disconnect/reconnect resubscription increments the
  connection generation and requires a new book snapshot before deltas apply.
- Ignored local evidence SHA-256:
  `7444218a3d325aea7f541a12e883e2967a86047e641aa9934b0c1ec2f106b57f`.
- Review decision: D1 passed. This provides no private or order authority.

## D2 Read-only private state

Status: PASSED (2026-07-15, automated operator run).

- Credential scope: isolated `BYBIT_API_KEY_DEMO` / `BYBIT_API_SECRET_DEMO`
  variables, sealed by the adapter to `https://api-demo.bybit.com` and
  `wss://stream-demo.bybit.com/v5/private`.
- Clock sample: +154 ms offset, 412 ms round trip and 206 ms uncertainty; all
  remained inside the configured signed-request gates.
- Signed REST snapshot: zero open orders, zero recent orders, zero executions,
  one flat position row, zero nonzero positions and one UNIFIED wallet.
- The demo account's isolated-margin response omitted account-wide available
  balance. The parser records this as unavailable rather than zero, matching the
  current Bybit V5 contract.
- Exact empty-local-state reconciliation passed with zero differences.
- Private WebSocket authentication passed; new entries remained blocked until
  the authenticated connection generation was explicitly marked reconciled.
- Review decision: D2 passed. No mutation was issued by this stage.

## D3 Manual one-level hedge

Status: NOT RUN. Record persisted intent, fill, protection, exit, actual P&L,
restart evidence, operator, and review decision.

## D4 Automatic one-level hedge

Status: NOT RUN. Record LAST_TRADE crossing, quantization, risk decision, complete
lifecycle, restart evidence, operator, and review decision.

## D5 Multiple baseline levels

Status: NOT RUN. Record ordered crossings, aggregate position reconciliation,
independent exits, no recovery escalation, operator, and review decision.

## D6 Full-next-TP recovery

Status: NOT RUN. Record actual stop debt, quantized recovery, finite risk limits,
allocation/settlement, rejection behavior, operator, and review decision.
