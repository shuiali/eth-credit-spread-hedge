"""Authenticated Bybit REST adapter contract tests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlsplit

import pytest

from eth_credit_hedge.domain.execution import PlaceOrderRequest
from eth_credit_hedge.infrastructure.bybit.auth import (
    ApiCredentials,
    BybitV5Signer,
    SecretStr,
)
from eth_credit_hedge.infrastructure.bybit.clock import (
    ClockNotSynchronizedError,
    ServerClock,
)
from eth_credit_hedge.infrastructure.bybit.environment import BybitDemoProfile
from eth_credit_hedge.infrastructure.bybit.private_rest import (
    BybitPrivateRestClient,
    BybitUncertainRequestError,
    PreparedBybitRequest,
)


NOW_MS = 1_658_385_579_423
ORDER_LINK_ID = "ECH-01-C0007-L01-ENTRY-A02-9F3C"


def credentials() -> ApiCredentials:
    return ApiCredentials(
        api_key=SecretStr("demo-key"),
        api_secret=SecretStr("demo-secret"),
    )


def synchronized_clock() -> ServerClock:
    clock = ServerClock(
        max_absolute_offset_ms=1000,
        max_uncertainty_ms=100,
        max_age_seconds=30,
        wall_time_ms=lambda: NOW_MS,
        monotonic_seconds=lambda: 10.0,
    )
    clock.record_sample(
        request_sent_at_ms=NOW_MS - 10,
        response_received_at_ms=NOW_MS + 10,
        server_time_ms=NOW_MS,
    )
    return clock


class FakeRequester:
    def __init__(
        self,
        responses: list[dict[str, object] | BaseException],
    ) -> None:
        self.responses = list(responses)
        self.requests: list[PreparedBybitRequest] = []

    def __call__(self, request: PreparedBybitRequest) -> dict[str, object]:
        self.requests.append(request)
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def client(
    requester: Callable[[PreparedBybitRequest], dict[str, object]],
    *,
    clock: ServerClock | None = None,
) -> BybitPrivateRestClient:
    return BybitPrivateRestClient(
        profile=BybitDemoProfile(credentials()),
        clock=clock or synchronized_clock(),
        requester=requester,
    )


def success_response(
    result: dict[str, object],
    *,
    response_time: int = NOW_MS,
) -> dict[str, object]:
    return {
        "retCode": 0,
        "retMsg": "OK",
        "result": result,
        "retExtInfo": {},
        "time": response_time,
    }


def order_item(
    order_id: str,
    order_link_id: str,
    *,
    status: str = "New",
) -> dict[str, object]:
    return {
        "category": "linear",
        "orderId": order_id,
        "orderLinkId": order_link_id,
        "symbol": "ETHUSDT",
        "orderStatus": status,
        "side": "Sell",
        "orderType": "Limit",
        "price": "3000.1",
        "qty": "0.010",
        "cumExecQty": "0.002",
        "avgPrice": "3000.0",
        "reduceOnly": False,
        "triggerPrice": "",
        "triggerBy": "",
        "triggerDirection": 0,
        "createdTime": "1658385579000",
        "updatedTime": "1658385579423",
    }


def test_place_order_signs_the_exact_transmitted_body_once() -> None:
    requester = FakeRequester(
        [
            success_response(
                {"orderId": "exchange-1", "orderLinkId": ORDER_LINK_ID}
            )
        ]
    )
    rest = client(requester)
    request = PlaceOrderRequest(
        category="linear",
        symbol="ETHUSDT",
        side="Sell",
        order_type="Limit",
        quantity=Decimal("0.010"),
        order_link_id=ORDER_LINK_ID,
        price=Decimal("3000.1"),
        time_in_force="IOC",
        reduce_only=False,
        position_idx=0,
    )

    acknowledgement = asyncio.run(rest.place_order(request))

    assert acknowledgement.order_id == "exchange-1"
    assert acknowledgement.order_link_id == request.order_link_id
    assert len(requester.requests) == 1
    sent = requester.requests[0]
    assert sent.method == "POST"
    assert sent.url == "https://api-demo.bybit.com/v5/order/create"
    assert sent.body is not None
    assert json.loads(sent.body) == {
        "category": "linear",
        "symbol": "ETHUSDT",
        "side": "Sell",
        "orderType": "Limit",
        "qty": "0.010",
        "orderLinkId": ORDER_LINK_ID,
        "timeInForce": "IOC",
        "reduceOnly": False,
        "positionIdx": 0,
        "price": "3000.1",
    }
    signed_body = sent.body.decode("utf-8")
    expected_headers = BybitV5Signer(credentials()).sign_post(
        timestamp_ms=NOW_MS,
        body=signed_body,
    )
    assert sent.headers == expected_headers.as_http_headers()
    assert "demo-key" not in repr(sent)
    assert sent.headers["X-BAPI-SIGN"] not in repr(sent)


def test_account_margin_mode_can_be_read_and_changed() -> None:
    isolated = success_response({"marginMode": "ISOLATED_MARGIN"})
    changed = success_response({"reasons": []})
    regular = success_response({"marginMode": "REGULAR_MARGIN"})
    isolated.pop("time")
    changed.pop("time")
    regular.pop("time")
    requester = FakeRequester(
        [isolated, changed, regular]
    )
    rest = client(requester)

    before = asyncio.run(rest.get_margin_mode())
    asyncio.run(rest.set_margin_mode("REGULAR_MARGIN"))
    after = asyncio.run(rest.get_margin_mode())

    assert before == "ISOLATED_MARGIN"
    assert after == "REGULAR_MARGIN"
    assert requester.requests[0].url.endswith("/v5/account/info")
    assert requester.requests[1].url.endswith("/v5/account/set-margin-mode")
    assert requester.requests[1].body is not None
    assert json.loads(requester.requests[1].body) == {
        "setMarginMode": "REGULAR_MARGIN"
    }


def test_conditional_close_sends_the_exchange_safety_flag() -> None:
    requester = FakeRequester(
        [
            success_response(
                {
                    "orderId": "stop-1",
                    "orderLinkId": "ECH-01-C0001-L01-STOP-A01-9F3C",
                }
            )
        ]
    )
    request = PlaceOrderRequest(
        category="linear",
        symbol="ETHUSDT",
        side="Buy",
        order_type="Market",
        quantity=Decimal("0.010"),
        order_link_id="ECH-01-C0001-L01-STOP-A01-9F3C",
        reduce_only=True,
        trigger_price=Decimal("3010"),
        trigger_direction=1,
        trigger_by="LastPrice",
        close_on_trigger=True,
    )

    asyncio.run(client(requester).place_order(request))

    assert requester.requests[0].body is not None
    assert json.loads(requester.requests[0].body)["closeOnTrigger"] is True


def test_mutating_timeout_is_uncertain_and_is_not_retried() -> None:
    requester = FakeRequester([TimeoutError("response timed out")])
    rest = client(requester)
    request = PlaceOrderRequest(
        category="linear",
        symbol="ETHUSDT",
        side="Sell",
        order_type="Limit",
        quantity=Decimal("0.010"),
        order_link_id=ORDER_LINK_ID,
        price=Decimal("3000.1"),
        time_in_force="IOC",
    )

    with pytest.raises(BybitUncertainRequestError) as caught:
        asyncio.run(rest.place_order(request))

    assert caught.value.order_link_id == request.order_link_id
    assert len(requester.requests) == 1


def test_mutating_connection_loss_is_also_uncertain() -> None:
    requester = FakeRequester([ConnectionResetError("connection reset")])
    rest = client(requester)
    request = PlaceOrderRequest(
        category="linear",
        symbol="ETHUSDT",
        side="Sell",
        order_type="Limit",
        quantity=Decimal("0.010"),
        order_link_id=ORDER_LINK_ID,
        price=Decimal("3000.1"),
        time_in_force="IOC",
    )

    with pytest.raises(BybitUncertainRequestError):
        asyncio.run(rest.place_order(request))

    assert len(requester.requests) == 1


def test_private_request_is_refused_before_clock_synchronization() -> None:
    requester = FakeRequester([])
    rest = client(requester, clock=ServerClock())

    with pytest.raises(ClockNotSynchronizedError):
        asyncio.run(rest.get_open_orders("linear", "ETHUSDT"))

    assert requester.requests == []


def test_server_clock_is_synchronized_from_an_unsigned_demo_time_request() -> None:
    requester = FakeRequester(
        [
            success_response(
                {"timeSecond": str(NOW_MS // 1000), "timeNano": "0"}
            )
        ]
    )
    wall_times = iter((NOW_MS - 20, NOW_MS + 20))
    clock = ServerClock(
        max_absolute_offset_ms=1000,
        max_uncertainty_ms=100,
        wall_time_ms=lambda: NOW_MS,
        monotonic_seconds=lambda: 10.0,
    )
    rest = BybitPrivateRestClient(
        profile=BybitDemoProfile(credentials()),
        clock=clock,
        requester=requester,
        wall_time_ms=lambda: next(wall_times),
    )

    sample = asyncio.run(rest.synchronize_clock())

    assert sample.offset_ms == Decimal("0")
    assert sample.round_trip_time_ms == 40
    assert requester.requests[0].method == "GET"
    assert requester.requests[0].url == (
        "https://api-demo.bybit.com/v5/market/time"
    )
    assert "X-BAPI-API-KEY" not in requester.requests[0].headers


def test_server_clock_retries_a_transient_low_quality_sample() -> None:
    requester = FakeRequester(
        [
            success_response(
                {"timeSecond": str(NOW_MS // 1000), "timeNano": "0"}
            ),
            success_response(
                {"timeSecond": str(NOW_MS // 1000), "timeNano": "0"}
            ),
        ]
    )
    wall_times = iter(
        (
            NOW_MS - 300,
            NOW_MS + 300,
            NOW_MS - 20,
            NOW_MS + 20,
        )
    )
    clock = ServerClock(
        max_absolute_offset_ms=1000,
        max_uncertainty_ms=250,
        wall_time_ms=lambda: NOW_MS,
        monotonic_seconds=lambda: 10.0,
    )
    rest = BybitPrivateRestClient(
        profile=BybitDemoProfile(credentials()),
        clock=clock,
        requester=requester,
        wall_time_ms=lambda: next(wall_times),
    )

    sample = asyncio.run(rest.synchronize_clock())

    assert sample.uncertainty_ms == Decimal("20")
    assert len(requester.requests) == 2


def test_private_read_refreshes_clock_before_existing_sample_becomes_stale() -> None:
    monotonic_seconds = 10.0
    clock = ServerClock(
        max_absolute_offset_ms=1000,
        max_uncertainty_ms=100,
        max_age_seconds=30,
        wall_time_ms=lambda: NOW_MS,
        monotonic_seconds=lambda: monotonic_seconds,
    )
    clock.record_sample(
        request_sent_at_ms=NOW_MS - 10,
        response_received_at_ms=NOW_MS + 10,
        server_time_ms=NOW_MS,
    )
    monotonic_seconds = 25.0
    requester = FakeRequester(
        [
            success_response(
                {"timeSecond": str(NOW_MS // 1000), "timeNano": "0"}
            ),
            success_response(
                {"category": "linear", "list": [], "nextPageCursor": ""}
            ),
        ]
    )
    rest = BybitPrivateRestClient(
        profile=BybitDemoProfile(credentials()),
        clock=clock,
        requester=requester,
        wall_time_ms=lambda: NOW_MS,
    )

    assert asyncio.run(rest.get_open_orders("linear", "ETHUSDT")) == ()

    assert requester.requests[0].url.endswith("/v5/market/time")
    assert "/v5/order/realtime?" in requester.requests[1].url


def test_open_order_pagination_uses_the_exact_signed_query() -> None:
    requester = FakeRequester(
        [
            success_response(
                {
                    "category": "linear",
                    "list": [order_item("order-1", "link-1")],
                    "nextPageCursor": "cursor:2",
                }
            ),
            success_response(
                {
                    "category": "linear",
                    "list": [order_item("order-2", "link-2")],
                    "nextPageCursor": "",
                }
            ),
        ]
    )
    rest = client(requester)

    orders = asyncio.run(rest.get_open_orders("linear", "ETHUSDT"))

    assert [order.order_id for order in orders] == ["order-1", "order-2"]
    assert len(requester.requests) == 2
    for sent in requester.requests:
        assert sent.method == "GET"
        query = urlsplit(sent.url).query
        expected = BybitV5Signer(credentials()).sign_get(
            timestamp_ms=NOW_MS,
            query_string=query,
        )
        assert sent.headers == expected.as_http_headers()
    assert "cursor=cursor%3A2" in requester.requests[1].url


def test_account_wide_linear_reads_use_usdt_settlement_scope() -> None:
    requester = FakeRequester(
        [
            success_response(
                {
                    "category": "linear",
                    "list": [order_item("sol-stop", "")],
                    "nextPageCursor": "",
                }
            )
        ]
    )
    rest = client(requester)

    orders = asyncio.run(rest.get_open_orders("linear"))

    assert len(orders) == 1
    assert "category=linear" in requester.requests[0].url
    assert "settleCoin=USDT" in requester.requests[0].url
    assert "symbol=" not in requester.requests[0].url


def test_conditional_order_query_preserves_trigger_contract() -> None:
    item = order_item("stop-order", "ECH-01-C0001-L01-STOP-A01-9F3C")
    item.update(
        {
            "side": "Buy",
            "orderType": "Market",
            "price": "",
            "cumExecQty": "0",
            "avgPrice": "",
            "reduceOnly": True,
            "triggerPrice": "3004.5",
            "triggerBy": "LastPrice",
            "triggerDirection": 1,
            "timeInForce": "GTC",
            "positionIdx": 0,
            "orderStatus": "Untriggered",
        }
    )
    requester = FakeRequester(
        [success_response({"category": "linear", "list": [item]})]
    )
    rest = client(requester)

    orders = asyncio.run(rest.get_open_orders("linear", "ETHUSDT"))

    assert len(orders) == 1
    assert orders[0].trigger_price == Decimal("3004.5")
    assert orders[0].trigger_by == "LastPrice"
    assert orders[0].trigger_direction == 1
    assert orders[0].time_in_force == "GTC"
    assert orders[0].position_idx == 0
    assert orders[0].reduce_only


def test_order_link_lookup_falls_back_to_durable_history() -> None:
    requester = FakeRequester(
        [
            success_response(
                {
                    "category": "linear",
                    "list": [],
                    "nextPageCursor": "",
                }
            ),
            success_response(
                {
                    "category": "linear",
                    "list": [order_item("order-1", ORDER_LINK_ID, status="Filled")],
                    "nextPageCursor": "",
                }
            ),
        ]
    )
    rest = client(requester)

    order = asyncio.run(
        rest.get_order_by_link_id("linear", "ETHUSDT", ORDER_LINK_ID)
    )

    assert order is not None
    assert order.status == "Filled"
    assert [urlsplit(request.url).path for request in requester.requests] == [
        "/v5/order/realtime",
        "/v5/order/history",
    ]


def test_execution_lookup_retries_filled_order_by_exchange_id() -> None:
    option_symbol = "ETH-31JUL26-1950-P-USDT"
    requester = FakeRequester(
        [
            success_response(
                {
                    "category": "option",
                    "list": [],
                    "nextPageCursor": "",
                }
            ),
            success_response(
                {
                    "category": "option",
                    "list": [
                        {
                            **order_item(
                                "option-order-1",
                                ORDER_LINK_ID,
                                status="Filled",
                            ),
                            "category": "option",
                            "symbol": option_symbol,
                            "side": "Buy",
                            "price": "80.9",
                            "qty": "0.1",
                            "cumExecQty": "0.1",
                            "avgPrice": "80.9",
                            "reduceOnly": True,
                        }
                    ],
                    "nextPageCursor": "",
                }
            ),
            success_response(
                {
                    "category": "option",
                    "list": [
                        {
                            "symbol": option_symbol,
                            "orderId": "option-order-1",
                            "orderLinkId": ORDER_LINK_ID,
                            "side": "Buy",
                            "execId": "option-execution-1",
                            "execPrice": "80.9",
                            "execQty": "0.1",
                            "execFee": "0.05",
                            "isMaker": False,
                            "execTime": "1658385579410",
                        }
                    ],
                    "nextPageCursor": "",
                }
            ),
        ]
    )

    executions = asyncio.run(
        client(requester).get_execution_history(
            "option",
            option_symbol,
            ORDER_LINK_ID,
        )
    )

    assert executions[0].execution_id == "option-execution-1"
    assert len(requester.requests) == 3
    assert "orderLinkId=" in urlsplit(requester.requests[0].url).query
    assert "orderId=option-order-1" in urlsplit(requester.requests[2].url).query


def test_read_only_account_and_execution_responses_are_normalized() -> None:
    requester = FakeRequester(
        [
            success_response(
                {
                    "category": "linear",
                    "list": [
                        {
                            "symbol": "ETHUSDT",
                            "orderId": "order-1",
                            "orderLinkId": "link-1",
                            "side": "Sell",
                            "execId": "execution-1",
                            "execPrice": "3000.2",
                            "execQty": "0.004",
                            "execFee": "0.0012",
                            "isMaker": False,
                            "execTime": "1658385579410",
                        }
                    ],
                    "nextPageCursor": "",
                }
            ),
            success_response(
                {
                    "category": "linear",
                    "list": [
                        {
                            "symbol": "ETHUSDT",
                            "side": "Sell",
                            "size": "0.004",
                            "avgPrice": "3000.2",
                            "markPrice": "2999.8",
                            "liqPrice": "3500.5",
                            "unrealisedPnl": "0.0016",
                            "updatedTime": "",
                            "positionIdx": 0,
                        }
                    ],
                    "nextPageCursor": "",
                }
            ),
            success_response(
                {
                    "list": [
                        {
                            "accountType": "UNIFIED",
                            "totalEquity": "1010.5",
                            "totalWalletBalance": "1000",
                            "totalAvailableBalance": "900.25",
                            "coin": [
                                {
                                    "coin": "USDT",
                                    "equity": "1010.5",
                                    "walletBalance": "1000",
                                    "unrealisedPnl": "10.5",
                                }
                            ],
                        }
                    ]
                }
            ),
        ]
    )
    rest = client(requester)

    executions = asyncio.run(
        rest.get_execution_history("linear", symbol="ETHUSDT")
    )
    positions = asyncio.run(rest.get_positions("linear", "ETHUSDT"))
    wallet = asyncio.run(rest.get_wallet_state())

    assert executions[0].execution_id == "execution-1"
    assert executions[0].quantity == Decimal("0.004")
    assert positions[0].side == "Sell"
    assert positions[0].quantity == Decimal("0.004")
    assert positions[0].position_idx == 0
    assert positions[0].liquidation_price == Decimal("3500.5")
    assert positions[0].updated_at == datetime.fromtimestamp(
        NOW_MS / 1000,
        tz=timezone.utc,
    )
    assert wallet.account_type == "UNIFIED"
    assert wallet.total_available_balance == Decimal("900.25")
    assert wallet.balances[0].coin == "USDT"


def test_wallet_allows_unavailable_account_wide_balance() -> None:
    requester = FakeRequester(
        [
            success_response(
                {
                    "list": [
                        {
                            "accountType": "UNIFIED",
                            "totalEquity": "1000",
                            "totalWalletBalance": "1000",
                            "totalAvailableBalance": "",
                            "coin": [],
                        }
                    ]
                }
            )
        ]
    )

    wallet = asyncio.run(client(requester).get_wallet_state())

    assert wallet.total_available_balance is None
