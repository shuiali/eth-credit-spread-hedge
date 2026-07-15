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

Status: NOT RUN. Record orders/executions/positions/wallet reconciliation, clock
drift, credential scope identifier, operator, and review decision.

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
