"""Classification and explicit repair actions for startup differences."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ReconciliationStatus(str, Enum):
    MATCHED = "MATCHED"
    REPAIRABLE = "REPAIRABLE"
    AMBIGUOUS = "AMBIGUOUS"
    DANGEROUS = "DANGEROUS"


class StateDifferenceKind(str, Enum):
    UNKNOWN_EXCHANGE_ORDER = "UNKNOWN_EXCHANGE_ORDER"
    MISSING_EXCHANGE_ORDER = "MISSING_EXCHANGE_ORDER"
    PROTECTION_MISMATCH = "PROTECTION_MISMATCH"
    MISSING_PROTECTION = "MISSING_PROTECTION"
    MISSING_TAKE_PROFIT = "MISSING_TAKE_PROFIT"
    POSITION_MISMATCH = "POSITION_MISMATCH"
    UNKNOWN_EXCHANGE_POSITION = "UNKNOWN_EXCHANGE_POSITION"
    UNKNOWN_OPTION_POSITION = "UNKNOWN_OPTION_POSITION"
    MISSING_LOCAL_EXECUTION = "MISSING_LOCAL_EXECUTION"
    UNKNOWN_EXCHANGE_EXECUTION = "UNKNOWN_EXCHANGE_EXECUTION"


class RepairActionKind(str, Enum):
    IMPORT_ORDER = "IMPORT_ORDER"
    RESTORE_PROTECTION = "RESTORE_PROTECTION"
    RESTORE_TAKE_PROFIT = "RESTORE_TAKE_PROFIT"
    IMPORT_EXECUTION = "IMPORT_EXECUTION"


@dataclass(frozen=True, slots=True)
class StateDifference:
    kind: StateDifferenceKind
    detail: str
    order_link_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", StateDifferenceKind(self.kind))
        if not self.detail.strip():
            raise ValueError("difference detail cannot be empty")


@dataclass(frozen=True, slots=True)
class RepairAction:
    kind: RepairActionKind
    detail: str
    order_link_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", RepairActionKind(self.kind))
        if not self.detail.strip():
            raise ValueError("repair detail cannot be empty")


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    status: ReconciliationStatus
    differences: tuple[StateDifference, ...]
    repair_actions: tuple[RepairAction, ...]
    trading_allowed: bool

    def __post_init__(self) -> None:
        status = ReconciliationStatus(self.status)
        differences = tuple(self.differences)
        actions = tuple(self.repair_actions)
        if type(self.trading_allowed) is not bool:
            raise ValueError("trading allowed must be boolean")
        if self.trading_allowed != (status is ReconciliationStatus.MATCHED):
            raise ValueError("only MATCHED reconciliation may allow trading")
        if status is ReconciliationStatus.MATCHED and (differences or actions):
            raise ValueError("MATCHED reconciliation cannot contain differences")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "differences", differences)
        object.__setattr__(self, "repair_actions", actions)
