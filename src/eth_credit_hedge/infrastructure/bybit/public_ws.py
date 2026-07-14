"""Supervised Bybit public WebSocket streams with generation fencing."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Protocol, cast

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from eth_credit_hedge.domain.market_data import (
    OrderBookEvent,
    TickerEvent,
    TradeEvent,
)
from eth_credit_hedge.infrastructure.bybit.parsers import (
    parse_orderbook_message,
    parse_ticker_message,
    parse_trade_message,
)


ZERO = Decimal("0")
WebSocketCategory = Literal["linear", "option"]


def _decimal(value: Decimal, field_name: str) -> Decimal:
    try:
        normalized = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be finite") from exc
    if not normalized.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return normalized


def _utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class ReconnectPolicy:
    initial_delay_seconds: Decimal = Decimal("1")
    multiplier: Decimal = Decimal("2")
    maximum_delay_seconds: Decimal = Decimal("30")

    def __post_init__(self) -> None:
        initial = _decimal(self.initial_delay_seconds, "initial reconnect delay")
        multiplier = _decimal(self.multiplier, "reconnect multiplier")
        maximum = _decimal(self.maximum_delay_seconds, "maximum reconnect delay")
        if initial <= ZERO:
            raise ValueError("initial reconnect delay must be positive")
        if multiplier < Decimal("1"):
            raise ValueError("reconnect multiplier must be at least one")
        if maximum < initial:
            raise ValueError("maximum reconnect delay cannot be below initial")
        object.__setattr__(self, "initial_delay_seconds", initial)
        object.__setattr__(self, "multiplier", multiplier)
        object.__setattr__(self, "maximum_delay_seconds", maximum)


@dataclass(frozen=True, slots=True)
class ConnectionRestorationPlan:
    generation: int
    subscriptions: tuple[str, ...]
    snapshot_topics: tuple[str, ...]


class MarketDataConnectionSupervisor:
    def __init__(
        self,
        *,
        reconnect_policy: ReconnectPolicy = ReconnectPolicy(),
        heartbeat_interval_seconds: Decimal = Decimal("20"),
        pong_timeout_seconds: Decimal = Decimal("5"),
    ) -> None:
        self.reconnect_policy = reconnect_policy
        self.heartbeat_interval_seconds = _decimal(
            heartbeat_interval_seconds,
            "heartbeat interval",
        )
        self.pong_timeout_seconds = _decimal(pong_timeout_seconds, "pong timeout")
        if self.heartbeat_interval_seconds <= ZERO:
            raise ValueError("heartbeat interval must be positive")
        if self.pong_timeout_seconds <= ZERO:
            raise ValueError("pong timeout must be positive")
        self.generation = 0
        self.connected = False
        self._subscriptions: set[str] = set()
        self._snapshot_topics: set[str] = set()
        self._last_activity_utc: datetime | None = None
        self._last_ping_utc: datetime | None = None
        self._last_pong_utc: datetime | None = None

    def register_subscription(
        self,
        topic: str,
        *,
        requires_snapshot: bool = False,
    ) -> None:
        if not topic.strip():
            raise ValueError("subscription topic cannot be empty")
        self._subscriptions.add(topic)
        if requires_snapshot:
            self._snapshot_topics.add(topic)

    def begin_connection(self, connected_at_utc: datetime) -> ConnectionRestorationPlan:
        connected_at = _utc(connected_at_utc, "connection time")
        self.generation += 1
        self.connected = True
        self._last_activity_utc = connected_at
        self._last_ping_utc = None
        self._last_pong_utc = None
        return ConnectionRestorationPlan(
            generation=self.generation,
            subscriptions=tuple(sorted(self._subscriptions)),
            snapshot_topics=tuple(sorted(self._snapshot_topics)),
        )

    def disconnect(self) -> None:
        self.connected = False

    def accept_event(self, generation: int, observed_at_utc: datetime) -> bool:
        if not self.connected or generation != self.generation:
            return False
        self._last_activity_utc = _utc(observed_at_utc, "event time")
        return True

    def heartbeat_due(self, as_of_utc: datetime) -> bool:
        if not self.connected or self._last_activity_utc is None:
            return False
        as_of = _utc(as_of_utc, "as of time")
        reference = max(
            timestamp
            for timestamp in (self._last_activity_utc, self._last_ping_utc)
            if timestamp is not None
        )
        elapsed = Decimal(str((as_of - reference).total_seconds()))
        return elapsed >= self.heartbeat_interval_seconds

    def record_ping(self, sent_at_utc: datetime) -> None:
        self._last_ping_utc = _utc(sent_at_utc, "ping time")

    def record_pong(self, received_at_utc: datetime) -> None:
        received = _utc(received_at_utc, "pong time")
        self._last_pong_utc = received
        self._last_activity_utc = received

    def pong_overdue(self, as_of_utc: datetime) -> bool:
        if self._last_ping_utc is None:
            return False
        if (
            self._last_pong_utc is not None
            and self._last_pong_utc >= self._last_ping_utc
        ):
            return False
        elapsed = Decimal(
            str((_utc(as_of_utc, "as of time") - self._last_ping_utc).total_seconds())
        )
        return elapsed > self.pong_timeout_seconds

    def reconnect_delay(self, attempt: int) -> Decimal:
        if attempt < 0:
            raise ValueError("reconnect attempt cannot be negative")
        delay = self.reconnect_policy.initial_delay_seconds * (
            self.reconnect_policy.multiplier**attempt
        )
        return min(delay, self.reconnect_policy.maximum_delay_seconds)


class WebSocketConnection(Protocol):
    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...


ConnectFactory = Callable[
    [str],
    AbstractAsyncContextManager[WebSocketConnection],
]
Sleep = Callable[[float], Awaitable[None]]


@asynccontextmanager
async def _default_connect(url: str) -> AsyncIterator[WebSocketConnection]:
    async with connect(
        url,
        ping_interval=None,
        ping_timeout=None,
        open_timeout=10,
        close_timeout=10,
    ) as websocket:
        yield cast(WebSocketConnection, websocket)


class BybitPublicWebSocketClient:
    def __init__(
        self,
        *,
        category: WebSocketCategory,
        testnet: bool = False,
        reconnect_policy: ReconnectPolicy = ReconnectPolicy(),
        connect_factory: ConnectFactory = _default_connect,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if category not in ("linear", "option"):
            raise ValueError("WebSocket category must be linear or option")
        host = "stream-testnet.bybit.com" if testnet else "stream.bybit.com"
        self.url = f"wss://{host}/v5/public/{category}"
        self.category = category
        self.reconnect_policy = reconnect_policy
        self._connect_factory = connect_factory
        self._sleep = sleep

    async def stream_ticker(self, symbol: str) -> AsyncIterator[TickerEvent]:
        topic = f"tickers.{symbol}"
        async for message, generation in self._stream_payloads(topic):
            yield parse_ticker_message(
                message,
                connection_generation=generation,
            )

    async def stream_trades(self, symbol: str) -> AsyncIterator[TradeEvent]:
        topic = f"publicTrade.{symbol}"
        async for message, generation in self._stream_payloads(topic):
            for event in parse_trade_message(
                message,
                connection_generation=generation,
            ):
                yield event

    async def stream_orderbook(
        self,
        symbol: str,
        depth: int,
    ) -> AsyncIterator[OrderBookEvent]:
        topic = f"orderbook.{depth}.{symbol}"
        async for message, generation in self._stream_payloads(
            topic,
            requires_snapshot=True,
        ):
            yield parse_orderbook_message(message, generation)

    async def _stream_payloads(
        self,
        topic: str,
        *,
        requires_snapshot: bool = False,
    ) -> AsyncIterator[tuple[dict[str, Any], int]]:
        supervisor = MarketDataConnectionSupervisor(
            reconnect_policy=self.reconnect_policy
        )
        supervisor.register_subscription(topic, requires_snapshot=requires_snapshot)
        attempt = 0
        while True:
            plan = supervisor.begin_connection(datetime.now(timezone.utc))
            try:
                async with self._connect_factory(self.url) as websocket:
                    await websocket.send(
                        json.dumps(
                            {
                                "req_id": f"generation-{plan.generation}",
                                "op": "subscribe",
                                "args": list(plan.subscriptions),
                            }
                        )
                    )
                    awaiting_snapshot = topic in plan.snapshot_topics
                    while True:
                        try:
                            raw_message = await asyncio.wait_for(
                                websocket.recv(),
                                timeout=float(supervisor.heartbeat_interval_seconds),
                            )
                        except TimeoutError:
                            sent_at = datetime.now(timezone.utc)
                            await websocket.send(
                                json.dumps(
                                    {
                                        "req_id": f"ping-{plan.generation}",
                                        "op": "ping",
                                    }
                                )
                            )
                            supervisor.record_ping(sent_at)
                            raw_message = await asyncio.wait_for(
                                websocket.recv(),
                                timeout=float(supervisor.pong_timeout_seconds),
                            )
                        message = _load_message(raw_message)
                        observed_at = datetime.now(timezone.utc)
                        if _is_pong(message):
                            supervisor.record_pong(observed_at)
                            continue
                        if _is_subscription_response(message):
                            failed_topics = _failed_subscription_topics(message)
                            if message.get("success") is False or failed_topics:
                                raise ValueError(
                                    f"Bybit subscription failed: "
                                    f"{message.get('ret_msg') or failed_topics}"
                                )
                            continue
                        if message.get("topic") != topic:
                            continue
                        if awaiting_snapshot:
                            if not _is_orderbook_snapshot(message):
                                continue
                            awaiting_snapshot = False
                        if not supervisor.accept_event(
                            plan.generation,
                            observed_at,
                        ):
                            continue
                        attempt = 0
                        yield message, plan.generation
            except asyncio.CancelledError:
                raise
            except (OSError, TimeoutError, ConnectionClosed):
                delay = supervisor.reconnect_delay(attempt)
                attempt += 1
                await self._sleep(float(delay))
            finally:
                supervisor.disconnect()


def _load_message(raw_message: str | bytes) -> dict[str, Any]:
    if isinstance(raw_message, bytes):
        raw_message = raw_message.decode("utf-8")
    parsed: object = json.loads(raw_message)
    if not isinstance(parsed, dict):
        raise ValueError("Bybit WebSocket message must be a JSON object")
    return cast(dict[str, Any], parsed)


def _is_pong(message: dict[str, Any]) -> bool:
    return message.get("op") == "pong" or (
        message.get("op") == "ping" and message.get("ret_msg") == "pong"
    )


def _is_orderbook_snapshot(message: dict[str, Any]) -> bool:
    if message.get("type") == "snapshot":
        return True
    data = message.get("data")
    return isinstance(data, dict) and data.get("u") == 1


def _is_subscription_response(message: dict[str, Any]) -> bool:
    return message.get("op") == "subscribe" or message.get("type") == "COMMAND_RESP"


def _failed_subscription_topics(message: dict[str, Any]) -> tuple[str, ...]:
    data = message.get("data")
    if not isinstance(data, dict):
        return ()
    failed = data.get("failTopics")
    if not isinstance(failed, list):
        return ()
    return tuple(str(topic) for topic in failed)
