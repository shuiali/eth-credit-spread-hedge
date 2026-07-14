"""Typed errors for Bybit V5 private REST and WebSocket responses."""

from __future__ import annotations


class BybitApiError(RuntimeError):
    """Base non-success response from Bybit."""

    def __init__(self, ret_code: int, ret_msg: str) -> None:
        self.ret_code = ret_code
        self.ret_msg = ret_msg
        super().__init__(f"Bybit error {ret_code}: {ret_msg}")


class BybitAuthenticationError(BybitApiError):
    """Credentials, signature, permissions, or environment binding failed."""


class BybitRequestTimeError(BybitApiError):
    """The signed timestamp is outside Bybit's accepted time window."""


class BybitRateLimitError(BybitApiError):
    """The exchange rejected a request due to a rate limit."""


class BybitOrderRejectedError(BybitApiError):
    """An order request violated exchange order rules."""


class BybitInsufficientMarginError(BybitOrderRejectedError):
    """Available balance or margin cannot support the order."""


class BybitDuplicateOrderLinkIdError(BybitOrderRejectedError):
    """The order-link ID was already used."""


class BybitUnknownOrderError(BybitOrderRejectedError):
    """No exchange order matched the supplied identifier."""


_AUTHENTICATION_CODES = frozenset(
    {
        -2015,
        33004,
        10003,
        10004,
        10005,
        10007,
        10008,
        10009,
        10010,
    }
)
_REQUEST_TIME_CODES = frozenset({-1, 10002})
_RATE_LIMIT_CODES = frozenset({429, 10006, 10429, 20003})
_INSUFFICIENT_MARGIN_CODES = frozenset(
    {
        110004,
        110006,
        110007,
        110012,
        110014,
        110044,
        110045,
        110051,
        110052,
        110053,
        170033,
        170131,
    }
)
_DUPLICATE_ORDER_LINK_ID_CODES = frozenset({110072, 170141})
_UNKNOWN_ORDER_CODES = frozenset({110001, 170143})
_ORDER_REJECTION_CODES = frozenset(
    {
        10001,
        30208,
        30209,
        110003,
        110009,
        110017,
        110020,
        110021,
        110022,
        110023,
        110032,
        110040,
        110057,
        110058,
        110064,
        110066,
        110074,
        110079,
        110118,
        110119,
        110120,
        110121,
        170105,
        170115,
        170116,
        170117,
        170121,
        170124,
        170130,
        170132,
        170133,
        170134,
        170136,
        170137,
        170140,
        170149,
    }
)


def map_bybit_error(ret_code: int, ret_msg: str) -> BybitApiError:
    """Map one non-zero response code to its most specific error type."""
    error_type: type[BybitApiError]
    if ret_code in _AUTHENTICATION_CODES:
        error_type = BybitAuthenticationError
    elif ret_code in _REQUEST_TIME_CODES:
        error_type = BybitRequestTimeError
    elif ret_code in _RATE_LIMIT_CODES:
        error_type = BybitRateLimitError
    elif ret_code in _INSUFFICIENT_MARGIN_CODES:
        error_type = BybitInsufficientMarginError
    elif ret_code in _DUPLICATE_ORDER_LINK_ID_CODES:
        error_type = BybitDuplicateOrderLinkIdError
    elif ret_code in _UNKNOWN_ORDER_CODES:
        error_type = BybitUnknownOrderError
    elif ret_code in _ORDER_REJECTION_CODES:
        error_type = BybitOrderRejectedError
    else:
        error_type = BybitApiError
    return error_type(ret_code, ret_msg)


def raise_for_bybit_error(ret_code: int, ret_msg: str) -> None:
    """Raise a typed exception for a non-success Bybit response code."""
    if ret_code != 0:
        raise map_bybit_error(ret_code, ret_msg)
