"""Authenticated, supervised Bybit demo private WebSocket stream."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol, cast

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from eth_credit_hedge.domain.execution import (
    OrderUpdateBatch,
    PrivateConnectionEvent,
    PrivateConnectionState,
)
from eth_credit_hedge.infrastructure.bybit.auth import BybitV5Signer
from eth_credit_hedge.infrastructure.bybit.private_parsers import (
    BybitPrivateEventParser,
)
from eth_credit_hedge.ports.private_events import PrivateStreamEvent


DEMO_PRIVATE_WEBSOCKET_URL = "wss://stream-demo.bybit.com/v5/private"
PRIVATE_TOPICS = ("order", "execution", "position")
ZERO = Decimal("0")

class PrivateAuthenticationError(RuntimeError):
    """Raised when Bybit rejects private WebSocket authentication."""


class PrivateSubscriptionError(RuntimeError):
    """Raised when Bybit rejects the required private topic subscription."""


class PrivateReconnectExhausted(ConnectionError):
    """Raised after the configured number of reconnect attempts is exhausted."""


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
class PrivateReconnectPolicy:
    initial_delay_seconds: Decimal = Decimal("1")
    multiplier: Decimal = Decimal("2")
    maximum_delay_seconds: Decimal = Decimal("30")
    maximum_attempts: int = 5

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
        if self.maximum_attempts < 0:
            raise ValueError("maximum reconnect attempts cannot be negative")
        object.__setattr__(self, "initial_delay_seconds", initial)
        object.__setattr__(self, "multiplier", multiplier)
        object.__setattr__(self, "maximum_delay_seconds", maximum)

    def delay(self, attempt: int) -> Decimal:
        if attempt < 0:
            raise ValueError("reconnect attempt cannot be negative")
        delay = self.initial_delay_seconds * (self.multiplier**attempt)
        return min(delay, self.maximum_delay_seconds)

    def can_retry(self, completed_attempts: int) -> bool:
        if completed_attempts < 0:
            raise ValueError("completed reconnect attempts cannot be negative")
        return completed_attempts < self.maximum_attempts


class PrivateConnectionSupervisor:
    """Fence generations and block entries until reconnect reconciliation."""

    def __init__(self) -> None:
        self.connection_generation = 0
        self.state = PrivateConnectionState.DISCONNECTED
        self.reconciliation_required = True
        self.new_entries_blocked = True
        self._last_activity_utc: datetime | None = None

    def begin_connection(self, observed_at_utc: datetime) -> int:
        observed_at = _utc(observed_at_utc, "connection time")
        self.connection_generation += 1
        self.state = PrivateConnectionState.CONNECTED
        self.new_entries_blocked = True
        self._last_activity_utc = observed_at
        return self.connection_generation

    def mark_authenticated(
        self,
        generation: int,
        observed_at_utc: datetime,
    ) -> PrivateConnectionEvent:
        self._require_current_generation(generation)
        if self.state is not PrivateConnectionState.CONNECTED:
            raise ValueError("private connection must be connected before authentication")
        observed_at = _utc(observed_at_utc, "authentication time")
        self.state = PrivateConnectionState.AUTHENTICATED
        self._last_activity_utc = observed_at
        self.new_entries_blocked = self.reconciliation_required
        return PrivateConnectionEvent(
            state=self.state,
            observed_at=observed_at,
            connection_generation=generation,
        )

    def mark_disconnected(
        self,
        generation: int,
        observed_at_utc: datetime,
        reason: str,
    ) -> PrivateConnectionEvent:
        self._require_current_generation(generation)
        observed_at = _utc(observed_at_utc, "disconnect time")
        self.state = PrivateConnectionState.DISCONNECTED
        self.reconciliation_required = True
        self.new_entries_blocked = True
        self._last_activity_utc = observed_at
        return PrivateConnectionEvent(
            state=self.state,
            observed_at=observed_at,
            connection_generation=generation,
            reason=reason or "private WebSocket disconnected",
        )

    def mark_reconnecting(
        self,
        generation: int,
        observed_at_utc: datetime,
    ) -> PrivateConnectionEvent:
        self._require_current_generation(generation)
        observed_at = _utc(observed_at_utc, "reconnect time")
        self.state = PrivateConnectionState.RECONNECTING
        self.new_entries_blocked = True
        self._last_activity_utc = observed_at
        return PrivateConnectionEvent(
            state=self.state,
            observed_at=observed_at,
            connection_generation=generation,
            reason="reconciliation required after disconnect",
        )

    def accept_event(self, generation: int, observed_at_utc: datetime) -> bool:
        if (
            generation != self.connection_generation
            or self.state is not PrivateConnectionState.AUTHENTICATED
        ):
            return False
        self._last_activity_utc = _utc(observed_at_utc, "private event time")
        return True

    def mark_reconciled(self, generation: int) -> None:
        self._require_current_generation(generation)
        if self.state is not PrivateConnectionState.AUTHENTICATED:
            raise ValueError("private connection must be authenticated to reconcile")
        self.reconciliation_required = False
        self.new_entries_blocked = False

    def _require_current_generation(self, generation: int) -> None:
        if generation != self.connection_generation:
            raise ValueError("private connection generation is stale")


class WebSocketConnection(Protocol):
    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...


ConnectFactory = Callable[
    [str],
    AbstractAsyncContextManager[WebSocketConnection],
]
Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], datetime]


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


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


class BybitPrivateWebSocketClient:
    """Receive demo-account private events through one authenticated connection."""

    def __init__(
        self,
        *,
        signer: BybitV5Signer,
        reconnect_policy: PrivateReconnectPolicy = PrivateReconnectPolicy(),
        connect_factory: ConnectFactory = _default_connect,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = _default_clock,
        heartbeat_interval_seconds: Decimal = Decimal("20"),
        pong_timeout_seconds: Decimal = Decimal("5"),
        auth_expiry_seconds: Decimal = Decimal("1"),
    ) -> None:
        self.url = DEMO_PRIVATE_WEBSOCKET_URL
        self._signer = signer
        self._reconnect_policy = reconnect_policy
        self._connect_factory = connect_factory
        self._sleep = sleep
        self._clock = clock
        self._heartbeat_interval_seconds = _decimal(
            heartbeat_interval_seconds,
            "heartbeat interval",
        )
        self._pong_timeout_seconds = _decimal(
            pong_timeout_seconds,
            "pong timeout",
        )
        self._auth_expiry_seconds = _decimal(
            auth_expiry_seconds,
            "authentication expiry",
        )
        if self._heartbeat_interval_seconds <= ZERO:
            raise ValueError("heartbeat interval must be positive")
        if self._pong_timeout_seconds <= ZERO:
            raise ValueError("pong timeout must be positive")
        if self._auth_expiry_seconds <= ZERO:
            raise ValueError("authentication expiry must be positive")
        self._supervisor = PrivateConnectionSupervisor()
        self._parser = BybitPrivateEventParser()

    @property
    def connection_generation(self) -> int:
        return self._supervisor.connection_generation

    @property
    def connection_state(self) -> PrivateConnectionState:
        return self._supervisor.state

    @property
    def reconciliation_required(self) -> bool:
        return self._supervisor.reconciliation_required

    @property
    def new_entries_blocked(self) -> bool:
        return self._supervisor.new_entries_blocked

    def mark_reconciled(self, generation: int) -> None:
        self._supervisor.mark_reconciled(generation)

    async def stream_events(self) -> AsyncIterator[PrivateStreamEvent]:
        reconnect_attempt = 0
        while True:
            generation = self._supervisor.begin_connection(self._now())
            try:
                async with self._connect_factory(self.url) as websocket:
                    await self._authenticate(websocket, generation)
                    authenticated = self._supervisor.mark_authenticated(
                        generation,
                        self._now(),
                    )
                    await self._subscribe(websocket, generation)
                    yield authenticated
                    while True:
                        message = await self._receive_with_heartbeat(
                            websocket,
                            generation,
                        )
                        if _is_pong(message) or _is_command_response(message):
                            continue
                        if not self._supervisor.accept_event(
                            generation,
                            self._now(),
                        ):
                            continue
                        reconnect_attempt = 0
                        event = self._parser.parse_message(message)
                        if isinstance(event, OrderUpdateBatch) and not event.updates:
                            continue
                        yield event
            except asyncio.CancelledError:
                raise
            except (OSError, TimeoutError, ConnectionClosed) as exc:
                disconnected = self._supervisor.mark_disconnected(
                    generation,
                    self._now(),
                    str(exc),
                )
                yield disconnected
                if not self._reconnect_policy.can_retry(reconnect_attempt):
                    raise PrivateReconnectExhausted(
                        "Bybit private WebSocket reconnect attempts exhausted"
                    ) from exc
                self._supervisor.mark_reconnecting(generation, self._now())
                delay = self._reconnect_policy.delay(reconnect_attempt)
                reconnect_attempt += 1
                await self._sleep(float(delay))
            finally:
                if self._supervisor.state in (
                    PrivateConnectionState.CONNECTED,
                    PrivateConnectionState.AUTHENTICATED,
                ):
                    self._supervisor.mark_disconnected(
                        generation,
                        self._now(),
                        "private stream closed",
                    )

    async def _authenticate(
        self,
        websocket: WebSocketConnection,
        generation: int,
    ) -> None:
        now = self._now()
        expires_at_ms = int(
            now.timestamp() * 1000 + float(self._auth_expiry_seconds * 1000)
        )
        auth = self._signer.sign_websocket_auth(expires_at_ms=expires_at_ms)
        await websocket.send(
            json.dumps(
                {
                    "req_id": f"auth-generation-{generation}",
                    "op": "auth",
                    "args": list(auth.as_auth_args()),
                }
            )
        )
        message = _load_message(await websocket.recv())
        if message.get("op") != "auth" or message.get("success") is not True:
            reason = message.get("ret_msg") or "unknown error"
            raise PrivateAuthenticationError(
                f"Bybit private authentication failed: {reason}"
            )

    async def _subscribe(
        self,
        websocket: WebSocketConnection,
        generation: int,
    ) -> None:
        await websocket.send(
            json.dumps(
                {
                    "req_id": f"subscribe-generation-{generation}",
                    "op": "subscribe",
                    "args": list(PRIVATE_TOPICS),
                }
            )
        )
        message = _load_message(await websocket.recv())
        failed_topics = _failed_subscription_topics(message)
        if (
            not _is_subscription_response(message)
            or message.get("success") is False
            or failed_topics
        ):
            reason = message.get("ret_msg") or failed_topics or "unknown error"
            raise PrivateSubscriptionError(
                f"Bybit private subscription failed: {reason}"
            )

    async def _receive_with_heartbeat(
        self,
        websocket: WebSocketConnection,
        generation: int,
    ) -> dict[str, Any]:
        try:
            raw_message = await asyncio.wait_for(
                websocket.recv(),
                timeout=float(self._heartbeat_interval_seconds),
            )
        except TimeoutError:
            await websocket.send(
                json.dumps(
                    {
                        "req_id": f"ping-generation-{generation}",
                        "op": "ping",
                    }
                )
            )
            raw_message = await asyncio.wait_for(
                websocket.recv(),
                timeout=float(self._pong_timeout_seconds),
            )
        return _load_message(raw_message)

    def _now(self) -> datetime:
        return _utc(self._clock(), "clock time")


def _load_message(raw_message: str | bytes) -> dict[str, Any]:
    if isinstance(raw_message, bytes):
        raw_message = raw_message.decode("utf-8")
    parsed: object = json.loads(raw_message)
    if not isinstance(parsed, dict):
        raise ValueError("Bybit private WebSocket message must be a JSON object")
    return cast(dict[str, Any], parsed)


def _is_pong(message: dict[str, Any]) -> bool:
    return message.get("op") == "pong" or (
        message.get("op") == "ping" and message.get("ret_msg") == "pong"
    )


def _is_subscription_response(message: dict[str, Any]) -> bool:
    return message.get("op") == "subscribe" or message.get("type") == "COMMAND_RESP"


def _is_command_response(message: dict[str, Any]) -> bool:
    return message.get("op") in ("auth", "subscribe") or (
        message.get("type") == "COMMAND_RESP"
    )


def _failed_subscription_topics(message: dict[str, Any]) -> tuple[str, ...]:
    data = message.get("data")
    if not isinstance(data, dict):
        return ()
    failed = data.get("failTopics")
    if not isinstance(failed, list):
        return ()
    return tuple(str(topic) for topic in failed)
