"""Fetch ETH puts, then build only the exact user-selected credit spread."""

from __future__ import annotations

import argparse

from eth_credit_hedge.data.bybit_options import (
    BybitOptionClient,
    select_put_credit_spread,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--short-symbol")
    parser.add_argument("--long-symbol")
    parser.add_argument("--quantity", default="1")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    chain = BybitOptionClient().fetch_eth_chain()
    if not args.short_symbol or not args.long_symbol:
        print("Available ETH puts (select exact symbols with --short-symbol and --long-symbol):")
        for entry in chain:
            print(
                f"{entry.symbol}  strike={entry.contract.strike} "
                f"expiry={entry.contract.expiry_time_utc.date()} "
                f"bid={entry.quote.bid_price} ask={entry.quote.ask_price} "
                f"mark={entry.quote.mark_price} mark_iv={entry.quote.mark_iv} "
                f"delta={entry.quote.delta}"
            )
        return

    selected = select_put_credit_spread(
        chain, args.short_symbol, args.long_symbol, args.quantity
    )
    print(f"Short put: {selected.short_put.symbol}")
    print(f"Long put:  {selected.long_put.symbol}")
    print(f"Mark credit:    {selected.mark_credit_calculation}")
    print(f"Natural credit: {selected.natural_credit_calculation} (display only)")
    print(f"Engine input: {selected.spread}")


if __name__ == "__main__":
    main()
