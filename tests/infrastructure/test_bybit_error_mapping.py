"""Typed Bybit private-response error mapping tests."""

import pytest

from eth_credit_hedge.infrastructure.bybit.error_mapping import (
    BybitApiError,
    BybitAuthenticationError,
    BybitDuplicateOrderLinkIdError,
    BybitInsufficientMarginError,
    BybitOrderRejectedError,
    BybitRateLimitError,
    BybitRequestTimeError,
    BybitUnknownOrderError,
    map_bybit_error,
    raise_for_bybit_error,
)


@pytest.mark.parametrize("ret_code", [10003, 10004, 10005, 10007, 33004])
def test_authentication_codes_map_to_typed_error(ret_code: int) -> None:
    error = map_bybit_error(ret_code, "authentication failed")

    assert isinstance(error, BybitAuthenticationError)
    assert error.ret_code == ret_code


@pytest.mark.parametrize("ret_code", [-1, 10002])
def test_clock_codes_map_to_typed_error(ret_code: int) -> None:
    assert isinstance(
        map_bybit_error(ret_code, "request expired"),
        BybitRequestTimeError,
    )


@pytest.mark.parametrize("ret_code", [429, 10006, 10429, 20003])
def test_rate_limit_codes_map_to_typed_error(ret_code: int) -> None:
    assert isinstance(
        map_bybit_error(ret_code, "too many requests"),
        BybitRateLimitError,
    )


@pytest.mark.parametrize(
    "ret_code",
    [110004, 110006, 110007, 110012, 110044, 110045, 170131],
)
def test_insufficient_margin_codes_map_to_typed_error(ret_code: int) -> None:
    assert isinstance(
        map_bybit_error(ret_code, "available balance is insufficient"),
        BybitInsufficientMarginError,
    )


@pytest.mark.parametrize("ret_code", [110072, 170141])
def test_duplicate_client_id_codes_map_to_typed_error(ret_code: int) -> None:
    assert isinstance(
        map_bybit_error(ret_code, "order link id is duplicate"),
        BybitDuplicateOrderLinkIdError,
    )


@pytest.mark.parametrize("ret_code", [110001, 170143])
def test_unknown_order_codes_map_to_typed_error(ret_code: int) -> None:
    assert isinstance(
        map_bybit_error(ret_code, "order does not exist"),
        BybitUnknownOrderError,
    )


@pytest.mark.parametrize("ret_code", [10001, 110003, 110017, 110032, 170136])
def test_rejection_codes_map_to_typed_error(ret_code: int) -> None:
    assert isinstance(
        map_bybit_error(ret_code, "order rejected"),
        BybitOrderRejectedError,
    )


def test_unknown_code_remains_typed_base_error() -> None:
    error = map_bybit_error(999999, "new exchange error")

    assert type(error) is BybitApiError
    assert str(error) == "Bybit error 999999: new exchange error"


def test_success_does_not_raise_and_failure_raises_mapped_type() -> None:
    raise_for_bybit_error(0, "OK")

    with pytest.raises(BybitDuplicateOrderLinkIdError):
        raise_for_bybit_error(110072, "duplicate")
