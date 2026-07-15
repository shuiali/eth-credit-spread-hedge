"""Secret-safe, auditable operator command models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum


class OperatorCommandType(str, Enum):
    SOFT_PAUSE = "SOFT_PAUSE"
    RESUME_AFTER_RECONCILIATION = "RESUME_AFTER_RECONCILIATION"
    CANCEL_PENDING_ENTRY = "CANCEL_PENDING_ENTRY"
    RESTORE_PROTECTION = "RESTORE_PROTECTION"
    CLOSE_HEDGE_POSITION = "CLOSE_HEDGE_POSITION"
    CLOSE_OPTION_SPREAD = "CLOSE_OPTION_SPREAD"
    FLATTEN_STRATEGY = "FLATTEN_STRATEGY"
    ACKNOWLEDGE_INCIDENT = "ACKNOWLEDGE_INCIDENT"


class OperatorCredential:
    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not value:
            raise ValueError("operator credential cannot be empty")
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "OperatorCredential(***)"

    def __str__(self) -> str:
        return "***"


def _utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class OperatorCommand:
    command_id: str
    command_type: OperatorCommandType
    operator_id: str
    reason: str
    issued_at_utc: datetime

    def __post_init__(self) -> None:
        for field_name in ("command_id", "operator_id", "reason"):
            if not getattr(self, field_name).strip():
                raise ValueError(f"operator command {field_name} cannot be empty")
        object.__setattr__(self, "command_type", OperatorCommandType(self.command_type))
        object.__setattr__(
            self,
            "issued_at_utc",
            _utc(self.issued_at_utc, "operator command timestamp"),
        )


@dataclass(frozen=True, slots=True)
class OperatorCommandResult:
    command_id: str
    command_type: OperatorCommandType
    outcome: str
    detail: str
    completed_at_utc: datetime

    def __post_init__(self) -> None:
        if not self.command_id.strip() or not self.outcome.strip() or not self.detail.strip():
            raise ValueError("operator command result fields cannot be empty")
        object.__setattr__(self, "command_type", OperatorCommandType(self.command_type))
        object.__setattr__(
            self,
            "completed_at_utc",
            _utc(self.completed_at_utc, "operator result timestamp"),
        )


@dataclass(frozen=True, slots=True)
class OperatorCommandAudit:
    command_id: str
    command_type: OperatorCommandType
    operator_id: str
    outcome: str
    timestamp_utc: datetime
    detail: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "command_type", OperatorCommandType(self.command_type))
        object.__setattr__(
            self,
            "timestamp_utc",
            _utc(self.timestamp_utc, "operator audit timestamp"),
        )
