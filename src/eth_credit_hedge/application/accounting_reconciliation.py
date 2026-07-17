"""Startup reconstruction and reconciliation for the authoritative accounting ledger."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Protocol

from eth_credit_hedge.domain.accounting.events import (
    AccountingEvent,
    EventSource,
    ExitReason,
    HedgeExecutionRecorded,
    HedgeRole,
    MigratedFromLegacySnapshot,
    RecoveryAllocationRecorded,
    RecoveryDebtChanged,
    RecoveryDebtIncremented,
)
from eth_credit_hedge.domain.accounting.recovery_projection import HedgeAttemptKey
from eth_credit_hedge.domain.strategy_math.units import Money
from eth_credit_hedge.domain.accounting.reconciliation import (
    AccountingExchangeState,
    AccountingReconciliationReport,
    evaluate_accounting_reconciliation,
)
from eth_credit_hedge.domain.accounting.reconstruction import (
    CombinedLedgerReconstructor,
    CombinedLedgerState,
)
from eth_credit_hedge.ports.accounting_persistence import AccountingLedgerPersistencePort


class AccountingStateReaderPort(Protocol):
    async def capture_accounting_state(self) -> AccountingExchangeState | None: ...


class AccountingReplayPort(AccountingLedgerPersistencePort, Protocol):
    async def replay(
        self,
        reconstructor: CombinedLedgerReconstructor,
    ) -> "AccountingReplayResultPort": ...

    async def legacy_migration_pending(self) -> bool: ...


class AccountingReplayResultPort(Protocol):
    state: CombinedLedgerState
    full_replay_digest: str
    snapshot_tail_digest: str
    snapshot_digest_matches: bool


@dataclass(frozen=True, slots=True)
class AccountingStartupReconciliationResult:
    state: CombinedLedgerState
    report: AccountingReconciliationReport
    replay_digest_matches: bool


class AccountingReconciliationService:
    """The sole reconciliation authority for persisted M2 accounting facts."""

    def __init__(
        self,
        *,
        store: AccountingReplayPort,
        reconstructor: CombinedLedgerReconstructor,
        state_reader: AccountingStateReaderPort,
        clock: Callable[[], datetime],
    ) -> None:
        self._store = store
        self._reconstructor = reconstructor
        self._state_reader = state_reader
        self._clock = clock

    async def reconcile(self) -> AccountingStartupReconciliationResult:
        replay = await self._store.replay(self._reconstructor)
        state = replay.state
        replay_digest_matches = (
            replay.full_replay_digest == replay.snapshot_tail_digest
            and replay.snapshot_digest_matches
        )
        report = evaluate_accounting_reconciliation(
            state,
            await self._state_reader.capture_accounting_state(),
            replay_digest_matches=replay_digest_matches,
            legacy_migration_pending=await self._store.legacy_migration_pending(),
            events=await self._store.load_events_after(0),
        )
        await self._store.persist_reconciliation_result(
            report,
            state.ledger_digest,
            self._clock(),
        )
        return AccountingStartupReconciliationResult(
            state=state,
            report=report,
            replay_digest_matches=replay_digest_matches,
        )


class LegacyAccountingMigrator:
    """Persist an explicit migration marker without synthesizing executions."""

    def __init__(
        self,
        *,
        store: AccountingLedgerPersistencePort,
        reconstructor: CombinedLedgerReconstructor,
    ) -> None:
        self._store = store
        self._reconstructor = reconstructor

    async def migrate(
        self,
        event: MigratedFromLegacySnapshot,
    ) -> CombinedLedgerState:
        events = await self._store.load_events_after(0)
        state = self._reconstructor.reconstruct((*events, event))
        await self._store.append_event_and_snapshot(
            event,
            state,
            hedge_mark=None,
            hedge_liquidation=None,
        )
        return state


class LegacyRecoveryDebtMigrator:
    """One-way v1 implicit-stop-debt to v2 keyed-event migration."""

    def __init__(
        self, *, store: AccountingLedgerPersistencePort, reconstructor: CombinedLedgerReconstructor
    ) -> None:
        self._store = store
        self._reconstructor = reconstructor

    async def migrate(self) -> CombinedLedgerState:
        events = await self._store.load_events_after(0)
        if any(isinstance(event, RecoveryDebtIncremented) for event in events):
            return self._reconstructor.reconstruct(events)
        legacy = self._reconstructor.reconstruct(events)
        additions: list[AccountingEvent] = []
        seen_targets: set[HedgeAttemptKey] = set()
        for lot in legacy.hedge.lots:
            if lot.debt_increment.value == 0:
                continue
            target = HedgeAttemptKey(lot.cycle_id, lot.level_id, lot.attempt)
            if target in seen_targets:
                raise RuntimeError("ambiguous legacy recovery debt ownership")
            seen_targets.add(target)
            source = tuple(
                event.execution_id for event in events
                if isinstance(event, HedgeExecutionRecorded) and event.lot_id == lot.lot_id
                and event.exit_reason is ExitReason.STOP
                and event.execution_id is not None
            )
            if not source:
                raise RuntimeError("legacy stopped lot has no execution ownership")
            finalization = f"hedge-finalized:{lot.lot_id}"
            additions.append(RecoveryDebtIncremented(
                event_id=(f"recovery-debt:{lot.cycle_id}:{lot.level_id}:{lot.attempt}:{finalization}"),
                event_version=2, cycle_id=lot.cycle_id, level_id=lot.level_id,
                timestamp=legacy.as_of, source=EventSource.LEGACY_DEBT_MIGRATION,
                correlation_id=finalization,
                target=target,
                source_hedge_lot_id=lot.lot_id, source_stop_execution_ids=source,
                amount=lot.debt_increment,
            ))
        legacy_allocations = tuple(
            event
            for event in events
            if isinstance(event, RecoveryDebtChanged)
            and event.actual_recovery_allocation.value > 0
        )
        debt_additions = tuple(
            event for event in additions if isinstance(event, RecoveryDebtIncremented)
        )
        if legacy_allocations and len(debt_additions) != 1:
            raise RuntimeError("ambiguous legacy recovery allocation ownership")
        if legacy_allocations:
            target = debt_additions[0].target
            recovery_lots = tuple(
                lot
                for lot in legacy.hedge.lots
                if lot.role is HedgeRole.RECOVERY
                and lot.level_id == target.level_id
                and lot.net_realized_pnl.value > 0
            )
            if len(recovery_lots) != 1:
                raise RuntimeError("ambiguous legacy recovery allocation ownership")
            recovery_lot = recovery_lots[0]
            for allocation in legacy_allocations:
                additions.append(
                    RecoveryAllocationRecorded(
                        event_id=(
                            "recovery-allocation-migration:"
                            f"{allocation.event_id}:{target.cycle_id}:"
                            f"{target.level_id}:{target.attempt}"
                        ),
                        event_version=2,
                        cycle_id=target.cycle_id,
                        level_id=target.level_id,
                        timestamp=allocation.timestamp,
                        source=EventSource.LEGACY_DEBT_MIGRATION,
                        correlation_id=allocation.event_id,
                        target=target,
                        recovery_hedge_lot_id=recovery_lot.lot_id,
                        gross_realized_recovery_profit=recovery_lot.gross_realized_pnl,
                        fees=Money(
                            recovery_lot.entry_fees.value
                            + recovery_lot.exit_fees.value
                        ),
                        funding=recovery_lot.allocated_funding,
                        allocated_amount=allocation.actual_recovery_allocation,
                    )
                )
        if not additions:
            return legacy
        marker = MigratedFromLegacySnapshot(
            event_id="legacy-recovery-debt-migration:v2",
            event_version=2,
            cycle_id="legacy-recovery-debt",
            timestamp=legacy.as_of,
            source=EventSource.LEGACY_DEBT_MIGRATION,
            correlation_id="legacy-recovery-debt-migration:v2",
            legacy_snapshot_type="RecoveryDebt",
            legacy_snapshot_key="implicit-stopped-hedge-lots",
            legacy_payload_digest=hashlib.sha256(
                "|".join(event.event_id for event in additions).encode("utf-8")
            ).hexdigest(),
        )
        migration_events = (marker, *additions)
        state = self._reconstructor.reconstruct((*events, *migration_events))
        await self._store.append_events_and_snapshot(
            migration_events, state, hedge_mark=None, hedge_liquidation=None,
        )
        return state
