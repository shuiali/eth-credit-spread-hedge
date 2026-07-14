"""Fill aggregation and option-spread valuation tests."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from eth_credit_hedge.domain.instruments import (
    OptionContract,
    OptionFill,
    OptionMarketQuote,
)
from eth_credit_hedge.domain.option_position import (
    OptionLegPosition,
    OptionPositionState,
    OptionQuoteValidationPolicy,
    PutCreditSpreadPosition,
)


NOW = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
EXPIRY = datetime(2026, 7, 31, 8, tzinfo=timezone.utc)
SHORT_SYMBOL = "ETH-31JUL26-3000-P-USDT"
LONG_SYMBOL = "ETH-31JUL26-2900-P-USDT"


def make_contract(symbol: str, strike: str) -> OptionContract:
    return OptionContract(
        symbol=symbol,
        base_coin="ETH",
        quote_coin="USDT",
        settle_coin="USDT",
        option_type="Put",
        strike=Decimal(strike),
        expiry_time_utc=EXPIRY,
        contract_multiplier=Decimal("1"),
    )


def make_fill(
    execution_id: str,
    *,
    symbol: str,
    side: str,
    price: str,
    quantity: str,
    fee: str = "0.10",
    seconds: int = 0,
) -> OptionFill:
    return OptionFill(
        order_id=f"order-{execution_id}",
        execution_id=execution_id,
        symbol=symbol,
        side=side,  # type: ignore[arg-type]
        price=Decimal(price),
        quantity=Decimal(quantity),
        fee=Decimal(fee),
        timestamp_utc=NOW + timedelta(seconds=seconds),
    )


def make_long_leg(
    fills: tuple[OptionFill, ...],
    *,
    requested: str = "1",
) -> OptionLegPosition:
    return OptionLegPosition.from_fills(
        contract=make_contract(LONG_SYMBOL, "2900"),
        side="Long",
        requested_quantity=Decimal(requested),
        fills=fills,
    )


def make_short_leg(
    fills: tuple[OptionFill, ...],
    *,
    requested: str = "1",
) -> OptionLegPosition:
    return OptionLegPosition.from_fills(
        contract=make_contract(SHORT_SYMBOL, "3000"),
        side="Short",
        requested_quantity=Decimal(requested),
        fills=fills,
    )


def make_open_position() -> PutCreditSpreadPosition:
    long_fills = (
        make_fill(
            "long-1",
            symbol=LONG_SYMBOL,
            side="Buy",
            price="30",
            quantity="0.4",
        ),
        make_fill(
            "long-2",
            symbol=LONG_SYMBOL,
            side="Buy",
            price="32",
            quantity="0.6",
            seconds=1,
        ),
    )
    short_fills = (
        make_fill(
            "short-1",
            symbol=SHORT_SYMBOL,
            side="Sell",
            price="60",
            quantity="0.5",
            seconds=2,
        ),
        make_fill(
            "short-2",
            symbol=SHORT_SYMBOL,
            side="Sell",
            price="62",
            quantity="0.5",
            seconds=3,
        ),
    )
    return PutCreditSpreadPosition(
        short_put=make_short_leg(short_fills),
        long_put=make_long_leg(long_fills),
        state=OptionPositionState.OPEN,
    )


def make_quote(
    symbol: str,
    *,
    mark: str,
    bid: str | None,
    ask: str | None,
    timestamp: datetime | None = None,
    index: str = "3010",
) -> OptionMarketQuote:
    return OptionMarketQuote(
        symbol=symbol,
        timestamp_utc=timestamp or NOW,
        bid_price=None if bid is None else Decimal(bid),
        bid_size=Decimal("10") if bid is not None else None,
        ask_price=None if ask is None else Decimal(ask),
        ask_size=Decimal("10") if ask is not None else None,
        mark_price=Decimal(mark),
        underlying_price=Decimal("3011"),
        index_price=Decimal(index),
        bid_iv=Decimal("0.49"),
        ask_iv=Decimal("0.51"),
        mark_iv=Decimal("0.50"),
        delta=Decimal("-0.30"),
        gamma=Decimal("0.001"),
        vega=Decimal("2.1"),
        theta=Decimal("-1.2"),
    )


def validation_policy() -> OptionQuoteValidationPolicy:
    return OptionQuoteValidationPolicy(
        max_quote_age_seconds=Decimal("5"),
        max_leg_timestamp_difference_seconds=Decimal("2"),
        max_index_price_difference_ratio=Decimal("0.001"),
    )


def test_actual_credit_aggregates_multiple_fills_and_fees() -> None:
    position = make_open_position()

    assert position.long_put.average_entry_price == Decimal("31.2")
    assert position.short_put.average_entry_price == Decimal("61")
    assert position.short_proceeds == Decimal("61.0")
    assert position.long_cost == Decimal("31.20")
    assert position.total_entry_fees == Decimal("0.40")
    assert position.actual_net_credit == Decimal("29.40")
    assert position.opened_time_utc == NOW + timedelta(seconds=3)


def test_fees_reduce_actual_credit() -> None:
    position = make_open_position()

    credit_before_fees = position.short_proceeds - position.long_cost

    assert credit_before_fees == Decimal("29.80")
    assert position.actual_net_credit == credit_before_fees - Decimal("0.40")


def test_partial_long_fill_tracks_remaining_quantity() -> None:
    long_leg = make_long_leg(
        (
            make_fill(
                "long-partial",
                symbol=LONG_SYMBOL,
                side="Buy",
                price="30",
                quantity="0.7",
            ),
        ),
        requested="2",
    )
    position = PutCreditSpreadPosition(
        short_put=make_short_leg((), requested="2"),
        long_put=long_leg,
        state=OptionPositionState.LONG_PROTECTION_FILLED,
    )

    assert position.long_put.filled_quantity == Decimal("0.7")
    assert position.long_put.remaining_quantity == Decimal("1.3")
    assert position.matched_quantity == Decimal("0")


def test_partial_short_fill_counts_only_matched_quantity() -> None:
    long_leg = make_long_leg(
        (
            make_fill(
                "long-full",
                symbol=LONG_SYMBOL,
                side="Buy",
                price="30",
                quantity="1",
            ),
        )
    )
    short_leg = make_short_leg(
        (
            make_fill(
                "short-partial",
                symbol=SHORT_SYMBOL,
                side="Sell",
                price="60",
                quantity="0.4",
            ),
        )
    )
    position = PutCreditSpreadPosition(
        short_put=short_leg,
        long_put=long_leg,
        state=OptionPositionState.PARTIALLY_OPEN,
    )

    assert position.short_put.remaining_quantity == Decimal("0.6")
    assert position.matched_quantity == Decimal("0.4")
    assert not position.has_naked_short


def test_fill_aggregation_rejects_duplicate_executions() -> None:
    fill = make_fill(
        "duplicate",
        symbol=LONG_SYMBOL,
        side="Buy",
        price="30",
        quantity="0.4",
    )

    with pytest.raises(ValueError, match="duplicate execution"):
        make_long_leg((fill, fill))


def test_fill_aggregation_rejects_wrong_symbol_or_side() -> None:
    wrong_symbol = make_fill(
        "wrong-symbol",
        symbol=SHORT_SYMBOL,
        side="Buy",
        price="30",
        quantity="0.4",
    )
    wrong_side = make_fill(
        "wrong-side",
        symbol=LONG_SYMBOL,
        side="Sell",
        price="30",
        quantity="0.4",
    )

    with pytest.raises(ValueError, match="fill symbol"):
        make_long_leg((wrong_symbol,))
    with pytest.raises(ValueError, match="fill side"):
        make_long_leg((wrong_side,))


def test_fill_aggregation_rejects_quantity_above_request() -> None:
    fill = make_fill(
        "overfill",
        symbol=LONG_SYMBOL,
        side="Buy",
        price="30",
        quantity="1.1",
    )

    with pytest.raises(ValueError, match="filled quantity"):
        make_long_leg((fill,))


def test_open_position_rejects_non_positive_actual_credit() -> None:
    long_leg = make_long_leg(
        (
            make_fill(
                "expensive-long",
                symbol=LONG_SYMBOL,
                side="Buy",
                price="30",
                quantity="1",
            ),
        )
    )
    short_leg = make_short_leg(
        (
            make_fill(
                "cheap-short",
                symbol=SHORT_SYMBOL,
                side="Sell",
                price="20",
                quantity="1",
            ),
        )
    )

    with pytest.raises(ValueError, match="positive actual net credit"):
        PutCreditSpreadPosition(
            short_put=short_leg,
            long_put=long_leg,
            state=OptionPositionState.OPEN,
        )


def test_mark_and_liquidation_pnl_are_distinct() -> None:
    position = make_open_position()
    short_quote = make_quote(SHORT_SYMBOL, mark="55", bid="54", ask="56")
    long_quote = make_quote(LONG_SYMBOL, mark="28", bid="27", ask="29")

    mark_pnl = position.mark_pnl(
        short_quote,
        long_quote,
        as_of_utc=NOW + timedelta(seconds=1),
        validation_policy=validation_policy(),
    )
    liquidation_pnl = position.liquidation_pnl(
        short_quote,
        long_quote,
        as_of_utc=NOW + timedelta(seconds=1),
        validation_policy=validation_policy(),
    )

    assert mark_pnl == Decimal("2.40")
    assert liquidation_pnl == Decimal("0.40")
    assert mark_pnl != liquidation_pnl


@pytest.mark.parametrize(
    ("underlying", "expected"),
    [("3100", "29.40"), ("2950", "-20.60"), ("2800", "-70.60")],
)
def test_expiration_pnl_retains_terminal_payoff(
    underlying: str,
    expected: str,
) -> None:
    position = make_open_position()

    assert position.expiration_pnl(Decimal(underlying)) == Decimal(expected)


def test_mark_pnl_rejects_stale_quote() -> None:
    position = make_open_position()
    stale_time = NOW - timedelta(seconds=6)
    short_quote = make_quote(
        SHORT_SYMBOL,
        mark="55",
        bid="54",
        ask="56",
        timestamp=stale_time,
    )
    long_quote = make_quote(LONG_SYMBOL, mark="28", bid="27", ask="29")

    with pytest.raises(ValueError, match="stale"):
        position.mark_pnl(
            short_quote,
            long_quote,
            as_of_utc=NOW,
            validation_policy=validation_policy(),
        )


def test_mark_pnl_rejects_timestamp_and_index_mismatch() -> None:
    position = make_open_position()
    short_quote = make_quote(SHORT_SYMBOL, mark="55", bid="54", ask="56")
    skewed_quote = make_quote(
        LONG_SYMBOL,
        mark="28",
        bid="27",
        ask="29",
        timestamp=NOW - timedelta(seconds=3),
    )
    mismatched_index = make_quote(
        LONG_SYMBOL,
        mark="28",
        bid="27",
        ask="29",
        index="3020",
    )

    with pytest.raises(ValueError, match="timestamps"):
        position.mark_pnl(
            short_quote,
            skewed_quote,
            as_of_utc=NOW,
            validation_policy=validation_policy(),
        )
    with pytest.raises(ValueError, match="index prices"):
        position.mark_pnl(
            short_quote,
            mismatched_index,
            as_of_utc=NOW,
            validation_policy=validation_policy(),
        )


def test_valuation_rejects_non_trading_or_expired_contract() -> None:
    position = make_open_position()
    short_quote = make_quote(SHORT_SYMBOL, mark="55", bid="54", ask="56")
    long_quote = make_quote(LONG_SYMBOL, mark="28", bid="27", ask="29")

    with pytest.raises(ValueError, match="not Trading"):
        position.mark_pnl(
            short_quote,
            long_quote,
            as_of_utc=NOW,
            validation_policy=validation_policy(),
            short_instrument_status="Settling",
        )
    with pytest.raises(ValueError, match="expiry"):
        position.mark_pnl(
            short_quote,
            long_quote,
            as_of_utc=EXPIRY,
            validation_policy=validation_policy(),
        )


def test_liquidation_pnl_requires_executable_close_sides() -> None:
    position = make_open_position()
    short_without_ask = make_quote(
        SHORT_SYMBOL,
        mark="55",
        bid="54",
        ask=None,
    )
    long_without_bid = make_quote(
        LONG_SYMBOL,
        mark="28",
        bid=None,
        ask="29",
    )
    valid_short = make_quote(SHORT_SYMBOL, mark="55", bid="54", ask="56")
    valid_long = make_quote(LONG_SYMBOL, mark="28", bid="27", ask="29")

    with pytest.raises(ValueError, match="short ask"):
        position.liquidation_pnl(
            short_without_ask,
            valid_long,
            as_of_utc=NOW,
            validation_policy=validation_policy(),
        )
    with pytest.raises(ValueError, match="long bid"):
        position.liquidation_pnl(
            valid_short,
            long_without_bid,
            as_of_utc=NOW,
            validation_policy=validation_policy(),
        )
