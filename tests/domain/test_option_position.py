"""Option-spread position lifecycle invariant tests."""

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from eth_credit_hedge.domain.instruments import OptionContract
from eth_credit_hedge.domain.option_position import (
    OptionLegPosition,
    OptionPositionState,
    PutCreditSpreadPosition,
)


EXPIRY = datetime(2026, 7, 31, 8, tzinfo=timezone.utc)


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


def make_long(*, filled: str = "0", requested: str = "1") -> OptionLegPosition:
    filled_quantity = Decimal(filled)
    return OptionLegPosition(
        contract=make_contract("ETH-31JUL26-2900-P-USDT", "2900"),
        side="Long",
        requested_quantity=Decimal(requested),
        filled_quantity=filled_quantity,
        average_entry_price=(Decimal("30") if filled_quantity else Decimal("0")),
        fees_paid=(Decimal("0.01") if filled_quantity else Decimal("0")),
    )


def make_short(*, filled: str = "0", requested: str = "1") -> OptionLegPosition:
    filled_quantity = Decimal(filled)
    return OptionLegPosition(
        contract=make_contract("ETH-31JUL26-3000-P-USDT", "3000"),
        side="Short",
        requested_quantity=Decimal(requested),
        filled_quantity=filled_quantity,
        average_entry_price=(Decimal("60") if filled_quantity else Decimal("0")),
        fees_paid=(Decimal("0.01") if filled_quantity else Decimal("0")),
    )


def test_leg_tracks_confirmed_and_remaining_quantity() -> None:
    leg = make_long(filled="0.4")

    assert leg.filled_quantity == Decimal("0.4")
    assert leg.remaining_quantity == Decimal("0.6")


def test_planned_spread_has_no_matched_quantity() -> None:
    position = PutCreditSpreadPosition(
        short_put=make_short(),
        long_put=make_long(),
        state=OptionPositionState.PLANNED,
    )

    assert position.matched_quantity == Decimal("0")
    assert not position.has_naked_short


def test_open_spread_requires_equal_confirmed_leg_quantities() -> None:
    position = PutCreditSpreadPosition(
        short_put=make_short(filled="1"),
        long_put=make_long(filled="1"),
        state=OptionPositionState.OPEN,
    )

    assert position.matched_quantity == Decimal("1")
    assert not position.has_naked_short


def test_partially_open_spread_counts_only_matched_quantity() -> None:
    position = PutCreditSpreadPosition(
        short_put=make_short(filled="0.4"),
        long_put=make_long(filled="1"),
        state=OptionPositionState.PARTIALLY_OPEN,
    )

    assert position.matched_quantity == Decimal("0.4")


def test_position_rejects_short_exposure_above_protective_long() -> None:
    with pytest.raises(ValueError, match="naked short"):
        PutCreditSpreadPosition(
            short_put=make_short(filled="1"),
            long_put=make_long(filled="0.4"),
            state=OptionPositionState.PARTIALLY_OPEN,
        )


def test_open_state_rejects_unmatched_quantities() -> None:
    with pytest.raises(ValueError, match="OPEN requires equal"):
        PutCreditSpreadPosition(
            short_put=make_short(filled="0.4"),
            long_put=make_long(filled="1"),
            state=OptionPositionState.OPEN,
        )


def test_short_submission_state_requires_confirmed_long_protection() -> None:
    with pytest.raises(ValueError, match="confirmed protective long"):
        PutCreditSpreadPosition(
            short_put=make_short(),
            long_put=make_long(),
            state=OptionPositionState.SHORT_PREMIUM_PENDING,
        )


def test_spread_rejects_expiry_mismatch() -> None:
    different_expiry = replace(
        make_long().contract,
        expiry_time_utc=datetime(2026, 8, 7, 8, tzinfo=timezone.utc),
    )

    with pytest.raises(ValueError, match="same expiry"):
        PutCreditSpreadPosition(
            short_put=make_short(),
            long_put=replace(make_long(), contract=different_expiry),
            state=OptionPositionState.PLANNED,
        )
