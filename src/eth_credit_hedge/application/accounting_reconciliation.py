"""Startup reconstruction and reconciliation for the authoritative accounting ledger."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Protocol

from eth_credit_hedge.domain.accounting.events import MigratedFromLegacySnapshot
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
