"""Exchange-neutral authenticated execution and account models."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Literal

from eth_credit_hedge.domain.client_order_ids import ClientOrderId


ZERO = Decimal("0")
Category = Literal["option", "linear"]
OrderSide = Literal["Buy", "Sell"]
OrderType = Literal["Market", "Limit"]
TimeInForce = Literal["GTC", "IOC", "FOK", "PostOnly"]
TriggerBy = Literal["LastPrice", "IndexPrice", "MarkPrice"]
_EXCHANGE_LINK_PATTERN = re.compile(r"^[A-Za-z0-9_-]{0,36}$")


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


def _category(value: str) -> None:
    if value not in ("option", "linear"):
        raise ValueError("category must be option or linear")


def _side(value: str | None, *, optional: bool = False) -> None:
    if value is None and optional:
        return
    if value not in ("Buy", "Sell"):
        raise ValueError("side must be Buy or Sell")


def _order_type(value: str) -> None:
    if value not in ("Market", "Limit"):
        raise ValueError("order type must be Market or Limit")


def _own_order_link_id(value: str) -> None:
    ClientOrderId.parse(value)


def _exchange_order_link_id(value: str) -> None:
    if _EXCHANGE_LINK_PATTERN.fullmatch(value) is None:
        raise ValueError("order link ID must use at most 36 safe characters")


class OrderRequestKind(str, Enum):
    PLACE = "PLACE"
    AMEND = "AMEND"
    CANCEL = "CANCEL"


class LiveExecutionState(str, Enum):
    READY = "READY"
    TRIGGERED = "TRIGGERED"
    ENTRY_REQUEST_PERSISTED = "ENTRY_REQUEST_PERSISTED"
    ENTRY_SUBMITTED = "ENTRY_SUBMITTED"
    ENTRY_ACKNOWLEDGED = "ENTRY_ACKNOWLEDGED"
    ENTRY_PARTIALLY_FILLED = "ENTRY_PARTIALLY_FILLED"
    ACTIVE_UNPROTECTED = "ACTIVE_UNPROTECTED"
    ACTIVE_PROTECTED = "ACTIVE_PROTECTED"
    EXIT_PARTIALLY_FILLED = "EXIT_PARTIALLY_FILLED"
    CLOSED_TP = "CLOSED_TP"
    CLOSED_STOP = "CLOSED_STOP"
    CANCEL_PENDING = "CANCEL_PENDING"
    RECONCILING = "RECONCILING"
    LOCKED = "LOCKED"
    ERROR = "ERROR"


class PrivateConnectionState(str, Enum):
    CONNECTED = "CONNECTED"
    AUTHENTICATED = "AUTHENTICATED"
    DISCONNECTED = "DISCONNECTED"
    RECONNECTING = "RECONNECTING"


class UncertainOrderOutcomeError(RuntimeError):
    """A mutation may have reached the exchange and must be reconciled."""

    def __init__(
        self,
        *,
        order_link_id: str | None,
        operation: str,
    ) -> None:
        self.order_link_id = order_link_id
        self.operation = operation
        identity = (
            f" for orderLinkId {order_link_id}"
            if order_link_id is not None
            else ""
        )
        super().__init__(
            f"{operation} outcome is uncertain{identity}; reconcile before retrying"
        )


@dataclass(frozen=True, slots=True)
class PlaceOrderRequest:
    category: Category
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: Decimal
    order_link_id: str
    price: Decimal | None = None
    time_in_force: TimeInForce = "GTC"
    reduce_only: bool = False
    trigger_price: Decimal | None = None
    trigger_direction: int | None = None
    trigger_by: TriggerBy | None = None
    position_idx: int = 0

    def __post_init__(self) -> None:
        _category(self.category)
        _require_text(self.symbol, "symbol")
        _side(self.side)
        _order_type(self.order_type)
        _own_order_link_id(self.order_link_id)
        quantity = _decimal(self.quantity, "quantity")
        price = _optional_decimal(self.price, "price")
        trigger_price = _optional_decimal(self.trigger_price, "trigger price")
        if quantity <= ZERO:
            raise ValueError("quantity must be positive")
        if self.order_type == "Limit" and price is None:
            raise ValueError("limit order requires a price")
        if self.order_type == "Market" and price is not None:
            raise ValueError("market order cannot include a price")
        if price is not None and price <= ZERO:
            raise ValueError("price must be positive")
        if self.time_in_force not in ("GTC", "IOC", "FOK", "PostOnly"):
            raise ValueError("unsupported time in force")
        if type(self.reduce_only) is not bool:
            raise ValueError("reduce only must be boolean")
        if trigger_price is None:
            if self.trigger_direction is not None or self.trigger_by is not None:
                raise ValueError("trigger fields require a trigger price")
        else:
            if trigger_price <= ZERO:
                raise ValueError("trigger price must be positive")
            if self.trigger_direction not in (1, 2):
                raise ValueError("trigger direction is required and must be 1 or 2")
            if self.trigger_by not in ("LastPrice", "IndexPrice", "MarkPrice"):
                raise ValueError("trigger source is required")
        if type(self.position_idx) is not int or self.position_idx not in (0, 1, 2):
            raise ValueError("position index must be 0, 1, or 2")
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "trigger_price", trigger_price)


@dataclass(frozen=True, slots=True)
class AmendOrderRequest:
    category: Category
    symbol: str
    order_link_id: str
    quantity: Decimal | None = None
    price: Decimal | None = None
    trigger_price: Decimal | None = None

    def __post_init__(self) -> None:
        _category(self.category)
        _require_text(self.symbol, "symbol")
        _own_order_link_id(self.order_link_id)
        if self.quantity is None and self.price is None and self.trigger_price is None:
            raise ValueError("amend request requires at least one change")
        for field_name in ("quantity", "price", "trigger_price"):
            value = _optional_decimal(
                getattr(self, field_name),
                field_name.replace("_", " "),
            )
            if value is not None and value <= ZERO:
                raise ValueError(f"{field_name.replace('_', ' ')} must be positive")
            object.__setattr__(self, field_name, value)


@dataclass(frozen=True, slots=True)
class CancelOrderRequest:
    category: Category
    symbol: str
    order_link_id: str

    def __post_init__(self) -> None:
        _category(self.category)
        _require_text(self.symbol, "symbol")
        _own_order_link_id(self.order_link_id)


@dataclass(frozen=True, slots=True)
class OrderRequestAck:
    """Transport acknowledgement; it proves neither visibility nor a fill."""

    request_kind: OrderRequestKind
    order_id: str
    order_link_id: str
    acknowledged_at: datetime

    def __post_init__(self) -> None:
        try:
            request_kind = OrderRequestKind(self.request_kind)
        except ValueError as exc:
            raise ValueError("unknown order request kind") from exc
        _require_text(self.order_id, "order ID")
        _own_order_link_id(self.order_link_id)
        object.__setattr__(self, "request_kind", request_kind)
        object.__setattr__(
            self,
            "acknowledged_at",
            _utc(self.acknowledged_at, "acknowledgement time"),
        )


@dataclass(frozen=True, slots=True)
class ExchangeOrder:
    """Reported order lifecycle state; executions remain fill-authoritative."""

    category: Category
    order_id: str
    order_link_id: str
    symbol: str
    status: str
    side: OrderSide
    order_type: OrderType
    price: Decimal | None
    quantity: Decimal
    cumulative_filled_quantity: Decimal
    average_price: Decimal | None
    reduce_only: bool
    created_at: datetime
    updated_at: datetime
    trigger_price: Decimal | None = None
    trigger_by: TriggerBy | None = None
    trigger_direction: int | None = None
    time_in_force: TimeInForce | None = None
    position_idx: int | None = None

    def __post_init__(self) -> None:
        _category(self.category)
        for value, field_name in (
            (self.order_id, "order ID"),
            (self.symbol, "symbol"),
            (self.status, "order status"),
        ):
            _require_text(value, field_name)
        _exchange_order_link_id(self.order_link_id)
        _side(self.side)
        _order_type(self.order_type)
        price = _optional_decimal(self.price, "order price")
        quantity = _decimal(self.quantity, "order quantity")
        filled = _decimal(
            self.cumulative_filled_quantity,
            "cumulative filled quantity",
        )
        average_price = _optional_decimal(self.average_price, "average price")
        trigger_price = _optional_decimal(self.trigger_price, "trigger price")
        if price is not None and price <= ZERO:
            raise ValueError("order price must be positive")
        if quantity <= ZERO:
            raise ValueError("order quantity must be positive")
        if not ZERO <= filled <= quantity:
            raise ValueError("cumulative filled quantity must be within order quantity")
        if average_price is not None and average_price <= ZERO:
            raise ValueError("average price must be positive")
        if trigger_price is not None and trigger_price <= ZERO:
            raise ValueError("trigger price must be positive")
        if trigger_price is None:
            if self.trigger_by is not None or self.trigger_direction is not None:
                raise ValueError("trigger fields require a trigger price")
        else:
            if self.trigger_by not in ("LastPrice", "IndexPrice", "MarkPrice"):
                raise ValueError("trigger price requires a trigger source")
            if self.trigger_direction not in (1, 2):
                raise ValueError("trigger price requires direction 1 or 2")
        if self.time_in_force is not None and self.time_in_force not in (
            "GTC",
            "IOC",
            "FOK",
            "PostOnly",
        ):
            raise ValueError("unsupported time in force")
        if self.position_idx is not None and self.position_idx not in (0, 1, 2):
            raise ValueError("position index must be 0, 1, or 2")
        if type(self.reduce_only) is not bool:
            raise ValueError("reduce only must be boolean")
        created_at = _utc(self.created_at, "order creation time")
        updated_at = _utc(self.updated_at, "order update time")
        if updated_at < created_at:
            raise ValueError("order update time cannot precede creation time")
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "cumulative_filled_quantity", filled)
        object.__setattr__(self, "average_price", average_price)
        object.__setattr__(self, "trigger_price", trigger_price)
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "updated_at", updated_at)


@dataclass(frozen=True, slots=True)
class OrderUpdate:
    """Order lifecycle notification; never an accounting fill record."""

    order_id: str
    order_link_id: str
    symbol: str
    status: str
    side: OrderSide
    order_type: OrderType
    price: Decimal | None
    quantity: Decimal
    cumulative_filled_quantity: Decimal
    average_price: Decimal | None
    updated_at: datetime

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.order_id, "order ID"),
            (self.symbol, "symbol"),
            (self.status, "order status"),
        ):
            _require_text(value, field_name)
        _exchange_order_link_id(self.order_link_id)
        _side(self.side)
        _order_type(self.order_type)
        price = _optional_decimal(self.price, "order price")
        quantity = _decimal(self.quantity, "order quantity")
        filled = _decimal(
            self.cumulative_filled_quantity,
            "cumulative filled quantity",
        )
        average_price = _optional_decimal(self.average_price, "average price")
        if price is not None and price <= ZERO:
            raise ValueError("order price must be positive")
        if quantity <= ZERO:
            raise ValueError("order quantity must be positive")
        if not ZERO <= filled <= quantity:
            raise ValueError("cumulative filled quantity must be within order quantity")
        if average_price is not None and average_price <= ZERO:
            raise ValueError("average price must be positive")
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "cumulative_filled_quantity", filled)
        object.__setattr__(self, "average_price", average_price)
        object.__setattr__(self, "updated_at", _utc(self.updated_at, "update time"))


@dataclass(frozen=True, slots=True)
class ExecutionUpdate:
    """Authoritative record of one exchange fill."""

    execution_id: str
    order_id: str
    order_link_id: str
    symbol: str
    side: OrderSide
    price: Decimal
    quantity: Decimal
    fee: Decimal
    is_maker: bool | None
    executed_at: datetime

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.execution_id, "execution ID"),
            (self.order_id, "order ID"),
            (self.symbol, "symbol"),
        ):
            _require_text(value, field_name)
        _exchange_order_link_id(self.order_link_id)
        _side(self.side)
        price = _decimal(self.price, "execution price")
        quantity = _decimal(self.quantity, "execution quantity")
        fee = _decimal(self.fee, "execution fee")
        if price <= ZERO:
            raise ValueError("execution price must be positive")
        if quantity <= ZERO:
            raise ValueError("execution quantity must be positive")
        if self.is_maker is not None and type(self.is_maker) is not bool:
            raise ValueError("maker flag must be boolean or None")
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "fee", fee)
        object.__setattr__(
            self,
            "executed_at",
            _utc(self.executed_at, "execution time"),
        )


def _payload_hash(value: str) -> None:
    if re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
        raise ValueError("raw payload hash must be a SHA-256 hexadecimal digest")


@dataclass(frozen=True, slots=True)
class OrderUpdateBatch:
    updates: tuple[OrderUpdate, ...]
    received_at: datetime
    raw_payload_hash: str

    def __post_init__(self) -> None:
        updates = tuple(self.updates)
        _payload_hash(self.raw_payload_hash)
        object.__setattr__(self, "updates", updates)
        object.__setattr__(
            self,
            "received_at",
            _utc(self.received_at, "batch receive time"),
        )


@dataclass(frozen=True, slots=True)
class ExecutionUpdateBatch:
    executions: tuple[ExecutionUpdate, ...]
    received_at: datetime
    raw_payload_hash: str

    def __post_init__(self) -> None:
        executions = tuple(self.executions)
        _payload_hash(self.raw_payload_hash)
        object.__setattr__(self, "executions", executions)
        object.__setattr__(
            self,
            "received_at",
            _utc(self.received_at, "batch receive time"),
        )


@dataclass(frozen=True, slots=True)
class ExchangePosition:
    category: Category
    symbol: str
    side: OrderSide | None
    quantity: Decimal
    average_price: Decimal | None
    mark_price: Decimal | None
    unrealized_pnl: Decimal
    updated_at: datetime

    def __post_init__(self) -> None:
        _category(self.category)
        _require_text(self.symbol, "symbol")
        _side(self.side, optional=True)
        quantity = _decimal(self.quantity, "position quantity")
        average_price = _optional_decimal(self.average_price, "average price")
        mark_price = _optional_decimal(self.mark_price, "mark price")
        unrealized_pnl = _decimal(self.unrealized_pnl, "unrealized P&L")
        if quantity < ZERO:
            raise ValueError("position quantity cannot be negative")
        if quantity > ZERO and self.side is None:
            raise ValueError("non-flat position requires a side")
        if quantity > ZERO and (average_price is None or average_price <= ZERO):
            raise ValueError("non-flat position requires a positive average price")
        if average_price is not None and average_price <= ZERO:
            raise ValueError("average price must be positive")
        if mark_price is not None and mark_price <= ZERO:
            raise ValueError("mark price must be positive")
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "average_price", average_price)
        object.__setattr__(self, "mark_price", mark_price)
        object.__setattr__(self, "unrealized_pnl", unrealized_pnl)
        object.__setattr__(self, "updated_at", _utc(self.updated_at, "position time"))


@dataclass(frozen=True, slots=True)
class WalletBalance:
    coin: str
    equity: Decimal
    wallet_balance: Decimal
    available_balance: Decimal | None
    unrealized_pnl: Decimal

    def __post_init__(self) -> None:
        _require_text(self.coin, "coin")
        for field_name in (
            "equity",
            "wallet_balance",
            "unrealized_pnl",
        ):
            object.__setattr__(
                self,
                field_name,
                _decimal(getattr(self, field_name), field_name.replace("_", " ")),
            )
        object.__setattr__(
            self,
            "available_balance",
            _optional_decimal(self.available_balance, "available balance"),
        )


@dataclass(frozen=True, slots=True)
class WalletState:
    account_type: str
    total_equity: Decimal
    total_wallet_balance: Decimal
    total_available_balance: Decimal | None
    balances: tuple[WalletBalance, ...]
    updated_at: datetime

    def __post_init__(self) -> None:
        _require_text(self.account_type, "account type")
        for field_name in (
            "total_equity",
            "total_wallet_balance",
        ):
            object.__setattr__(
                self,
                field_name,
                _decimal(getattr(self, field_name), field_name.replace("_", " ")),
            )
        object.__setattr__(
            self,
            "total_available_balance",
            _optional_decimal(
                self.total_available_balance,
                "total available balance",
            ),
        )
        balances = tuple(self.balances)
        coins = [balance.coin for balance in balances]
        if len(coins) != len(set(coins)):
            raise ValueError("wallet balances must have unique coins")
        object.__setattr__(self, "balances", balances)
        object.__setattr__(self, "updated_at", _utc(self.updated_at, "wallet time"))


@dataclass(frozen=True, slots=True)
class PrivateConnectionEvent:
    state: PrivateConnectionState
    observed_at: datetime
    connection_generation: int
    reason: str | None = None

    def __post_init__(self) -> None:
        try:
            state = PrivateConnectionState(self.state)
        except ValueError as exc:
            raise ValueError("unknown private connection state") from exc
        if type(self.connection_generation) is not int or self.connection_generation < 0:
            raise ValueError("connection generation cannot be negative")
        if self.reason is not None:
            _require_text(self.reason, "connection reason")
        object.__setattr__(self, "state", state)
        object.__setattr__(
            self,
            "observed_at",
            _utc(self.observed_at, "connection event time"),
        )
