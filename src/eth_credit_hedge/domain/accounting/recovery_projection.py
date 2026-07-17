"""Immutable ledger-derived recovery-debt ownership projections."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from eth_credit_hedge.domain.accounting.errors import AccountingContractError
from eth_credit_hedge.domain.accounting.fills import required_text
from eth_credit_hedge.domain.strategy_math.units import Money


class AttemptRecoveryStatus(str, Enum):
    OUTSTANDING = "OUTSTANDING"
    SETTLED = "SETTLED"


@dataclass(frozen=True, slots=True)
class HedgeAttemptKey:
    cycle_id: str
    level_id: int
    attempt: int

    def __post_init__(self) -> None:
        required_text(self.cycle_id, "recovery cycle ID")
        if not isinstance(self.level_id, int) or self.level_id <= 0:
            raise AccountingContractError("recovery level ID must be positive")
        if not isinstance(self.attempt, int) or self.attempt <= 0:
            raise AccountingContractError("recovery attempt must be positive")


@dataclass(frozen=True, slots=True)
class AttemptRecoveryProjection:
    key: HedgeAttemptKey
    debt_increments: Money
    recovery_allocations: Money
    remaining_debt: Money
    status: AttemptRecoveryStatus
    source_event_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not all(isinstance(value, Money) for value in (
            self.debt_increments,
            self.recovery_allocations,
            self.remaining_debt,
        )):
            raise AccountingContractError("recovery projection values must be Money")
        if any(value.value < 0 for value in (
            self.debt_increments,
            self.recovery_allocations,
            self.remaining_debt,
        )):
            raise AccountingContractError("recovery projection values cannot be negative")
        if self.remaining_debt.value != (
            self.debt_increments.value - self.recovery_allocations.value
        ):
            raise AccountingContractError("recovery projection debt identity failed")
        ids = tuple(self.source_event_ids)
        if not ids or len(ids) != len(set(ids)):
            raise AccountingContractError("recovery projection source event IDs are invalid")
        if self.status is AttemptRecoveryStatus.SETTLED and self.remaining_debt.value:
            raise AccountingContractError("settled recovery projection has remaining debt")
        object.__setattr__(self, "source_event_ids", ids)


@dataclass(frozen=True, slots=True)
class LevelRecoveryProjection:
    cycle_id: str
    level_id: int
    attempt_projections: tuple[AttemptRecoveryProjection, ...]
    total_remaining_debt: Money

    def __post_init__(self) -> None:
        required_text(self.cycle_id, "recovery cycle ID")
        if not isinstance(self.level_id, int) or self.level_id <= 0:
            raise AccountingContractError("recovery level ID must be positive")
        attempts = tuple(self.attempt_projections)
        if not attempts:
            raise AccountingContractError("level recovery projection needs an attempt")
        if any(
            attempt.key.cycle_id != self.cycle_id or attempt.key.level_id != self.level_id
            for attempt in attempts
        ):
            raise AccountingContractError("attempt projection belongs to another level")
        if len({attempt.key for attempt in attempts}) != len(attempts):
            raise AccountingContractError("level recovery projection has duplicate attempts")
        if self.total_remaining_debt.value != sum(
            (attempt.remaining_debt.value for attempt in attempts),
            start=0,
        ):
            raise AccountingContractError("level recovery total debt identity failed")
        object.__setattr__(self, "attempt_projections", attempts)


class RecoveryDebtProjectionPort:
    def debt_for_attempt(self, key: HedgeAttemptKey) -> Money:
        raise NotImplementedError

    def debt_for_level(self, cycle_id: str, level_id: int) -> Money:
        raise NotImplementedError

    def projection_for_level(
        self, cycle_id: str, level_id: int
    ) -> LevelRecoveryProjection | None:
        raise NotImplementedError
