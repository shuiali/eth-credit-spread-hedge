"""Bybit public REST/WebSocket normalization and recording tests."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from eth_credit_hedge.domain.market_data import (
    LocalOrderBook,
    MarketDataEventType,
    OrderBookSnapshot,
    TickerEvent,
)
from eth_credit_hedge.infrastructure.bybit.parsers import (
    parse_instrument_spec,
    parse_orderbook_message,
    parse_ticker_message,
    parse_trade_message,
)
from eth_credit_hedge.infrastructure.bybit.public_rest import (
    BybitPublicRestClient,
)
from eth_credit_hedge.infrastructure.bybit.public_ws import (
    BybitPublicWebSocketClient,
    MarketDataConnectionSupervisor,
    ReconnectPolicy,
)
from eth_credit_hedge.infrastructure.recording.jsonl import (
    JsonLinesMarketDataRecorder,
)


NOW = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)


def option_instrument(symbol: str = "ETH-31JUL26-3000-P-USDT") -> dict[str, Any]:
    return {
        "symbol": symbol,
        "status": "Trading",
        "baseCoin": "ETH",
        "quoteCoin": "USDT",
        "settleCoin": "USDT",
        "optionsType": "Put",
        "deliveryTime": "1785484800000",
        "priceFilter": {
            "minPrice": "0.1",
            "maxPrice": "41000",
            "tickSize": "0.1",
        },
        "lotSizeFilter": {
            "maxOrderQty": "5000",
            "minOrderQty": "0.1",
            "qtyStep": "0.1",
        },
    }


def linear_instrument(symbol: str = "ETHUSDT") -> dict[str, Any]:
    return {
        "symbol": symbol,
        "contractType": "LinearPerpetual",
        "status": "Trading",
        "baseCoin": "ETH",
        "quoteCoin": "USDT",
        "settleCoin": "USDT",
        "deliveryTime": "0",
        "priceFilter": {
            "minPrice": "0.01",
            "maxPrice": "1000000",
            "tickSize": "0.01",
        },
        "lotSizeFilter": {
            "maxOrderQty": "1000",
            "maxMktOrderQty": "500",
            "minOrderQty": "0.001",
            "qtyStep": "0.001",
            "minNotionalValue": "5",
        },
    }


def response(
    category: str,
    items: list[dict[str, Any]],
    *,
    cursor: str = "",
) -> dict[str, Any]:
    return {
        "retCode": 0,
        "retMsg": "OK",
        "result": {
            "category": category,
            "list": items,
            "nextPageCursor": cursor,
        },
        "time": 1784030400000,
    }


def test_option_and_perpetual_constraints_parse_without_invented_limits() -> None:
    option = parse_instrument_spec(option_instrument(), "option")
    linear = parse_instrument_spec(linear_instrument(), "linear")

    assert option.symbol == "ETH-31JUL26-3000-P-USDT"
    assert option.price_filter.tick_size == Decimal("0.1")
    assert option.lot_size_filter.qty_step == Decimal("0.1")
    assert option.lot_size_filter.max_market_order_qty is None
    assert option.lot_size_filter.min_notional is None
    assert option.delivery_time_utc == datetime(
        2026,
        7,
        31,
        8,
        tzinfo=timezone.utc,
    )

    assert linear.symbol == "ETHUSDT"
    assert linear.price_filter.tick_size == Decimal("0.01")
    assert linear.lot_size_filter.max_market_order_qty == Decimal("500")
    assert linear.lot_size_filter.min_notional == Decimal("5")
    assert linear.delivery_time_utc is None
    assert linear.contract_multiplier == Decimal("1")


def test_instrument_client_follows_pagination_cursor() -> None:
    calls: list[dict[str, str | int]] = []

    def requester(endpoint: str, params: dict[str, str | int]) -> dict[str, Any]:
        assert endpoint == "/v5/market/instruments-info"
        calls.append(dict(params))
        if params.get("cursor") == "page-2":
            return response("linear", [linear_instrument("ETHUSDC")])
        return response("linear", [linear_instrument()], cursor="page-2")

    client = BybitPublicRestClient(requester=requester)
    instruments = asyncio.run(client.list_instruments("linear", base_coin="ETH"))

    assert [instrument.symbol for instrument in instruments] == [
        "ETHUSDT",
        "ETHUSDC",
    ]
    assert len(calls) == 2
    assert "cursor" not in calls[0]
    assert calls[1]["cursor"] == "page-2"


def test_option_chain_retains_missing_optional_greeks() -> None:
    instrument = option_instrument()
    ticker = {
        "symbol": instrument["symbol"],
        "bid1Price": "99.5",
        "bid1Size": "2",
        "ask1Price": "100.5",
        "ask1Size": "3",
        "markPrice": "100",
        "underlyingPrice": "3011",
        "indexPrice": "3010",
        "bid1Iv": "",
        "ask1Iv": "",
        "markIv": "",
        "delta": "",
        "gamma": "",
        "vega": "",
        "theta": "",
    }

    def requester(endpoint: str, params: dict[str, str | int]) -> dict[str, Any]:
        if endpoint == "/v5/market/instruments-info":
            return response("option", [instrument])
        assert endpoint == "/v5/market/tickers"
        assert params == {"category": "option", "baseCoin": "ETH"}
        return response("option", [ticker])

    quotes = asyncio.run(
        BybitPublicRestClient(requester=requester).get_option_chain("ETH")
    )

    assert len(quotes) == 1
    quote = quotes[0]
    assert quote.symbol == instrument["symbol"]
    assert quote.mark_price == Decimal("100")
    assert quote.bid_iv is None
    assert quote.ask_iv is None
    assert quote.mark_iv is None
    assert quote.delta is None
    assert quote.gamma is None
    assert quote.vega is None
    assert quote.theta is None


def test_rest_orderbook_uses_exchange_snapshot_timestamp() -> None:
    def requester(endpoint: str, params: dict[str, str | int]) -> dict[str, Any]:
        assert endpoint == "/v5/market/orderbook"
        assert params == {"category": "linear", "symbol": "ETHUSDT", "limit": 50}
        return {
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "s": "ETHUSDT",
                "b": [["3000", "2"]],
                "a": [["3000.1", "3"]],
                "u": 10,
                "seq": 110,
                "ts": 1784030399900,
                "cts": 1784030399890,
            },
            "time": 1784030400000,
        }

    snapshot = asyncio.run(
        BybitPublicRestClient(requester=requester).get_orderbook_snapshot(
            "ETHUSDT",
            50,
        )
    )

    assert snapshot.timestamp_utc == NOW - timedelta(milliseconds=100)
    assert snapshot.bids == ((Decimal("3000"), Decimal("2")),)


def ticker_message() -> dict[str, Any]:
    return {
        "topic": "tickers.ETHUSDT",
        "type": "snapshot",
        "ts": 1784030400000,
        "cs": 101,
        "data": {
            "symbol": "ETHUSDT",
            "lastPrice": "3000.1",
            "markPrice": "3000.2",
            "indexPrice": "3000.3",
            "bid1Price": "3000.0",
            "ask1Price": "3000.1",
        },
    }


def option_ticker_message() -> dict[str, Any]:
    return {
        "id": "option-ticker-1",
        "topic": "tickers.ETH-31JUL26-3000-P-USDT",
        "type": "snapshot",
        "ts": 1784030400000,
        "data": {
            "symbol": "ETH-31JUL26-3000-P-USDT",
            "bidPrice": "0",
            "bidSize": "0",
            "bidIv": "0",
            "askPrice": "100.5",
            "askSize": "3.2",
            "askIv": "0.51",
            "lastPrice": "100",
            "markPrice": "100.1",
            "indexPrice": "3010",
            "underlyingPrice": "3011",
            "markPriceIv": "0.50",
            "delta": "-0.30",
            "gamma": "0.001",
            "vega": "2.1",
            "theta": "-1.2",
        },
    }


def trade_message() -> dict[str, Any]:
    return {
        "topic": "publicTrade.ETHUSDT",
        "type": "snapshot",
        "ts": 1784030400100,
        "data": [
            {
                "T": 1784030400090,
                "s": "ETHUSDT",
                "S": "Sell",
                "v": "0.5",
                "p": "3000.1",
                "i": "trade-1",
                "seq": 102,
            }
        ],
    }


def book_message(*, update_id: int, bid: str) -> dict[str, Any]:
    return {
        "topic": "orderbook.50.ETHUSDT",
        "type": "snapshot",
        "ts": 1784030400200,
        "data": {
            "s": "ETHUSDT",
            "b": [[bid, "2"]],
            "a": [[str(Decimal(bid) + Decimal("0.1")), "3"]],
            "u": update_id,
            "seq": 200 + update_id,
        },
    }


def test_websocket_messages_normalize_without_raw_json_leaking() -> None:
    ticker = parse_ticker_message(ticker_message(), connection_generation=4)
    trades = parse_trade_message(trade_message(), connection_generation=4)
    book = parse_orderbook_message(book_message(update_id=10, bid="3000"), 4)

    assert ticker.symbol == "ETHUSDT"
    assert ticker.last_price == Decimal("3000.1")
    assert ticker.connection_generation == 4
    assert len(ticker.raw_payload_hash) == 64
    assert len(trades) == 1
    assert trades[0].price == Decimal("3000.1")
    assert trades[0].size == Decimal("0.5")
    assert trades[0].sequence == 102
    assert isinstance(book, OrderBookSnapshot)
    assert book.bids == ((Decimal("3000"), Decimal("2")),)
    assert book.connection_generation == 4


def test_option_websocket_ticker_normalizes_option_field_names_and_greeks() -> None:
    ticker = parse_ticker_message(
        option_ticker_message(),
        connection_generation=5,
    )

    assert ticker.symbol == "ETH-31JUL26-3000-P-USDT"
    assert ticker.bid_price is None
    assert ticker.bid_size is None
    assert ticker.ask_price == Decimal("100.5")
    assert ticker.ask_size == Decimal("3.2")
    assert ticker.bid_iv is None
    assert ticker.ask_iv == Decimal("0.51")
    assert ticker.mark_iv == Decimal("0.50")
    assert ticker.underlying_price == Decimal("3011")
    assert ticker.delta == Decimal("-0.30")
    assert ticker.gamma == Decimal("0.001")
    assert ticker.vega == Decimal("2.1")
    assert ticker.theta == Decimal("-1.2")


def test_connection_supervisor_restores_subscriptions_and_snapshots() -> None:
    supervisor = MarketDataConnectionSupervisor(
        reconnect_policy=ReconnectPolicy(
            initial_delay_seconds=Decimal("1"),
            multiplier=Decimal("2"),
            maximum_delay_seconds=Decimal("8"),
        ),
        heartbeat_interval_seconds=Decimal("20"),
        pong_timeout_seconds=Decimal("5"),
    )
    supervisor.register_subscription("tickers.ETHUSDT")
    supervisor.register_subscription(
        "orderbook.50.ETHUSDT",
        requires_snapshot=True,
    )

    first = supervisor.begin_connection(NOW)
    supervisor.disconnect()
    second = supervisor.begin_connection(NOW + timedelta(seconds=1))

    assert first.generation == 1
    assert second.generation == 2
    assert second.subscriptions == (
        "orderbook.50.ETHUSDT",
        "tickers.ETHUSDT",
    )
    assert second.snapshot_topics == ("orderbook.50.ETHUSDT",)
    assert not supervisor.accept_event(1, NOW + timedelta(seconds=2))
    assert supervisor.accept_event(2, NOW + timedelta(seconds=2))
    assert supervisor.reconnect_delay(0) == Decimal("1")
    assert supervisor.reconnect_delay(1) == Decimal("2")
    assert supervisor.reconnect_delay(3) == Decimal("8")
    assert supervisor.reconnect_delay(20) == Decimal("8")
    assert supervisor.heartbeat_due(NOW + timedelta(seconds=22))
    supervisor.record_ping(NOW + timedelta(seconds=22))
    assert not supervisor.pong_overdue(NOW + timedelta(seconds=26))
    assert supervisor.pong_overdue(NOW + timedelta(seconds=28))
    supervisor.record_pong(NOW + timedelta(seconds=28))
    assert not supervisor.pong_overdue(NOW + timedelta(seconds=40))


class FakeSocket:
    def __init__(self, messages: list[dict[str, Any] | BaseException]) -> None:
        self.messages = list(messages)
        self.sent: list[dict[str, Any]] = []

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    async def recv(self) -> str:
        item = self.messages.pop(0)
        if isinstance(item, BaseException):
            raise item
        return json.dumps(item)


def test_websocket_reconnect_resubscribes_and_new_snapshot_resets_book() -> None:
    subscribe_ack = {"success": True, "op": "subscribe", "ret_msg": ""}
    sockets = [
        FakeSocket(
            [
                subscribe_ack,
                book_message(update_id=10, bid="3000"),
                OSError("connection lost"),
            ]
        ),
        FakeSocket(
            [
                subscribe_ack,
                book_message(update_id=20, bid="2900"),
            ]
        ),
    ]
    connected_urls: list[str] = []
    sleeps: list[float] = []

    @asynccontextmanager
    async def connect_factory(url: str) -> AsyncIterator[FakeSocket]:
        connected_urls.append(url)
        yield sockets[len(connected_urls) - 1]

    async def no_wait(delay: float) -> None:
        sleeps.append(delay)

    async def consume() -> tuple[OrderBookSnapshot, OrderBookSnapshot]:
        client = BybitPublicWebSocketClient(
            category="linear",
            connect_factory=connect_factory,
            sleep=no_wait,
        )
        stream = client.stream_orderbook("ETHUSDT", 50)
        first = await anext(stream)
        second = await anext(stream)
        await stream.aclose()
        assert isinstance(first, OrderBookSnapshot)
        assert isinstance(second, OrderBookSnapshot)
        return first, second

    first, second = asyncio.run(consume())
    book = LocalOrderBook("ETHUSDT")
    book.apply_snapshot(first)
    book.apply_snapshot(second)

    assert first.connection_generation == 1
    assert second.connection_generation == 2
    assert book.bids == {Decimal("2900"): Decimal("2")}
    assert len(connected_urls) == 2
    assert sleeps == [1.0]
    assert sockets[0].sent[0]["args"] == ["orderbook.50.ETHUSDT"]
    assert sockets[1].sent[0]["args"] == ["orderbook.50.ETHUSDT"]


def test_websocket_backoff_grows_until_a_market_event_is_accepted() -> None:
    subscribe_ack = {"success": True, "op": "subscribe", "ret_msg": ""}
    sockets = [
        FakeSocket([OSError("connection lost")]),
        FakeSocket([OSError("connection lost")]),
        FakeSocket([OSError("connection lost")]),
        FakeSocket([subscribe_ack, ticker_message()]),
    ]
    connection_count = 0
    sleeps: list[float] = []

    @asynccontextmanager
    async def connect_factory(_: str) -> AsyncIterator[FakeSocket]:
        nonlocal connection_count
        socket = sockets[connection_count]
        connection_count += 1
        yield socket

    async def no_wait(delay: float) -> None:
        sleeps.append(delay)

    async def receive_ticker() -> TickerEvent:
        client = BybitPublicWebSocketClient(
            category="linear",
            connect_factory=connect_factory,
            sleep=no_wait,
        )
        stream = client.stream_ticker("ETHUSDT")
        event = await anext(stream)
        await stream.aclose()
        return event

    assert asyncio.run(receive_ticker()).symbol == "ETHUSDT"
    assert sleeps == [1.0, 2.0, 4.0]


def test_json_lines_recorder_writes_normalized_hashed_events(tmp_path: Path) -> None:
    ticker = parse_ticker_message(ticker_message(), connection_generation=4)
    trade = parse_trade_message(trade_message(), connection_generation=4)[0]
    path = tmp_path / "normalized_market_data.jsonl"
    recorder = JsonLinesMarketDataRecorder(path)

    recorder.append(trade)
    recorder.append(ticker)
    snapshot = parse_orderbook_message(book_message(update_id=10, bid="3000"), 4)
    recorder.append(snapshot)
    delta_message = book_message(update_id=11, bid="3000")
    delta_message["type"] = "delta"
    delta_message["data"]["b"] = [["3000", "0"]]
    delta_message["data"]["a"] = []
    recorder.append(parse_orderbook_message(delta_message, 4))

    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert records[0] == {
        "timestamp": "2026-07-14T12:00:00.090000+00:00",
        "symbol": "ETHUSDT",
        "event_type": MarketDataEventType.TRADE_OBSERVED.value,
        "sequence": 102,
        "update_id": None,
        "price": "3000.1",
        "size": "0.5",
        "book_side": None,
        "connection_generation": 4,
        "raw_payload_hash": trade.raw_payload_hash,
    }
    assert records[1]["event_type"] == MarketDataEventType.TICKER_UPDATED.value
    assert records[1]["price"] == "3000.1"
    assert records[1]["size"] is None
    assert records[2]["event_type"] == (
        MarketDataEventType.ORDER_BOOK_SNAPSHOT_APPLIED.value
    )
    assert records[2]["book_side"] == "bid"
    assert records[2]["update_id"] == 10
    assert records[2]["price"] == "3000"
    assert records[2]["size"] == "2"
    assert records[3]["book_side"] == "ask"
    assert records[3]["price"] == "3000.1"
    assert records[3]["size"] == "3"
    assert records[4]["event_type"] == (
        MarketDataEventType.ORDER_BOOK_DELTA_APPLIED.value
    )
    assert records[4]["update_id"] == 11
    assert records[4]["book_side"] == "bid"
    assert records[4]["price"] == "3000"
    assert records[4]["size"] == "0"
