"""Normalize Bybit V5 public payloads at the infrastructure boundary."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, cast

from eth_credit_hedge.domain.instruments import (
    InstrumentCategory,
    InstrumentSpec,
    LotSizeFilter,
    PriceFilter,
)
from eth_credit_hedge.domain.market_data import (
    BookLevel,
    OrderBookDelta,
    OrderBookEvent,
    OrderBookSnapshot,
    TickerEvent,
    TradeEvent,
    TradeSide,
)


def raw_payload_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def parse_instrument_spec(
    item: dict[str, Any],
    category: InstrumentCategory,
) -> InstrumentSpec:
    price_filter = _object(item, "priceFilter")
    lot_filter = _object(item, "lotSizeFilter")
    delivery_milliseconds = int(item.get("deliveryTime", 0) or 0)
    delivery = (
        _timestamp(delivery_milliseconds) if delivery_milliseconds > 0 else None
    )
    return InstrumentSpec(
        symbol=str(item["symbol"]),
        category=category,
        status=str(item["status"]),
        base_coin=str(item["baseCoin"]),
        quote_coin=str(item["quoteCoin"]),
        settle_coin=str(item["settleCoin"]),
        price_filter=PriceFilter(
            tick_size=Decimal(str(price_filter["tickSize"])),
            min_price=_optional_decimal(price_filter.get("minPrice")),
            max_price=_optional_decimal(price_filter.get("maxPrice")),
        ),
        lot_size_filter=LotSizeFilter(
            qty_step=Decimal(str(lot_filter["qtyStep"])),
            min_order_qty=Decimal(str(lot_filter["minOrderQty"])),
            max_order_qty=Decimal(str(lot_filter["maxOrderQty"])),
            max_market_order_qty=_optional_decimal(
                lot_filter.get("maxMktOrderQty")
                or lot_filter.get("maxMarketOrderQty")
            ),
            min_notional=_optional_decimal(lot_filter.get("minNotionalValue")),
        ),
        contract_multiplier=Decimal("1"),
        delivery_time_utc=delivery,
    )


def parse_ticker_message(
    message: dict[str, Any],
    *,
    connection_generation: int,
) -> TickerEvent:
    data = _message_data_object(message)
    bid_price, bid_size = _ticker_top_level(
        data,
        price_keys=("bid1Price", "bidPrice"),
        size_keys=("bid1Size", "bidSize"),
    )
    ask_price, ask_size = _ticker_top_level(
        data,
        price_keys=("ask1Price", "askPrice"),
        size_keys=("ask1Size", "askSize"),
    )
    bid_iv = _optional_decimal(data.get("bidIv")) if bid_price is not None else None
    ask_iv = _optional_decimal(data.get("askIv")) if ask_price is not None else None
    return TickerEvent(
        symbol=str(data["symbol"]),
        timestamp_utc=_timestamp(int(message["ts"])),
        last_price=_optional_decimal(data.get("lastPrice")),
        mark_price=_optional_decimal(data.get("markPrice")),
        index_price=_optional_decimal(data.get("indexPrice")),
        bid_price=bid_price,
        ask_price=ask_price,
        sequence=_optional_int(message.get("cs")),
        connection_generation=connection_generation,
        raw_payload_hash=raw_payload_hash(message),
        bid_size=bid_size,
        ask_size=ask_size,
        bid_iv=bid_iv,
        ask_iv=ask_iv,
        mark_iv=_optional_decimal(
            data.get("markPriceIv") or data.get("markIv")
        ),
        underlying_price=_optional_decimal(data.get("underlyingPrice")),
        delta=_optional_decimal(data.get("delta")),
        gamma=_optional_decimal(data.get("gamma")),
        vega=_optional_decimal(data.get("vega")),
        theta=_optional_decimal(data.get("theta")),
    )


def parse_trade_message(
    message: dict[str, Any],
    *,
    connection_generation: int,
) -> tuple[TradeEvent, ...]:
    data = message.get("data")
    if not isinstance(data, list):
        raise ValueError("Bybit trade message data must be a list")
    payload_hash = raw_payload_hash(message)
    trades: list[TradeEvent] = []
    for raw_item in data:
        if not isinstance(raw_item, dict):
            raise ValueError("Bybit trade item must be an object")
        item = cast(dict[str, Any], raw_item)
        raw_side = str(item["S"])
        if raw_side not in ("Buy", "Sell"):
            raise ValueError("Bybit trade side must be Buy or Sell")
        side = cast(TradeSide, raw_side)
        trades.append(
            TradeEvent(
                symbol=str(item["s"]),
                timestamp_utc=_timestamp(int(item["T"])),
                price=Decimal(str(item["p"])),
                size=Decimal(str(item["v"])),
                side=side,
                trade_id=str(item["i"]),
                sequence=_optional_int(item.get("seq")),
                connection_generation=connection_generation,
                raw_payload_hash=payload_hash,
            )
        )
    return tuple(trades)


def parse_orderbook_message(
    message: dict[str, Any],
    connection_generation: int,
) -> OrderBookEvent:
    data = _message_data_object(message)
    update_id = int(data["u"])
    symbol = str(data["s"])
    bids = _levels(data.get("b", []))
    asks = _levels(data.get("a", []))
    sequence = int(data["seq"])
    timestamp = _timestamp(int(message["ts"]))
    payload_hash = raw_payload_hash(message)
    if str(message.get("type")) == "snapshot" or update_id == 1:
        return OrderBookSnapshot(
            symbol=symbol,
            bids=bids,
            asks=asks,
            update_id=update_id,
            sequence=sequence,
            timestamp_utc=timestamp,
            connection_generation=connection_generation,
            raw_payload_hash=payload_hash,
        )
    if str(message.get("type")) != "delta":
        raise ValueError("Bybit order-book type must be snapshot or delta")
    return OrderBookDelta(
        symbol=symbol,
        bids=bids,
        asks=asks,
        update_id=update_id,
        sequence=sequence,
        timestamp_utc=timestamp,
        connection_generation=connection_generation,
        raw_payload_hash=payload_hash,
    )


def _levels(raw_levels: object) -> tuple[BookLevel, ...]:
    if not isinstance(raw_levels, list):
        raise ValueError("Bybit order-book levels must be a list")
    levels: list[BookLevel] = []
    for raw_level in raw_levels:
        if not isinstance(raw_level, list) or len(raw_level) != 2:
            raise ValueError("Bybit order-book level must contain price and size")
        levels.append((Decimal(str(raw_level[0])), Decimal(str(raw_level[1]))))
    return tuple(levels)


def _object(parent: dict[str, Any], key: str) -> dict[str, Any]:
    value = parent.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"Bybit {key} must be an object")
    return cast(dict[str, Any], value)


def _message_data_object(message: dict[str, Any]) -> dict[str, Any]:
    data = message.get("data")
    if isinstance(data, list):
        if len(data) != 1 or not isinstance(data[0], dict):
            raise ValueError("Bybit message must contain one data object")
        return cast(dict[str, Any], data[0])
    if not isinstance(data, dict):
        raise ValueError("Bybit message data must be an object")
    return cast(dict[str, Any], data)


def _optional_decimal(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    return Decimal(str(value))


def _ticker_top_level(
    data: dict[str, Any],
    *,
    price_keys: tuple[str, ...],
    size_keys: tuple[str, ...],
) -> tuple[Decimal | None, Decimal | None]:
    raw_price = next((data[key] for key in price_keys if key in data), None)
    raw_size = next((data[key] for key in size_keys if key in data), None)
    price = _optional_decimal(raw_price)
    size = _optional_decimal(raw_size)
    if size == 0:
        return None, None
    if price == 0:
        if size is None:
            return None, None
        raise ValueError("top-of-book zero price and size must be consistent")
    if price is None and size is not None:
        raise ValueError("top-of-book price is missing for nonzero size")
    return price, size


def _optional_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    return int(cast(str | int, value))


def _timestamp(milliseconds: int) -> datetime:
    return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc)
