"""Clock-gated, demo-only Bybit V5 private REST adapter."""

from __future__ import annotations

import asyncio
import http.client
import json
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Literal, cast

from eth_credit_hedge.domain.execution import (
    AmendOrderRequest,
    CancelOrderRequest,
    Category,
    ExchangeOrder,
    ExchangePosition,
    ExecutionUpdate,
    OrderRequestAck,
    OrderRequestKind,
    OrderSide,
    OrderType,
    PlaceOrderRequest,
    TimeInForce,
    TriggerBy,
    UncertainOrderOutcomeError,
    WalletBalance,
    WalletState,
)
from eth_credit_hedge.infrastructure.bybit.auth import BybitV5Signer
from eth_credit_hedge.infrastructure.bybit.clock import ClockSyncSample, ServerClock
from eth_credit_hedge.infrastructure.bybit.environment import BybitDemoProfile
from eth_credit_hedge.infrastructure.bybit.error_mapping import (
    BybitApiError,
    BybitUnknownOrderError,
    raise_for_bybit_error,
)


JsonObject = dict[str, object]
PrivateRequester = Callable[["PreparedBybitRequest"], JsonObject]


@dataclass(frozen=True, slots=True, repr=False)
class PreparedBybitRequest:
    """Exact request bytes and headers, with a secret-safe representation."""

    method: Literal["GET", "POST"]
    url: str
    headers: dict[str, str]
    body: bytes | None = None

    def __repr__(self) -> str:
        body_description = "None" if self.body is None else f"<{len(self.body)} bytes>"
        return (
            "PreparedBybitRequest("
            f"method={self.method!r}, url={self.url!r}, "
            "headers='<redacted>', "
            f"body={body_description})"
        )


class BybitUncertainRequestError(UncertainOrderOutcomeError):
    """A mutation may have reached Bybit but no response was received."""

    pass


class BybitPrivateRestClient:
    """Implement private trading and account reads against Bybit demo only."""

    def __init__(
        self,
        *,
        profile: BybitDemoProfile,
        clock: ServerClock,
        requester: PrivateRequester | None = None,
        timeout_seconds: float = 10.0,
        wall_time_ms: Callable[[], int] | None = None,
    ) -> None:
        if type(profile) is not BybitDemoProfile:
            raise TypeError("private REST requires the fixed Bybit demo profile")
        if not isinstance(clock, ServerClock):
            raise TypeError("clock must be a ServerClock")
        if timeout_seconds <= 0:
            raise ValueError("timeout must be positive")
        self._profile = profile
        self._clock = clock
        self._signer = BybitV5Signer(profile.credentials)
        self._timeout_seconds = timeout_seconds
        self._requester = requester or self._http_request
        self._wall_time_ms = wall_time_ms or _wall_time_ms

    async def synchronize_clock(self) -> ClockSyncSample:
        """Measure and record demo REST server time using the local midpoint."""
        request_sent_at_ms = self._wall_time_ms()
        response = await self._send(
            PreparedBybitRequest(
                method="GET",
                url=f"{self._profile.rest_base_url}/v5/market/time",
                headers={"User-Agent": "eth-credit-spread-hedge/0.1"},
            )
        )
        response_received_at_ms = self._wall_time_ms()
        return self._clock.record_sample(
            request_sent_at_ms=request_sent_at_ms,
            response_received_at_ms=response_received_at_ms,
            server_time_ms=_response_time_ms(response),
        )

    async def place_order(self, request: PlaceOrderRequest) -> OrderRequestAck:
        payload: JsonObject = {
            "category": request.category,
            "symbol": request.symbol,
            "side": request.side,
            "orderType": request.order_type,
            "qty": _decimal_text(request.quantity),
            "orderLinkId": request.order_link_id,
            "timeInForce": request.time_in_force,
            "reduceOnly": request.reduce_only,
            "positionIdx": request.position_idx,
        }
        if request.price is not None:
            payload["price"] = _decimal_text(request.price)
        if request.trigger_price is not None:
            payload["triggerPrice"] = _decimal_text(request.trigger_price)
            payload["triggerDirection"] = request.trigger_direction
            payload["triggerBy"] = request.trigger_by
        if request.close_on_trigger:
            payload["closeOnTrigger"] = True
        response = await self._mutate(
            "/v5/order/create",
            payload,
            operation="place order",
            order_link_id=request.order_link_id,
        )
        return _parse_ack(response, OrderRequestKind.PLACE, request.order_link_id)

    async def amend_order(self, request: AmendOrderRequest) -> OrderRequestAck:
        payload: JsonObject = {
            "category": request.category,
            "symbol": request.symbol,
            "orderLinkId": request.order_link_id,
        }
        if request.quantity is not None:
            payload["qty"] = _decimal_text(request.quantity)
        if request.price is not None:
            payload["price"] = _decimal_text(request.price)
        if request.trigger_price is not None:
            payload["triggerPrice"] = _decimal_text(request.trigger_price)
        response = await self._mutate(
            "/v5/order/amend",
            payload,
            operation="amend order",
            order_link_id=request.order_link_id,
        )
        return _parse_ack(response, OrderRequestKind.AMEND, request.order_link_id)

    async def cancel_order(self, request: CancelOrderRequest) -> OrderRequestAck:
        response = await self._mutate(
            "/v5/order/cancel",
            {
                "category": request.category,
                "symbol": request.symbol,
                "orderLinkId": request.order_link_id,
            },
            operation="cancel order",
            order_link_id=request.order_link_id,
        )
        return _parse_ack(response, OrderRequestKind.CANCEL, request.order_link_id)

    async def cancel_all(
        self,
        category: str,
        symbol: str | None = None,
    ) -> None:
        normalized_category = _category(category)
        payload: JsonObject = {"category": normalized_category}
        if symbol is not None:
            payload["symbol"] = _required_argument(symbol, "symbol")
        await self._mutate(
            "/v5/order/cancel-all",
            payload,
            operation="cancel all orders",
            order_link_id=None,
        )

    async def get_open_orders(
        self,
        category: str,
        symbol: str | None = None,
    ) -> tuple[ExchangeOrder, ...]:
        normalized_category = _category(category)
        params = _category_symbol_params(normalized_category, symbol)
        responses = await self._get_all_pages("/v5/order/realtime", params)
        return tuple(
            _parse_exchange_order(item, normalized_category)
            for response in responses
            for item in _result_items(response)
        )

    async def get_order_history(
        self,
        category: str,
        symbol: str | None = None,
    ) -> tuple[ExchangeOrder, ...]:
        normalized_category = _category(category)
        params = _category_symbol_params(normalized_category, symbol)
        responses = await self._get_all_pages("/v5/order/history", params)
        return tuple(
            _parse_exchange_order(item, normalized_category)
            for response in responses
            for item in _result_items(response)
        )

    async def get_order_by_link_id(
        self,
        category: str,
        symbol: str,
        order_link_id: str,
    ) -> ExchangeOrder | None:
        normalized_category = _category(category)
        params = {
            "category": normalized_category,
            "symbol": _required_argument(symbol, "symbol"),
            "orderLinkId": _required_argument(
                order_link_id,
                "order link ID",
            ),
        }
        try:
            response = await self._get("/v5/order/realtime", params)
            items = _result_items(response)
        except BybitUnknownOrderError:
            items = []
        if not items:
            try:
                response = await self._get("/v5/order/history", params)
                items = _result_items(response)
            except BybitUnknownOrderError:
                return None
        if not items:
            return None
        if len(items) != 1:
            raise ValueError("Bybit returned multiple orders for one orderLinkId")
        return _parse_exchange_order(items[0], normalized_category)

    async def get_execution_history(
        self,
        category: str,
        symbol: str | None = None,
        order_link_id: str | None = None,
    ) -> tuple[ExecutionUpdate, ...]:
        normalized_category = _category(category)
        params = _category_symbol_params(normalized_category, symbol)
        if order_link_id is not None:
            params["orderLinkId"] = _required_argument(
                order_link_id,
                "order link ID",
            )
        responses = await self._get_all_pages("/v5/execution/list", params)
        return tuple(
            _parse_execution(item)
            for response in responses
            for item in _result_items(response)
        )

    async def get_positions(
        self,
        category: str,
        symbol: str | None = None,
    ) -> tuple[ExchangePosition, ...]:
        normalized_category = _category(category)
        params = _category_symbol_params(normalized_category, symbol)
        responses = await self._get_all_pages("/v5/position/list", params)
        return tuple(
            _parse_position(
                item,
                normalized_category,
                observed_at=_response_time(response),
            )
            for response in responses
            for item in _result_items(response)
        )

    async def get_wallet_state(self) -> WalletState:
        response = await self._get(
            "/v5/account/wallet-balance",
            {"accountType": "UNIFIED"},
        )
        accounts = _result_items(response)
        if len(accounts) != 1:
            raise ValueError("Bybit wallet response must contain exactly one account")
        return _parse_wallet(accounts[0], _response_time(response))

    async def _get_all_pages(
        self,
        endpoint: str,
        initial_params: Mapping[str, str],
    ) -> tuple[JsonObject, ...]:
        params = dict(initial_params)
        responses: list[JsonObject] = []
        seen_cursors: set[str] = set()
        while True:
            response = await self._get(endpoint, params)
            responses.append(response)
            next_cursor = _optional_text(
                _result_object(response).get("nextPageCursor")
            )
            if next_cursor is None:
                break
            if next_cursor in seen_cursors:
                raise ValueError("Bybit private pagination cursor repeated")
            seen_cursors.add(next_cursor)
            params["cursor"] = next_cursor
        return tuple(responses)

    async def _get(
        self,
        endpoint: str,
        params: Mapping[str, str],
    ) -> JsonObject:
        query_string = urllib.parse.urlencode(list(params.items()))
        timestamp_ms = self._clock.timestamp_ms()
        signed_headers = self._signer.sign_get(
            timestamp_ms=timestamp_ms,
            query_string=query_string,
        )
        suffix = f"?{query_string}" if query_string else ""
        prepared = PreparedBybitRequest(
            method="GET",
            url=f"{self._profile.rest_base_url}{endpoint}{suffix}",
            headers=signed_headers.as_http_headers(),
        )
        return await self._send(prepared)

    async def _mutate(
        self,
        endpoint: str,
        payload: Mapping[str, object],
        *,
        operation: str,
        order_link_id: str | None,
    ) -> JsonObject:
        body = json.dumps(
            dict(payload),
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        timestamp_ms = self._clock.timestamp_ms()
        signed_headers = self._signer.sign_post(
            timestamp_ms=timestamp_ms,
            body=body.decode("utf-8"),
        )
        prepared = PreparedBybitRequest(
            method="POST",
            url=f"{self._profile.rest_base_url}{endpoint}",
            headers=signed_headers.as_http_headers(),
            body=body,
        )
        try:
            return await self._send(prepared)
        except BybitApiError:
            raise
        except (
            OSError,
            http.client.HTTPException,
            json.JSONDecodeError,
            ValueError,
        ) as exc:
            raise BybitUncertainRequestError(
                order_link_id=order_link_id,
                operation=operation,
            ) from exc

    async def _send(self, request: PreparedBybitRequest) -> JsonObject:
        response = await asyncio.to_thread(self._requester, request)
        return _validate_response(response)

    def _http_request(self, request: PreparedBybitRequest) -> JsonObject:
        wire_request = urllib.request.Request(
            request.url,
            data=request.body,
            headers=request.headers,
            method=request.method,
        )
        with urllib.request.urlopen(
            wire_request,
            timeout=self._timeout_seconds,
        ) as response:
            payload: object = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Bybit response must be a JSON object")
        return cast(JsonObject, payload)


def _validate_response(response: JsonObject) -> JsonObject:
    raw_code = response.get("retCode")
    if type(raw_code) is not int:
        raise ValueError("Bybit response retCode must be an integer")
    raw_message = response.get("retMsg")
    if not isinstance(raw_message, str):
        raise ValueError("Bybit response retMsg must be a string")
    raise_for_bybit_error(raw_code, raw_message)
    _result_object(response)
    _response_time(response)
    return response


def _result_object(response: JsonObject) -> JsonObject:
    result = response.get("result")
    if not isinstance(result, dict):
        raise ValueError("Bybit response result must be an object")
    return cast(JsonObject, result)


def _result_items(response: JsonObject) -> list[JsonObject]:
    raw_items = _result_object(response).get("list")
    if not isinstance(raw_items, list):
        raise ValueError("Bybit response list must be an array")
    items: list[JsonObject] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            raise ValueError("Bybit response item must be an object")
        items.append(cast(JsonObject, raw_item))
    return items


def _response_time(response: JsonObject) -> datetime:
    return _timestamp(_response_time_ms(response), "response time")


def _response_time_ms(response: JsonObject) -> int:
    value = response.get("time")
    if isinstance(value, bool):
        raise ValueError("response time must be epoch milliseconds")
    try:
        milliseconds = int(cast(int | str, value))
    except (TypeError, ValueError) as exc:
        raise ValueError("response time must be epoch milliseconds") from exc
    if milliseconds < 0:
        raise ValueError("response time cannot be negative")
    return milliseconds


def _wall_time_ms() -> int:
    return time.time_ns() // 1_000_000


def _parse_ack(
    response: JsonObject,
    request_kind: OrderRequestKind,
    requested_order_link_id: str,
) -> OrderRequestAck:
    result = _result_object(response)
    returned_order_link_id = _optional_text(result.get("orderLinkId"))
    return OrderRequestAck(
        request_kind=request_kind,
        order_id=_required_text(result, "orderId"),
        order_link_id=returned_order_link_id or requested_order_link_id,
        acknowledged_at=_response_time(response),
    )


def _parse_exchange_order(
    item: JsonObject,
    fallback_category: Category,
) -> ExchangeOrder:
    category = _category(_optional_text(item.get("category")) or fallback_category)
    return ExchangeOrder(
        category=category,
        order_id=_required_text(item, "orderId"),
        order_link_id=_text(item.get("orderLinkId"), "orderLinkId"),
        symbol=_required_text(item, "symbol"),
        status=_required_text(item, "orderStatus"),
        side=_order_side(item.get("side")),
        order_type=_order_type(item.get("orderType")),
        price=_optional_positive_decimal(item.get("price"), "price"),
        quantity=_decimal(item.get("qty"), "qty"),
        cumulative_filled_quantity=_decimal(
            item.get("cumExecQty"),
            "cumExecQty",
            default=Decimal("0"),
        ),
        average_price=_optional_positive_decimal(
            item.get("avgPrice"),
            "avgPrice",
        ),
        reduce_only=_bool(item.get("reduceOnly"), "reduceOnly"),
        created_at=_timestamp(item.get("createdTime"), "createdTime"),
        updated_at=_timestamp(item.get("updatedTime"), "updatedTime"),
        trigger_price=_optional_positive_decimal(
            item.get("triggerPrice"),
            "triggerPrice",
        ),
        trigger_by=cast(
            TriggerBy | None,
            _optional_text(item.get("triggerBy")),
        ),
        trigger_direction=_optional_integer(
            item.get("triggerDirection"),
            "triggerDirection",
        ),
        time_in_force=cast(
            TimeInForce | None,
            _optional_text(item.get("timeInForce")),
        ),
        position_idx=_optional_integer(item.get("positionIdx"), "positionIdx"),
        close_on_trigger=(
            False
            if item.get("closeOnTrigger") is None
            else _bool(item.get("closeOnTrigger"), "closeOnTrigger")
        ),
    )


def _parse_execution(item: JsonObject) -> ExecutionUpdate:
    is_maker = item.get("isMaker")
    if is_maker is not None and type(is_maker) is not bool:
        raise ValueError("isMaker must be a boolean when present")
    return ExecutionUpdate(
        execution_id=_required_text(item, "execId"),
        order_id=_required_text(item, "orderId"),
        order_link_id=_text(item.get("orderLinkId"), "orderLinkId"),
        symbol=_required_text(item, "symbol"),
        side=_order_side(item.get("side")),
        price=_decimal(item.get("execPrice"), "execPrice"),
        quantity=_decimal(item.get("execQty"), "execQty"),
        fee=_decimal(item.get("execFee"), "execFee", default=Decimal("0")),
        is_maker=is_maker,
        executed_at=_timestamp(item.get("execTime"), "execTime"),
    )


def _parse_position(
    item: JsonObject,
    fallback_category: Category,
    *,
    observed_at: datetime,
) -> ExchangePosition:
    raw_side = _optional_text(item.get("side"))
    side = None if raw_side is None else _order_side(raw_side)
    return ExchangePosition(
        category=_category(
            _optional_text(item.get("category")) or fallback_category
        ),
        symbol=_required_text(item, "symbol"),
        side=side,
        quantity=_decimal(item.get("size"), "size", default=Decimal("0")),
        average_price=_optional_positive_decimal(
            item.get("avgPrice"),
            "avgPrice",
        ),
        mark_price=_optional_positive_decimal(item.get("markPrice"), "markPrice"),
        liquidation_price=_optional_positive_decimal(
            item.get("liqPrice"),
            "liqPrice",
        ),
        unrealized_pnl=_decimal(
            item.get("unrealisedPnl"),
            "unrealisedPnl",
            default=Decimal("0"),
        ),
        updated_at=(
            observed_at
            if item.get("updatedTime") in (None, "")
            else _timestamp(item.get("updatedTime"), "updatedTime")
        ),
        position_idx=(
            _optional_integer(item.get("positionIdx"), "positionIdx") or 0
        ),
    )


def _parse_wallet(item: JsonObject, updated_at: datetime) -> WalletState:
    raw_coins = item.get("coin")
    if not isinstance(raw_coins, list):
        raise ValueError("Bybit wallet coin list must be an array")
    balances: list[WalletBalance] = []
    for raw_coin in raw_coins:
        if not isinstance(raw_coin, dict):
            raise ValueError("Bybit wallet coin entry must be an object")
        coin = cast(JsonObject, raw_coin)
        available_value = coin.get("availableBalance")
        if available_value is None:
            available_value = coin.get("availableToWithdraw")
        balances.append(
            WalletBalance(
                coin=_required_text(coin, "coin"),
                equity=_decimal(coin.get("equity"), "equity"),
                wallet_balance=_decimal(
                    coin.get("walletBalance"),
                    "walletBalance",
                ),
                available_balance=_optional_decimal(
                    available_value,
                    "available balance",
                ),
                unrealized_pnl=_decimal(
                    coin.get("unrealisedPnl"),
                    "unrealisedPnl",
                    default=Decimal("0"),
                ),
            )
        )
    return WalletState(
        account_type=_required_text(item, "accountType"),
        total_equity=_decimal(item.get("totalEquity"), "totalEquity"),
        total_wallet_balance=_decimal(
            item.get("totalWalletBalance"),
            "totalWalletBalance",
        ),
        total_available_balance=_optional_decimal(
            item.get("totalAvailableBalance"),
            "totalAvailableBalance",
        ),
        balances=tuple(balances),
        updated_at=updated_at,
    )


def _category(value: str) -> Category:
    if value not in ("linear", "option"):
        raise ValueError("category must be linear or option")
    return cast(Category, value)


def _category_symbol_params(
    category: Category,
    symbol: str | None,
) -> dict[str, str]:
    params: dict[str, str] = {"category": category}
    if symbol is not None:
        params["symbol"] = _required_argument(symbol, "symbol")
    return params


def _required_argument(value: str, field_name: str) -> str:
    if not value.strip():
        raise ValueError(f"{field_name} cannot be empty")
    return value


def _required_text(item: Mapping[str, object], key: str) -> str:
    value = _text(item.get(key), key)
    if not value:
        raise ValueError(f"{key} cannot be empty")
    return value


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    return value


def _optional_text(value: object) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("optional Bybit text field must be a string")
    return value


def _optional_integer(value: object, field_name: str) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer")
    try:
        return int(cast(str | int, value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _decimal(
    value: object,
    field_name: str,
    *,
    default: Decimal | None = None,
) -> Decimal:
    if value is None or value == "":
        if default is not None:
            return default
        raise ValueError(f"{field_name} cannot be empty")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be a decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return result


def _optional_decimal(value: object, field_name: str) -> Decimal | None:
    if value is None or value == "":
        return None
    return _decimal(value, field_name)


def _optional_positive_decimal(
    value: object,
    field_name: str,
) -> Decimal | None:
    result = _optional_decimal(value, field_name)
    if result == Decimal("0"):
        return None
    return result


def _bool(value: object, field_name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{field_name} must be a boolean")
    return value


def _order_side(value: object) -> OrderSide:
    if value not in ("Buy", "Sell"):
        raise ValueError("side must be Buy or Sell")
    return value


def _order_type(value: object) -> OrderType:
    if value not in ("Market", "Limit"):
        raise ValueError("orderType must be Market or Limit")
    return value


def _timestamp(value: object, field_name: str) -> datetime:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be epoch milliseconds")
    try:
        milliseconds = int(cast(int | str, value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be epoch milliseconds") from exc
    if milliseconds < 0:
        raise ValueError(f"{field_name} cannot be negative")
    return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc)
