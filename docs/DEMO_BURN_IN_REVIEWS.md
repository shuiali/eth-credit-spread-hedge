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

## Integrated Plan 9 preflight and legacy close

Status: PREFLIGHT PASSED (2026-07-15); integrated order session not yet run.

- The first integrated `OPEN_NEW` preflight refused to continue because durable
  cycle `D3-C0002` still owned the matched 0.1 option spread. Exchange and local
  state reconciled with zero differences, so no state was deleted or fabricated.
- After explicit operator authorization, the bounded close service bought back
  the 0.1 short `ETH-31JUL26-1800-P-USDT` first at 36.5, then sold the 0.1
  protective long `ETH-31JUL26-1750-P-USDT` at 24.1. Fees were 0.05645849 and
  0.05644833 USDT respectively.
- The durable option-exit snapshot reached `CLOSED` with zero quantity on both
  legs. ETHUSDT remained flat, the account had no open orders, exact
  reconciliation reported zero differences, and the kill switch returned to
  `RUNNING` only after those checks passed.
- Delayed Bybit execution-history visibility was detected during the audit. Both
  already-completed fills were imported by execution ID, and the close service
  was tightened so position flatness alone cannot report success without matching
  durable execution evidence. A delayed-visibility restart test now covers this
  boundary.
- The official non-mutating integrated preflight then passed with supported demo
  REST/private-WebSocket capabilities, `REGULAR_MARGIN`, zero option positions,
  zero open orders, current execution schema 6, journal schema 3, and exact
  reconciliation. It issued zero exchange mutations.
- Selected preflight pair: short `ETH-31JUL26-1800-P-USDT`, long
  `ETH-31JUL26-1750-P-USDT`, quantity 0.1, executable net credit 1.20 USDT,
  minimum credit 0.01 USDT, and maximum entry deviation 100 bps.
- Evidence: `artifacts/demo-preflight-20260715T111126322292Z.json`, SHA-256
  `f6360351fc531fecfcc769ac2d2fd433cffbd64e9dd5376fe2f403dda6470c80`.
- Review decision: the non-mutating Plan 9 preflight gate passed. This does not
  approve the selected pair or limits for an order-bearing demo session.

## Integrated one-level order-bearing attempts

Status: NOT PASSED (2026-07-15); account reconciled flat and retry stopped.

- Account-wide reconciliation initially found an unrelated unowned SOLUSDT sell
  stop that symbol-scoped ETHUSDT reads had missed. After explicit operator
  authorization, only exchange order
  `f52edca5-ccb5-4cc2-a7a6-7dac2f2cd244` was cancelled. The private reader,
  capability probe, and preflight now query all USDT-settled linear orders,
  history, executions, and positions. Subsequent preflights reported zero open
  account orders.
- `DEMO-C0001` filled both 0.1 option legs, but normal option fees were
  incorrectly included in the 100 bps leg-price deviation check. The cycle
  failed closed and was later reconciled `CLOSED`. Price deviation now compares
  fee-exclusive gross credit, while fees remain included in the independent
  minimum-net-credit gate.
- `DEMO-C0002` opened the matched spread, then the 60-second server-clock sample
  expired during the bounded runtime. A freshly synchronized cleanup closed both
  legs and proved exact flat reconciliation. The supervised runtime now renews
  the clock every 30 seconds and synchronizes again before shutdown.
- `DEMO-C0003` timed out waiting for the protective long to settle before the
  supervised loop began. Cleanup exposed the same clock boundary during startup.
  Private REST reads and mutations now proactively refresh an existing clock
  sample halfway to expiry, under a single refresh lock. The complete local test
  suite, lint, and type checks passed after this fix. Read-only restore evidence
  proved zero positions and orders: `artifacts/demo-preflight-20260715T120414438491Z.json`,
  SHA-256 `a865cfb893493ba8cb426dae0207f4bf8f1af153f09930a359df77f0d0b72613`.
- `DEMO-C0004` was rejected because the protective-long IOC did not fully fill.
  The partial long was closed without opening an unprotected short. Exact flat
  restore evidence: `artifacts/demo-preflight-20260715T120820873175Z.json`,
  SHA-256 `d1f39b36c6582215ab3cad9871b6255f9236ba804c395a52198b638fe496dea0`.
- `DEMO-C0005` was rejected because the short IOC did not open the full matched
  spread. Cleanup closed the protective quantity. Final account-wide restore
  evidence reports zero option positions, zero linear positions, zero open
  orders, and exact reconciliation:
  `artifacts/demo-preflight-20260715T120954363650Z.json`, SHA-256
  `02b40e3ff5bfef3a92739b7c027ed80313267dae44c46c955adb40749ca66aa5`.
- Review decision: the implementation and safety behavior are retained, but the
  one-level bounded demo gate is not passed. Select and review a sufficiently
  liquid exact demo spread, or separately review a bounded pricing-policy
  change, before a new attempt. TP, stop/restart, multi-level, actual same-level
  recovery, burn-in/shadow, finite-limit approval, pilot approval, eligibility,
  and any mainnet runner remain gated.

## Integrated protected restart and bounded stop runs

Status: PARTIAL PASS (2026-07-15); final account state is flat and reconciled.

- The liquid exact pair was short `ETH-31JUL26-1950-P-USDT`, long
  `ETH-31JUL26-1900-P-USDT`, quantity 0.1, minimum credit 0.01 USDT and maximum
  entry deviation 100 bps. Preflight accepted the sealed demo transports,
  `REGULAR_MARGIN`, current schemas, finite limits and exact reconciliation.
- `DEMO-C0015` used 50 virtual levels at 1 USDT spacing. Level 34 crossed at
  1917, filled 0.1 ETH at 1917.01, and received confirmed exchange-hosted TP
  and stop protection. The process was deliberately terminated only after that
  confirmation. `RESTORE_ONLY` then reconstructed one linear position, both
  protective orders and both option legs with zero reconciliation differences.
- The restored hosted stop closed level 34 with actual realized P&L
  `-0.50503280` USDT. REST execution-history replay imported the missed exit and
  stored exactly `0.50503280` USDT of confirmed recovery debt. A Bybit flat
  position update containing `entryPrice="0"` exposed a parser defect; flat zero
  entry prices now normalize to missing, with a regression test.
- `DEMO-C0015` then completed `CLOSE_ALL`. Canonical evidence:
  `artifacts/integrated-demo-runtime-20260715T163628581039Z.json`, SHA-256
  `420709cf10c2adb46f1d90e5205b8679d1565e03c312c0532d996a9c1db1377f`.
- `DEMO-C0016` exposed a reconciliation race between confirmed entry fill and
  durable protection confirmation. The supervisor now honors the configured
  limit of two consecutive reconciliation failures: one transient mismatch
  suspends entries, a successful next pass clears it, and the second consecutive
  mismatch still fails closed. Regression tests cover both paths.
- Patched live revalidation `DEMO-C0017` crossed level 9, filled 0.1 ETH at
  1909.7, confirmed TP 1905 and stop 1912.865, and stayed `MATCHED` through
  protection. The hosted stop realized `-0.5402485` USDT and stored the same
  confirmed debt. Level 8 then filled 0.1 ETH at 1914.79 with independent hosted
  TP/stop protection. `/health/live` returned true and strategy status reported
  zero unprotected quantity.
- The 600-second run completed bounded `CLOSE_ALL`, leaving zero linear and
  option positions and zero open orders. Canonical evidence:
  `artifacts/integrated-demo-runtime-20260715T165228739727Z.json`, SHA-256
  `7f7fa6c5fee11c04704405058621349dcb35f96fe8a269599c4d3e6f2f0d3ab1`.
  Final read-only preflight evidence is
  `artifacts/demo-preflight-20260715T165405553600Z.json`, SHA-256
  `559cadfdf4be78238d44ceda98316e7ecaa57fb74feac738e026699f6711f670`.
- Review decision: protected restart, actual stop/history recovery, sequential
  multiple baselines, operations visibility and bounded flat closure pass. A
  natural integrated TP, successful same-level recovery TP, concurrent protected
  baselines remained pending after these runs. The actual 5 USDT-spacing stop
  debt would quantize recovery to 0.3 ETH, so the approved 0.20 ETH and 500 USDT
  limits correctly veto it; limits were not loosened to manufacture a pass.
  Mainnet remains disabled because finite pilot limits, pilot configuration,
  legal/account eligibility and signed accountable approvals are not complete.

## Integrated one-hour configured multi-level burn-in

Status: PASSED (2026-07-16); successful same-level recovery TP remains pending.

- `DEMO-C0018` used ten configured levels across the selected 1950/1850 put
  spread, giving 10 USDT TP spacing while retaining the sealed 0.20 ETH and
  500 USDT demo caps.
- Level 8 crossed, filled 0.1 ETH at 1880.44, and received confirmed hosted TP
  and stop protection. The hosted stop then filled and recorded exactly
  `0.4970079` USDT of confirmed recovery debt before the same level re-armed.
- The market did not cross 1880 again during the bounded hour, so no recovery
  order was submitted and this run is not claimed as successful recovery-TP
  evidence.
- The full 3600-second supervised session completed with accepted runtime
  status and verified `CLOSE_ALL`. Final state was zero linear positions, zero
  option positions, and zero open orders.
- Canonical evidence:
  `artifacts/integrated-demo-runtime-20260716T103007817853Z.json`, SHA-256
  `e58a81100223250a62c71eda080fd28e423b66bb80e5ada13a229e8734028530`.
- After the verified close, the durable kill switch was operator-reset to
  `RUNNING`. Fresh read-only preflight again proved exact flat reconciliation:
  `artifacts/demo-preflight-20260716T103155075430Z.json`, SHA-256
  `85e63504d40742546a9d9a2e056645f1082be9b7754ea4e2436ea07b6e78daad`.
- Review decision: the one-hour configured multi-level burn-in and bounded
  closure gate pass. Successful integrated same-level recovery TP remains open;
  production approvals and mainnet creation remain blocked on external evidence.

## Tight-spacing recovery attempt

Status: SAFE CLOSE; RECOVERY NOT OBSERVED (2026-07-16).

- `DEMO-C0019` used twenty levels, giving 5 USDT spacing without changing the
  sealed finite limits. Level 13 crossed 1890, filled 0.1 ETH at 1889.81, and
  received confirmed hosted TP at 1885 and stop at 1892.835.
- Neither hosted exit fired during the 900-second window, so the run created no
  stop debt and cannot count as same-level recovery evidence.
- Bounded `CLOSE_ALL` passed with zero final positions. Evidence:
  `artifacts/integrated-demo-runtime-20260716T105038813224Z.json`, SHA-256
  `a4fcf4cf3e68923c0780c68662532590fb7c86509cbd15321197d269486133c3`.
- Fresh read-only flat reconciliation passed after the operator-acknowledged
  kill-switch reset: `artifacts/demo-preflight-20260716T105110153844Z.json`,
  SHA-256
  `c50d94e1fad118a00ab4dce05b2d82bb3f4650905a096df2c2d97eec33835fa2`.

## Linked-price-step TP/SL validation

Status: BOUNDED TP/SL PASS; FULL REVALIDATION PENDING (2026-07-16).

Milestone 1.3 note: this section records the historical
`PRICE_STEP_FRACTION=0.15` demo strategy. It is not the current default; the
approved default is now `ENTRY_PERCENT=0.0015`, and the historical evidence is
retained without reinterpretation.

- The geometry contract now defines TP distance as one full price step and SL
  distance as 15% of that step. A 100-level test over the 100 USDT spread
  therefore used 1 USDT TP distance and 0.15 USDT SL distance.
- `DEMO-C0021` opened three independent 0.1 ETH baselines. Level 66 filled at
  1884.79 with hosted stop 1884.94 and TP 1884; level 64 filled at 1886.98 with
  hosted stop 1887.13 and TP 1886. Two stops and one TP were received and
  journaled, followed by verified `CLOSE_ALL` and zero final positions.
- The level-64 TP realized `-0.0435039` USDT after fees. The exit operated
  correctly, but 1 USDT spacing is economically too tight at 0.1 ETH because
  gross TP value is smaller than round-trip fees.
- Evidence: `artifacts/integrated-demo-runtime-20260716T111839265824Z.json`,
  SHA-256
  `4389ca31e6f6dd05e2aa4cc22f2ac0980bf54cf3aff9f6db87403138984755a3`.
- `DEMO-C0022` used 2.5 USDT spacing and exposed the intended fail-closed edge:
  price had already crossed the 0.375 USDT stop before Bybit accepted the hosted
  trigger. Bybit rejected the stale rising trigger, the runtime closed the whole
  strategy, and final linear/option exposure was zero. Evidence:
  `artifacts/integrated-demo-runtime-20260716T112318419720Z.json`.
- Fresh post-close preflight proved zero positions, zero orders, `RUNNING` kill
  switch, and exact reconciliation:
  `artifacts/demo-preflight-20260716T112448281735Z.json`, SHA-256
  `a70ba5698bf8242056db6776872ea5124f1f71b1a705a8943c85451d094d0d59`.
- The linked-price-step mainnet shadow capture and replay have passed. The previous
  one-hour demo burn-in used the old entry-relative stop rule and still requires
  a complete linked-price-step rerun before pilot approval.

## Linked-price-step burn-in attempt

Status: NOT PASSED; VERIFIED SAFE CLOSE (2026-07-16).

- `DEMO-C0023` used 20 levels over the 100 USDT spread: TP distance 5 USDT and
  SL distance 0.75 USDT. It ran for about 38 minutes.
- Level 14 completed a hosted TP. Level 15 then completed a hosted stop and
  recorded actual recovery debt of `0.28179945`; the same level was re-armed
  with zero residual hedge exposure.
- No same-level recovery recross occurred before a runtime task failed. The
  mandatory close path verified zero final linear and option positions.
- Runtime evidence:
  `artifacts/integrated-demo-runtime-20260716T120813947520Z.json`.
- Fresh non-mutating post-close preflight passed with zero positions, zero open
  orders, and exact reconciliation:
  `artifacts/demo-preflight-20260716T121037906336Z.json`, SHA-256
  `fa0b9a5874662a66a406e0594ae736567a729ca235199dcfcee2d6fc0cbc3b61`.
- This attempt proves linked TP/SL operation, durable debt, same-level re-arming,
  and fail-closed cleanup. It does not satisfy the continuous one-hour burn-in
  or successful same-level recovery-TP gates.
