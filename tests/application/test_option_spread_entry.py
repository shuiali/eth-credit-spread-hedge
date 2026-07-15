"""Protective-long-first option spread entry from actual executions."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from eth_credit_hedge.application.option_spread_entry import (
    OptionSpreadEntryPlan,
    OptionSpreadEntryService,
    OptionSpreadNotOpenedError,
)
from eth_credit_hedge.domain.execution import (
    ExchangeOrder,
    ExecutionUpdate,
    OrderRequestAck,
    OrderRequestKind,
    PlaceOrderRequest,
)
from eth_credit_hedge.domain.option_lifecycle import (
    OptionEntryPolicy,
    UnmatchedLongPolicy,
)
from eth_credit_hedge.domain.option_position import OptionPositionState
from eth_credit_hedge.infrastructure.persistence.sqlite_execution_store import (
    SqliteExecutionStore,
)


NOW = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
EXPIRY = datetime(2026, 7, 31, 8, tzinfo=timezone.utc)
LONG_SYMBOL = "ETH-31JUL26-1700-P-USDT"
SHORT_SYMBOL = "ETH-31JUL26-1800-P-USDT"
LONG_ID = "ECH-D3-C0001-L00-OL-A01-B001"
SHORT_ID = "ECH-D3-C0001-L00-OS-A01-B002"


def plan() -> OptionSpreadEntryPlan:
    return OptionSpreadEntryPlan(
        cycle_id="D3-C0001",
        long_symbol=LONG_SYMBOL,
        short_symbol=SHORT_SYMBOL,
        expiry_time_utc=EXPIRY,
        quantity=Decimal("0.1"),
        long_limit_price=Decimal("19"),
        short_limit_price=Decimal("41"),
        expected_net_credit=Decimal("2.2"),
        long_order_link_id=LONG_ID,
        short_order_link_id=SHORT_ID,
    )


def policy() -> OptionEntryPolicy:
    return OptionEntryPolicy(
        max_leg_wait_seconds=Decimal("5"),
        allow_partial_spread=False,
        minimum_matched_quantity=Decimal("0.1"),
        maximum_credit_deviation=Decimal("1"),
        unmatched_long_policy=UnmatchedLongPolicy.RETAIN,
    )


class FilledOptionExchange:
    def __init__(self, store: SqliteExecutionStore, *, reject_short: bool = False):
        self.store = store
        self.reject_short = reject_short
        self.requests: list[PlaceOrderRequest] = []
        self.executions: dict[str, ExecutionUpdate] = {}

    async def place_order(self, request: PlaceOrderRequest) -> OrderRequestAck:
        assert await self.store.load_order_intent(request.order_link_id) == request
        self.requests.append(request)
        if self.reject_short and request.side == "Sell":
            raise RuntimeError("short rejected")
        order_id = f"order-{len(self.requests)}"
        price = Decimal("18.9") if request.side == "Buy" else Decimal("41.1")
        self.executions[request.order_link_id] = ExecutionUpdate(
            execution_id=f"execution-{len(self.requests)}",
            order_id=order_id,
            order_link_id=request.order_link_id,
            symbol=request.symbol,
            side=request.side,
            price=price,
            quantity=request.quantity,
            fee=Decimal("0.01"),
            is_maker=False,
            executed_at=NOW,
        )
        return OrderRequestAck(
            request_kind=OrderRequestKind.PLACE,
            order_id=order_id,
            order_link_id=request.order_link_id,
            acknowledged_at=NOW,
        )

    async def get_execution_history(
        self,
        category: str,
        symbol: str | None = None,
        order_link_id: str | None = None,
    ) -> tuple[ExecutionUpdate, ...]:
        assert category == "option"
        return tuple(
            execution
            for key, execution in self.executions.items()
            if (order_link_id is None or key == order_link_id)
            and (symbol is None or execution.symbol == symbol)
        )

    async def get_order_by_link_id(
        self,
        category: str,
        symbol: str,
        order_link_id: str,
    ) -> ExchangeOrder | None:
        execution = self.executions.get(order_link_id)
        if execution is None:
            return None
        request = next(
            request
            for request in self.requests
            if request.order_link_id == order_link_id
        )
        return ExchangeOrder(
            category="option",
            order_id=execution.order_id,
            order_link_id=order_link_id,
            symbol=symbol,
            status="Filled",
            side=request.side,
            order_type="Limit",
            price=request.price,
            quantity=request.quantity,
            cumulative_filled_quantity=request.quantity,
            average_price=execution.price,
            reduce_only=False,
            created_at=NOW,
            updated_at=NOW,
            time_in_force="IOC",
            position_idx=0,
        )


def make_service(
    path: Path,
    *,
    reject_short: bool = False,
) -> tuple[OptionSpreadEntryService, FilledOptionExchange, SqliteExecutionStore]:
    store = SqliteExecutionStore(path)
    asyncio.run(store.initialize())
    exchange = FilledOptionExchange(store, reject_short=reject_short)
    service = OptionSpreadEntryService(
        trading=exchange,
        store=store,
        clock=lambda: NOW,
        fill_attempts=2,
        fill_interval_seconds=0,
    )
    return service, exchange, store


def test_long_fills_before_short_and_open_snapshot_survives_restart(
    tmp_path: Path,
) -> None:
    service, exchange, store = make_service(tmp_path / "option.sqlite3")

    snapshot = asyncio.run(service.open_spread(plan(), policy()))

    assert [request.side for request in exchange.requests] == ["Buy", "Sell"]
    assert snapshot.state is OptionPositionState.OPEN
    assert snapshot.matched_quantity == Decimal("0.1")
    assert snapshot.actual_net_credit == Decimal("2.20")
    assert asyncio.run(store.execution_count()) == 2
    restarted = SqliteExecutionStore(tmp_path / "option.sqlite3")
    asyncio.run(restarted.initialize())
    assert asyncio.run(restarted.load_option_spread_snapshot("D3-C0001")) == snapshot
    assert snapshot.position_snapshot().actual_net_credit == Decimal("2.20")


def test_short_rejection_retains_only_the_confirmed_protective_long(
    tmp_path: Path,
) -> None:
    service, exchange, store = make_service(
        tmp_path / "rejected.sqlite3",
        reject_short=True,
    )

    with pytest.raises(OptionSpreadNotOpenedError, match="short rejected"):
        asyncio.run(service.open_spread(plan(), policy()))

    snapshot = asyncio.run(store.load_option_spread_snapshot("D3-C0001"))
    assert snapshot is not None
    assert snapshot.state is OptionPositionState.ERROR
    assert snapshot.long_filled_quantity == Decimal("0.1")
    assert snapshot.short_filled_quantity == Decimal("0")
    assert not snapshot.has_naked_short
    assert [request.side for request in exchange.requests] == ["Buy", "Sell"]
