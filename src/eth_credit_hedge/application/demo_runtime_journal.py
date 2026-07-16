"""Persistence boundary for the integrated demo runtime reducer."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Self
from uuid import uuid4

from eth_credit_hedge.application.demo_runtime_state import (
    DemoRuntimeState,
    demo_runtime_state_from_payload,
    demo_runtime_state_to_payload,
    reduce_demo_runtime_state,
)
from eth_credit_hedge.application.startup_replay import StartupReplayService
from eth_credit_hedge.domain.journal import (
    CycleSnapshot,
    JournalEvent,
    JournalEventType,
    PendingJournalEvent,
)
from eth_credit_hedge.ports.journal import JournalPersistencePort


EventIdFactory = Callable[[], str]


class DemoRuntimeJournal:
    """Atomically append one event and the exact state it produces."""

    def __init__(
        self,
        *,
        store: JournalPersistencePort,
        state: DemoRuntimeState,
        last_event_sequence: int,
        clock: Callable[[], datetime],
        event_id_factory: EventIdFactory | None = None,
    ) -> None:
        if last_event_sequence < 0:
            raise ValueError("last event sequence cannot be negative")
        self._store = store
        self._state = state
        self._last_event_sequence = last_event_sequence
        self._clock = clock
        self._event_id_factory = event_id_factory or (lambda: uuid4().hex)
        self._lock = asyncio.Lock()

    @property
    def state(self) -> DemoRuntimeState:
        return self._state

    @property
    def last_event_sequence(self) -> int:
        return self._last_event_sequence

    @classmethod
    async def create(
        cls,
        *,
        store: JournalPersistencePort,
        state: DemoRuntimeState,
        clock: Callable[[], datetime],
        event_id_factory: EventIdFactory | None = None,
    ) -> Self:
        journal = cls(
            store=store,
            state=state,
            last_event_sequence=0,
            clock=clock,
            event_id_factory=event_id_factory,
        )
        event_id = journal._event_id_factory()
        pending = PendingJournalEvent(
            event_id=event_id,
            event_type=JournalEventType.STRATEGY_CYCLE_CREATED,
            event_version=1,
            cycle_id=state.cycle_id,
            level_id=None,
            timestamp_utc=clock(),
            payload={"runtime_state": demo_runtime_state_to_payload(state)},
            causation_id=None,
            correlation_id=state.cycle_id,
        )
        await journal._commit(pending, previous_payload={})
        return journal

    @classmethod
    async def restore(
        cls,
        *,
        store: JournalPersistencePort,
        cycle_id: str,
        clock: Callable[[], datetime],
        event_id_factory: EventIdFactory | None = None,
    ) -> Self:
        result = await StartupReplayService(
            store=store,
            reducer=reduce_demo_runtime_state,
        ).rebuild(cycle_id)
        if not result.state:
            raise ValueError(f"runtime cycle does not exist: {cycle_id}")
        return cls(
            store=store,
            state=demo_runtime_state_from_payload(result.state),
            last_event_sequence=result.last_event_sequence,
            clock=clock,
            event_id_factory=event_id_factory,
        )

    async def append(
        self,
        event_type: JournalEventType,
        *,
        payload: dict[str, object],
        level_id: int | None = None,
        event_id: str | None = None,
        causation_id: str | None = None,
        correlation_id: str | None = None,
    ) -> DemoRuntimeState:
        async with self._lock:
            resolved_event_id = event_id or self._event_id_factory()
            if resolved_event_id in self._state.processed_event_ids:
                return self._state
            pending = PendingJournalEvent(
                event_id=resolved_event_id,
                event_type=event_type,
                event_version=1,
                cycle_id=self._state.cycle_id,
                level_id=level_id,
                timestamp_utc=self._clock(),
                payload=payload,
                causation_id=causation_id,
                correlation_id=correlation_id or self._state.cycle_id,
            )
            await self._commit(
                pending,
                previous_payload=demo_runtime_state_to_payload(self._state),
            )
            return self._state

    async def _commit(
        self,
        event: PendingJournalEvent,
        *,
        previous_payload: dict[str, object],
    ) -> None:
        provisional = JournalEvent(
            sequence=max(1, self._last_event_sequence + 1),
            event_id=event.event_id,
            event_type=event.event_type,
            event_version=event.event_version,
            cycle_id=event.cycle_id,
            level_id=event.level_id,
            timestamp_utc=event.timestamp_utc,
            payload=event.payload,
            causation_id=event.causation_id,
            correlation_id=event.correlation_id,
        )
        next_payload = reduce_demo_runtime_state(previous_payload, provisional)
        snapshot = CycleSnapshot(
            cycle_id=event.cycle_id,
            last_event_sequence=self._last_event_sequence,
            snapshot_version=1,
            state=next_payload,
            updated_at_utc=event.timestamp_utc,
        )
        stored_event, _ = await self._store.append_event_and_snapshot(
            event,
            snapshot,
        )
        self._state = demo_runtime_state_from_payload(next_payload)
        self._last_event_sequence = stored_event.sequence


__all__ = ["DemoRuntimeJournal"]
