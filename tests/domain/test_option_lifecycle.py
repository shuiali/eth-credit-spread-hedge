"""Option entry policy, expiry lifecycle, and snapshot tests."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from eth_credit_hedge.domain.instruments import OptionContract, OptionFill
from eth_credit_hedge.domain.option_lifecycle import (
    OptionEntryPolicy,
    OptionLifecycleEvent,
    OptionLifecyclePolicy,
    UnmatchedLongPolicy,
)
from eth_credit_hedge.domain.option_position import (
    OptionLegPosition,
    OptionPositionState,
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


def make_leg(
    *,
    symbol: str,
    strike: str,
    leg_side: str,
    fill_side: str,
    price: str,
    timestamp: datetime,
) -> OptionLegPosition:
    fill = OptionFill(
        order_id=f"order-{fill_side.lower()}",
        execution_id=f"execution-{fill_side.lower()}",
        symbol=symbol,
        side=fill_side,  # type: ignore[arg-type]
        price=Decimal(price),
        quantity=Decimal("1"),
        fee=Decimal("0.10"),
        timestamp_utc=timestamp,
    )
    return OptionLegPosition.from_fills(
        contract=make_contract(symbol, strike),
        side=leg_side,  # type: ignore[arg-type]
        requested_quantity=Decimal("1"),
        fills=(fill,),
    )


def make_position() -> PutCreditSpreadPosition:
    return PutCreditSpreadPosition(
        short_put=make_leg(
            symbol=SHORT_SYMBOL,
            strike="3000",
            leg_side="Short",
            fill_side="Sell",
            price="60",
            timestamp=NOW + timedelta(seconds=1),
        ),
        long_put=make_leg(
            symbol=LONG_SYMBOL,
            strike="2900",
            leg_side="Long",
            fill_side="Buy",
            price="30",
            timestamp=NOW,
        ),
        state=OptionPositionState.OPEN,
    )


def test_entry_policy_applies_partial_quantity_and_credit_deviation_limits() -> None:
    strict_policy = OptionEntryPolicy(
        max_leg_wait_seconds=Decimal("10"),
        allow_partial_spread=False,
        minimum_matched_quantity=Decimal("0.25"),
        maximum_credit_deviation=Decimal("2"),
    )
    partial_policy = OptionEntryPolicy(
        max_leg_wait_seconds=Decimal("10"),
        allow_partial_spread=True,
        minimum_matched_quantity=Decimal("0.25"),
        maximum_credit_deviation=Decimal("2"),
        unmatched_long_policy=UnmatchedLongPolicy.RETAIN,
    )

    assert strict_policy.accepts_completion(
        requested_quantity=Decimal("1"),
        matched_quantity=Decimal("1"),
        credit_deviation=Decimal("2"),
    )
    assert not strict_policy.accepts_completion(
        requested_quantity=Decimal("1"),
        matched_quantity=Decimal("0.5"),
        credit_deviation=Decimal("1"),
    )
    assert partial_policy.accepts_completion(
        requested_quantity=Decimal("1"),
        matched_quantity=Decimal("0.5"),
        credit_deviation=Decimal("1"),
    )
    assert not partial_policy.accepts_completion(
        requested_quantity=Decimal("1"),
        matched_quantity=Decimal("0.2"),
        credit_deviation=Decimal("1"),
    )
    assert not partial_policy.accepts_completion(
        requested_quantity=Decimal("1"),
        matched_quantity=Decimal("0.5"),
        credit_deviation=Decimal("2.01"),
    )
    assert partial_policy.unmatched_long_policy is UnmatchedLongPolicy.RETAIN


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_leg_wait_seconds", "0"),
        ("minimum_matched_quantity", "0"),
        ("maximum_credit_deviation", "-0.01"),
    ],
)
def test_entry_policy_rejects_invalid_limits(field: str, value: str) -> None:
    values: dict[str, object] = {
        "max_leg_wait_seconds": Decimal("10"),
        "allow_partial_spread": True,
        "minimum_matched_quantity": Decimal("0.25"),
        "maximum_credit_deviation": Decimal("2"),
    }
    values[field] = Decimal(value)

    with pytest.raises(ValueError, match=field.replace("_", " ")):
        OptionEntryPolicy(**values)  # type: ignore[arg-type]


def make_lifecycle_policy() -> OptionLifecyclePolicy:
    return OptionLifecyclePolicy(
        last_new_hedge_time_utc=EXPIRY - timedelta(hours=24),
        last_option_adjustment_time_utc=EXPIRY - timedelta(hours=2),
        forced_close_time_utc=EXPIRY - timedelta(minutes=30),
        expiry_time_utc=EXPIRY,
    )


def test_expiry_policy_enforces_cutoffs_and_forced_close() -> None:
    policy = make_lifecycle_policy()

    assert policy.allows_new_hedge(EXPIRY - timedelta(hours=25))
    assert not policy.allows_new_hedge(EXPIRY - timedelta(hours=24))
    assert policy.allows_option_adjustment(EXPIRY - timedelta(hours=3))
    assert not policy.allows_option_adjustment(EXPIRY - timedelta(hours=2))
    assert not policy.requires_forced_close(EXPIRY - timedelta(minutes=31))
    assert policy.requires_forced_close(EXPIRY - timedelta(minutes=30))
    assert not policy.requires_forced_close(EXPIRY)


def test_expiry_policy_reports_ordered_lifecycle_events() -> None:
    policy = make_lifecycle_policy()

    assert policy.events_due(EXPIRY - timedelta(hours=25)) == ()
    assert policy.events_due(EXPIRY - timedelta(hours=1)) == (
        OptionLifecycleEvent.OPTION_EXPIRY_APPROACHING,
        OptionLifecycleEvent.OPTION_TRADING_CUTOFF,
    )
    assert policy.events_due(EXPIRY, settled=False) == (
        OptionLifecycleEvent.OPTION_EXPIRY_APPROACHING,
        OptionLifecycleEvent.OPTION_TRADING_CUTOFF,
        OptionLifecycleEvent.OPTION_DELIVERY,
    )
    assert policy.events_due(EXPIRY, settled=True)[-1] is (
        OptionLifecycleEvent.OPTION_SETTLED
    )


def test_expiry_policy_rejects_out_of_order_or_naive_times() -> None:
    with pytest.raises(ValueError, match="ordered"):
        OptionLifecyclePolicy(
            last_new_hedge_time_utc=EXPIRY - timedelta(hours=1),
            last_option_adjustment_time_utc=EXPIRY - timedelta(hours=2),
            forced_close_time_utc=EXPIRY - timedelta(minutes=30),
            expiry_time_utc=EXPIRY,
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        OptionLifecyclePolicy(
            last_new_hedge_time_utc=datetime(2026, 7, 30, 8),
            last_option_adjustment_time_utc=EXPIRY - timedelta(hours=2),
            forced_close_time_utc=EXPIRY - timedelta(minutes=30),
            expiry_time_utc=EXPIRY,
        )


def test_position_snapshot_persists_fill_derived_values() -> None:
    snapshot = make_position().snapshot()

    assert snapshot.short_symbol == SHORT_SYMBOL
    assert snapshot.long_symbol == LONG_SYMBOL
    assert snapshot.matched_quantity == Decimal("1")
    assert snapshot.short_average_entry_price == Decimal("60")
    assert snapshot.long_average_entry_price == Decimal("30")
    assert snapshot.actual_net_credit == Decimal("29.80")
    assert snapshot.total_fees == Decimal("0.20")
    assert snapshot.opened_time_utc == NOW + timedelta(seconds=1)
    assert snapshot.expiry_time_utc == EXPIRY
    assert snapshot.state is OptionPositionState.OPEN

    serialized = snapshot.to_dict()
    assert serialized["actual_net_credit"] == "29.80"
    assert serialized["opened_time_utc"] == "2026-07-14T12:00:01+00:00"
    assert serialized["state"] == "OPEN"
