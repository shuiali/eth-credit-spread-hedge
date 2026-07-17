"""Durable event and snapshot boundary for the authoritative accounting ledger."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from eth_credit_hedge.domain.accounting.events import AccountingEvent
from eth_credit_hedge.domain.accounting.reconciliation import (
    AccountingReconciliationReport,
)
from eth_credit_hedge.domain.accounting.reconstruction import CombinedLedgerState
from eth_credit_hedge.domain.strategy_math.units import Price


class AccountingLedgerPersistencePort(Protocol):
    async def append_event_and_snapshot(
        self,
        event: AccountingEvent,
        state: CombinedLedgerState,
        *,
        hedge_mark: Price | None,
        hedge_liquidation: Price | None,
    ) -> int: ...

    async def load_events_after(self, sequence: int) -> tuple[AccountingEvent, ...]: ...

    async def persist_reconciliation_result(
        self,
        report: AccountingReconciliationReport,
        ledger_digest: str,
        recorded_at: datetime,
    ) -> None: ...
