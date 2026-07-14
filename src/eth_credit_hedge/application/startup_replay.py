"""Load the latest cycle snapshot and replay every later event in order."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from eth_credit_hedge.domain.journal import JournalEvent
from eth_credit_hedge.ports.journal import JournalPersistencePort


JournalReducer = Callable[[dict[str, object], JournalEvent], dict[str, object]]


@dataclass(frozen=True, slots=True)
class StartupReplayResult:
    cycle_id: str
    state: dict[str, object]
    last_event_sequence: int
    replayed_event_count: int


class StartupReplayService:
    def __init__(
        self,
        *,
        store: JournalPersistencePort,
        reducer: JournalReducer,
    ) -> None:
        self._store = store
        self._reducer = reducer

    async def rebuild(self, cycle_id: str) -> StartupReplayResult:
        snapshot = await self._store.load_latest_snapshot(cycle_id)
        state = {} if snapshot is None else dict(snapshot.state)
        last_sequence = 0 if snapshot is None else snapshot.last_event_sequence
        events = await self._store.load_events_after(cycle_id, last_sequence)
        for event in events:
            if event.sequence <= last_sequence:
                raise ValueError("journal replay sequence is not strictly increasing")
            state = self._reducer(state, event)
            last_sequence = event.sequence
        return StartupReplayResult(
            cycle_id=cycle_id,
            state=state,
            last_event_sequence=last_sequence,
            replayed_event_count=len(events),
        )
