"""Durable soft pause, strategy close, and verified emergency flatten."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from eth_credit_hedge.application.emergency_flatten import EmergencyFlattenService
from eth_credit_hedge.application.kill_switch import (
    EmergencyKillSwitchExecutor,
    KillSwitchController,
    KillSwitchMode,
    StrategyCloseService,
)
from eth_credit_hedge.domain.execution import (
    ExchangeOrder,
    ExchangePosition,
    OrderRequestAck,
    OrderRequestKind,
    PlaceOrderRequest,
)
from eth_credit_hedge.infrastructure.persistence.file_kill_switch_store import (
    FileKillSwitchStore,
)
from eth_credit_hedge.infrastructure.persistence.sqlite_execution_store import (
    SqliteExecutionStore,
)


NOW = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
FLATTEN_ID = "ECH-01-C0001-L01-EC-A01-ABCD"


def controller(tmp_path) -> KillSwitchController:
    value = KillSwitchController(
        store=FileKillSwitchStore(tmp_path / "kill-switch.json"),
        clock=lambda: NOW,
    )
    asyncio.run(value.initialize())
    return value


def test_soft_pause_is_durable_and_reset_requires_operator_ack(tmp_path) -> None:
    first = controller(tmp_path)

    paused = asyncio.run(
        first.activate(
            KillSwitchMode.SOFT_PAUSE,
            reason="operator pause",
            requested_by="operator-1",
        )
    )

    assert paused.mode is KillSwitchMode.SOFT_PAUSE
    assert not first.entries_allowed
    assert first.protection_management_allowed
    assert first.reconciliation_allowed

    restarted = controller(tmp_path)
    assert restarted.state == paused
    assert not restarted.entries_allowed
    with pytest.raises(ValueError, match="operator acknowledgement"):
        asyncio.run(restarted.reset(requested_by="operator-2", operator_acknowledged=False))
    reset = asyncio.run(
        restarted.reset(requested_by="operator-2", operator_acknowledged=True)
    )
    assert reset.mode is KillSwitchMode.RUNNING
    assert restarted.entries_allowed


def test_kill_switch_can_only_escalate_until_explicit_reset(tmp_path) -> None:
    value = controller(tmp_path)
    asyncio.run(value.activate(KillSwitchMode.STRATEGY_CLOSE, reason="close", requested_by="op"))

    with pytest.raises(ValueError, match="cannot de-escalate"):
        asyncio.run(value.activate(KillSwitchMode.SOFT_PAUSE, reason="pause", requested_by="op"))
    emergency = asyncio.run(
        value.activate(KillSwitchMode.EMERGENCY_FLATTEN, reason="urgent", requested_by="op")
    )
    assert emergency.mode is KillSwitchMode.EMERGENCY_FLATTEN


class ClosingTrading:
    def __init__(self) -> None:
        self.calls = []

    async def cancel_all(self, category: str, symbol: str | None = None) -> None:
        self.calls.append(("cancel", category, symbol))


class ClosingOperations:
    def __init__(self) -> None:
        self.calls = []

    async def close_hedges(self) -> None:
        self.calls.append("close_hedges")

    async def close_option_spread(self) -> None:
        self.calls.append("close_option_spread")

    async def verify_strategy_closed(self) -> bool:
        self.calls.append("verify_strategy_closed")
        return True


def test_strategy_close_cancels_entries_closes_both_assets_and_verifies(tmp_path) -> None:
    switch = controller(tmp_path)
    trading = ClosingTrading()
    operations = ClosingOperations()
    service = StrategyCloseService(
        controller=switch,
        trading=trading,  # type: ignore[arg-type]
        operations=operations,
    )

    result = asyncio.run(service.close(reason="risk review", requested_by="operator"))

    assert result.verified_closed
    assert trading.calls == [
        ("cancel", "linear", "ETHUSDT"),
        ("cancel", "option", None),
    ]
    assert operations.calls == [
        "close_hedges",
        "close_option_spread",
        "verify_strategy_closed",
    ]
    assert not switch.entries_allowed


def short_position() -> ExchangePosition:
    return ExchangePosition(
        category="linear",
        symbol="ETHUSDT",
        side="Sell",
        quantity=Decimal("0.010"),
        average_price=Decimal("3000"),
        mark_price=Decimal("2999"),
        unrealized_pnl=Decimal("0.01"),
        updated_at=NOW,
    )


class EmergencyAccount:
    def __init__(self) -> None:
        self.responses = [(short_position(),), (short_position(),), ()]

    async def get_positions(self, category: str, symbol: str | None = None):
        return self.responses.pop(0)


class EmergencyTrading:
    def __init__(self) -> None:
        self.cancellations = []
        self.requests = []

    async def cancel_all(self, category: str, symbol: str | None = None) -> None:
        self.cancellations.append((category, symbol))

    async def place_order(self, request: PlaceOrderRequest) -> OrderRequestAck:
        self.requests.append(request)
        return OrderRequestAck(
            request_kind=OrderRequestKind.PLACE,
            order_id="flatten-order",
            order_link_id=request.order_link_id,
            acknowledged_at=NOW,
        )

    async def get_order_by_link_id(
        self,
        category: str,
        symbol: str,
        order_link_id: str,
    ) -> ExchangeOrder | None:
        return None


class NotificationRecorder:
    def __init__(self) -> None:
        self.notifications = []

    async def send(self, notification) -> None:
        self.notifications.append(notification)


def test_emergency_switch_cancels_persists_flattens_reconciles_and_alerts(
    tmp_path,
) -> None:
    switch = controller(tmp_path)
    trading = EmergencyTrading()
    account = EmergencyAccount()
    execution_store = SqliteExecutionStore(tmp_path / "execution.sqlite3")
    asyncio.run(execution_store.initialize())
    flatten = EmergencyFlattenService(
        trading=trading,  # type: ignore[arg-type]
        account=account,  # type: ignore[arg-type]
        store=execution_store,
        clock=lambda: NOW,
    )
    reconciliations = []

    async def reconcile() -> bool:
        reconciliations.append(True)
        return True

    notifications = NotificationRecorder()
    executor = EmergencyKillSwitchExecutor(
        controller=switch,
        trading=trading,  # type: ignore[arg-type]
        flatten=flatten,
        reconcile=reconcile,
        notifications=notifications,  # type: ignore[arg-type]
        order_link_id_factory=lambda attempt: FLATTEN_ID,
        maximum_attempts=2,
    )

    result = asyncio.run(executor.execute(reason="unprotected", requested_by="risk"))

    assert result.position_confirmed_flat
    assert result.reconciliation_succeeded
    assert trading.cancellations == [("linear", "ETHUSDT"), ("option", None)]
    assert len(trading.requests) == 1
    assert trading.requests[0].reduce_only
    assert awaitable_order_intent(execution_store, FLATTEN_ID) == trading.requests[0]
    assert reconciliations == [True]
    assert len(notifications.notifications) == 1


def awaitable_order_intent(store: SqliteExecutionStore, order_link_id: str):
    return asyncio.run(store.load_order_intent(order_link_id))
