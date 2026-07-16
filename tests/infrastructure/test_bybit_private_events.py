"""Bybit private event normalization and authenticated stream tests."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pytest

from eth_credit_hedge.domain.execution import (
    ExecutionUpdateBatch,
    OrderUpdateBatch,
    PrivateConnectionEvent,
    PrivateConnectionState,
)
from eth_credit_hedge.infrastructure.bybit.auth import (
    ApiCredentials,
    BybitV5Signer,
    SecretStr,
)
from eth_credit_hedge.infrastructure.bybit.private_parsers import (
    BybitPrivateEventParser,
    parse_execution_message,
    parse_position_message,
)
from eth_credit_hedge.infrastructure.bybit.private_ws import (
    BybitPrivateWebSocketClient,
    PrivateAuthenticationError,
    PrivateConnectionSupervisor,
    PrivateReconnectPolicy,
)


NOW = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
NOW_MS = 1_784_030_400_000


def order_message() -> dict[str, Any]:
    return {
        "id": "order-message-1",
        "topic": "order",
        "creationTime": NOW_MS,
        "data": [
            {
                "category": "linear",
                "orderId": "order-1",
                "orderLinkId": "ECH-01-C0001-L01-ENTRY-A01-9F3C",
                "symbol": "ETHUSDT",
                "side": "Sell",
                "orderType": "Limit",
                "orderStatus": "PartiallyFilled",
                "price": "",
                "qty": "1.25",
                "cumExecQty": "0.5",
                "avgPrice": "",
                "updatedTime": str(NOW_MS - 10),
            }
        ],
    }


def execution_message() -> dict[str, Any]:
    return {
        "id": "execution-message-1",
        "topic": "execution",
        "creationTime": NOW_MS,
        "data": [
            {
                "category": "linear",
                "execId": "execution-1",
                "orderId": "order-1",
                "orderLinkId": "ECH-01-C0001-L01-ENTRY-A01-9F3C",
                "symbol": "ETHUSDT",
                "side": "Sell",
                "execPrice": "3000.10",
                "execQty": "0.2",
                "execFee": "0.04",
                "isMaker": True,
                "execTime": str(NOW_MS - 8),
            },
            {
                "category": "linear",
                "execId": "execution-2",
                "orderId": "order-1",
                "orderLinkId": "ECH-01-C0001-L01-ENTRY-A01-9F3C",
                "symbol": "ETHUSDT",
                "side": "Sell",
                "execPrice": "3000.00",
                "execQty": "0.3",
                "execFee": "0.06",
                "execTime": str(NOW_MS - 5),
            },
        ],
    }


def position_message() -> dict[str, Any]:
    return {
        "id": "position-message-1",
        "topic": "position",
        "creationTime": NOW_MS,
        "data": [
            {
                "category": "linear",
                "symbol": "ETHUSDT",
                "side": "",
                "size": "0",
                "entryPrice": "",
                "markPrice": "3000.20",
                "liqPrice": "",
                "unrealisedPnl": "",
                "updatedTime": str(NOW_MS - 3),
                "positionIdx": 2,
            }
        ],
    }


def test_private_parser_normalizes_optional_decimals_and_order_duplicates() -> None:
    parser = BybitPrivateEventParser()

    first = parser.parse_message(order_message())
    duplicate = parser.parse_message(order_message())

    assert isinstance(first, OrderUpdateBatch)
    assert len(first.updates) == 1
    update = first.updates[0]
    assert update.order_id == "order-1"
    assert update.price is None
    assert update.quantity == Decimal("1.25")
    assert update.cumulative_filled_quantity == Decimal("0.5")
    assert update.average_price is None
    assert update.updated_at == datetime.fromtimestamp(
        (NOW_MS - 10) / 1000,
        tz=timezone.utc,
    )
    assert first.received_at == NOW
    assert len(first.raw_payload_hash) == 64
    assert isinstance(duplicate, OrderUpdateBatch)
    assert duplicate.updates == ()


def test_private_parser_treats_market_order_zero_prices_as_missing() -> None:
    message = order_message()
    item = message["data"][0]
    item["orderType"] = "Market"
    item["price"] = "0"
    item["avgPrice"] = "0"

    event = BybitPrivateEventParser().parse_message(message)

    assert isinstance(event, OrderUpdateBatch)
    assert event.updates[0].order_type == "Market"
    assert event.updates[0].price is None
    assert event.updates[0].average_price is None


def test_execution_parser_preserves_every_execution_in_one_message() -> None:
    batch = parse_execution_message(execution_message())

    assert isinstance(batch, ExecutionUpdateBatch)
    assert [execution.execution_id for execution in batch.executions] == [
        "execution-1",
        "execution-2",
    ]
    assert [execution.quantity for execution in batch.executions] == [
        Decimal("0.2"),
        Decimal("0.3"),
    ]
    assert batch.executions[0].is_maker is True
    assert batch.executions[1].is_maker is None
    assert batch.received_at == NOW


def test_position_parser_treats_empty_exchange_decimals_as_missing_or_zero() -> None:
    positions = parse_position_message(position_message())

    assert len(positions) == 1
    position = positions[0]
    assert position.category == "linear"
    assert position.symbol == "ETHUSDT"
    assert position.side is None
    assert position.quantity == Decimal("0")
    assert position.average_price is None
    assert position.mark_price == Decimal("3000.20")
    assert position.liquidation_price is None
    assert position.unrealized_pnl == Decimal("0")
    assert position.position_idx == 2


def test_position_parser_treats_flat_zero_entry_price_as_missing() -> None:
    message = position_message()
    message["data"][0]["entryPrice"] = "0"

    position = parse_position_message(message)[0]

    assert position.quantity == Decimal("0")
    assert position.average_price is None


class FakeSocket:
    def __init__(self, messages: list[dict[str, Any] | BaseException]) -> None:
        self.messages = list(messages)
        self.sent: list[dict[str, Any]] = []

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    async def recv(self) -> str:
        item = self.messages.pop(0)
        if isinstance(item, BaseException):
            raise item
        return json.dumps(item)


def auth_ack(*, success: bool = True) -> dict[str, Any]:
    return {
        "success": success,
        "ret_msg": "" if success else "invalid signature",
        "op": "auth",
        "conn_id": "private-connection-1",
    }


def subscribe_ack() -> dict[str, Any]:
    return {"success": True, "ret_msg": "", "op": "subscribe"}


def signer() -> BybitV5Signer:
    return BybitV5Signer(
        ApiCredentials(
            api_key=SecretStr("demo-api-key"),
            api_secret=SecretStr("demo-api-secret"),
        )
    )


def test_private_websocket_authenticates_before_one_subscription() -> None:
    socket = FakeSocket([auth_ack(), subscribe_ack(), order_message()])
    connected_urls: list[str] = []

    @asynccontextmanager
    async def connect_factory(url: str) -> AsyncIterator[FakeSocket]:
        connected_urls.append(url)
        yield socket

    async def receive() -> tuple[PrivateConnectionEvent, OrderUpdateBatch]:
        client = BybitPrivateWebSocketClient(
            signer=signer(),
            connect_factory=connect_factory,
            clock=lambda: NOW,
        )
        stream = client.stream_events()
        authenticated = await anext(stream)
        assert isinstance(authenticated, PrivateConnectionEvent)
        assert authenticated.state is PrivateConnectionState.AUTHENTICATED
        assert authenticated.connection_generation == 1
        assert client.new_entries_blocked
        client.mark_reconciled(authenticated.connection_generation)
        assert not client.new_entries_blocked
        event = await anext(stream)
        await stream.aclose()
        assert isinstance(event, OrderUpdateBatch)
        return authenticated, event

    _, event = asyncio.run(receive())
    assert event.updates[0].order_id == "order-1"
    expires_at_ms = NOW_MS + 1_000
    expected_signature = hmac.new(
        b"demo-api-secret",
        f"GET/realtime{expires_at_ms}".encode(),
        hashlib.sha256,
    ).hexdigest()
    assert connected_urls == ["wss://stream-demo.bybit.com/v5/private"]
    assert socket.sent == [
        {
            "req_id": "auth-generation-1",
            "op": "auth",
            "args": ["demo-api-key", expires_at_ms, expected_signature],
        },
        {
            "req_id": "subscribe-generation-1",
            "op": "subscribe",
            "args": ["order", "execution", "position"],
        },
    ]


def test_private_websocket_rejects_auth_failure_without_subscribing() -> None:
    socket = FakeSocket([auth_ack(success=False)])

    @asynccontextmanager
    async def connect_factory(_: str) -> AsyncIterator[FakeSocket]:
        yield socket

    async def receive() -> None:
        client = BybitPrivateWebSocketClient(
            signer=signer(),
            connect_factory=connect_factory,
            clock=lambda: NOW,
        )
        with pytest.raises(PrivateAuthenticationError, match="authentication failed"):
            await anext(client.stream_events())

    asyncio.run(receive())
    assert [message["op"] for message in socket.sent] == ["auth"]


def test_disconnect_blocks_entries_reconnects_and_requires_reconciliation() -> None:
    sockets = [
        FakeSocket(
            [
                auth_ack(),
                subscribe_ack(),
                order_message(),
                OSError("connection lost"),
            ]
        ),
        FakeSocket([auth_ack(), subscribe_ack(), execution_message()]),
    ]
    connection_count = 0
    sleeps: list[float] = []

    @asynccontextmanager
    async def connect_factory(_: str) -> AsyncIterator[FakeSocket]:
        nonlocal connection_count
        socket = sockets[connection_count]
        connection_count += 1
        yield socket

    async def no_wait(delay: float) -> None:
        sleeps.append(delay)

    async def receive() -> tuple[object, object, object, object, bool]:
        client = BybitPrivateWebSocketClient(
            signer=signer(),
            connect_factory=connect_factory,
            clock=lambda: NOW,
            sleep=no_wait,
        )
        stream = client.stream_events()
        connected = await anext(stream)
        assert isinstance(connected, PrivateConnectionEvent)
        client.mark_reconciled(connected.connection_generation)
        order = await anext(stream)
        disconnected = await anext(stream)
        assert client.new_entries_blocked
        reconnected = await anext(stream)
        assert isinstance(reconnected, PrivateConnectionEvent)
        assert reconnected.state is PrivateConnectionState.AUTHENTICATED
        assert reconnected.connection_generation == 2
        still_blocked = client.new_entries_blocked
        client.mark_reconciled(reconnected.connection_generation)
        execution = await anext(stream)
        assert not client.new_entries_blocked
        await stream.aclose()
        return order, disconnected, reconnected, execution, still_blocked

    order, disconnected, reconnected, execution, still_blocked = asyncio.run(
        receive()
    )
    assert isinstance(order, OrderUpdateBatch)
    assert isinstance(disconnected, PrivateConnectionEvent)
    assert disconnected.state is PrivateConnectionState.DISCONNECTED
    assert disconnected.connection_generation == 1
    assert disconnected.reason == "connection lost"
    assert isinstance(reconnected, PrivateConnectionEvent)
    assert isinstance(execution, ExecutionUpdateBatch)
    assert still_blocked
    assert sleeps == [1.0]


def test_private_websocket_sends_heartbeat_and_fences_stale_generations() -> None:
    socket = FakeSocket(
        [
            auth_ack(),
            subscribe_ack(),
            TimeoutError(),
            {"success": True, "ret_msg": "pong", "op": "ping"},
            position_message(),
        ]
    )

    @asynccontextmanager
    async def connect_factory(_: str) -> AsyncIterator[FakeSocket]:
        yield socket

    async def receive() -> object:
        client = BybitPrivateWebSocketClient(
            signer=signer(),
            connect_factory=connect_factory,
            clock=lambda: NOW,
        )
        stream = client.stream_events()
        authenticated = await anext(stream)
        assert isinstance(authenticated, PrivateConnectionEvent)
        event = await anext(stream)
        await stream.aclose()
        return event

    positions = asyncio.run(receive())
    assert isinstance(positions, tuple)
    assert positions[0].symbol == "ETHUSDT"
    assert socket.sent[-1] == {"req_id": "ping-generation-1", "op": "ping"}

    supervisor = PrivateConnectionSupervisor()
    first_generation = supervisor.begin_connection(NOW)
    supervisor.mark_authenticated(first_generation, NOW)
    supervisor.mark_disconnected(first_generation, NOW, "lost")
    second_generation = supervisor.begin_connection(NOW)
    supervisor.mark_authenticated(second_generation, NOW)
    assert not supervisor.accept_event(first_generation, NOW)
    assert supervisor.accept_event(second_generation, NOW)


def test_private_reconnect_policy_caps_delay_and_attempts() -> None:
    policy = PrivateReconnectPolicy(
        initial_delay_seconds=Decimal("1"),
        multiplier=Decimal("2"),
        maximum_delay_seconds=Decimal("5"),
        maximum_attempts=3,
    )

    assert [policy.delay(attempt) for attempt in range(3)] == [
        Decimal("1"),
        Decimal("2"),
        Decimal("4"),
    ]
    assert policy.delay(20) == Decimal("5")
    assert policy.can_retry(2)
    assert not policy.can_retry(3)
