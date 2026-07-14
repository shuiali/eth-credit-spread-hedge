"""Exchange-neutral option contract, quote, and fill tests."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from eth_credit_hedge.domain.instruments import (
    OptionContract,
    OptionFill,
    OptionMarketQuote,
)


EXPIRY = datetime(2026, 7, 31, 8, tzinfo=timezone.utc)
OBSERVED = datetime(2026, 7, 14, 9, 30, tzinfo=timezone.utc)


def make_contract() -> OptionContract:
    return OptionContract(
        symbol="ETH-31JUL26-3000-P-USDT",
        base_coin="ETH",
        quote_coin="USDT",
        settle_coin="USDT",
        option_type="Put",
        strike=Decimal("3000"),
        expiry_time_utc=EXPIRY,
        contract_multiplier=Decimal("1"),
    )


def make_quote() -> OptionMarketQuote:
    return OptionMarketQuote(
        symbol="ETH-31JUL26-3000-P-USDT",
        timestamp_utc=OBSERVED,
        bid_price=Decimal("99.5"),
        bid_size=Decimal("2.1"),
        ask_price=Decimal("100.5"),
        ask_size=Decimal("3.2"),
        mark_price=Decimal("100"),
        underlying_price=Decimal("3010"),
        index_price=Decimal("3009.5"),
        bid_iv=None,
        ask_iv=None,
        mark_iv=Decimal("0.55"),
        delta=Decimal("-0.45"),
        gamma=None,
        vega=None,
        theta=None,
    )


def test_contract_normalizes_an_aware_expiry_to_utc() -> None:
    local_expiry = datetime(
        2026,
        7,
        31,
        11,
        tzinfo=timezone(timedelta(hours=3)),
    )

    contract = replace(make_contract(), expiry_time_utc=local_expiry)

    assert contract.expiry_time_utc == EXPIRY


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"option_type": "Straddle"}, "option type"),
        ({"strike": Decimal("0")}, "strike"),
        ({"contract_multiplier": Decimal("0")}, "contract multiplier"),
        ({"expiry_time_utc": datetime(2026, 7, 31, 8)}, "timezone-aware"),
    ],
)
def test_contract_rejects_invalid_identity_or_terms(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        replace(make_contract(), **changes)


def test_quote_retains_missing_optional_greeks_and_normalizes_time() -> None:
    local_observed = datetime(
        2026,
        7,
        14,
        12,
        30,
        tzinfo=timezone(timedelta(hours=3)),
    )

    quote = replace(make_quote(), timestamp_utc=local_observed)

    assert quote.timestamp_utc == OBSERVED
    assert quote.bid_iv is None
    assert quote.gamma is None
    assert quote.vega is None
    assert quote.theta is None


def test_quote_rejects_crossed_bid_and_ask() -> None:
    with pytest.raises(ValueError, match="bid cannot exceed ask"):
        replace(
            make_quote(),
            bid_price=Decimal("101"),
            ask_price=Decimal("100"),
        )


def test_fill_is_decimal_exact_and_utc_normalized() -> None:
    fill = OptionFill(
        order_id="order-1",
        execution_id="execution-1",
        symbol=make_contract().symbol,
        side="Buy",
        price=Decimal("30.1"),
        quantity=Decimal("0.4"),
        fee=Decimal("0.015"),
        timestamp_utc=OBSERVED,
    )

    assert fill.price * fill.quantity + fill.fee == Decimal("12.055")
    assert fill.timestamp_utc is OBSERVED


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"side": "Hold"}, "fill side"),
        ({"quantity": Decimal("0")}, "fill quantity"),
        ({"fee": Decimal("-0.01")}, "fee"),
    ],
)
def test_fill_rejects_invalid_execution_values(
    changes: dict[str, object], message: str
) -> None:
    values: dict[str, object] = {
        "order_id": "order-1",
        "execution_id": "execution-1",
        "symbol": make_contract().symbol,
        "side": "Buy",
        "price": Decimal("30.1"),
        "quantity": Decimal("0.4"),
        "fee": Decimal("0.015"),
        "timestamp_utc": OBSERVED,
    }
    values.update(changes)

    with pytest.raises(ValueError, match=message):
        OptionFill(**values)  # type: ignore[arg-type]
