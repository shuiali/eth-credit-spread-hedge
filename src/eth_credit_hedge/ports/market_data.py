"""Exchange-neutral public market-data port."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from eth_credit_hedge.domain.instruments import InstrumentSpec, OptionMarketQuote
from eth_credit_hedge.domain.market_data import (
    OrderBookEvent,
    OrderBookSnapshot,
    TickerEvent,
    TradeEvent,
)


class MarketDataPort(Protocol):
    async def get_instrument(self, symbol: str) -> InstrumentSpec: ...

    async def get_option_chain(
        self,
        base_coin: str,
    ) -> tuple[OptionMarketQuote, ...]: ...

    async def get_orderbook_snapshot(
        self,
        symbol: str,
        depth: int,
    ) -> OrderBookSnapshot: ...

    def stream_ticker(self, symbol: str) -> AsyncIterator[TickerEvent]: ...

    def stream_orderbook(
        self,
        symbol: str,
        depth: int,
    ) -> AsyncIterator[OrderBookEvent]: ...

    def stream_trades(self, symbol: str) -> AsyncIterator[TradeEvent]: ...
