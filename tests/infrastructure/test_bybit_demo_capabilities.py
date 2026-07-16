"""Read-only Bybit demo capability matrix fixtures."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import ClassVar

import pytest

from eth_credit_hedge.domain.execution import (
    PrivateConnectionEvent,
    PrivateConnectionState,
)
from eth_credit_hedge.infrastructure.bybit.auth import (
    ApiCredentials,
    SecretStr,
)
from eth_credit_hedge.infrastructure.bybit.clock import ServerClock
from eth_credit_hedge.infrastructure.bybit.demo_capabilities import (
    BybitDemoCapabilityProbe,
)
from eth_credit_hedge.infrastructure.bybit.environment import BybitDemoProfile


def profile() -> BybitDemoProfile:
    return BybitDemoProfile(
        ApiCredentials(SecretStr("key"), SecretStr("secret"))
    )


class FakePrivateRest:
    def __init__(self, *, account_type: str = "UNIFIED") -> None:
        self.account_type = account_type
        self.mutations = 0

    async def synchronize_clock(self) -> object:
        return SimpleNamespace(offset_ms=Decimal("1"), uncertainty_ms=Decimal("1"))

    async def get_margin_mode(self) -> str:
        return "REGULAR_MARGIN"

    async def get_wallet_state(self) -> object:
        return SimpleNamespace(account_type=self.account_type)

    async def get_positions(
        self,
        category: str,
        symbol: str | None = None,
    ) -> tuple[object, ...]:
        del category, symbol
        return ()

    async def get_open_orders(
        self,
        category: str,
        symbol: str | None = None,
    ) -> tuple[object, ...]:
        del category, symbol
        return ()

    async def get_execution_history(
        self,
        category: str,
        symbol: str | None = None,
    ) -> tuple[object, ...]:
        del category, symbol
        return ()

    async def place_order(self, request: object) -> None:
        del request
        self.mutations += 1

    async def amend_order(self, request: object) -> None:
        del request
        self.mutations += 1

    async def cancel_order(self, request: object) -> None:
        del request
        self.mutations += 1


class FakePublicRest:
    def __init__(self, *, option_count: int = 1) -> None:
        self.option_count = option_count

    async def get_instrument(self, symbol: str) -> object:
        return SimpleNamespace(category="linear", symbol=symbol, status="Trading")

    async def list_instruments(
        self,
        category: str,
        *,
        base_coin: str,
    ) -> tuple[object, ...]:
        del category, base_coin
        return tuple(object() for _ in range(self.option_count))


class _PrivateStream(AsyncIterator[object]):
    def __init__(self) -> None:
        self._sent = False

    def __aiter__(self) -> _PrivateStream:
        return self

    async def __anext__(self) -> object:
        if self._sent:
            raise StopAsyncIteration
        self._sent = True
        return PrivateConnectionEvent(
            state=PrivateConnectionState.AUTHENTICATED,
            observed_at=datetime.now(timezone.utc),
            connection_generation=1,
        )

    async def aclose(self) -> None:
        return None


class FakePrivateWebSocket:
    new_entries_blocked = True

    def stream_events(self) -> AsyncIterator[object]:
        return _PrivateStream()


class WrongRestProfile(BybitDemoProfile):
    REST_BASE_URL: ClassVar[str] = "https://api.bybit.com"


def probe(
    *,
    selected_profile: BybitDemoProfile | None = None,
    private: FakePrivateRest | None = None,
    public: FakePublicRest | None = None,
) -> BybitDemoCapabilityProbe:
    return BybitDemoCapabilityProbe(
        profile=selected_profile or profile(),
        clock=ServerClock(),
        private_rest=private or FakePrivateRest(),  # type: ignore[arg-type]
        public_rest=public or FakePublicRest(),  # type: ignore[arg-type]
        private_websocket=FakePrivateWebSocket(),  # type: ignore[arg-type]
        websocket_timeout_seconds=1,
    )


def test_supported_capability_fixture_is_accepted_without_mutation() -> None:
    private = FakePrivateRest()
    result = asyncio.run(probe(private=private).probe())
    assert result.accepted
    assert private.mutations == 0


def test_partially_supported_account_is_not_accepted() -> None:
    result = asyncio.run(
        probe(public=FakePublicRest(option_count=0)).probe()
    )
    assert not result.accepted
    assert result.option_instrument_count == 0


def test_unsupported_transport_is_rejected_before_network_reads() -> None:
    with pytest.raises(ValueError, match="REST endpoint is not sealed"):
        asyncio.run(
            probe(
                selected_profile=WrongRestProfile(profile().credentials)
            ).probe()
        )
