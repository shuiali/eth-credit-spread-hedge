"""Canonical durable state and reducer for the integrated demo runtime."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum

from eth_credit_hedge.core.virtual_levels import HedgeLevel, LevelState
from eth_credit_hedge.domain.journal import JournalEvent, JournalEventType


ZERO = Decimal("0")


class LiveHedgeRole(str, Enum):
    BASELINE = "BASELINE"
    RECOVERY = "RECOVERY"


@dataclass(frozen=True, slots=True)
class DemoLevelRuntimeState:
    level_id: int
    entry_price: Decimal
    take_profit_price: Decimal
    stop_price: Decimal
    option_budget: Decimal
    state: LevelState = LevelState.READY
    armed: bool = False
    connection_generation: int | None = None
    attempts: int = 0
    active_entry_order_link_id: str | None = None
    active_role: LiveHedgeRole | None = None
    active_stop_order_link_id: str | None = None
    active_take_profit_order_link_id: str | None = None
    active_quantity: Decimal = ZERO
    average_entry_price: Decimal | None = None
    confirmed_debt: Decimal = ZERO
    allocated_debt: Decimal = ZERO
    realized_pnl: Decimal = ZERO

    def __post_init__(self) -> None:
        if self.level_id <= 0:
            raise ValueError("level ID must be positive")
        for name in (
            "entry_price",
            "take_profit_price",
            "stop_price",
            "option_budget",
            "confirmed_debt",
            "allocated_debt",
            "realized_pnl",
            "active_quantity",
        ):
            value = Decimal(getattr(self, name))
            if not value.is_finite():
                raise ValueError(f"{name.replace('_', ' ')} must be finite")
            object.__setattr__(self, name, value)
        if self.entry_price <= self.take_profit_price:
            raise ValueError("entry price must exceed take-profit price")
        if self.stop_price <= self.entry_price:
            raise ValueError("stop price must exceed entry price")
        if self.option_budget <= ZERO:
            raise ValueError("option budget must be positive")
        if self.confirmed_debt < ZERO or self.allocated_debt < ZERO:
            raise ValueError("recovery debt cannot be negative")
        if self.active_quantity < ZERO:
            raise ValueError("active quantity cannot be negative")
        if self.average_entry_price is not None:
            average_entry_price = Decimal(self.average_entry_price)
            if not average_entry_price.is_finite() or average_entry_price <= ZERO:
                raise ValueError("average entry price must be positive")
            object.__setattr__(self, "average_entry_price", average_entry_price)
        if self.attempts < 0:
            raise ValueError("attempts cannot be negative")
        if self.connection_generation is not None and self.connection_generation < 0:
            raise ValueError("connection generation cannot be negative")
        object.__setattr__(self, "state", LevelState(self.state))
        if self.active_role is not None:
            object.__setattr__(self, "active_role", LiveHedgeRole(self.active_role))
        active_fields = (
            self.active_entry_order_link_id is not None,
            self.active_role is not None,
        )
        if active_fields[0] != active_fields[1]:
            raise ValueError("active entry ID and hedge role must be set together")
        protection_fields = (
            self.active_stop_order_link_id,
            self.active_take_profit_order_link_id,
        )
        if (protection_fields[0] is None) != (protection_fields[1] is None):
            raise ValueError("active stop and take-profit IDs must be set together")
        if protection_fields[0] is not None and self.active_entry_order_link_id is None:
            raise ValueError("active protection requires an active entry")
        if self.active_quantity == ZERO and self.average_entry_price is not None:
            raise ValueError("average entry price requires active quantity")
        if self.active_quantity > ZERO and self.average_entry_price is None:
            raise ValueError("active quantity requires average entry price")

    @classmethod
    def from_level(cls, level: HedgeLevel) -> DemoLevelRuntimeState:
        return cls(
            level_id=level.level_id,
            entry_price=level.entry_price,
            take_profit_price=level.tp_price,
            stop_price=level.stop_price,
            option_budget=level.option_budget,
        )


@dataclass(frozen=True, slots=True)
class DemoRuntimeState:
    cycle_id: str
    short_option_symbol: str
    long_option_symbol: str
    option_quantity: Decimal
    levels: tuple[DemoLevelRuntimeState, ...]
    last_public_event_id: str | None = None
    last_private_event_id: str | None = None
    last_reconciliation_at_utc: datetime | None = None
    reconciliation_complete: bool = False
    consecutive_reconciliation_failures: int = 0
    suspended_reason: str | None = None
    daily_realized_pnl: Decimal = ZERO
    order_request_times_utc: tuple[datetime, ...] = ()
    processed_event_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for value, name in (
            (self.cycle_id, "cycle ID"),
            (self.short_option_symbol, "short option symbol"),
            (self.long_option_symbol, "long option symbol"),
        ):
            if not value.strip():
                raise ValueError(f"{name} cannot be empty")
        quantity = Decimal(self.option_quantity)
        if not quantity.is_finite() or quantity <= ZERO:
            raise ValueError("option quantity must be positive")
        object.__setattr__(self, "option_quantity", quantity)
        levels = tuple(self.levels)
        if not levels:
            raise ValueError("runtime requires at least one level")
        ids = tuple(level.level_id for level in levels)
        if ids != tuple(range(1, len(levels) + 1)):
            raise ValueError("runtime level IDs must be contiguous from one")
        object.__setattr__(self, "levels", levels)
        pnl = Decimal(self.daily_realized_pnl)
        if not pnl.is_finite():
            raise ValueError("daily realized P&L must be finite")
        object.__setattr__(self, "daily_realized_pnl", pnl)
        if self.consecutive_reconciliation_failures < 0:
            raise ValueError("reconciliation failures cannot be negative")
        if self.last_reconciliation_at_utc is not None:
            object.__setattr__(
                self,
                "last_reconciliation_at_utc",
                _utc(self.last_reconciliation_at_utc),
            )
        object.__setattr__(
            self,
            "order_request_times_utc",
            tuple(_utc(value) for value in self.order_request_times_utc),
        )
        processed = tuple(self.processed_event_ids)
        if any(not value.strip() for value in processed):
            raise ValueError("processed event IDs cannot be empty")
        if len(processed) != len(set(processed)):
            raise ValueError("processed event IDs must be unique")
        object.__setattr__(self, "processed_event_ids", processed)

    def level(self, level_id: int) -> DemoLevelRuntimeState:
        if level_id <= 0 or level_id > len(self.levels):
            raise ValueError(f"unknown level ID {level_id}")
        return self.levels[level_id - 1]

    def replace_level(self, updated: DemoLevelRuntimeState) -> DemoRuntimeState:
        current = self.level(updated.level_id)
        if current.level_id != updated.level_id:
            raise AssertionError("level identity changed")
        levels = list(self.levels)
        levels[updated.level_id - 1] = updated
        return replace(self, levels=tuple(levels))


def reduce_demo_runtime_state(
    state: dict[str, object],
    event: JournalEvent,
) -> dict[str, object]:
    if not state:
        if event.event_type is not JournalEventType.STRATEGY_CYCLE_CREATED:
            raise ValueError("first runtime event must create the strategy cycle")
        initial = event.payload.get("runtime_state")
        if not isinstance(initial, dict):
            raise ValueError("strategy-cycle event requires runtime_state")
        runtime = demo_runtime_state_from_payload(initial)
        if runtime.cycle_id != event.cycle_id:
            raise ValueError("strategy-cycle event cycle ID does not match state")
        return demo_runtime_state_to_payload(
            replace(runtime, processed_event_ids=(event.event_id,))
        )
    runtime = demo_runtime_state_from_payload(state)
    if event.cycle_id != runtime.cycle_id:
        raise ValueError("journal event cycle ID does not match runtime state")
    if event.event_id in runtime.processed_event_ids:
        return demo_runtime_state_to_payload(runtime)
    payload = event.payload
    level_id = event.level_id
    if event.event_type is JournalEventType.VIRTUAL_LEVEL_ARMED:
        level = _required_level(runtime, level_id)
        runtime = runtime.replace_level(
            replace(
                level,
                armed=True,
                connection_generation=_required_int(
                    payload, "connection_generation"
                ),
            )
        )
        runtime = replace(runtime, last_public_event_id=event.event_id)
    elif event.event_type is JournalEventType.HEDGE_ENTRY_INTENT_CREATED:
        level = _required_level(runtime, level_id)
        runtime = runtime.replace_level(
            replace(
                level,
                armed=False,
                state=LevelState.ACTIVE,
                attempts=level.attempts + 1,
                active_entry_order_link_id=_required_text(
                    payload, "order_link_id"
                ),
                active_role=LiveHedgeRole(_required_text(payload, "role")),
                allocated_debt=_decimal(payload, "allocated_debt", default=ZERO),
            )
        )
        runtime = replace(
            runtime,
            order_request_times_utc=(
                runtime.order_request_times_utc + (event.timestamp_utc,)
            ),
        )
    elif event.event_type is JournalEventType.PROTECTION_CONFIRMED:
        level = _required_level(runtime, level_id)
        if level.active_entry_order_link_id != _required_text(
            payload, "entry_order_link_id"
        ):
            raise ValueError("protection does not belong to the active level entry")
        runtime = runtime.replace_level(
            replace(
                level,
                active_stop_order_link_id=_required_text(
                    payload, "stop_order_link_id"
                ),
                active_take_profit_order_link_id=_required_text(
                    payload, "take_profit_order_link_id"
                ),
                active_quantity=_decimal(payload, "quantity"),
                average_entry_price=_decimal(payload, "average_entry_price"),
            )
        )
    elif event.event_type is JournalEventType.TAKE_PROFIT_RECEIVED:
        level = _required_level(runtime, level_id)
        realized = _decimal(payload, "realized_pnl")
        remaining_debt = _decimal(payload, "remaining_debt", default=ZERO)
        runtime = runtime.replace_level(
            replace(
                level,
                state=(
                    LevelState.PAID
                    if remaining_debt == ZERO
                    else LevelState.READY
                ),
                active_entry_order_link_id=None,
                active_role=None,
                active_stop_order_link_id=None,
                active_take_profit_order_link_id=None,
                active_quantity=ZERO,
                average_entry_price=None,
                allocated_debt=ZERO,
                confirmed_debt=remaining_debt,
                realized_pnl=level.realized_pnl + realized,
            )
        )
        runtime = replace(
            runtime,
            daily_realized_pnl=runtime.daily_realized_pnl + realized,
            last_private_event_id=event.event_id,
        )
    elif event.event_type is JournalEventType.STOP_RECEIVED:
        level = _required_level(runtime, level_id)
        realized = _decimal(payload, "realized_pnl")
        added_debt = _decimal(payload, "actual_stop_debt")
        runtime = runtime.replace_level(
            replace(
                level,
                state=LevelState.READY,
                armed=False,
                active_entry_order_link_id=None,
                active_role=None,
                active_stop_order_link_id=None,
                active_take_profit_order_link_id=None,
                active_quantity=ZERO,
                average_entry_price=None,
                allocated_debt=ZERO,
                confirmed_debt=level.confirmed_debt + added_debt,
                realized_pnl=level.realized_pnl + realized,
            )
        )
        runtime = replace(
            runtime,
            daily_realized_pnl=runtime.daily_realized_pnl + realized,
            last_private_event_id=event.event_id,
        )
    elif event.event_type is JournalEventType.LEVEL_PAID:
        level = _required_level(runtime, level_id)
        runtime = runtime.replace_level(replace(level, state=LevelState.PAID))
    elif event.event_type is JournalEventType.LEVEL_LOCKED:
        level = _required_level(runtime, level_id)
        runtime = runtime.replace_level(replace(level, state=LevelState.LOCKED))
    elif event.event_type is JournalEventType.TRADING_SUSPENDED:
        runtime = replace(
            runtime,
            suspended_reason=_required_text(payload, "reason"),
            reconciliation_complete=False,
            consecutive_reconciliation_failures=(
                runtime.consecutive_reconciliation_failures + 1
            ),
        )
    elif event.event_type is JournalEventType.RECONCILIATION_COMPLETED:
        runtime = replace(
            runtime,
            reconciliation_complete=True,
            suspended_reason=None,
            last_reconciliation_at_utc=event.timestamp_utc,
            consecutive_reconciliation_failures=0,
        )
    runtime = replace(
        runtime,
        processed_event_ids=runtime.processed_event_ids + (event.event_id,),
    )
    return demo_runtime_state_to_payload(runtime)


def demo_runtime_state_to_payload(state: DemoRuntimeState) -> dict[str, object]:
    return {
        "cycle_id": state.cycle_id,
        "short_option_symbol": state.short_option_symbol,
        "long_option_symbol": state.long_option_symbol,
        "option_quantity": str(state.option_quantity),
        "levels": [
            {
                "level_id": level.level_id,
                "entry_price": str(level.entry_price),
                "take_profit_price": str(level.take_profit_price),
                "stop_price": str(level.stop_price),
                "option_budget": str(level.option_budget),
                "state": level.state.value,
                "armed": level.armed,
                "connection_generation": level.connection_generation,
                "attempts": level.attempts,
                "active_entry_order_link_id": level.active_entry_order_link_id,
                "active_role": (
                    None if level.active_role is None else level.active_role.value
                ),
                "active_stop_order_link_id": level.active_stop_order_link_id,
                "active_take_profit_order_link_id": (
                    level.active_take_profit_order_link_id
                ),
                "active_quantity": str(level.active_quantity),
                "average_entry_price": (
                    None
                    if level.average_entry_price is None
                    else str(level.average_entry_price)
                ),
                "confirmed_debt": str(level.confirmed_debt),
                "allocated_debt": str(level.allocated_debt),
                "realized_pnl": str(level.realized_pnl),
            }
            for level in state.levels
        ],
        "last_public_event_id": state.last_public_event_id,
        "last_private_event_id": state.last_private_event_id,
        "last_reconciliation_at_utc": (
            None
            if state.last_reconciliation_at_utc is None
            else state.last_reconciliation_at_utc.isoformat()
        ),
        "reconciliation_complete": state.reconciliation_complete,
        "consecutive_reconciliation_failures": (
            state.consecutive_reconciliation_failures
        ),
        "suspended_reason": state.suspended_reason,
        "daily_realized_pnl": str(state.daily_realized_pnl),
        "order_request_times_utc": [
            value.isoformat() for value in state.order_request_times_utc
        ],
        "processed_event_ids": list(state.processed_event_ids),
    }


def demo_runtime_state_from_payload(payload: dict[str, object]) -> DemoRuntimeState:
    levels_raw = payload.get("levels")
    if not isinstance(levels_raw, list):
        raise ValueError("runtime payload requires levels")
    levels = tuple(_level_from_payload(value) for value in levels_raw)
    reconciliation = payload.get("last_reconciliation_at_utc")
    request_times = payload.get("order_request_times_utc", [])
    if not isinstance(request_times, list):
        raise ValueError("order request times must be a list")
    processed_event_ids = payload.get("processed_event_ids", [])
    if not isinstance(processed_event_ids, list):
        raise ValueError("processed event IDs must be a list")
    return DemoRuntimeState(
        cycle_id=_payload_text(payload, "cycle_id"),
        short_option_symbol=_payload_text(payload, "short_option_symbol"),
        long_option_symbol=_payload_text(payload, "long_option_symbol"),
        option_quantity=Decimal(_payload_text(payload, "option_quantity")),
        levels=levels,
        last_public_event_id=_payload_optional_text(
            payload.get("last_public_event_id")
        ),
        last_private_event_id=_payload_optional_text(
            payload.get("last_private_event_id")
        ),
        last_reconciliation_at_utc=(
            None
            if reconciliation is None
            else datetime.fromisoformat(str(reconciliation))
        ),
        reconciliation_complete=bool(payload.get("reconciliation_complete", False)),
        consecutive_reconciliation_failures=int(
            str(payload.get("consecutive_reconciliation_failures", 0))
        ),
        suspended_reason=_payload_optional_text(payload.get("suspended_reason")),
        daily_realized_pnl=Decimal(str(payload.get("daily_realized_pnl", "0"))),
        order_request_times_utc=tuple(
            datetime.fromisoformat(str(value)) for value in request_times
        ),
        processed_event_ids=tuple(str(value) for value in processed_event_ids),
    )


def _level_from_payload(raw: object) -> DemoLevelRuntimeState:
    if not isinstance(raw, dict):
        raise ValueError("runtime level must be an object")
    return DemoLevelRuntimeState(
        level_id=_required_int(raw, "level_id"),
        entry_price=_decimal(raw, "entry_price"),
        take_profit_price=_decimal(raw, "take_profit_price"),
        stop_price=_decimal(raw, "stop_price"),
        option_budget=_decimal(raw, "option_budget"),
        state=LevelState(_required_text(raw, "state")),
        armed=bool(raw.get("armed", False)),
        connection_generation=(
            None
            if raw.get("connection_generation") is None
            else int(str(raw["connection_generation"]))
        ),
        attempts=int(str(raw.get("attempts", 0))),
        active_entry_order_link_id=_payload_optional_text(
            raw.get("active_entry_order_link_id")
        ),
        active_role=(
            None
            if raw.get("active_role") is None
            else LiveHedgeRole(str(raw["active_role"]))
        ),
        active_stop_order_link_id=_payload_optional_text(
            raw.get("active_stop_order_link_id")
        ),
        active_take_profit_order_link_id=_payload_optional_text(
            raw.get("active_take_profit_order_link_id")
        ),
        active_quantity=_decimal(raw, "active_quantity", default=ZERO),
        average_entry_price=(
            None
            if raw.get("average_entry_price") is None
            else _decimal(raw, "average_entry_price")
        ),
        confirmed_debt=_decimal(raw, "confirmed_debt", default=ZERO),
        allocated_debt=_decimal(raw, "allocated_debt", default=ZERO),
        realized_pnl=_decimal(raw, "realized_pnl", default=ZERO),
    )


def _required_level(
    runtime: DemoRuntimeState,
    level_id: int | None,
) -> DemoLevelRuntimeState:
    if level_id is None:
        raise ValueError("journal event requires a level ID")
    return runtime.level(level_id)


def _required_text(payload: dict[str, object], name: str) -> str:
    return _payload_text(payload, name)


def _payload_text(payload: dict[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"runtime payload requires {name}")
    return value


def _payload_optional_text(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _required_int(payload: dict[str, object], name: str) -> int:
    value = payload.get(name)
    if type(value) is not int:
        raise ValueError(f"runtime payload requires integer {name}")
    return value


def _decimal(
    payload: dict[str, object],
    name: str,
    *,
    default: Decimal | None = None,
) -> Decimal:
    value = payload.get(name, default)
    if value is None:
        raise ValueError(f"runtime payload requires {name}")
    normalized = Decimal(str(value))
    if not normalized.is_finite():
        raise ValueError(f"runtime payload {name} must be finite")
    return normalized


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("runtime timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


__all__ = [
    "DemoLevelRuntimeState",
    "DemoRuntimeState",
    "LiveHedgeRole",
    "demo_runtime_state_from_payload",
    "demo_runtime_state_to_payload",
    "reduce_demo_runtime_state",
]
