"""Async public Bybit V5 REST adapter with cursor pagination."""

from __future__ import annotations

import asyncio
import json
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any, cast

from eth_credit_hedge.data.bybit_options import parse_option_fixture
from eth_credit_hedge.domain.instruments import (
    InstrumentCategory,
    InstrumentSpec,
    OptionMarketQuote,
)
from eth_credit_hedge.domain.market_data import OrderBookSnapshot
from eth_credit_hedge.infrastructure.bybit.parsers import (
    parse_instrument_spec,
    parse_orderbook_message,
)


PublicRequester = Callable[[str, dict[str, str | int]], dict[str, Any]]


class BybitPublicRestClient:
    def __init__(
        self,
        base_url: str = "https://api.bybit.com",
        timeout_seconds: float = 10.0,
        *,
        requester: PublicRequester | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._requester = requester or self._http_get

    async def list_instruments(
        self,
        category: InstrumentCategory,
        *,
        base_coin: str | None = None,
        status: str | None = "Trading",
    ) -> tuple[InstrumentSpec, ...]:
        items, _ = await self._fetch_instrument_items(
            category,
            base_coin=base_coin,
            status=status,
        )
        return tuple(parse_instrument_spec(item, category) for item in items)

    async def get_instrument(self, symbol: str) -> InstrumentSpec:
        category: InstrumentCategory = "option" if "-" in symbol else "linear"
        response = await self._request(
            "/v5/market/instruments-info",
            {"category": category, "symbol": symbol},
        )
        _validate_response(response, category)
        items = _result_items(response)
        matches = [item for item in items if str(item.get("symbol")) == symbol]
        if len(matches) != 1:
            raise ValueError(f"expected exactly one instrument for {symbol}")
        return parse_instrument_spec(matches[0], category)

    async def get_option_chain(
        self,
        base_coin: str,
    ) -> tuple[OptionMarketQuote, ...]:
        items, instrument_time = await self._fetch_instrument_items(
            "option",
            base_coin=base_coin,
            status="Trading",
        )
        ticker_response = await self._request(
            "/v5/market/tickers",
            {"category": "option", "baseCoin": base_coin},
        )
        _validate_response(ticker_response, "option")
        combined_instruments = {
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "category": "option",
                "nextPageCursor": "",
                "list": items,
            },
            "time": instrument_time,
        }
        fixture = {
            "requests": [
                {"response": combined_instruments},
                {"response": ticker_response},
            ]
        }
        return tuple(entry.quote for entry in parse_option_fixture(fixture))

    async def get_orderbook_snapshot(
        self,
        symbol: str,
        depth: int,
    ) -> OrderBookSnapshot:
        category: InstrumentCategory = "option" if "-" in symbol else "linear"
        response = await self._request(
            "/v5/market/orderbook",
            {"category": category, "symbol": symbol, "limit": depth},
        )
        _validate_response(response, category)
        result = response["result"]
        if not isinstance(result, dict):
            raise ValueError("Bybit order-book result must be an object")
        event = parse_orderbook_message(
            {
                "topic": f"orderbook.{depth}.{symbol}",
                "type": "snapshot",
                "ts": result.get("ts", response["time"]),
                "data": result,
            },
            connection_generation=0,
        )
        if not isinstance(event, OrderBookSnapshot):
            raise ValueError("Bybit REST order book must normalize as a snapshot")
        return event

    async def _fetch_instrument_items(
        self,
        category: InstrumentCategory,
        *,
        base_coin: str | None,
        status: str | None,
    ) -> tuple[list[dict[str, Any]], int]:
        items: list[dict[str, Any]] = []
        cursor = ""
        seen_cursors: set[str] = set()
        latest_time = 0
        while True:
            params: dict[str, str | int] = {"category": category, "limit": 1000}
            if base_coin is not None:
                params["baseCoin"] = base_coin
            if status is not None:
                params["status"] = status
            if cursor:
                params["cursor"] = cursor
            response = await self._request("/v5/market/instruments-info", params)
            _validate_response(response, category)
            items.extend(_result_items(response))
            latest_time = int(response["time"])
            result = cast(dict[str, Any], response["result"])
            next_cursor = str(result.get("nextPageCursor", ""))
            if not next_cursor:
                break
            if next_cursor in seen_cursors:
                raise ValueError("Bybit instrument pagination cursor repeated")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return items, latest_time

    async def _request(
        self,
        endpoint: str,
        params: dict[str, str | int],
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self._requester, endpoint, params)

    def _http_get(
        self,
        endpoint: str,
        params: dict[str, str | int],
    ) -> dict[str, Any]:
        url = f"{self.base_url}{endpoint}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "eth-credit-spread-hedge/0.1"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            payload: object = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Bybit response must be a JSON object")
        return cast(dict[str, Any], payload)


def _validate_response(
    response: dict[str, Any],
    expected_category: InstrumentCategory,
) -> None:
    if response.get("retCode") != 0:
        raise ValueError(
            f"Bybit response error {response.get('retCode')}: "
            f"{response.get('retMsg')}"
        )
    result = response.get("result")
    if not isinstance(result, dict):
        raise ValueError("Bybit response result must be an object")
    category = result.get("category")
    if category is not None and category != expected_category:
        raise ValueError("Bybit response category does not match request")
    if "time" not in response:
        raise ValueError("Bybit response is missing server time")


def _result_items(response: dict[str, Any]) -> list[dict[str, Any]]:
    result = cast(dict[str, Any], response["result"])
    raw_items = result.get("list")
    if not isinstance(raw_items, list):
        raise ValueError("Bybit response list must be an array")
    items: list[dict[str, Any]] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            raise ValueError("Bybit response item must be an object")
        items.append(cast(dict[str, Any], raw_item))
    return items
