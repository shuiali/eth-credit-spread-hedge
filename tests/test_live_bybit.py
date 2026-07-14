"""Opt-in live public-chain smoke test."""

import asyncio
import os

import pytest

from eth_credit_hedge.data.bybit_options import BybitOptionClient
from eth_credit_hedge.domain.market_data import LocalOrderBook, OrderBookSnapshot
from eth_credit_hedge.infrastructure.bybit.public_rest import BybitPublicRestClient
from eth_credit_hedge.infrastructure.bybit.public_ws import (
    BybitPublicWebSocketClient,
)


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_BYBIT_TESTS") != "1",
    reason="set RUN_LIVE_BYBIT_TESTS=1 to contact Bybit",
)
def test_live_eth_chain_contains_only_valid_put_quotes() -> None:
    chain = BybitOptionClient().fetch_eth_chain()

    assert chain
    assert all(entry.contract.base_coin == "ETH" for entry in chain)
    assert all(entry.contract.option_type == "Put" for entry in chain)


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_BYBIT_TESTS") != "1",
    reason="set RUN_LIVE_BYBIT_TESTS=1 to contact Bybit",
)
def test_live_eth_perpetual_spec_and_orderbook_are_normalized() -> None:
    async def fetch():
        client = BybitPublicRestClient()
        return (
            await client.get_instrument("ETHUSDT"),
            await client.get_orderbook_snapshot("ETHUSDT", 50),
        )

    instrument, orderbook = asyncio.run(fetch())

    assert instrument.symbol == "ETHUSDT"
    assert instrument.category == "linear"
    assert instrument.status == "Trading"
    assert instrument.price_filter.tick_size > 0
    assert instrument.lot_size_filter.qty_step > 0
    assert orderbook.symbol == "ETHUSDT"
    assert orderbook.bids
    assert orderbook.asks


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_BYBIT_TESTS") != "1",
    reason="set RUN_LIVE_BYBIT_TESTS=1 to contact Bybit",
)
def test_live_eth_perpetual_trade_stream_is_normalized() -> None:
    async def receive_one_trade():
        client = BybitPublicWebSocketClient(category="linear")
        stream = client.stream_trades("ETHUSDT")
        try:
            return await asyncio.wait_for(anext(stream), timeout=20)
        finally:
            await stream.aclose()

    trade = asyncio.run(receive_one_trade())

    assert trade.symbol == "ETHUSDT"
    assert trade.price > 0
    assert trade.size > 0
    assert trade.connection_generation >= 1
    assert len(trade.raw_payload_hash) == 64


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_BYBIT_TESTS") != "1",
    reason="set RUN_LIVE_BYBIT_TESTS=1 to contact Bybit",
)
def test_live_orderbook_stream_applies_snapshot_then_deltas() -> None:
    async def receive_book_events():
        client = BybitPublicWebSocketClient(category="linear")
        stream = client.stream_orderbook("ETHUSDT", 50)
        events = []
        try:
            for _ in range(20):
                events.append(await asyncio.wait_for(anext(stream), timeout=20))
        finally:
            await stream.aclose()
        return events

    book = LocalOrderBook("ETHUSDT")
    events = asyncio.run(receive_book_events())
    for event in events:
        if isinstance(event, OrderBookSnapshot):
            assert book.apply_snapshot(event)
        else:
            assert book.apply_delta(event)

    assert book.synchronized
    assert book.best_bid is not None
    assert book.best_ask is not None
