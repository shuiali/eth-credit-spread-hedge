"""Environment boundaries required by the authoritative strategy runtime.

These protocols deliberately separate public data, private executions, trading
mutations, read-only exchange queries, funding, and time.  Environment adapters
may implement more than one protocol; the runtime must receive them explicitly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Protocol

from eth_credit_hedge.domain.execution import (
    AmendOrderRequest,
    CancelOrderRequest,
    ExchangeOrder,
    ExchangePosition,
    ExecutionUpdate,
    ExecutionUpdateBatch,
    OrderRequestAck,
    PlaceOrderRequest,
    WalletState,
)
from eth_credit_hedge.domain.accounting.events import FundingRecorded
from eth_credit_hedge.ports.market_data import MarketDataPort


class PrivateExecutionPort(Protocol):
    """Authenticated execution facts and the reconciliation entry gate."""

    @property
    def new_entries_blocked(self) -> bool: ...

    def stream_execution_batches(self) -> AsyncIterator[ExecutionUpdateBatch]: ...

    def mark_reconciled(self, connection_generation: int) -> None: ...


class TradingMutationPort(Protocol):
    """Bounded order mutation capability; not a public-data capability."""

    async def place_order(self, request: PlaceOrderRequest) -> OrderRequestAck: ...

    async def amend_order(self, request: AmendOrderRequest) -> OrderRequestAck: ...

    async def cancel_order(self, request: CancelOrderRequest) -> OrderRequestAck: ...

    async def cancel_all(
        self,
        category: str,
        symbol: str | None = None,
    ) -> None: ...


class ExchangeQueryPort(Protocol):
    """Read-only exchange state used for reconciliation and risk."""

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

    async def get_execution_history(
        self,
        category: str,
        symbol: str | None = None,
        order_link_id: str | None = None,
    ) -> tuple[ExecutionUpdate, ...]: ...

    async def get_positions(
        self,
        category: str,
        symbol: str | None = None,
    ) -> tuple[ExchangePosition, ...]: ...

    async def get_wallet_state(self) -> WalletState: ...


class FundingPort(Protocol):
    """Source of confirmed perpetual funding facts for the accounting runtime."""

    async def get_confirmed_funding(
        self,
        symbol: str,
        since_utc: datetime | None = None,
    ) -> tuple[FundingRecorded, ...]: ...


class ClockPort(Protocol):
    """UTC clock supplied by the selected environment adapter."""

    def now_utc(self) -> datetime: ...


__all__ = [
    "ClockPort",
    "ExchangeQueryPort",
    "FundingPort",
    "MarketDataPort",
    "PrivateExecutionPort",
    "TradingMutationPort",
]
