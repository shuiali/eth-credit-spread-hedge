"""Thin backend launcher for the calculation-free dashboard renderer."""

from __future__ import annotations

import argparse
from decimal import Decimal
from pathlib import Path
from collections.abc import Sequence

from backtesting.market_path import expand_price_anchors
from backtesting.monte_carlo import MonteCarloConfig, run_monte_carlo
from core.credit_spread import CreditSpread
from core.hedge_engine import HedgeEngine, LockPolicy
from data.bybit_options import (
    BybitOptionClient,
    QuotedCreditSpread,
    load_option_fixture,
    parse_option_fixture,
    select_put_credit_spread,
)
from visualization.dashboard import Dashboard
from visualization.payload import build_dashboard_payload


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spot", default="3010")
    parser.add_argument("--short-put", default="3000")
    parser.add_argument("--long-put", default="2900")
    parser.add_argument("--quantity", default="1")
    parser.add_argument("--credit", default="30")
    parser.add_argument("--levels", type=int, default=5)
    parser.add_argument(
        "--path",
        default=None,
        help="comma-separated macro price anchors",
    )
    parser.add_argument("--ticks-per-segment", type=int, default=3_000)
    parser.add_argument("--path-seed", type=int, default=42)
    parser.add_argument("--mc-paths", type=int, default=500)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--option-fixture", type=Path)
    source.add_argument("--live-bybit", action="store_true")
    parser.add_argument("--short-symbol")
    parser.add_argument("--long-symbol")
    parser.add_argument("--save", type=Path)
    return parser.parse_args(argv)


def resolve_spread(
    args: argparse.Namespace,
) -> tuple[CreditSpread, QuotedCreditSpread | None]:
    """Resolve manual inputs or an exact user-selected Bybit option pair."""
    chain = None
    if args.option_fixture:
        chain = parse_option_fixture(load_option_fixture(args.option_fixture))
    elif args.live_bybit:
        chain = BybitOptionClient().fetch_eth_chain()

    if chain is None:
        if args.short_symbol or args.long_symbol:
            raise ValueError("option symbols require --option-fixture or --live-bybit")
        return (
            CreditSpread(
                args.spot,
                args.short_put,
                args.long_put,
                args.quantity,
                args.credit,
            ),
            None,
        )

    if not args.short_symbol or not args.long_symbol:
        raise ValueError("Bybit inputs require both exact option symbols")
    selected = select_put_credit_spread(
        chain,
        args.short_symbol,
        args.long_symbol,
        args.quantity,
    )
    return selected.spread, selected


def main() -> None:
    args = parse_args()
    spread, selected = resolve_spread(args)
    path_text = args.path or ",".join(_default_path_anchors(spread))
    anchors = [item.strip() for item in path_text.split(",") if item.strip()]
    prices = expand_price_anchors(
        anchors,
        ticks_per_segment=args.ticks_per_segment,
        tick_size="0.1",
        seed=args.path_seed,
    )
    result = HedgeEngine(
        spread,
        args.levels,
        lock_policy=LockPolicy.UNHEDGED,
    ).run_with_accounting(list(prices))
    monte_carlo = None
    if args.mc_paths:
        monte_carlo = run_monte_carlo(
            spread,
            args.levels,
            MonteCarloConfig(path_count=args.mc_paths),
            Path("artifacts") / "dashboard_monte_carlo_paths.json",
            lock_policy=LockPolicy.UNHEDGED,
        )
    payload = build_dashboard_payload(
        spread,
        result,
        monte_carlo,
        selected_path_name=(
            f"{selected.short_put.symbol} / {selected.long_put.symbol} — "
            if selected
            else ""
        )
        + (f"market bridge ({len(prices):,} exact $0.10 ticks)"),
    )
    dashboard = Dashboard(payload)
    figure = dashboard.build_figure()
    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        figure.savefig(args.save, dpi=130, facecolor=figure.get_facecolor())
    else:
        dashboard.show()


def _default_path_anchors(spread: CreditSpread) -> tuple[str, ...]:
    width = spread.short_put_strike - spread.long_put_strike
    short = spread.short_put_strike
    offsets = (
        Decimal("-0.08"),
        Decimal("0.07"),
        Decimal("-0.16"),
        Decimal("-0.03"),
        Decimal("-0.32"),
        Decimal("-0.19"),
        Decimal("-0.52"),
        Decimal("-0.38"),
        Decimal("-0.72"),
        Decimal("-0.59"),
        Decimal("-1.10"),
    )
    start = max(spread.spot, short + width * Decimal("0.10"))
    anchors = (start,) + tuple(short + width * offset for offset in offsets)
    tick = Decimal("0.1")
    return tuple(str((anchor / tick).to_integral_value() * tick) for anchor in anchors)


if __name__ == "__main__":
    main()
