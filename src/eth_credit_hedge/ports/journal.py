"""Persistence port for versioned cycle events and restart snapshots."""

from __future__ import annotations

from typing import Protocol

from eth_credit_hedge.domain.journal import (
    CycleSnapshot,
    JournalEvent,
    PendingJournalEvent,
)


class JournalPersistencePort(Protocol):
    async def append_event(
        self,
        event: PendingJournalEvent,
    ) -> JournalEvent: ...

    async def append_event_and_snapshot(
        self,
        event: PendingJournalEvent,
        snapshot: CycleSnapshot,
    ) -> tuple[JournalEvent, CycleSnapshot]: ...

    async def load_latest_snapshot(
        self,
        cycle_id: str,
    ) -> CycleSnapshot | None: ...

    async def load_events_after(
        self,
        cycle_id: str,
        sequence: int,
    ) -> tuple[JournalEvent, ...]: ...
