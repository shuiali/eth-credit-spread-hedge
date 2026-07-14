"""Opt-in read-only Bybit demo private-state smoke test."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

import pytest

from eth_credit_hedge.application.read_only_reconciliation import (
    BybitPrivateStateReader,
)
from eth_credit_hedge.config.bybit import load_bybit_demo_profile
from eth_credit_hedge.domain.execution import (
    PrivateConnectionEvent,
    PrivateConnectionState,
)
from eth_credit_hedge.infrastructure.bybit.auth import BybitV5Signer
from eth_credit_hedge.infrastructure.bybit.clock import ServerClock
from eth_credit_hedge.infrastructure.bybit.private_rest import (
    BybitPrivateRestClient,
)
from eth_credit_hedge.infrastructure.bybit.private_ws import (
    BybitPrivateWebSocketClient,
)


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("RUN_BYBIT_DEMO_READ_ONLY") != "1",
    reason="set RUN_BYBIT_DEMO_READ_ONLY=1 for demo private read-only checks",
)
def test_live_demo_private_state_and_websocket_authenticate_read_only() -> None:
    async def verify() -> None:
        profile = load_bybit_demo_profile()
        clock = ServerClock()
        rest = BybitPrivateRestClient(profile=profile, clock=clock)
        await rest.synchronize_clock()
        reader = BybitPrivateStateReader(
            trading=rest,
            account=rest,
            clock=lambda: rest_clock_time(clock),
        )
        snapshot = await reader.capture()
        assert snapshot.wallet.account_type == "UNIFIED"
        assert all(
            position.category in ("linear", "option")
            for position in snapshot.positions
        )
        assert all(
            order.category in ("linear", "option")
            for order in (*snapshot.open_orders, *snapshot.recent_orders)
        )

        websocket = BybitPrivateWebSocketClient(
            signer=BybitV5Signer(profile.credentials)
        )
        stream = websocket.stream_events()
        try:
            event = await asyncio.wait_for(anext(stream), timeout=20)
            assert isinstance(event, PrivateConnectionEvent)
            assert event.state is PrivateConnectionState.AUTHENTICATED
            assert websocket.new_entries_blocked
            websocket.mark_reconciled(event.connection_generation)
            assert not websocket.new_entries_blocked
        finally:
            await stream.aclose()

    try:
        asyncio.run(verify())
    except Exception as exc:
        pytest.fail(
            f"demo read-only verification failed: {type(exc).__name__}: {exc}",
            pytrace=False,
        )


def rest_clock_time(clock: ServerClock) -> datetime:
    """Return an aware timestamp derived from the synchronized exchange clock."""
    return datetime.fromtimestamp(clock.timestamp_ms() / 1000, tz=timezone.utc)
