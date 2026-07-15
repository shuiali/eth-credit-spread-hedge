"""Normalize Bybit private WebSocket payloads into execution-domain events."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, cast

from eth_credit_hedge.domain.execution import (
    ExchangePosition,
    ExecutionUpdate,
    ExecutionUpdateBatch,
    OrderUpdate,
    OrderUpdateBatch,
)
from eth_credit_hedge.infrastructure.bybit.parsers import raw_payload_hash


PrivateParsedEvent = (
    OrderUpdateBatch | ExecutionUpdateBatch | tuple[ExchangePosition, ...]
)


def parse_order_message(message: dict[str, Any]) -> OrderUpdateBatch:
    """Parse every order update in one Bybit private message."""
    received_at = _message_time(message)
    updates = tuple(
        OrderUpdate(
            order_id=_required_text(item, "orderId"),
            order_link_id=_optional_text(item.get("orderLinkId")),
            symbol=_required_text(item, "symbol"),
            status=_required_text(item, "orderStatus"),
            side=_side(item.get("side")),
            order_type=_order_type(item.get("orderType")),
            price=_optional_decimal(item.get("price"), "price"),
            quantity=_required_decimal(item, "qty"),
            cumulative_filled_quantity=_decimal_or_zero(
                item.get("cumExecQty"),
                "cumulative executed quantity",
            ),
            average_price=_optional_decimal(item.get("avgPrice"), "average price"),
            updated_at=_item_time(item, received_at),
        )
        for item in _message_items(message)
    )
    return OrderUpdateBatch(
        updates=updates,
        received_at=received_at,
        raw_payload_hash=raw_payload_hash(message),
    )


def parse_execution_message(message: dict[str, Any]) -> ExecutionUpdateBatch:
    """Parse all executions; Bybit may batch several fills in one message."""
    received_at = _message_time(message)
    executions = tuple(
        ExecutionUpdate(
            execution_id=_required_text(item, "execId"),
            order_id=_required_text(item, "orderId"),
            order_link_id=_optional_text(item.get("orderLinkId")),
            symbol=_required_text(item, "symbol"),
            side=_side(item.get("side")),
            price=_required_decimal(item, "execPrice"),
            quantity=_required_decimal(item, "execQty"),
            fee=_decimal_or_zero(item.get("execFee"), "execution fee"),
            is_maker=_optional_bool(item.get("isMaker"), "isMaker"),
            executed_at=_timestamp(
                _required_int(item, "execTime"),
                "execution time",
            ),
        )
        for item in _message_items(message)
    )
    return ExecutionUpdateBatch(
        executions=executions,
        received_at=received_at,
        raw_payload_hash=raw_payload_hash(message),
    )


def parse_position_message(
    message: dict[str, Any],
) -> tuple[ExchangePosition, ...]:
    """Parse a Bybit position message, preserving every position in its data list."""
    received_at = _message_time(message)
    return tuple(
        ExchangePosition(
            category=_category(item.get("category")),
            symbol=_required_text(item, "symbol"),
            side=_position_side(item.get("side")),
            quantity=_required_decimal(item, "size"),
            average_price=_optional_decimal(item.get("entryPrice"), "entry price"),
            mark_price=_optional_decimal(item.get("markPrice"), "mark price"),
            unrealized_pnl=_decimal_or_zero(
                item.get("unrealisedPnl"),
                "unrealized P&L",
            ),
            updated_at=_item_time(item, received_at),
            position_idx=_optional_position_idx(item.get("positionIdx")),
        )
        for item in _message_items(message)
    )


class BybitPrivateEventParser:
    """Route private topics and suppress repeated normalized order updates."""

    def __init__(self) -> None:
        self._last_order_update: dict[str, OrderUpdate] = {}

    def parse_message(self, message: dict[str, Any]) -> PrivateParsedEvent:
        topic = message.get("topic")
        if not isinstance(topic, str):
            raise ValueError("Bybit private message topic must be a string")
        topic_family = topic.split(".", maxsplit=1)[0]
        if topic_family == "order":
            batch = parse_order_message(message)
            unique_updates = tuple(
                update
                for update in batch.updates
                if self._remember_order_update(update)
            )
            return OrderUpdateBatch(
                updates=unique_updates,
                received_at=batch.received_at,
                raw_payload_hash=batch.raw_payload_hash,
            )
        if topic_family == "execution":
            return parse_execution_message(message)
        if topic_family == "position":
            return parse_position_message(message)
        raise ValueError(f"unsupported Bybit private topic: {topic}")

    def _remember_order_update(self, update: OrderUpdate) -> bool:
        if self._last_order_update.get(update.order_id) == update:
            return False
        self._last_order_update[update.order_id] = update
        return True


def _message_items(message: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    data = message.get("data")
    if not isinstance(data, list):
        raise ValueError("Bybit private message data must be a list")
    items: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("Bybit private message item must be an object")
        items.append(cast(dict[str, Any], item))
    return tuple(items)


def _message_time(message: dict[str, Any]) -> datetime:
    return _timestamp(
        _required_int(message, "creationTime"),
        "message creation time",
    )


def _item_time(item: dict[str, Any], fallback: datetime) -> datetime:
    value = item.get("updatedTime")
    if value in (None, ""):
        return fallback
    return _timestamp(_int(value, "updatedTime"), "item update time")


def _timestamp(milliseconds: int, field_name: str) -> datetime:
    if milliseconds < 0:
        raise ValueError(f"{field_name} cannot be negative")
    return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc)


def _required_int(parent: dict[str, Any], key: str) -> int:
    if key not in parent:
        raise ValueError(f"Bybit {key} is required")
    return _int(parent[key], key)


def _int(value: object, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"Bybit {field_name} must be an integer")
    try:
        return int(cast(str | int, value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Bybit {field_name} must be an integer") from exc


def _optional_position_idx(value: object) -> int:
    if value in (None, ""):
        return 0
    position_idx = _int(value, "positionIdx")
    if position_idx not in (0, 1, 2):
        raise ValueError("Bybit positionIdx must be 0, 1, or 2")
    return position_idx


def _required_text(parent: dict[str, Any], key: str) -> str:
    value = parent.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Bybit {key} must be a non-empty string")
    return value


def _optional_text(value: object) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise ValueError("Bybit optional text must be a string")
    return value


def _required_decimal(parent: dict[str, Any], key: str) -> Decimal:
    if key not in parent or parent[key] in (None, ""):
        raise ValueError(f"Bybit {key} is required")
    value = _optional_decimal(parent[key], key)
    if value is None:
        raise ValueError(f"Bybit {key} is required")
    return value


def _decimal_or_zero(value: object, field_name: str) -> Decimal:
    parsed = _optional_decimal(value, field_name)
    return Decimal("0") if parsed is None else parsed


def _optional_decimal(value: object, field_name: str) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Bybit {field_name} must be a finite decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"Bybit {field_name} must be a finite decimal")
    return parsed


def _side(value: object) -> Literal["Buy", "Sell"]:
    if value not in ("Buy", "Sell"):
        raise ValueError("Bybit side must be Buy or Sell")
    return value


def _position_side(value: object) -> Literal["Buy", "Sell"] | None:
    if value == "":
        return None
    return _side(value)


def _order_type(value: object) -> Literal["Market", "Limit"]:
    if value not in ("Market", "Limit"):
        raise ValueError("Bybit order type must be Market or Limit")
    return value


def _category(value: object) -> Literal["option", "linear"]:
    if value not in ("option", "linear"):
        raise ValueError("Bybit position category must be option or linear")
    return value


def _optional_bool(value: object, field_name: str) -> bool | None:
    if value in (None, ""):
        return None
    if not isinstance(value, bool):
        raise ValueError(f"Bybit {field_name} must be a boolean")
    return value
