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

Status: PASSED (2026-07-15, automated operator run).

- Account precondition: the isolated demo account was moved through Bybit's
  account-level API to `REGULAR_MARGIN`, which is required for option trading.
- Option execution: protective long `ETH-31JUL26-1750-P-USDT` filled first at
  27.6; short `ETH-31JUL26-1800-P-USDT` then filled at 42.8. Matched quantity
  was 0.1, total option fees were 0.11242587 USDT and execution-derived net
  credit was 1.40757413 USDT.
- Restart recovery: a process failure after the long acknowledgement was
  recovered from exchange execution history. The late fill was imported
  idempotently before the short leg was allowed.
- Protected cycle: `D3-C0004` sold 0.1 ETHUSDT at an actual average of 1867.72.
  The exchange confirmed a reduce-only, close-on-trigger stop at 1877.06 and a
  reduce-only TP with original price 1858.37.
- Protected restart: a new SQLite store and a fresh private snapshot reconciled
  `MATCHED` while the position and both exits were live.
- Controlled exit: the TP was amended marketable and filled 0.1 at 1867.55.
  Entry plus exit fees produced actual realized P&L of -0.1230756 USDT; the
  sibling stop finished `Deactivated`, the linear position was flat and final
  reconciliation was `MATCHED`.
- Fail-closed evidence: an earlier stop-visibility schema mismatch triggered an
  execution-recorded emergency flatten. Its -0.17456965 USDT result and debt
  remain in the durable audit history; no failed run was counted as a pass.
- Review decision: D3 passed. Automatic crossing, multiple levels and recovery
  escalation remained disabled.

## D4 Automatic one-level hedge

Status: PASSED (2026-07-15, automated operator run).

- Trigger source: normalized public WebSocket `LAST_TRADE`; the first trade at
  1868.88 armed a level at the valid 0.01 tick price 1868.87.
- Crossing: a subsequent trade at 1868.85 crossed downward on the same
  connection generation (`1`) at `2026-07-15T02:01:16.903000+00:00`.
- Decision: the one-level coordinator accepted fresh market/option data,
  quantized the proposal and the independent risk engine approved exactly 0.1
  ETH. Quantity, notional, projected stop loss, margin use, liquidation-distance
  precheck and existing audit debt all remained inside the sealed demo limits.
- Execution: cycle `D3-C0005` persisted and submitted one 0.1 ETHUSDT market
  short. Actual average entry was 1868.56 and the exchange confirmed its
  reduce-only, close-on-trigger stop at 1877.91 before the TP was accepted.
- Restart and exit: a fresh SQLite/private-state restart reconciled `MATCHED`;
  the controlled TP path then closed the position with actual P&L of
  -0.1411422 USDT, reconciled the sibling stop and finished flat with final
  status `MATCHED`.
- Review decision: D4 passed. No reconnect boundary, stale event or non-trade
  source was allowed to infer the crossing; multiple levels remained disabled.

## D5 Multiple baseline levels

Status: PASSED (2026-07-15, automated operator run).

- Scope and limits: demo maximum aggregate quantity was explicitly capped at
  0.20 ETH; the existing 500 USDT notional, 30% margin-use, 20% liquidation
  distance and 2 USDT per-entry projected-stop limits remained unchanged.
- Ordered crossings: distinct levels at 1870.46 and 1870.45 were armed above
  market and crossed in level order by one normalized `LAST_TRADE` at 1870.44.
- Actual entries: both persistence-first baseline requests filled 0.1 at
  1870.44. No confirmed debt was added to either requested quantity; aggregate
  exchange exposure reconciled exactly at 0.2 ETH.
- Independent protection: each level received its own reduce-only TP and
  reduce-only, close-on-trigger stop. Both stops were confirmed at 1879.80.
- Restart: a fresh SQLite/private-state reconciliation passed `MATCHED` while
  both levels and all four exits were live.
- Independent exits: both controlled TP paths reached `CLOSED_TP`; actual
  per-level P&L was -0.1452840 and -0.1102770 USDT. Each sibling stop was
  reconciled independently and final aggregate position was flat with status
  `MATCHED`.
- Review decision: D5 passed. Baseline concurrency is enabled only within the
  finite demo cap; recovery escalation and distributed recovery stayed off.

## D6 Full-next-TP recovery

Status: PASSED (2026-07-15, automated operator run).

- Actual stop debt: cycle `D3-C0016` sold the 0.1 ETH baseline at an actual
  average of 1876.38. Its reduce-only stop was hosted at 1876.76 and filled at
  1876.82. Entry, stop loss and execution fees produced 0.2504260 USDT of
  confirmed debt; projected debt did not affect the recovery quantity.
- Finite sizing: `FULL_NEXT_TP` requested 0.191064 ETH from the 0.275 USDT zone
  budget plus confirmed debt over the 2.75 USDT TP distance. Quantity rounded
  upward to the valid 0.20 ETH step and stayed inside the sealed 0.20 ETH,
  500 USDT notional and 2 USDT projected-stop caps.
- Rejection behavior: the same plan was evaluated against a deliberately
  stricter finite quantity cap. It sent no order, allocated no debt and locked
  the level to `CLOSE_OPTION_STRATEGY` rather than silently reducing quantity.
- Same-level trigger: normalized public `LAST_TRADE` crossed downward at
  1876.37 on connection generation 1 at
  `2026-07-15T04:51:09.881000+00:00`. Stale segments, reconnect boundaries and
  transient option-quote timeouts could not authorize the recovery entry.
- Recovery execution: the persistence-first 0.20 ETH request filled at an
  actual average of 1876.40. Bybit confirmed a reduce-only, close-on-trigger
  stop at 1885.79 and a reduce-only TP at 1873.62; a fresh store/private-state
  restart reconciled `MATCHED` while both exits were live.
- Settlement: the TP filled 0.20 ETH at 1873.62. Gross hedge P&L was 0.556 USDT,
  entry and exit fees were 0.2813488 USDT, and actual realized P&L was
  0.2746512 USDT. The net zone budget was zero after those fees, so explicit
  settlement paid the full 0.2504260 USDT allocation and left exactly zero
  remaining debt.
- Final state: the recovery reached `CLOSED_TP`, its sibling stop reconciled,
  the ETHUSDT position was flat and final reconciliation was `MATCHED`.
- Fail-closed evidence: non-passing attempts exposed long-wait clock expiry,
  transient public quote timeouts, host interruption and recovery-exit timeout.
  The runner now renews clock samples, retries quote timeouts only with crossing
  state reset, and rolls recorded emergency recovery losses into unallocated
  debt idempotently on restart. No failed attempt was counted as a pass.
- Review decision: D6 passed. Recovery remained same-level only; distributed
  cross-level recovery and mainnet order mutation stayed disabled.
