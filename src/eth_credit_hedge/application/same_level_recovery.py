"""Persistence-first same-level FULL_NEXT_TP recovery submission."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal

from eth_credit_hedge.application.one_level_entry import OneLevelEntryService
from eth_credit_hedge.core.virtual_levels import HedgeLevel
from eth_credit_hedge.domain.execution import (
    PlaceOrderRequest,
    UncertainOrderOutcomeError,
)
from eth_credit_hedge.domain.instruments import InstrumentSpec
from eth_credit_hedge.domain.live_execution import EntryExecutionSnapshot
from eth_credit_hedge.domain.live_recovery import (
    RecoveryDebtSnapshot,
    RecoveryDebtState,
    RecoveryEntryPlan,
    SameLevelRecoveryPlanner,
    add_confirmed_stop_debt,
    allocate_confirmed_debt,
    release_allocated_debt,
    settle_recovery_take_profit,
)
from eth_credit_hedge.domain.risk import RiskLimits, RiskState
from eth_credit_hedge.ports.persistence import ExecutionPersistencePort
from eth_credit_hedge.ports.control import EntryGatePort


@dataclass(frozen=True, slots=True)
class RecoverySubmission:
    plan: RecoveryEntryPlan
    entry_snapshot: EntryExecutionSnapshot | None
    debt_snapshot: RecoveryDebtSnapshot


class SameLevelRecoveryService:
    def __init__(
        self,
        *,
        entry_service: OneLevelEntryService,
        store: ExecutionPersistencePort,
        planner: SameLevelRecoveryPlanner,
        clock: Callable[[], datetime],
        entry_gate: EntryGatePort | None = None,
    ) -> None:
        self._entry_service = entry_service
        self._store = store
        self._planner = planner
        self._clock = clock
        self._entry_gate = entry_gate

    async def record_confirmed_stop_debt(
        self,
        *,
        level_id: int,
        actual_stop_debt: Decimal,
        projected_debt: Decimal,
    ) -> RecoveryDebtSnapshot:
        current = await self._store.load_recovery_debt_snapshot(level_id)
        if current is None:
            debt = add_confirmed_stop_debt(
                RecoveryDebtState.empty(projected_debt=projected_debt),
                actual_stop_debt,
            )
            snapshot = RecoveryDebtSnapshot(
                level_id=level_id,
                debt=debt,
                version=1,
                updated_at=self._clock(),
            )
            await self._store.persist_recovery_debt_snapshot(snapshot)
            return snapshot
        debt_with_projection = replace(
            current.debt,
            projected_debt=projected_debt,
        )
        updated = replace(
            current,
            debt=add_confirmed_stop_debt(
                debt_with_projection,
                actual_stop_debt,
            ),
            version=current.version + 1,
            updated_at=self._clock(),
        )
        await self._store.transition_recovery_debt_snapshot(
            current.version,
            updated,
        )
        return updated

    async def submit_recovery(
        self,
        *,
        level: HedgeLevel,
        instrument: InstrumentSpec,
        risk_state: RiskState,
        limits: RiskLimits,
        order_link_id: str,
        before_persisted_submission: (
            Callable[[RecoveryEntryPlan], Awaitable[None]] | None
        ) = None,
    ) -> RecoverySubmission:
        debt_snapshot = await self._store.load_recovery_debt_snapshot(
            level.level_id
        )
        if debt_snapshot is None:
            raise ValueError("recovery debt snapshot does not exist")
        plan = self._planner.plan(
            level,
            debt_snapshot.debt,
            instrument,
            risk_state,
            limits,
        )
        if (
            plan.approved
            and self._entry_gate is not None
            and not self._entry_gate.entries_allowed
        ):
            plan = replace(
                plan,
                approved=False,
                quantity=None,
                expected_take_profit=Decimal("0"),
                allocated_debt=Decimal("0"),
                reasons=plan.reasons + ("kill switch blocks new entries",),
                locked_action=None,
            )
        if not plan.approved or plan.quantity is None:
            return RecoverySubmission(plan, None, debt_snapshot)

        if before_persisted_submission is not None:
            await before_persisted_submission(plan)

        request = PlaceOrderRequest(
            category="linear",
            symbol="ETHUSDT",
            side="Sell",
            order_type="Market",
            quantity=plan.quantity,
            order_link_id=order_link_id,
            time_in_force="IOC",
            reduce_only=False,
            position_idx=0,
        )
        persisted_entry = await self._entry_service.persist_entry_intent(request)
        allocated = replace(
            debt_snapshot,
            debt=allocate_confirmed_debt(debt_snapshot.debt),
            version=debt_snapshot.version + 1,
            updated_at=self._clock(),
        )
        await self._store.transition_recovery_debt_snapshot(
            debt_snapshot.version,
            allocated,
        )
        try:
            entry_snapshot = await self._entry_service.submit_persisted_entry(
                request,
                persisted_entry,
            )
        except UncertainOrderOutcomeError:
            raise
        except Exception:
            released = replace(
                allocated,
                debt=release_allocated_debt(allocated.debt),
                version=allocated.version + 1,
                updated_at=self._clock(),
            )
            await self._store.transition_recovery_debt_snapshot(
                allocated.version,
                released,
            )
            raise
        return RecoverySubmission(plan, entry_snapshot, allocated)

    async def settle_take_profit(
        self,
        *,
        level_id: int,
        realized_take_profit: Decimal,
        zone_budget: Decimal,
    ) -> RecoveryDebtSnapshot:
        current = await self._store.load_recovery_debt_snapshot(level_id)
        if current is None:
            raise ValueError("recovery debt snapshot does not exist")
        settled = replace(
            current,
            debt=settle_recovery_take_profit(
                current.debt,
                realized_take_profit=realized_take_profit,
                zone_budget=zone_budget,
            ),
            version=current.version + 1,
            updated_at=self._clock(),
        )
        await self._store.transition_recovery_debt_snapshot(
            current.version,
            settled,
        )
        return settled

    async def record_recovery_stop_debt(
        self,
        *,
        level_id: int,
        actual_stop_debt: Decimal,
        projected_debt: Decimal,
    ) -> RecoveryDebtSnapshot:
        current = await self._store.load_recovery_debt_snapshot(level_id)
        if current is None:
            raise ValueError("recovery debt snapshot does not exist")
        if current.debt.allocated_debt == Decimal("0"):
            raise ValueError("recovery stop requires explicitly allocated debt")
        released = release_allocated_debt(current.debt)
        stopped = replace(
            current,
            debt=add_confirmed_stop_debt(
                replace(released, projected_debt=projected_debt),
                actual_stop_debt,
            ),
            version=current.version + 1,
            updated_at=self._clock(),
        )
        await self._store.transition_recovery_debt_snapshot(
            current.version,
            stopped,
        )
        return stopped
