"""Executable bid/ask option-spread cost model."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from eth_credit_hedge.domain.instruments import OptionMarketQuote


@dataclass(frozen=True, slots=True)
class OptionSpreadEntryResult:
    short_fill_price: Decimal
    long_fill_price: Decimal
    filled_quantity: Decimal
    gross_credit: Decimal
    fees: Decimal
    net_credit: Decimal


@dataclass(frozen=True, slots=True)
class OptionLiquidationResult:
    short_buy_price: Decimal
    long_sell_price: Decimal
    quantity: Decimal
    fees: Decimal
    net_cash_flow: Decimal


def _positive(value: Decimal, name: str) -> Decimal:
    result = Decimal(value)
    if not result.is_finite() or result <= 0:
        raise ValueError(f"{name} must be positive")
    return result


def _fee_rate(value: Decimal) -> Decimal:
    result = Decimal(value)
    if not result.is_finite() or result < 0:
        raise ValueError("fee rate cannot be negative")
    return result


def _executable_prices(
    short_quote: OptionMarketQuote,
    long_quote: OptionMarketQuote,
) -> tuple[Decimal, Decimal]:
    if short_quote.timestamp_utc != long_quote.timestamp_utc:
        raise ValueError("option quotes must have the same timestamp")
    if short_quote.bid_price is None or long_quote.ask_price is None:
        raise ValueError("entry requires a short bid and long ask")
    return short_quote.bid_price, long_quote.ask_price


def simulate_option_spread_entry(
    *,
    short_quote: OptionMarketQuote,
    long_quote: OptionMarketQuote,
    requested_quantity: Decimal,
    fill_fraction: Decimal,
    fee_rate: Decimal,
) -> OptionSpreadEntryResult:
    short_price, long_price = _executable_prices(short_quote, long_quote)
    quantity = _positive(requested_quantity, "requested quantity")
    fraction = Decimal(fill_fraction)
    if not fraction.is_finite() or not Decimal("0") < fraction <= Decimal("1"):
        raise ValueError("fill fraction must be in (0, 1]")
    filled_quantity = quantity * fraction
    gross_credit = (short_price - long_price) * filled_quantity
    fees = (short_price + long_price) * filled_quantity * _fee_rate(fee_rate)
    return OptionSpreadEntryResult(
        short_fill_price=short_price,
        long_fill_price=long_price,
        filled_quantity=filled_quantity,
        gross_credit=gross_credit,
        fees=fees,
        net_credit=gross_credit - fees,
    )


def simulate_option_liquidation(
    *,
    short_quote: OptionMarketQuote,
    long_quote: OptionMarketQuote,
    quantity: Decimal,
    fee_rate: Decimal,
) -> OptionLiquidationResult:
    if short_quote.timestamp_utc != long_quote.timestamp_utc:
        raise ValueError("option quotes must have the same timestamp")
    if short_quote.ask_price is None or long_quote.bid_price is None:
        raise ValueError("liquidation requires a short ask and long bid")
    filled_quantity = _positive(quantity, "quantity")
    short_cost = short_quote.ask_price * filled_quantity
    long_proceeds = long_quote.bid_price * filled_quantity
    fees = (short_cost + long_proceeds) * _fee_rate(fee_rate)
    return OptionLiquidationResult(
        short_buy_price=short_quote.ask_price,
        long_sell_price=long_quote.bid_price,
        quantity=filled_quantity,
        fees=fees,
        net_cash_flow=long_proceeds - short_cost - fees,
    )
