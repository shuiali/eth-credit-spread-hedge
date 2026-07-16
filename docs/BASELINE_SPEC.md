# Deterministic Baseline Specification

This document freezes the behavior of the deterministic ETH put-credit-spread
hedge before exchange execution is introduced. The reference configuration is:

```text
RecoveryMode.FULL_NEXT_TP
LockPolicy.UNHEDGED
StopMode.ENTRY_PERCENT
entry_stop_rate = 0.0015 of entry price
```

All monetary, price, and quantity calculations use `Decimal`.

## Put-credit-spread expiration payoff

For terminal ETH price `S`, short-put strike `K_short`, long-put strike
`K_long`, option quantity `q`, and confirmed net premium credit `C`:

```text
option_pnl(S) = C
              - q * max(K_short - S, 0)
              + q * max(K_long - S, 0)
```

`K_short` must be greater than `K_long`, `q` must be positive, and `C` must
be between zero and `q * (K_short - K_long)`, inclusive.

## Virtual levels

For `N > 0`, partition only the interval from `K_short` down to `K_long`
into `N` equal zones. For one-indexed level `i`:

```text
zone_width    = (K_short - K_long) / N
entry_i       = K_short - (i - 1) * zone_width
tp_i          = K_short - i * zone_width
option_budget = q * (entry_i - tp_i)
price_step_i  = entry_i - tp_i
stop_i        = entry_i + 0.0015 * entry_i
```

No virtual level exists below the long-put strike.

## Entry and re-entry

A ready level is armed only after price is at or above its entry. Opening a
normal hedge requires a new downward price segment that reaches the armed
entry. Starting exactly at an entry arms the level but does not itself open a
hedge; a later downward move does.

After a fixed stop, the same level returns to `READY`, is re-armed, and may
open again only on a later downward crossing. Remaining below an entry does
not generate duplicate entries. `PAID` and `LOCKED` levels do not open normal
hedges.

When a downward segment reaches a boundary shared by one level's take-profit
and the next level's entry, the take-profit is processed first.

`PRICE_STEP_FRACTION` remains an explicit alternate strategy. It uses
`stop_i = entry_i + price_step_i * fraction`; it is never selected by
interpreting the entry-percent parameter differently.

## Full-next-TP recovery

Each level owns its recovery debt. A fixed-stop loss `L` increases that
level's debt by `L`. In `FULL_NEXT_TP` mode, the next entry for that same level
uses:

```text
quantity = (option_budget + recovery_debt) / (entry_price - tp_price)
```

A successful take-profit realizes `option_budget + recovery_debt` and reduces
the explicitly allocated debt to zero. Debt never becomes negative and cannot
decrease without a recovery allocation.

## Premium stop budget

The initial stop budget equals the option premium credit. Before an entry, the
engine calculates:

```text
projected_stop_loss = quantity * (stop_price - entry_price)
```

The entry is allowed when accumulated realized stop losses plus the projected
stop loss are less than or equal to the initial budget. If the sum is greater,
the baseline `UNHEDGED` policy moves the level to `LOCKED` without opening a
hedge.

This premium budget limits modeled fixed-stop losses only. It does **not**
guarantee nonnegative combined P&L, especially after a level locks and option
loss remains unhedged. Fees, slippage, funding, gaps, partial fills, and live
exchange behavior are outside this frozen deterministic baseline.
