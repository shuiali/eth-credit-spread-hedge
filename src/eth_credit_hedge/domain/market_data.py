"""Exchange-neutral public-market events, local book, and health gate."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Literal

from eth_credit_hedge.domain.instruments import InstrumentSpec


ZERO = Decimal("0")
BookLevel = tuple[Decimal, Decimal]
TradeSide = Literal["Buy", "Sell"]


def _decimal(value: Decimal, field_name: str) -> Decimal:
    try:
        normalized = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be finite") from exc
    if not normalized.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return normalized


def _optional_decimal(value: Decimal | None, field_name: str) -> Decimal | None:
    return None if value is None else _decimal(value, field_name)


def _utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _require_text(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} cannot be empty")


class MarketDataEventType(str, Enum):
    TICKER_UPDATED = "TickerUpdated"
    TRADE_OBSERVED = "TradeObserved"
    ORDER_BOOK_SNAPSHOT_APPLIED = "OrderBookSnapshotApplied"
    ORDER_BOOK_DELTA_APPLIED = "OrderBookDeltaApplied"
    MARKET_DATA_STALE = "MarketDataStale"
    MARKET_DATA_RECOVERED = "MarketDataRecovered"
    INSTRUMENT_CHANGED = "InstrumentChanged"
    INSTRUMENT_DISABLED = "InstrumentDisabled"


class TriggerPriceSource(str, Enum):
    LAST_TRADE = "LAST_TRADE"
    MARK_PRICE = "MARK_PRICE"
    INDEX_PRICE = "INDEX_PRICE"


DEFAULT_TRIGGER_PRICE_SOURCE = TriggerPriceSource.LAST_TRADE
DEFAULT_TRIGGER_SYMBOL = "ETHUSDT"


@dataclass(frozen=True, slots=True)
class TickerEvent:
    symbol: str
    timestamp_utc: datetime
    last_price: Decimal | None
    mark_price: Decimal | None
    index_price: Decimal | None
    bid_price: Decimal | None
    ask_price: Decimal | None
    sequence: int | None
    connection_generation: int
    raw_payload_hash: str
    bid_size: Decimal | None = None
    ask_size: Decimal | None = None
    bid_iv: Decimal | None = None
    ask_iv: Decimal | None = None
    mark_iv: Decimal | None = None
    underlying_price: Decimal | None = None
    delta: Decimal | None = None
    gamma: Decimal | None = None
    vega: Decimal | None = None
    theta: Decimal | None = None

    def __post_init__(self) -> None:
        _require_text(self.symbol, "symbol")
        object.__setattr__(self, "timestamp_utc", _utc(self.timestamp_utc, "time"))
        for field_name in (
            "last_price",
            "mark_price",
            "index_price",
            "bid_price",
            "ask_price",
            "bid_size",
            "ask_size",
            "bid_iv",
            "ask_iv",
            "mark_iv",
            "underlying_price",
            "delta",
            "gamma",
            "vega",
            "theta",
        ):
            value = _optional_decimal(getattr(self, field_name), field_name)
            if value is not None and field_name not in (
                "delta",
                "gamma",
                "vega",
                "theta",
            ) and value < ZERO:
                raise ValueError(f"{field_name} cannot be negative")
            object.__setattr__(self, field_name, value)
        if self.underlying_price is not None and self.underlying_price <= ZERO:
            raise ValueError("underlying price must be positive")
        if (
            self.bid_price is not None
            and self.ask_price is not None
            and self.bid_price > self.ask_price
        ):
            raise ValueError("ticker bid cannot exceed ask")
        if self.connection_generation < 0:
            raise ValueError("connection generation cannot be negative")
        _require_text(self.raw_payload_hash, "raw payload hash")

    @property
    def event_type(self) -> MarketDataEventType:
        return MarketDataEventType.TICKER_UPDATED


@dataclass(frozen=True, slots=True)
class TradeEvent:
    symbol: str
    timestamp_utc: datetime
    price: Decimal
    size: Decimal
    side: TradeSide
    trade_id: str
    sequence: int | None
    connection_generation: int
    raw_payload_hash: str

    def __post_init__(self) -> None:
        _require_text(self.symbol, "symbol")
        _require_text(self.trade_id, "trade ID")
        if self.side not in ("Buy", "Sell"):
            raise ValueError("trade side must be Buy or Sell")
        price = _decimal(self.price, "trade price")
        size = _decimal(self.size, "trade size")
        if price <= ZERO or size <= ZERO:
            raise ValueError("trade price and size must be positive")
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "size", size)
        object.__setattr__(
            self,
            "timestamp_utc",
            _utc(self.timestamp_utc, "trade time"),
        )
        if self.connection_generation < 0:
            raise ValueError("connection generation cannot be negative")
        _require_text(self.raw_payload_hash, "raw payload hash")

    @property
    def event_type(self) -> MarketDataEventType:
        return MarketDataEventType.TRADE_OBSERVED


def _normalize_levels(levels: tuple[BookLevel, ...]) -> tuple[BookLevel, ...]:
    normalized: list[BookLevel] = []
    for price, size in levels:
        normalized_price = _decimal(price, "book price")
        normalized_size = _decimal(size, "book size")
        if normalized_price <= ZERO or normalized_size < ZERO:
            raise ValueError("book price must be positive and size non-negative")
        normalized.append((normalized_price, normalized_size))
    return tuple(normalized)


@dataclass(frozen=True, slots=True)
class OrderBookSnapshot:
    symbol: str
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    update_id: int
    sequence: int
    timestamp_utc: datetime
    connection_generation: int
    raw_payload_hash: str

    def __post_init__(self) -> None:
        _require_text(self.symbol, "symbol")
        if self.update_id < 0 or self.sequence < 0:
            raise ValueError("book update ID and sequence cannot be negative")
        if self.connection_generation < 0:
            raise ValueError("connection generation cannot be negative")
        object.__setattr__(self, "bids", _normalize_levels(self.bids))
        object.__setattr__(self, "asks", _normalize_levels(self.asks))
        object.__setattr__(
            self,
            "timestamp_utc",
            _utc(self.timestamp_utc, "book time"),
        )
        _require_text(self.raw_payload_hash, "raw payload hash")

    @property
    def event_type(self) -> MarketDataEventType:
        return MarketDataEventType.ORDER_BOOK_SNAPSHOT_APPLIED


@dataclass(frozen=True, slots=True)
class OrderBookDelta:
    symbol: str
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    update_id: int
    sequence: int
    timestamp_utc: datetime
    connection_generation: int
    raw_payload_hash: str

    def __post_init__(self) -> None:
        _require_text(self.symbol, "symbol")
        if self.update_id < 0 or self.sequence < 0:
            raise ValueError("book update ID and sequence cannot be negative")
        if self.connection_generation < 0:
            raise ValueError("connection generation cannot be negative")
        object.__setattr__(self, "bids", _normalize_levels(self.bids))
        object.__setattr__(self, "asks", _normalize_levels(self.asks))
        object.__setattr__(
            self,
            "timestamp_utc",
            _utc(self.timestamp_utc, "book time"),
        )
        _require_text(self.raw_payload_hash, "raw payload hash")

    @property
    def event_type(self) -> MarketDataEventType:
        return MarketDataEventType.ORDER_BOOK_DELTA_APPLIED


OrderBookEvent = OrderBookSnapshot | OrderBookDelta


class LocalOrderBook:
    def __init__(self, symbol: str) -> None:
        _require_text(symbol, "symbol")
        self.symbol = symbol
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}
        self.update_id: int | None = None
        self.sequence: int | None = None
        self.timestamp_utc: datetime | None = None
        self.connection_generation = 0
        self.synchronized = False

    @property
    def best_bid(self) -> BookLevel | None:
        if not self.bids:
            return None
        price = max(self.bids)
        return price, self.bids[price]

    @property
    def best_ask(self) -> BookLevel | None:
        if not self.asks:
            return None
        price = min(self.asks)
        return price, self.asks[price]

    def apply_snapshot(self, event: OrderBookSnapshot) -> bool:
        self._validate_symbol(event.symbol)
        if event.connection_generation < self.connection_generation:
            return False
        self.bids = {price: size for price, size in event.bids if size > ZERO}
        self.asks = {price: size for price, size in event.asks if size > ZERO}
        self.update_id = event.update_id
        self.sequence = event.sequence
        self.timestamp_utc = event.timestamp_utc
        self.connection_generation = event.connection_generation
        self.synchronized = True
        return True

    def apply_delta(self, event: OrderBookDelta) -> bool:
        self._validate_symbol(event.symbol)
        if event.connection_generation < self.connection_generation:
            return False
        if event.connection_generation > self.connection_generation:
            self.synchronized = False
            return False
        if not self.synchronized or self.update_id is None or self.sequence is None:
            return False
        if event.update_id != self.update_id + 1 or event.sequence <= self.sequence:
            self.synchronized = False
            return False
        self._apply_levels(self.bids, event.bids)
        self._apply_levels(self.asks, event.asks)
        self.update_id = event.update_id
        self.sequence = event.sequence
        self.timestamp_utc = event.timestamp_utc
        return True

    def mark_unsynchronized(self) -> None:
        self.synchronized = False

    def is_execution_ready(
        self,
        as_of_utc: datetime,
        max_age_seconds: Decimal,
    ) -> bool:
        if (
            not self.synchronized
            or self.timestamp_utc is None
            or self.best_bid is None
            or self.best_ask is None
        ):
            return False
        as_of = _utc(as_of_utc, "as of time")
        age = Decimal(str((as_of - self.timestamp_utc).total_seconds()))
        maximum_age = _decimal(max_age_seconds, "maximum book age")
        return ZERO <= age <= maximum_age

    def _validate_symbol(self, symbol: str) -> None:
        if symbol != self.symbol:
            raise ValueError("order book event symbol does not match local book")

    @staticmethod
    def _apply_levels(
        book_side: dict[Decimal, Decimal],
        levels: tuple[BookLevel, ...],
    ) -> None:
        for price, size in levels:
            if size == ZERO:
                book_side.pop(price, None)
            else:
                book_side[price] = size


@dataclass(frozen=True, slots=True)
class TriggerPriceEvent:
    symbol: str
    source: TriggerPriceSource
    observed_price: Decimal
    observed_timestamp: datetime
    connection_generation: int


class TriggerPriceRouter:
    def __init__(self, source: TriggerPriceSource) -> None:
        self.source = TriggerPriceSource(source)

    def from_trade(self, event: TradeEvent) -> TriggerPriceEvent | None:
        if self.source is not TriggerPriceSource.LAST_TRADE:
            return None
        return TriggerPriceEvent(
            symbol=event.symbol,
            source=self.source,
            observed_price=event.price,
            observed_timestamp=event.timestamp_utc,
            connection_generation=event.connection_generation,
        )

    def from_ticker(self, event: TickerEvent) -> TriggerPriceEvent | None:
        if self.source is TriggerPriceSource.LAST_TRADE:
            return None
        price = (
            event.mark_price
            if self.source is TriggerPriceSource.MARK_PRICE
            else event.index_price
        )
        if price is None:
            return None
        return TriggerPriceEvent(
            symbol=event.symbol,
            source=self.source,
            observed_price=price,
            observed_timestamp=event.timestamp_utc,
            connection_generation=event.connection_generation,
        )


@dataclass(frozen=True, slots=True)
class MarketDataHealthPolicy:
    max_trigger_age_seconds: Decimal
    max_option_quote_age_seconds: Decimal
    max_order_book_age_seconds: Decimal

    def __post_init__(self) -> None:
        for field_name in (
            "max_trigger_age_seconds",
            "max_option_quote_age_seconds",
            "max_order_book_age_seconds",
        ):
            value = _decimal(getattr(self, field_name), field_name.replace("_", " "))
            if value < ZERO:
                raise ValueError(f"{field_name.replace('_', ' ')} cannot be negative")
            object.__setattr__(self, field_name, value)


@dataclass(frozen=True, slots=True)
class MarketDataHealthSnapshot:
    trigger_timestamp_utc: datetime | None
    instrument_loaded: bool
    websocket_connected: bool
    option_quote_timestamps_utc: tuple[datetime, ...]
    order_book_synchronized: bool
    order_book_timestamp_utc: datetime | None
    clock_synchronized: bool


@dataclass(frozen=True, slots=True)
class MarketDataHealthResult:
    trading_allowed: bool
    reasons: tuple[str, ...]
    event_type: MarketDataEventType


@dataclass(frozen=True, slots=True)
class InstrumentStatusEvent:
    symbol: str
    status: str
    timestamp_utc: datetime
    connection_generation: int
    event_type: MarketDataEventType


def normalize_instrument_change(
    previous: InstrumentSpec,
    current: InstrumentSpec,
    *,
    observed_at_utc: datetime,
    connection_generation: int,
) -> InstrumentStatusEvent:
    if previous.symbol != current.symbol:
        raise ValueError("instrument update symbols must match")
    if previous == current:
        raise ValueError("instrument update must contain a change")
    return InstrumentStatusEvent(
        symbol=current.symbol,
        status=current.status,
        timestamp_utc=_utc(observed_at_utc, "instrument observation time"),
        connection_generation=connection_generation,
        event_type=(
            MarketDataEventType.INSTRUMENT_DISABLED
            if current.status != "Trading"
            else MarketDataEventType.INSTRUMENT_CHANGED
        ),
    )


def evaluate_market_data_health(
    snapshot: MarketDataHealthSnapshot,
    policy: MarketDataHealthPolicy,
    *,
    as_of_utc: datetime,
    order_book_required: bool,
) -> MarketDataHealthResult:
    as_of = _utc(as_of_utc, "as of time")
    reasons: list[str] = []
    if snapshot.trigger_timestamp_utc is None:
        reasons.append("trigger price is unavailable")
    elif not _is_fresh(
        snapshot.trigger_timestamp_utc,
        as_of,
        policy.max_trigger_age_seconds,
    ):
        reasons.append("trigger price is stale")
    if not snapshot.instrument_loaded:
        reasons.append("instrument is not loaded")
    if not snapshot.websocket_connected:
        reasons.append("public websocket is disconnected")
    if not snapshot.option_quote_timestamps_utc:
        reasons.append("option quotes are unavailable")
    elif any(
        not _is_fresh(timestamp, as_of, policy.max_option_quote_age_seconds)
        for timestamp in snapshot.option_quote_timestamps_utc
    ):
        reasons.append("option quote is stale")
    if order_book_required:
        if not snapshot.order_book_synchronized:
            reasons.append("order book is not synchronized")
        elif snapshot.order_book_timestamp_utc is None:
            reasons.append("order book timestamp is unavailable")
        elif not _is_fresh(
            snapshot.order_book_timestamp_utc,
            as_of,
            policy.max_order_book_age_seconds,
        ):
            reasons.append("order book is stale")
    if not snapshot.clock_synchronized:
        reasons.append("clock is not synchronized")
    trading_allowed = not reasons
    return MarketDataHealthResult(
        trading_allowed=trading_allowed,
        reasons=tuple(reasons),
        event_type=(
            MarketDataEventType.MARKET_DATA_RECOVERED
            if trading_allowed
            else MarketDataEventType.MARKET_DATA_STALE
        ),
    )


def _is_fresh(timestamp: datetime, as_of: datetime, maximum_age: Decimal) -> bool:
    observed = _utc(timestamp, "market-data timestamp")
    age = Decimal(str((as_of - observed).total_seconds()))
    return ZERO <= age <= maximum_age
