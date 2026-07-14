"""Persistence-first one-level entry execution tests."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from eth_credit_hedge.application.one_level_entry import OneLevelEntryService
from eth_credit_hedge.domain.execution import (
    ExchangeOrder,
    ExchangePosition,
    ExecutionUpdate,
    LiveExecutionState,
    OrderRequestAck,
    OrderRequestKind,
    PlaceOrderRequest,
    UncertainOrderOutcomeError,
)
from eth_credit_hedge.infrastructure.persistence.sqlite_execution_store import (
    SqliteExecutionStore,
)


NOW = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
ORDER_LINK_ID = "ECH-01-C0001-L01-ENTRY-A01-9F3C"


def entry_request() -> PlaceOrderRequest:
    return PlaceOrderRequest(
        category="linear",
        symbol="ETHUSDT",
        side="Sell",
        order_type="Limit",
        quantity=Decimal("0.010"),
        order_link_id=ORDER_LINK_ID,
        price=Decimal("3000.0"),
        time_in_force="IOC",
        reduce_only=False,
        position_idx=0,
    )


def entry_execution(
    execution_id: str,
    quantity: str,
    price: str,
    fee: str,
) -> ExecutionUpdate:
    return ExecutionUpdate(
        execution_id=execution_id,
        order_id="exchange-order-1",
        order_link_id=ORDER_LINK_ID,
        symbol="ETHUSDT",
        side="Sell",
        price=Decimal(price),
        quantity=Decimal(quantity),
        fee=Decimal(fee),
        is_maker=False,
        executed_at=NOW + timedelta(seconds=1),
    )


class RecordingTradingAdapter:
    def __init__(
        self,
        store: SqliteExecutionStore,
        *,
        uncertain: bool = False,
        discovered_order: ExchangeOrder | None = None,
    ) -> None:
        self.store = store
        self.uncertain = uncertain
        self.discovered_order = discovered_order
        self.place_calls = 0
        self.lookup_calls = 0

    async def place_order(self, request: PlaceOrderRequest) -> OrderRequestAck:
        self.place_calls += 1
        assert await self.store.load_order_intent(request.order_link_id) == request
        snapshot = await self.store.load_entry_snapshot(request.order_link_id)
        assert snapshot is not None
        assert snapshot.state is LiveExecutionState.ENTRY_SUBMITTED
        if self.uncertain:
            raise UncertainOrderOutcomeError(
                order_link_id=request.order_link_id,
                operation="place order",
            )
        return OrderRequestAck(
            request_kind=OrderRequestKind.PLACE,
            order_id="exchange-order-1",
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
        return self.discovered_order


def discovered_order() -> ExchangeOrder:
    return ExchangeOrder(
        category="linear",
        order_id="exchange-order-1",
        order_link_id=ORDER_LINK_ID,
        symbol="ETHUSDT",
        status="New",
        side="Sell",
        order_type="Limit",
        price=Decimal("3000"),
        quantity=Decimal("0.010"),
        cumulative_filled_quantity=Decimal("0"),
        average_price=None,
        reduce_only=False,
        created_at=NOW,
        updated_at=NOW,
    )


def make_store(path: Path) -> SqliteExecutionStore:
    store = SqliteExecutionStore(path)
    asyncio.run(store.initialize())
    return store


def test_intent_is_durable_before_submission_and_ack_is_not_a_fill(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path / "execution.sqlite3")
    trading = RecordingTradingAdapter(store)
    service = OneLevelEntryService(trading=trading, store=store, clock=lambda: NOW)

    snapshot = asyncio.run(service.submit_entry(entry_request()))

    assert trading.place_calls == 1
    assert snapshot.state is LiveExecutionState.ENTRY_ACKNOWLEDGED
    assert snapshot.entry_order_id == "exchange-order-1"
    assert snapshot.filled_quantity == Decimal("0")
    assert snapshot.average_entry_price is None
    assert asyncio.run(store.execution_count()) == 0


def test_partial_executions_aggregate_once_and_restart_restores_state(
    tmp_path: Path,
) -> None:
    database = tmp_path / "execution.sqlite3"
    store = make_store(database)
    service = OneLevelEntryService(
        trading=RecordingTradingAdapter(store),
        store=store,
        clock=lambda: NOW,
    )
    asyncio.run(service.submit_entry(entry_request()))

    first = asyncio.run(
        service.apply_execution(
            entry_execution("execution-1", "0.004", "3000", "0.001"),
            received_at=NOW + timedelta(seconds=2),
            payload_hash="a" * 64,
        )
    )
    duplicate = asyncio.run(
        service.apply_execution(
            entry_execution("execution-1", "0.004", "3000", "0.001"),
            received_at=NOW + timedelta(seconds=3),
            payload_hash="a" * 64,
        )
    )
    filled = asyncio.run(
        service.apply_execution(
            entry_execution("execution-2", "0.006", "2999", "0.002"),
            received_at=NOW + timedelta(seconds=4),
            payload_hash="b" * 64,
        )
    )

    assert first.state is LiveExecutionState.ENTRY_PARTIALLY_FILLED
    assert first.filled_quantity == Decimal("0.004")
    assert duplicate == first
    assert filled.state is LiveExecutionState.ACTIVE_UNPROTECTED
    assert filled.filled_quantity == Decimal("0.010")
    assert filled.entry_notional == Decimal("29.994")
    assert filled.average_entry_price == Decimal("2999.4")
    assert filled.entry_fees == Decimal("0.003")
    assert asyncio.run(store.execution_count()) == 2

    restarted = make_store(database)
    restored = asyncio.run(restarted.load_entry_snapshot(ORDER_LINK_ID))
    assert restored == filled
    assert asyncio.run(restarted.has_execution("execution-1"))


def test_uncertain_submission_queries_same_id_without_resubmitting(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path / "execution.sqlite3")
    trading = RecordingTradingAdapter(
        store,
        uncertain=True,
        discovered_order=discovered_order(),
    )
    service = OneLevelEntryService(trading=trading, store=store, clock=lambda: NOW)

    snapshot = asyncio.run(service.submit_entry(entry_request()))

    assert trading.place_calls == 1
    assert trading.lookup_calls == 1
    assert snapshot.state is LiveExecutionState.ENTRY_ACKNOWLEDGED
    assert snapshot.entry_order_id == "exchange-order-1"


def test_unresolved_uncertain_submission_stays_reconciling(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path / "execution.sqlite3")
    trading = RecordingTradingAdapter(store, uncertain=True)
    service = OneLevelEntryService(trading=trading, store=store, clock=lambda: NOW)

    with pytest.raises(UncertainOrderOutcomeError):
        asyncio.run(service.submit_entry(entry_request()))

    snapshot = asyncio.run(store.load_entry_snapshot(ORDER_LINK_ID))
    assert snapshot is not None
    assert snapshot.state is LiveExecutionState.RECONCILING
    assert trading.place_calls == 1
    assert trading.lookup_calls == 1


def test_local_entry_quantity_must_equal_one_exchange_short_position(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path / "execution.sqlite3")
    service = OneLevelEntryService(
        trading=RecordingTradingAdapter(store),
        store=store,
        clock=lambda: NOW,
    )
    asyncio.run(service.submit_entry(entry_request()))
    asyncio.run(
        service.apply_execution(
            entry_execution("execution-1", "0.010", "3000", "0.001"),
            received_at=NOW + timedelta(seconds=2),
            payload_hash="c" * 64,
        )
    )
    matching = ExchangePosition(
        category="linear",
        symbol="ETHUSDT",
        side="Sell",
        quantity=Decimal("0.010"),
        average_price=Decimal("3000"),
        mark_price=Decimal("2999"),
        unrealized_pnl=Decimal("0.01"),
        updated_at=NOW,
    )

    assert asyncio.run(service.reconcile_position((matching,)))

    mismatched = ExchangePosition(
        category="linear",
        symbol="ETHUSDT",
        side="Sell",
        quantity=Decimal("0.009"),
        average_price=Decimal("3000"),
        mark_price=Decimal("2999"),
        unrealized_pnl=Decimal("0.01"),
        updated_at=NOW,
    )
    assert not asyncio.run(service.reconcile_position((mismatched,)))
    snapshot = asyncio.run(store.load_entry_snapshot(ORDER_LINK_ID))
    assert snapshot is not None
    assert snapshot.state is LiveExecutionState.RECONCILING
