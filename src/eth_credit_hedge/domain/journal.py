"""Versioned audit events and restart snapshots with canonical JSON state."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum


class JournalEventType(str, Enum):
    STRATEGY_CYCLE_CREATED = "StrategyCycleCreated"
    OPTION_ENTRY_INTENT_CREATED = "OptionEntryIntentCreated"
    OPTION_EXECUTION_RECEIVED = "OptionExecutionReceived"
    OPTION_SPREAD_OPENED = "OptionSpreadOpened"
    VIRTUAL_LEVEL_ARMED = "VirtualLevelArmed"
    HEDGE_ENTRY_INTENT_CREATED = "HedgeEntryIntentCreated"
    ORDER_ACKNOWLEDGED = "OrderAcknowledged"
    EXECUTION_RECEIVED = "ExecutionReceived"
    PROTECTION_INTENT_CREATED = "ProtectionIntentCreated"
    PROTECTION_CONFIRMED = "ProtectionConfirmed"
    TAKE_PROFIT_RECEIVED = "TakeProfitReceived"
    STOP_RECEIVED = "StopReceived"
    RECOVERY_DEBT_CHANGED = "RecoveryDebtChanged"
    LEVEL_PAID = "LevelPaid"
    LEVEL_LOCKED = "LevelLocked"
    TRADING_SUSPENDED = "TradingSuspended"
    RECONCILIATION_COMPLETED = "ReconciliationCompleted"


def _utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _require_text(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} cannot be empty")


def _canonical_value(value: object) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, Decimal):
        try:
            normalized_decimal = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("journal Decimal must be finite") from exc
        if not normalized_decimal.is_finite():
            raise ValueError("journal Decimal must be finite")
        return str(normalized_decimal)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("journal float must be finite")
        return value
    if isinstance(value, Enum):
        return _canonical_value(value.value)
    if isinstance(value, datetime):
        return _utc(value, "journal datetime").isoformat()
    if isinstance(value, dict):
        normalized_object: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("journal object keys must be strings")
            normalized_object[key] = _canonical_value(item)
        return normalized_object
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    raise ValueError(f"unsupported journal JSON value: {type(value).__name__}")


def canonical_object(value: dict[str, object]) -> dict[str, object]:
    normalized = _canonical_value(value)
    if not isinstance(normalized, dict):
        raise AssertionError("canonical journal object must remain an object")
    json.dumps(normalized, allow_nan=False, separators=(",", ":"), sort_keys=True)
    return normalized


def canonical_json(value: dict[str, object]) -> str:
    return json.dumps(
        canonical_object(value),
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


@dataclass(frozen=True, slots=True)
class PendingJournalEvent:
    event_id: str
    event_type: JournalEventType
    event_version: int
    cycle_id: str
    level_id: int | None
    timestamp_utc: datetime
    payload: dict[str, object]
    causation_id: str | None
    correlation_id: str

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.event_id, "event ID"),
            (self.cycle_id, "cycle ID"),
            (self.correlation_id, "correlation ID"),
        ):
            _require_text(value, field_name)
        event_type = JournalEventType(self.event_type)
        if type(self.event_version) is not int or self.event_version <= 0:
            raise ValueError("event version must be positive")
        if self.level_id is not None and (
            type(self.level_id) is not int or self.level_id <= 0
        ):
            raise ValueError("level ID must be positive when present")
        if self.causation_id is not None:
            _require_text(self.causation_id, "causation ID")
        object.__setattr__(self, "event_type", event_type)
        object.__setattr__(
            self,
            "timestamp_utc",
            _utc(self.timestamp_utc, "event timestamp"),
        )
        object.__setattr__(self, "payload", canonical_object(self.payload))


@dataclass(frozen=True, slots=True)
class JournalEvent:
    sequence: int
    event_id: str
    event_type: JournalEventType
    event_version: int
    cycle_id: str
    level_id: int | None
    timestamp_utc: datetime
    payload: dict[str, object]
    causation_id: str | None
    correlation_id: str

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence <= 0:
            raise ValueError("event sequence must be positive")
        pending = PendingJournalEvent(
            event_id=self.event_id,
            event_type=self.event_type,
            event_version=self.event_version,
            cycle_id=self.cycle_id,
            level_id=self.level_id,
            timestamp_utc=self.timestamp_utc,
            payload=self.payload,
            causation_id=self.causation_id,
            correlation_id=self.correlation_id,
        )
        object.__setattr__(self, "event_type", pending.event_type)
        object.__setattr__(self, "timestamp_utc", pending.timestamp_utc)
        object.__setattr__(self, "payload", pending.payload)


@dataclass(frozen=True, slots=True)
class CycleSnapshot:
    cycle_id: str
    last_event_sequence: int
    state: dict[str, object]
    snapshot_version: int
    updated_at_utc: datetime

    def __post_init__(self) -> None:
        _require_text(self.cycle_id, "cycle ID")
        if (
            type(self.last_event_sequence) is not int
            or self.last_event_sequence < 0
        ):
            raise ValueError("last event sequence cannot be negative")
        if type(self.snapshot_version) is not int or self.snapshot_version <= 0:
            raise ValueError("snapshot version must be positive")
        object.__setattr__(self, "state", canonical_object(self.state))
        object.__setattr__(
            self,
            "updated_at_utc",
            _utc(self.updated_at_utc, "snapshot update time"),
        )
