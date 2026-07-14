"""Fixture-first Bybit V5 ETH option-chain parser and public client."""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from eth_credit_hedge.core.credit_spread import (
    CreditSpread,
    DecimalLike,
    ZERO,
    to_decimal,
)
from eth_credit_hedge.domain.instruments import (
    OptionContract,
    OptionMarketQuote,
    OptionType,
)


SYMBOL_PATTERN = re.compile(
    r"^(?P<base>[A-Z]+)-(?P<expiry>\d{1,2}[A-Z]{3}\d{2})-"
    r"(?P<strike>\d+(?:\.\d+)?)-(?P<option_type>[CP])(?:-(?P<settle>[A-Z]+))?$"
)
MONTHS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}


@dataclass(frozen=True, slots=True)
class OptionChainEntry:
    """Normalized contract and quote pair returned by the public adapter."""

    contract: OptionContract
    quote: OptionMarketQuote
    status: str

    def __post_init__(self) -> None:
        if self.contract.symbol != self.quote.symbol:
            raise ValueError("contract and quote symbols differ")
        if not self.status.strip():
            raise ValueError("instrument status cannot be empty")

    @property
    def symbol(self) -> str:
        return self.contract.symbol


@dataclass(frozen=True, slots=True)
class _TimedTicker:
    item: dict[str, Any]
    timestamp_utc: datetime


@dataclass(frozen=True, slots=True)
class QuotedCreditSpread:
    short_put: OptionChainEntry
    long_put: OptionChainEntry
    option_quantity: Decimal
    mark_credit: Decimal
    natural_credit: Decimal
    mark_credit_calculation: str
    natural_credit_calculation: str
    spread: CreditSpread


def load_option_fixture(path: str | Path) -> dict[str, Any]:
    return _load_json_object(Path(path).read_text(encoding="utf-8"))


def parse_option_fixture(fixture: dict[str, Any]) -> tuple[OptionChainEntry, ...]:
    """Parse saved raw responses without contacting Bybit."""
    instruments: dict[str, dict[str, Any]] = {}
    tickers: dict[str, _TimedTicker] = {}
    for request in fixture.get("requests", []):
        response = request.get("response", {})
        _validate_response(response)
        for item in response["result"].get("list", []):
            if "optionsType" in item:
                instruments[item["symbol"]] = item
            elif "markPrice" in item:
                tickers[item["symbol"]] = _TimedTicker(
                    item=item,
                    timestamp_utc=_response_timestamp(response),
                )
    return _join_quotes(instruments, tickers)


def build_credit_spread_from_quotes(
    short_put: OptionChainEntry,
    long_put: OptionChainEntry,
    option_quantity: DecimalLike,
    index_price_relative_tolerance: DecimalLike = "0.001",
) -> QuotedCreditSpread:
    """Build the validated engine input using mark-to-mark premium only."""
    if (
        short_put.contract.option_type != "Put"
        or long_put.contract.option_type != "Put"
    ):
        raise ValueError("both selected instruments must be puts")
    if (
        short_put.contract.base_coin != "ETH"
        or long_put.contract.base_coin != "ETH"
    ):
        raise ValueError("both selected instruments must be ETH options")
    if short_put.contract.expiry_time_utc != long_put.contract.expiry_time_utc:
        raise ValueError("put legs must have the same expiry")
    if short_put.contract.strike <= long_put.contract.strike:
        raise ValueError("short put strike must be above long put strike")
    tolerance = to_decimal(index_price_relative_tolerance)
    if tolerance < ZERO:
        raise ValueError("index price tolerance cannot be negative")
    short_quote = short_put.quote
    long_quote = long_put.quote
    index_difference = abs(short_quote.index_price - long_quote.index_price)
    index_reference = max(short_quote.index_price, long_quote.index_price)
    if index_reference <= ZERO:
        raise ValueError("ETH index price must be positive")
    if index_difference / index_reference > tolerance:
        raise ValueError("selected quote index prices differ beyond tolerance")
    reference_price = (short_quote.index_price + long_quote.index_price) / Decimal(
        "2"
    )

    quantity = to_decimal(option_quantity)
    mark_credit = short_quote.mark_price - long_quote.mark_price
    if short_quote.bid_price is None or long_quote.ask_price is None:
        raise ValueError("selected quotes require short bid and long ask prices")
    natural_credit = short_quote.bid_price - long_quote.ask_price
    if mark_credit <= ZERO:
        raise ValueError("selected mark prices do not form a positive credit")
    spread = CreditSpread(
        spot=reference_price,
        short_put_strike=short_put.contract.strike,
        long_put_strike=long_put.contract.strike,
        option_quantity=quantity,
        premium_credit=mark_credit * quantity,
    )
    return QuotedCreditSpread(
        short_put=short_put,
        long_put=long_put,
        option_quantity=quantity,
        mark_credit=mark_credit * quantity,
        natural_credit=natural_credit * quantity,
        mark_credit_calculation=(
            f"({short_quote.mark_price} - {long_quote.mark_price}) × {quantity} = "
            f"{mark_credit * quantity}"
        ),
        natural_credit_calculation=(
            f"({short_quote.bid_price} - {long_quote.ask_price}) × {quantity} = "
            f"{natural_credit * quantity}"
        ),
        spread=spread,
    )


def select_put_credit_spread(
    chain: tuple[OptionChainEntry, ...] | list[OptionChainEntry],
    short_symbol: str,
    long_symbol: str,
    option_quantity: DecimalLike,
) -> QuotedCreditSpread:
    """Select exactly the two user-named symbols; no automatic ranking."""
    by_symbol = {quote.symbol: quote for quote in chain}
    missing = [
        symbol for symbol in (short_symbol, long_symbol) if symbol not in by_symbol
    ]
    if missing:
        raise ValueError(f"selected symbol not found: {', '.join(missing)}")
    return build_credit_spread_from_quotes(
        by_symbol[short_symbol], by_symbol[long_symbol], option_quantity
    )


class BybitOptionClient:
    """Minimal unauthenticated client for the public ETH option chain."""

    def __init__(
        self,
        base_url: str = "https://api.bybit.com",
        timeout_seconds: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def fetch_eth_chain(self) -> tuple[OptionChainEntry, ...]:
        instruments: dict[str, dict[str, Any]] = {}
        cursor = ""
        while True:
            params: dict[str, str | int] = {
                "category": "option",
                "baseCoin": "ETH",
                "status": "Trading",
                "limit": 1000,
            }
            if cursor:
                params["cursor"] = cursor
            response = self._get("/v5/market/instruments-info", params)
            _validate_response(response)
            for item in response["result"]["list"]:
                instruments[item["symbol"]] = item
            cursor = response["result"].get("nextPageCursor", "")
            if not cursor:
                break

        ticker_response = self._get(
            "/v5/market/tickers", {"category": "option", "baseCoin": "ETH"}
        )
        _validate_response(ticker_response)
        ticker_timestamp = _response_timestamp(ticker_response)
        tickers = {
            item["symbol"]: _TimedTicker(item=item, timestamp_utc=ticker_timestamp)
            for item in ticker_response["result"]["list"]
        }
        return _join_quotes(instruments, tickers)

    def _get(self, endpoint: str, params: dict[str, str | int]) -> dict[str, Any]:
        url = f"{self.base_url}{endpoint}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(
            url, headers={"User-Agent": "eth-credit-spread-hedge/0.1"}
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            return _load_json_object(response.read().decode("utf-8"))


def _load_json_object(payload: str) -> dict[str, Any]:
    parsed: object = json.loads(payload)
    if not isinstance(parsed, dict):
        raise ValueError("expected a JSON object")
    return cast(dict[str, Any], parsed)


def _join_quotes(
    instruments: dict[str, dict[str, Any]],
    tickers: dict[str, _TimedTicker],
) -> tuple[OptionChainEntry, ...]:
    quotes: list[OptionChainEntry] = []
    for symbol, instrument_item in instruments.items():
        timed_ticker = tickers.get(symbol)
        if timed_ticker is None:
            continue
        contract = _parse_contract(instrument_item)
        if contract.base_coin != "ETH" or contract.option_type != "Put":
            continue
        quote = _parse_quote(contract, timed_ticker)
        quotes.append(
            OptionChainEntry(
                contract=contract,
                quote=quote,
                status=str(instrument_item["status"]),
            )
        )
    return tuple(
        sorted(
            quotes,
            key=lambda entry: (
                entry.contract.expiry_time_utc,
                entry.contract.strike,
            ),
        )
    )


def _parse_contract(item: dict[str, Any]) -> OptionContract:
    symbol = str(item["symbol"])
    parsed = _parse_symbol(symbol)
    reported_type = str(item["optionsType"])
    expected_type: OptionType = "Put" if parsed[3] == "P" else "Call"
    if reported_type != expected_type:
        raise ValueError(f"option type mismatch for {symbol}")
    delivery_time_ms = int(item["deliveryTime"])
    delivery_time = datetime.fromtimestamp(delivery_time_ms / 1000, tz=timezone.utc)
    if delivery_time.date() != parsed[1]:
        raise ValueError(f"expiry mismatch for {symbol}")
    if str(item["baseCoin"]) != parsed[0]:
        raise ValueError(f"base coin mismatch for {symbol}")
    if parsed[4] is not None and str(item["settleCoin"]) != parsed[4]:
        raise ValueError(f"settle coin mismatch for {symbol}")
    return OptionContract(
        symbol=symbol,
        base_coin=str(item["baseCoin"]),
        quote_coin=str(item["quoteCoin"]),
        settle_coin=str(item["settleCoin"]),
        option_type=expected_type,
        strike=parsed[2],
        expiry_time_utc=delivery_time,
        contract_multiplier=Decimal("1"),
    )


def _parse_quote(
    contract: OptionContract,
    timed_ticker: _TimedTicker,
) -> OptionMarketQuote:
    item = timed_ticker.item
    if str(item["symbol"]) != contract.symbol:
        raise ValueError("ticker and instrument symbols differ")
    bid = _optional_decimal(item, "bid1Price")
    ask = _optional_decimal(item, "ask1Price")
    mark = to_decimal(item["markPrice"])
    if mark < ZERO:
        raise ValueError(f"negative quote for {contract.symbol}")
    return OptionMarketQuote(
        symbol=contract.symbol,
        timestamp_utc=timed_ticker.timestamp_utc,
        bid_price=bid,
        bid_size=_optional_decimal(item, "bid1Size"),
        ask_price=ask,
        ask_size=_optional_decimal(item, "ask1Size"),
        mark_price=mark,
        index_price=to_decimal(item["indexPrice"]),
        underlying_price=to_decimal(item["underlyingPrice"]),
        bid_iv=_optional_decimal(item, "bid1Iv"),
        ask_iv=_optional_decimal(item, "ask1Iv"),
        mark_iv=_optional_decimal(item, "markIv"),
        delta=_optional_decimal(item, "delta"),
        gamma=_optional_decimal(item, "gamma"),
        vega=_optional_decimal(item, "vega"),
        theta=_optional_decimal(item, "theta"),
    )


def _response_timestamp(response: dict[str, Any]) -> datetime:
    value = response.get("time")
    if value is None:
        raise ValueError("Bybit response is missing server timestamp")
    return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)


def _optional_decimal(item: dict[str, Any], key: str) -> Decimal | None:
    value = item.get(key)
    if value in (None, ""):
        return None
    return to_decimal(value)


def _parse_symbol(symbol: str) -> tuple[str, date, Decimal, str, str | None]:
    match = SYMBOL_PATTERN.fullmatch(symbol)
    if match is None:
        raise ValueError(f"unsupported Bybit option symbol: {symbol}")
    expiry_text = match.group("expiry")
    expiry_match = re.fullmatch(r"(\d{1,2})([A-Z]{3})(\d{2})", expiry_text)
    if expiry_match is None:
        raise ValueError(f"unsupported Bybit option expiry: {expiry_text}")
    day = int(expiry_match.group(1))
    month = MONTHS[expiry_match.group(2)]
    year = 2000 + int(expiry_match.group(3))
    return (
        match.group("base"),
        date(year, month, day),
        Decimal(match.group("strike")),
        match.group("option_type"),
        match.group("settle"),
    )


def _validate_response(response: dict[str, Any]) -> None:
    if response.get("retCode") != 0:
        raise ValueError(
            f"Bybit response error {response.get('retCode')}: {response.get('retMsg')}"
        )
    result = response.get("result")
    if not isinstance(result, dict) or result.get("category") != "option":
        raise ValueError("response is not an option-market payload")
