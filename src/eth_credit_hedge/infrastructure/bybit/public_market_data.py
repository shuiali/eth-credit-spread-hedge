"""Combined public REST/WebSocket implementation of the market-data port."""

from __future__ import annotations

from collections.abc import AsyncIterator

from eth_credit_hedge.domain.instruments import InstrumentSpec, OptionMarketQuote
from eth_credit_hedge.domain.market_data import (
    OrderBookEvent,
    OrderBookSnapshot,
    TickerEvent,
    TradeEvent,
)
from eth_credit_hedge.infrastructure.bybit.public_rest import BybitPublicRestClient
from eth_credit_hedge.infrastructure.bybit.public_ws import (
    BybitPublicWebSocketClient,
)
from eth_credit_hedge.ports.market_data import MarketDataPort


class BybitPublicMarketData(MarketDataPort):
    def __init__(
        self,
        *,
        rest: BybitPublicRestClient | None = None,
        linear_websocket: BybitPublicWebSocketClient | None = None,
        option_websocket: BybitPublicWebSocketClient | None = None,
    ) -> None:
        self.rest = rest or BybitPublicRestClient()
        self.linear_websocket = linear_websocket or BybitPublicWebSocketClient(
            category="linear"
        )
        self.option_websocket = option_websocket or BybitPublicWebSocketClient(
            category="option"
        )

    async def get_instrument(self, symbol: str) -> InstrumentSpec:
        return await self.rest.get_instrument(symbol)

    async def get_option_chain(
        self,
        base_coin: str,
    ) -> tuple[OptionMarketQuote, ...]:
        return await self.rest.get_option_chain(base_coin)

    async def get_orderbook_snapshot(
        self,
        symbol: str,
        depth: int,
    ) -> OrderBookSnapshot:
        return await self.rest.get_orderbook_snapshot(symbol, depth)

    def stream_ticker(self, symbol: str) -> AsyncIterator[TickerEvent]:
        return self._websocket(symbol).stream_ticker(symbol)

    def stream_orderbook(
        self,
        symbol: str,
        depth: int,
    ) -> AsyncIterator[OrderBookEvent]:
        return self._websocket(symbol).stream_orderbook(symbol, depth)

    def stream_trades(self, symbol: str) -> AsyncIterator[TradeEvent]:
        return self._websocket(symbol).stream_trades(symbol)

    def _websocket(self, symbol: str) -> BybitPublicWebSocketClient:
        return self.option_websocket if "-" in symbol else self.linear_websocket
