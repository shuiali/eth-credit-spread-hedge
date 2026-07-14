"""Exchange-neutral option contract, market quote, and fill models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Literal


OptionType = Literal["Put", "Call"]
OrderSide = Literal["Buy", "Sell"]
InstrumentCategory = Literal["option", "linear"]


def _decimal(value: Decimal, field_name: str) -> Decimal:
    try:
        normalized = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite Decimal") from exc
    if not normalized.is_finite():
        raise ValueError(f"{field_name} must be a finite Decimal")
    return normalized


def _optional_decimal(value: Decimal | None, field_name: str) -> Decimal | None:
    if value is None:
        return None
    return _decimal(value, field_name)


def _utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _require_text(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} cannot be empty")


@dataclass(frozen=True, slots=True)
class OptionContract:
    symbol: str
    base_coin: str
    quote_coin: str
    settle_coin: str
    option_type: OptionType
    strike: Decimal
    expiry_time_utc: datetime
    contract_multiplier: Decimal

    def __post_init__(self) -> None:
        for field_name in ("symbol", "base_coin", "quote_coin", "settle_coin"):
            _require_text(getattr(self, field_name), field_name)
        if self.option_type not in ("Put", "Call"):
            raise ValueError("option type must be Put or Call")

        strike = _decimal(self.strike, "strike")
        multiplier = _decimal(self.contract_multiplier, "contract multiplier")
        if strike <= 0:
            raise ValueError("strike must be positive")
        if multiplier <= 0:
            raise ValueError("contract multiplier must be positive")

        object.__setattr__(self, "strike", strike)
        object.__setattr__(self, "contract_multiplier", multiplier)
        object.__setattr__(
            self,
            "expiry_time_utc",
            _utc(self.expiry_time_utc, "expiry time"),
        )


@dataclass(frozen=True, slots=True)
class OptionMarketQuote:
    symbol: str
    timestamp_utc: datetime
    bid_price: Decimal | None
    bid_size: Decimal | None
    ask_price: Decimal | None
    ask_size: Decimal | None
    mark_price: Decimal
    underlying_price: Decimal
    index_price: Decimal
    bid_iv: Decimal | None
    ask_iv: Decimal | None
    mark_iv: Decimal | None
    delta: Decimal | None
    gamma: Decimal | None
    vega: Decimal | None
    theta: Decimal | None

    def __post_init__(self) -> None:
        _require_text(self.symbol, "symbol")
        object.__setattr__(
            self,
            "timestamp_utc",
            _utc(self.timestamp_utc, "quote timestamp"),
        )

        for field_name in (
            "bid_price",
            "bid_size",
            "ask_price",
            "ask_size",
            "bid_iv",
            "ask_iv",
            "mark_iv",
            "delta",
            "gamma",
            "vega",
            "theta",
        ):
            object.__setattr__(
                self,
                field_name,
                _optional_decimal(getattr(self, field_name), field_name),
            )
        for field_name in ("mark_price", "underlying_price", "index_price"):
            object.__setattr__(
                self,
                field_name,
                _decimal(getattr(self, field_name), field_name),
            )

        if self.mark_price < 0:
            raise ValueError("mark price cannot be negative")
        if self.underlying_price <= 0 or self.index_price <= 0:
            raise ValueError("underlying and index prices must be positive")
        for field_name in ("bid_price", "ask_price", "bid_size", "ask_size"):
            value = getattr(self, field_name)
            if value is not None and value < 0:
                raise ValueError(f"{field_name} cannot be negative")
        for field_name in ("bid_iv", "ask_iv", "mark_iv"):
            value = getattr(self, field_name)
            if value is not None and value < 0:
                raise ValueError(f"{field_name} cannot be negative")
        if (
            self.bid_price is not None
            and self.ask_price is not None
            and self.bid_price > self.ask_price
        ):
            raise ValueError("bid cannot exceed ask")


@dataclass(frozen=True, slots=True)
class OptionFill:
    order_id: str
    execution_id: str
    symbol: str
    side: OrderSide
    price: Decimal
    quantity: Decimal
    fee: Decimal
    timestamp_utc: datetime

    def __post_init__(self) -> None:
        for field_name in ("order_id", "execution_id", "symbol"):
            _require_text(getattr(self, field_name), field_name)
        if self.side not in ("Buy", "Sell"):
            raise ValueError("fill side must be Buy or Sell")

        price = _decimal(self.price, "fill price")
        quantity = _decimal(self.quantity, "fill quantity")
        fee = _decimal(self.fee, "fee")
        if price < 0:
            raise ValueError("fill price cannot be negative")
        if quantity <= 0:
            raise ValueError("fill quantity must be positive")
        if fee < 0:
            raise ValueError("fee cannot be negative")

        object.__setattr__(self, "price", price)
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "fee", fee)
        object.__setattr__(
            self,
            "timestamp_utc",
            _utc(self.timestamp_utc, "fill timestamp"),
        )


@dataclass(frozen=True, slots=True)
class PriceFilter:
    tick_size: Decimal
    min_price: Decimal | None
    max_price: Decimal | None

    def __post_init__(self) -> None:
        tick_size = _decimal(self.tick_size, "tick size")
        min_price = _optional_decimal(self.min_price, "minimum price")
        max_price = _optional_decimal(self.max_price, "maximum price")
        if tick_size <= 0:
            raise ValueError("tick size must be positive")
        if min_price is not None and min_price < 0:
            raise ValueError("minimum price cannot be negative")
        if max_price is not None and max_price <= 0:
            raise ValueError("maximum price must be positive")
        if (
            min_price is not None
            and max_price is not None
            and min_price > max_price
        ):
            raise ValueError("minimum price cannot exceed maximum price")
        object.__setattr__(self, "tick_size", tick_size)
        object.__setattr__(self, "min_price", min_price)
        object.__setattr__(self, "max_price", max_price)


@dataclass(frozen=True, slots=True)
class LotSizeFilter:
    qty_step: Decimal
    min_order_qty: Decimal
    max_order_qty: Decimal
    max_market_order_qty: Decimal | None
    min_notional: Decimal | None

    def __post_init__(self) -> None:
        qty_step = _decimal(self.qty_step, "quantity step")
        min_order_qty = _decimal(self.min_order_qty, "minimum order quantity")
        max_order_qty = _decimal(self.max_order_qty, "maximum order quantity")
        max_market_order_qty = _optional_decimal(
            self.max_market_order_qty,
            "maximum market order quantity",
        )
        min_notional = _optional_decimal(self.min_notional, "minimum notional")
        if qty_step <= 0:
            raise ValueError("quantity step must be positive")
        if min_order_qty <= 0:
            raise ValueError("minimum order quantity must be positive")
        if max_order_qty < min_order_qty:
            raise ValueError("maximum order quantity cannot be below minimum")
        if max_market_order_qty is not None and max_market_order_qty <= 0:
            raise ValueError("maximum market order quantity must be positive")
        if min_notional is not None and min_notional <= 0:
            raise ValueError("minimum notional must be positive")
        object.__setattr__(self, "qty_step", qty_step)
        object.__setattr__(self, "min_order_qty", min_order_qty)
        object.__setattr__(self, "max_order_qty", max_order_qty)
        object.__setattr__(
            self,
            "max_market_order_qty",
            max_market_order_qty,
        )
        object.__setattr__(self, "min_notional", min_notional)


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    symbol: str
    category: InstrumentCategory
    status: str
    base_coin: str
    quote_coin: str
    settle_coin: str
    price_filter: PriceFilter
    lot_size_filter: LotSizeFilter
    contract_multiplier: Decimal
    delivery_time_utc: datetime | None

    def __post_init__(self) -> None:
        for field_name in (
            "symbol",
            "status",
            "base_coin",
            "quote_coin",
            "settle_coin",
        ):
            _require_text(getattr(self, field_name), field_name)
        if self.category not in ("option", "linear"):
            raise ValueError("instrument category must be option or linear")
        multiplier = _decimal(self.contract_multiplier, "contract multiplier")
        if multiplier <= 0:
            raise ValueError("contract multiplier must be positive")
        object.__setattr__(self, "contract_multiplier", multiplier)
        if self.delivery_time_utc is not None:
            object.__setattr__(
                self,
                "delivery_time_utc",
                _utc(self.delivery_time_utc, "delivery time"),
            )
