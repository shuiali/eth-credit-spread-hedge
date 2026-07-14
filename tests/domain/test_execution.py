"""Exchange-neutral private execution model tests."""

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from eth_credit_hedge.domain.execution import (
    AmendOrderRequest,
    CancelOrderRequest,
    ExchangeOrder,
    ExchangePosition,
    ExecutionUpdate,
    ExecutionUpdateBatch,
    LiveExecutionState,
    OrderRequestAck,
    OrderRequestKind,
    OrderUpdate,
    OrderUpdateBatch,
    PlaceOrderRequest,
    PrivateConnectionEvent,
    PrivateConnectionState,
    WalletBalance,
    WalletState,
)


NOW = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
ORDER_LINK_ID = "ECH-01-C0007-L01-ENTRY-A02-9F3C"


def test_place_order_request_is_immutable_and_normalizes_decimals() -> None:
    request = PlaceOrderRequest(
        category="linear",
        symbol="ETHUSDT",
        side="Sell",
        order_type="Limit",
        quantity=Decimal("0.010"),
        order_link_id=ORDER_LINK_ID,
        price=Decimal("3100.10"),
        time_in_force="GTC",
        reduce_only=False,
        position_idx=0,
    )

    assert request.quantity == Decimal("0.010")
    assert request.price == Decimal("3100.10")
    with pytest.raises(FrozenInstanceError):
        request.quantity = Decimal("1")  # type: ignore[misc]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"quantity": Decimal("0")}, "quantity must be positive"),
        ({"order_type": "Limit", "price": None}, "limit order requires a price"),
        (
            {"order_type": "Market", "price": Decimal("3000")},
            "market order cannot include a price",
        ),
        ({"trigger_price": Decimal("2990")}, "trigger direction is required"),
        ({"position_idx": 3}, "position index"),
    ],
)
def test_place_order_request_rejects_invalid_combinations(
    changes: dict[str, object],
    message: str,
) -> None:
    values: dict[str, object] = {
        "category": "linear",
        "symbol": "ETHUSDT",
        "side": "Sell",
        "order_type": "Market",
        "quantity": Decimal("0.01"),
        "order_link_id": ORDER_LINK_ID,
    }
    values.update(changes)

    with pytest.raises(ValueError, match=message):
        PlaceOrderRequest(**values)  # type: ignore[arg-type]


def test_trigger_order_requires_complete_trigger_definition() -> None:
    request = PlaceOrderRequest(
        category="linear",
        symbol="ETHUSDT",
        side="Buy",
        order_type="Market",
        quantity=Decimal("0.01"),
        order_link_id="ECH-01-C0007-L01-STOP-A01-A1B2",
        reduce_only=True,
        trigger_price=Decimal("3150.1"),
        trigger_direction=1,
        trigger_by="LastPrice",
    )

    assert request.trigger_price == Decimal("3150.1")
    assert request.trigger_direction == 1
    assert request.reduce_only


def test_amend_requires_a_positive_change_and_cancel_identifies_by_link_id() -> None:
    with pytest.raises(ValueError, match="at least one change"):
        AmendOrderRequest(
            category="linear",
            symbol="ETHUSDT",
            order_link_id=ORDER_LINK_ID,
        )
    with pytest.raises(ValueError, match="price must be positive"):
        AmendOrderRequest(
            category="linear",
            symbol="ETHUSDT",
            order_link_id=ORDER_LINK_ID,
            price=Decimal("0"),
        )

    cancel = CancelOrderRequest("linear", "ETHUSDT", ORDER_LINK_ID)

    assert cancel.order_link_id == ORDER_LINK_ID


def test_ack_and_order_report_do_not_create_execution_records() -> None:
    ack = OrderRequestAck(
        request_kind=OrderRequestKind.PLACE,
        order_id="order-1",
        order_link_id=ORDER_LINK_ID,
        acknowledged_at=NOW,
    )
    order = ExchangeOrder(
        category="linear",
        order_id="order-1",
        order_link_id=ORDER_LINK_ID,
        symbol="ETHUSDT",
        status="PartiallyFilled",
        side="Sell",
        order_type="Limit",
        price=Decimal("3100.1"),
        quantity=Decimal("0.02"),
        cumulative_filled_quantity=Decimal("0.01"),
        average_price=Decimal("3100.1"),
        reduce_only=False,
        created_at=NOW,
        updated_at=NOW + timedelta(seconds=1),
    )

    assert ack.request_kind is OrderRequestKind.PLACE
    assert order.cumulative_filled_quantity == Decimal("0.01")
    assert not hasattr(ack, "filled_quantity")
    assert not hasattr(order, "executions")


def test_order_update_and_execution_batches_are_strict_and_immutable() -> None:
    order_update = OrderUpdate(
        order_id="order-1",
        order_link_id=ORDER_LINK_ID,
        symbol="ETHUSDT",
        status="Filled",
        side="Sell",
        order_type="Limit",
        price=Decimal("3100.1"),
        quantity=Decimal("0.02"),
        cumulative_filled_quantity=Decimal("0.02"),
        average_price=Decimal("3100.1"),
        updated_at=NOW,
    )
    execution = ExecutionUpdate(
        execution_id="exec-1",
        order_id="order-1",
        order_link_id=ORDER_LINK_ID,
        symbol="ETHUSDT",
        side="Sell",
        price=Decimal("3100.1"),
        quantity=Decimal("0.02"),
        fee=Decimal("0.01"),
        is_maker=True,
        executed_at=NOW,
    )
    order_batch = OrderUpdateBatch(
        updates=(order_update,),
        received_at=NOW,
        raw_payload_hash="a" * 64,
    )
    execution_batch = ExecutionUpdateBatch(
        executions=(execution, execution),
        received_at=NOW,
        raw_payload_hash="b" * 64,
    )

    assert order_batch.updates == (order_update,)
    assert execution_batch.executions == (execution, execution)
    with pytest.raises(ValueError, match="execution price must be positive"):
        ExecutionUpdate(
            execution_id="exec-2",
            order_id="order-1",
            order_link_id=ORDER_LINK_ID,
            symbol="ETHUSDT",
            side="Sell",
            price=Decimal("0"),
            quantity=Decimal("0.01"),
            fee=Decimal("0"),
            is_maker=None,
            executed_at=NOW,
        )


def test_empty_private_batches_are_valid_normalized_noops() -> None:
    orders = OrderUpdateBatch(
        updates=(),
        received_at=NOW,
        raw_payload_hash="c" * 64,
    )
    executions = ExecutionUpdateBatch(
        executions=(),
        received_at=NOW,
        raw_payload_hash="d" * 64,
    )

    assert orders.updates == ()
    assert executions.executions == ()


def test_position_and_wallet_models_handle_flat_positions_and_signed_pnl() -> None:
    flat = ExchangePosition(
        category="linear",
        symbol="ETHUSDT",
        side=None,
        quantity=Decimal("0"),
        average_price=None,
        mark_price=Decimal("3000"),
        unrealized_pnl=Decimal("-1.25"),
        updated_at=NOW,
    )
    balance = WalletBalance(
        coin="USDT",
        equity=Decimal("1000"),
        wallet_balance=Decimal("1001.25"),
        available_balance=None,
        unrealized_pnl=Decimal("-1.25"),
    )
    wallet = WalletState(
        account_type="UNIFIED",
        total_equity=Decimal("1000"),
        total_wallet_balance=Decimal("1001.25"),
        total_available_balance=Decimal("900"),
        balances=(balance,),
        updated_at=NOW,
    )

    assert flat.side is None
    assert flat.average_price is None
    assert wallet.balances == (balance,)
    assert wallet.balances[0].available_balance is None

    with pytest.raises(ValueError, match="non-flat position requires a side"):
        ExchangePosition(
            category="linear",
            symbol="ETHUSDT",
            side=None,
            quantity=Decimal("1"),
            average_price=Decimal("3000"),
            mark_price=Decimal("3001"),
            unrealized_pnl=Decimal("1"),
            updated_at=NOW,
        )


def test_wallet_models_preserve_signed_distress_balances() -> None:
    balance = WalletBalance(
        coin="USDT",
        equity=Decimal("-10"),
        wallet_balance=Decimal("-8"),
        available_balance=Decimal("-12"),
        unrealized_pnl=Decimal("-2"),
    )
    wallet = WalletState(
        account_type="UNIFIED",
        total_equity=Decimal("-10"),
        total_wallet_balance=Decimal("-8"),
        total_available_balance=Decimal("-12"),
        balances=(balance,),
        updated_at=NOW,
    )

    assert wallet.total_equity == Decimal("-10")
    assert wallet.total_wallet_balance == Decimal("-8")
    assert wallet.balances[0].available_balance == Decimal("-12")


def test_all_live_execution_states_are_defined() -> None:
    assert {state.value for state in LiveExecutionState} == {
        "READY",
        "TRIGGERED",
        "ENTRY_REQUEST_PERSISTED",
        "ENTRY_SUBMITTED",
        "ENTRY_ACKNOWLEDGED",
        "ENTRY_PARTIALLY_FILLED",
        "ACTIVE_UNPROTECTED",
        "ACTIVE_PROTECTED",
        "EXIT_PARTIALLY_FILLED",
        "CLOSED_TP",
        "CLOSED_STOP",
        "CANCEL_PENDING",
        "RECONCILING",
        "LOCKED",
        "ERROR",
    }


def test_private_connection_event_normalizes_time_and_generation() -> None:
    event = PrivateConnectionEvent(
        state=PrivateConnectionState.AUTHENTICATED,
        observed_at=NOW,
        connection_generation=2,
    )

    assert event.state is PrivateConnectionState.AUTHENTICATED
    assert event.observed_at == NOW
    with pytest.raises(ValueError, match="generation cannot be negative"):
        PrivateConnectionEvent(
            state=PrivateConnectionState.DISCONNECTED,
            observed_at=NOW,
            connection_generation=-1,
        )


def test_models_reject_naive_timestamps_and_non_finite_decimals() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        OrderRequestAck(
            request_kind=OrderRequestKind.CANCEL,
            order_id="order-1",
            order_link_id=ORDER_LINK_ID,
            acknowledged_at=datetime(2026, 7, 14, 12),
        )
    with pytest.raises(ValueError, match="finite"):
        WalletBalance(
            coin="USDT",
            equity=Decimal("NaN"),
            wallet_balance=Decimal("1"),
            available_balance=Decimal("1"),
            unrealized_pnl=Decimal("0"),
        )
