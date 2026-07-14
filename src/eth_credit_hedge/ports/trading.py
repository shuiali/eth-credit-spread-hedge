"""Exchange-neutral private order-management port."""

from __future__ import annotations

from typing import Protocol

from eth_credit_hedge.domain.execution import (
    AmendOrderRequest,
    CancelOrderRequest,
    ExchangeOrder,
    ExecutionUpdate,
    OrderRequestAck,
    PlaceOrderRequest,
)


class TradingPort(Protocol):
    async def place_order(self, request: PlaceOrderRequest) -> OrderRequestAck: ...

    async def amend_order(self, request: AmendOrderRequest) -> OrderRequestAck: ...

    async def cancel_order(self, request: CancelOrderRequest) -> OrderRequestAck: ...

    async def cancel_all(
        self,
        category: str,
        symbol: str | None = None,
    ) -> None: ...

    async def get_open_orders(
        self,
        category: str,
        symbol: str | None = None,
    ) -> tuple[ExchangeOrder, ...]: ...

    async def get_order_history(
        self,
        category: str,
        symbol: str | None = None,
    ) -> tuple[ExchangeOrder, ...]: ...

    async def get_order_by_link_id(
        self,
        category: str,
        symbol: str,
        order_link_id: str,
    ) -> ExchangeOrder | None: ...

    async def get_execution_history(
        self,
        category: str,
        symbol: str | None = None,
        order_link_id: str | None = None,
    ) -> tuple[ExecutionUpdate, ...]: ...
