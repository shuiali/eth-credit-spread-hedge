"""Offline fixture and selected-live-structure tests."""

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from eth_credit_hedge.data.bybit_options import (
    build_credit_spread_from_quotes,
    load_option_fixture,
    parse_option_fixture,
    select_put_credit_spread,
)
from eth_credit_hedge.domain.instruments import OptionContract, OptionMarketQuote


FIXTURE = Path(__file__).parent / "fixtures" / "bybit_eth_option_pair.json"


def test_real_fixture_parses_exact_raw_symbol_and_quote_fields() -> None:
    raw = load_option_fixture(FIXTURE)
    quotes = parse_option_fixture(raw)
    by_symbol = {quote.symbol: quote for quote in quotes}
    short = by_symbol["ETH-31JUL26-1750-P-USDT"]
    long = by_symbol["ETH-31JUL26-1650-P-USDT"]

    assert raw["exchange"] == "Bybit"
    assert isinstance(short.contract, OptionContract)
    assert isinstance(short.quote, OptionMarketQuote)
    assert short.contract.strike == Decimal("1750")
    assert short.contract.expiry_time_utc == datetime(
        2026, 7, 31, 8, tzinfo=timezone.utc
    )
    assert short.contract.contract_multiplier == Decimal("1")
    assert short.contract.option_type == "Put"
    assert short.status == "Trading"
    assert short.quote.timestamp_utc == datetime(
        2026, 7, 13, 17, 3, 26, 868000, tzinfo=timezone.utc
    )
    assert short.quote.bid_price == Decimal("62.4")
    assert short.quote.bid_size == Decimal("48.3")
    assert short.quote.ask_price == Decimal("62.9")
    assert short.quote.ask_size == Decimal("61.8")
    assert short.quote.mark_price == Decimal("62.38000001")
    assert short.quote.bid_iv == Decimal("0.4794")
    assert short.quote.ask_iv == Decimal("0.4826")
    assert short.quote.mark_iv == Decimal("0.4793")
    assert short.quote.delta == Decimal("-0.42665899")
    assert short.quote.gamma == Decimal("0.00209858")
    assert short.quote.vega == Decimal("1.52919144")
    assert short.quote.theta == Decimal("-2.07945438")
    assert long.contract.strike == Decimal("1650")
    assert long.contract.expiry_time_utc == datetime(
        2026, 7, 31, 8, tzinfo=timezone.utc
    )
    assert long.quote.bid_price == Decimal("29.9")
    assert long.quote.ask_price == Decimal("30.1")
    assert long.quote.mark_price == Decimal("30.0643528")


def test_selected_quotes_build_the_validated_credit_spread_exactly() -> None:
    quotes = parse_option_fixture(load_option_fixture(FIXTURE))
    selected = select_put_credit_spread(
        quotes,
        "ETH-31JUL26-1750-P-USDT",
        "ETH-31JUL26-1650-P-USDT",
        "1",
    )

    assert selected.mark_credit == Decimal("32.31564721")
    assert selected.natural_credit == Decimal("32.3")
    assert selected.mark_credit_calculation == (
        "(62.38000001 - 30.0643528) × 1 = 32.31564721"
    )
    assert selected.natural_credit_calculation == "(62.4 - 30.1) × 1 = 32.3"
    assert selected.spread.spot == Decimal("1773.66297243")
    assert selected.spread.short_put_strike == Decimal("1750")
    assert selected.spread.long_put_strike == Decimal("1650")
    assert selected.spread.option_quantity == Decimal("1")
    assert selected.spread.premium_credit == Decimal("32.31564721")


def test_direct_builder_matches_symbol_selection() -> None:
    quotes = parse_option_fixture(load_option_fixture(FIXTURE))
    by_strike = {entry.contract.strike: entry for entry in quotes}

    selected = build_credit_spread_from_quotes(
        by_strike[Decimal("1750")], by_strike[Decimal("1650")], "2"
    )

    assert selected.option_quantity == Decimal("2")
    assert selected.mark_credit == Decimal("64.63129442")
    assert selected.spread.premium_credit == Decimal("64.63129442")


def test_small_index_snapshot_difference_uses_the_average_reference() -> None:
    quotes = parse_option_fixture(load_option_fixture(FIXTURE))
    by_strike = {entry.contract.strike: entry for entry in quotes}
    short = by_strike[Decimal("1750")]
    long = by_strike[Decimal("1650")]
    long = replace(
        long,
        quote=replace(
            long.quote,
            index_price=short.quote.index_price + Decimal("0.50"),
        ),
    )

    selected = build_credit_spread_from_quotes(short, long, "1")

    assert selected.spread.spot == short.quote.index_price + Decimal("0.25")


def test_material_index_snapshot_difference_is_rejected() -> None:
    quotes = parse_option_fixture(load_option_fixture(FIXTURE))
    by_strike = {entry.contract.strike: entry for entry in quotes}
    short = by_strike[Decimal("1750")]
    long = by_strike[Decimal("1650")]
    long = replace(
        long,
        quote=replace(
            long.quote,
            index_price=short.quote.index_price + Decimal("5"),
        ),
    )

    with pytest.raises(ValueError, match="index prices differ"):
        build_credit_spread_from_quotes(short, long, "1")


def test_dashboard_launcher_can_build_from_the_offline_bybit_fixture() -> None:
    from dashboard_app import parse_args, resolve_spread

    args = parse_args(
        [
            "--option-fixture",
            str(FIXTURE),
            "--short-symbol",
            "ETH-31JUL26-1750-P-USDT",
            "--long-symbol",
            "ETH-31JUL26-1650-P-USDT",
            "--mc-paths",
            "0",
        ]
    )

    spread, selected = resolve_spread(args)

    assert selected is not None
    assert spread == selected.spread
    assert selected.short_put.symbol == "ETH-31JUL26-1750-P-USDT"
