"""Durable kill-switch state shared by application and persistence layers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum


class KillSwitchMode(str, Enum):
    RUNNING = "RUNNING"
    SOFT_PAUSE = "SOFT_PAUSE"
    STRATEGY_CLOSE = "STRATEGY_CLOSE"
    EMERGENCY_FLATTEN = "EMERGENCY_FLATTEN"


@dataclass(frozen=True, slots=True)
class KillSwitchState:
    mode: KillSwitchMode
    reason: str
    requested_by: str
    changed_at_utc: datetime
    version: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", KillSwitchMode(self.mode))
        if not self.reason.strip() or not self.requested_by.strip():
            raise ValueError("kill-switch reason and requester cannot be empty")
        if self.changed_at_utc.tzinfo is None or self.changed_at_utc.utcoffset() is None:
            raise ValueError("kill-switch timestamp must be timezone-aware")
        object.__setattr__(
            self,
            "changed_at_utc",
            self.changed_at_utc.astimezone(timezone.utc),
        )
        if type(self.version) is not int or self.version <= 0:
            raise ValueError("kill-switch version must be positive")


@dataclass(frozen=True, slots=True)
class KillSwitchActivatedNotification:
    code: str
    severity: str
    message: str
