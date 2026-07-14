"""A separately gated, persistence-first one-position flatten command."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from eth_credit_hedge.application.emergency_flatten import EmergencyFlattenService
from eth_credit_hedge.domain.execution import (
    ExchangeOrder,
    ExchangePosition,
    OrderRequestAck,
    OrderRequestKind,
    PlaceOrderRequest,
    UncertainOrderOutcomeError,
)
from eth_credit_hedge.infrastructure.persistence.sqlite_execution_store import (
    SqliteExecutionStore,
)


NOW = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
ORDER_LINK_ID = "ECH-01-C0001-L01-EC-A01-9F3C"


def short_position(quantity: str = "0.010") -> ExchangePosition:
    return ExchangePosition(
        category="linear",
        symbol="ETHUSDT",
        side="Sell",
        quantity=Decimal(quantity),
        average_price=Decimal("3000"),
        mark_price=Decimal("2999"),
        unrealized_pnl=Decimal("0.01"),
        updated_at=NOW,
    )


class SequencedAccountAdapter:
    def __init__(self, responses: list[tuple[ExchangePosition, ...]]) -> None:
        self.responses = responses

    async def get_positions(
        self,
        category: str,
        symbol: str | None = None,
    ) -> tuple[ExchangePosition, ...]:
        assert (category, symbol) == ("linear", "ETHUSDT")
        if len(self.responses) == 1:
            return self.responses[0]
        return self.responses.pop(0)


class RecordingTradingAdapter:
    def __init__(
        self,
        store: SqliteExecutionStore,
        *,
        uncertain: bool = False,
        discovered: ExchangeOrder | None = None,
    ) -> None:
        self.store = store
        self.uncertain = uncertain
        self.discovered = discovered
        self.requests: list[PlaceOrderRequest] = []
        self.lookup_calls = 0

    async def place_order(self, request: PlaceOrderRequest) -> OrderRequestAck:
        assert await self.store.load_order_intent(request.order_link_id) == request
        self.requests.append(request)
        if self.uncertain:
            raise UncertainOrderOutcomeError(
                order_link_id=request.order_link_id,
                operation="place order",
            )
        return OrderRequestAck(
            request_kind=OrderRequestKind.PLACE,
            order_id="flatten-order-1",
            order_link_id=request.order_link_id,
            acknowledged_at=NOW,
        )

    async def get_order_by_link_id(
        self,
        category: str,
        symbol: str,
        order_link_id: str,
    ) -> ExchangeOrder | None:
        assert (category, symbol, order_link_id) == (
            "linear",
            "ETHUSDT",
            ORDER_LINK_ID,
        )
        self.lookup_calls += 1
        return self.discovered


def discovered_flatten() -> ExchangeOrder:
    return ExchangeOrder(
        category="linear",
        order_id="flatten-order-1",
        order_link_id=ORDER_LINK_ID,
        symbol="ETHUSDT",
        status="Filled",
        side="Buy",
        order_type="Market",
        price=None,
        quantity=Decimal("0.010"),
        cumulative_filled_quantity=Decimal("0.010"),
        average_price=Decimal("2999"),
        reduce_only=True,
        created_at=NOW,
        updated_at=NOW,
    )


def make_store(path: Path) -> SqliteExecutionStore:
    store = SqliteExecutionStore(path)
    asyncio.run(store.initialize())
    return store


def test_flatten_persists_exact_reduce_only_intent_and_confirms_position_zero(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path / "execution.sqlite3")
    account = SequencedAccountAdapter([(short_position(),), ()])
    trading = RecordingTradingAdapter(store)
    service = EmergencyFlattenService(
        trading=trading,
        account=account,
        store=store,
        clock=lambda: NOW,
    )

    result = asyncio.run(service.flatten_short(ORDER_LINK_ID))

    assert result.request == PlaceOrderRequest(
        category="linear",
        symbol="ETHUSDT",
        side="Buy",
        order_type="Market",
        quantity=Decimal("0.010"),
        order_link_id=ORDER_LINK_ID,
        reduce_only=True,
        position_idx=0,
    )
    assert not result.position_confirmed_flat
    assert asyncio.run(service.confirm_flattened())


def test_flatten_ack_does_not_claim_position_is_flat(tmp_path: Path) -> None:
    store = make_store(tmp_path / "execution.sqlite3")
    account = SequencedAccountAdapter([(short_position(),)])
    service = EmergencyFlattenService(
        trading=RecordingTradingAdapter(store),
        account=account,
        store=store,
        clock=lambda: NOW,
    )

    result = asyncio.run(service.flatten_short(ORDER_LINK_ID))

    assert not result.position_confirmed_flat
    assert not asyncio.run(service.confirm_flattened())


def test_uncertain_flatten_queries_same_id_without_resubmitting(tmp_path: Path) -> None:
    store = make_store(tmp_path / "execution.sqlite3")
    trading = RecordingTradingAdapter(
        store,
        uncertain=True,
        discovered=discovered_flatten(),
    )
    service = EmergencyFlattenService(
        trading=trading,
        account=SequencedAccountAdapter([(short_position(),)]),
        store=store,
        clock=lambda: NOW,
    )

    result = asyncio.run(service.flatten_short(ORDER_LINK_ID))

    assert result.acknowledgement.order_id == "flatten-order-1"
    assert len(trading.requests) == 1
    assert trading.lookup_calls == 1


def test_flatten_refuses_ambiguous_or_non_short_position(tmp_path: Path) -> None:
    long_position = ExchangePosition(
        category="linear",
        symbol="ETHUSDT",
        side="Buy",
        quantity=Decimal("0.010"),
        average_price=Decimal("3000"),
        mark_price=Decimal("3001"),
        unrealized_pnl=Decimal("0.01"),
        updated_at=NOW,
    )
    store = make_store(tmp_path / "execution.sqlite3")
    trading = RecordingTradingAdapter(store)
    service = EmergencyFlattenService(
        trading=trading,
        account=SequencedAccountAdapter([(long_position,)]),
        store=store,
        clock=lambda: NOW,
    )

    with pytest.raises(RuntimeError, match="exactly one ETHUSDT short"):
        asyncio.run(service.flatten_short(ORDER_LINK_ID))
    assert trading.requests == []
