"""Non-mutating capability probe for the sealed Bybit demo transport split."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Protocol, TypeVar, cast

from eth_credit_hedge.domain.execution import (
    PrivateConnectionEvent,
    PrivateConnectionState,
)
from eth_credit_hedge.infrastructure.bybit.clock import ServerClock
from eth_credit_hedge.infrastructure.bybit.environment import BybitDemoProfile
from eth_credit_hedge.infrastructure.bybit.private_rest import (
    BybitPrivateRestClient,
)
from eth_credit_hedge.infrastructure.bybit.private_ws import (
    BybitPrivateWebSocketClient,
)
from eth_credit_hedge.infrastructure.bybit.public_rest import (
    BybitPublicRestClient,
)
from eth_credit_hedge.infrastructure.bybit.public_ws import (
    BybitPublicWebSocketClient,
)
from eth_credit_hedge.ports.private_events import PrivateStreamEvent


PUBLIC_LINEAR_WEBSOCKET_URL = "wss://stream.bybit.com/v5/public/linear"
PUBLIC_OPTION_WEBSOCKET_URL = "wss://stream.bybit.com/v5/public/option"
REQUIRED_PRIVATE_TOPICS = ("order", "execution", "position")
_EventT = TypeVar("_EventT", covariant=True)


class _ClosableAsyncIterator(AsyncIterator[_EventT], Protocol):
    async def aclose(self) -> None: ...


@dataclass(frozen=True, slots=True)
class BybitDemoCapabilityResult:
    version: int
    checked_at_utc: datetime
    rest_base_url: str
    private_websocket_url: str
    public_linear_websocket_url: str
    public_option_websocket_url: str
    private_topics: tuple[str, ...]
    account_type: str
    margin_mode: str
    clock_offset_ms: Decimal
    clock_uncertainty_ms: Decimal
    linear_instrument_loaded: bool
    option_instrument_count: int
    linear_position_rows: int
    option_position_rows: int
    open_order_rows: int
    execution_rows: int
    private_websocket_authenticated: bool
    rest_order_mutations_only: bool

    @property
    def accepted(self) -> bool:
        return bool(
            self.rest_base_url == BybitDemoProfile.REST_BASE_URL
            and self.private_websocket_url
            == BybitDemoProfile.PRIVATE_WEBSOCKET_URL
            and self.public_linear_websocket_url
            == PUBLIC_LINEAR_WEBSOCKET_URL
            and self.public_option_websocket_url
            == PUBLIC_OPTION_WEBSOCKET_URL
            and self.private_topics == REQUIRED_PRIVATE_TOPICS
            and self.account_type == "UNIFIED"
            and self.linear_instrument_loaded
            and self.option_instrument_count > 0
            and self.private_websocket_authenticated
            and self.rest_order_mutations_only
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "accepted": self.accepted,
            "version": self.version,
            "checked_at_utc": self.checked_at_utc.isoformat(),
            "transport": {
                "rest_base_url": self.rest_base_url,
                "private_websocket_url": self.private_websocket_url,
                "public_linear_websocket_url": self.public_linear_websocket_url,
                "public_option_websocket_url": self.public_option_websocket_url,
                "private_topics": list(self.private_topics),
                "rest_order_mutations_only": self.rest_order_mutations_only,
            },
            "account": {
                "account_type": self.account_type,
                "margin_mode": self.margin_mode,
                "linear_position_rows": self.linear_position_rows,
                "option_position_rows": self.option_position_rows,
                "open_order_rows": self.open_order_rows,
                "execution_rows": self.execution_rows,
            },
            "clock": {
                "offset_ms": str(self.clock_offset_ms),
                "uncertainty_ms": str(self.clock_uncertainty_ms),
            },
            "market": {
                "linear_instrument_loaded": self.linear_instrument_loaded,
                "option_instrument_count": self.option_instrument_count,
            },
            "private_websocket_authenticated": (
                self.private_websocket_authenticated
            ),
        }


class BybitDemoCapabilityProbe:
    """Prove required read capabilities without calling a mutation method."""

    def __init__(
        self,
        *,
        profile: BybitDemoProfile,
        clock: ServerClock,
        private_rest: BybitPrivateRestClient,
        public_rest: BybitPublicRestClient,
        private_websocket: BybitPrivateWebSocketClient,
        websocket_timeout_seconds: float = 20.0,
    ) -> None:
        if websocket_timeout_seconds <= 0:
            raise ValueError("WebSocket timeout must be positive")
        self._profile = profile
        self._clock = clock
        self._private_rest = private_rest
        self._public_rest = public_rest
        self._private_websocket = private_websocket
        self._websocket_timeout_seconds = websocket_timeout_seconds

    async def probe(self) -> BybitDemoCapabilityResult:
        self._validate_static_transport()
        sample = await self._private_rest.synchronize_clock()
        margin_mode = await self._private_rest.get_margin_mode()
        wallet = await self._private_rest.get_wallet_state()
        linear_positions = await self._private_rest.get_positions("linear")
        option_positions = await self._private_rest.get_positions("option")
        linear_orders = await self._private_rest.get_open_orders("linear")
        option_orders = await self._private_rest.get_open_orders("option")
        linear_executions = await self._private_rest.get_execution_history("linear")
        option_executions = await self._private_rest.get_execution_history(
            "option"
        )
        linear_instrument = await self._public_rest.get_instrument("ETHUSDT")
        option_instruments = await self._public_rest.list_instruments(
            "option", base_coin="ETH"
        )
        authenticated = await self._probe_private_websocket()
        return BybitDemoCapabilityResult(
            version=1,
            checked_at_utc=datetime.now(timezone.utc),
            rest_base_url=self._profile.rest_base_url,
            private_websocket_url=self._profile.private_websocket_url,
            public_linear_websocket_url=BybitPublicWebSocketClient(
                category="linear"
            ).url,
            public_option_websocket_url=BybitPublicWebSocketClient(
                category="option"
            ).url,
            private_topics=REQUIRED_PRIVATE_TOPICS,
            account_type=wallet.account_type,
            margin_mode=margin_mode,
            clock_offset_ms=sample.offset_ms,
            clock_uncertainty_ms=sample.uncertainty_ms,
            linear_instrument_loaded=(
                linear_instrument.category == "linear"
                and linear_instrument.symbol == "ETHUSDT"
                and linear_instrument.status == "Trading"
            ),
            option_instrument_count=len(option_instruments),
            linear_position_rows=len(linear_positions),
            option_position_rows=len(option_positions),
            open_order_rows=len(linear_orders) + len(option_orders),
            execution_rows=len(linear_executions) + len(option_executions),
            private_websocket_authenticated=authenticated,
            rest_order_mutations_only=all(
                callable(getattr(self._private_rest, name, None))
                for name in ("place_order", "amend_order", "cancel_order")
            ),
        )

    def _validate_static_transport(self) -> None:
        if self._profile.rest_base_url != BybitDemoProfile.REST_BASE_URL:
            raise ValueError("demo REST endpoint is not sealed")
        if (
            self._profile.private_websocket_url
            != BybitDemoProfile.PRIVATE_WEBSOCKET_URL
        ):
            raise ValueError("demo private WebSocket endpoint is not sealed")
        for category, expected in (
            ("linear", PUBLIC_LINEAR_WEBSOCKET_URL),
            ("option", PUBLIC_OPTION_WEBSOCKET_URL),
        ):
            actual = BybitPublicWebSocketClient(category=category).url  # type: ignore[arg-type]
            if actual != expected:
                raise ValueError("demo public market data must use mainnet streams")

    async def _probe_private_websocket(self) -> bool:
        stream = cast(
            _ClosableAsyncIterator[PrivateStreamEvent],
            self._private_websocket.stream_events(),
        )
        try:
            event = await asyncio.wait_for(
                anext(stream),
                timeout=self._websocket_timeout_seconds,
            )
            return bool(
                isinstance(event, PrivateConnectionEvent)
                and event.state is PrivateConnectionState.AUTHENTICATED
                and self._private_websocket.new_entries_blocked
            )
        finally:
            await stream.aclose()


__all__ = [
    "BybitDemoCapabilityProbe",
    "BybitDemoCapabilityResult",
    "PUBLIC_LINEAR_WEBSOCKET_URL",
    "PUBLIC_OPTION_WEBSOCKET_URL",
    "REQUIRED_PRIVATE_TOPICS",
]
