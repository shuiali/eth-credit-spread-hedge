"""Versioned event journal, restart replay, and transaction rollback tests."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from eth_credit_hedge.application.startup_replay import StartupReplayService
from eth_credit_hedge.domain.journal import (
    CycleSnapshot,
    JournalEvent,
    JournalEventType,
    PendingJournalEvent,
)
from eth_credit_hedge.infrastructure.persistence.sqlite_journal_store import (
    SqliteJournalStore,
)


NOW = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)


def event(
    event_id: str,
    event_type: JournalEventType,
    *,
    payload: dict[str, object] | None = None,
) -> PendingJournalEvent:
    return PendingJournalEvent(
        event_id=event_id,
        event_type=event_type,
        event_version=1,
        cycle_id="cycle-0001",
        level_id=1,
        timestamp_utc=NOW,
        payload={} if payload is None else payload,
        causation_id=None,
        correlation_id="correlation-1",
    )


def snapshot(*, trading_allowed: bool = True) -> CycleSnapshot:
    return CycleSnapshot(
        cycle_id="cycle-0001",
        last_event_sequence=0,
        state={
            "option_position": {"state": "OPEN", "quantity": Decimal("0.01")},
            "levels": [{"level_id": 1, "state": "ACTIVE_PROTECTED"}],
            "orders": [STOP_ORDER],
            "positions": [{"symbol": "ETHUSDT", "quantity": Decimal("0.01")}],
            "projected_recovery_debt": Decimal("0"),
            "confirmed_recovery_debt": Decimal("0.0338"),
            "allocated_recovery_debt": Decimal("0"),
            "remaining_recovery_debt": Decimal("0.0338"),
            "stop_budget": Decimal("5"),
            "trading_allowed": trading_allowed,
        },
        snapshot_version=1,
        updated_at_utc=NOW,
    )


STOP_ORDER = {
    "order_link_id": "ECH-01-C0001-L01-STOP-A01-9F3C",
    "status": "Untriggered",
}


def test_append_and_snapshot_are_decimal_exact_and_restartable(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        database = tmp_path / "journal.sqlite3"
        store = SqliteJournalStore(database)
        await store.initialize()
        stored_event, stored_snapshot = await store.append_event_and_snapshot(
            event(
                "event-1",
                JournalEventType.PROTECTION_CONFIRMED,
                payload={"trigger_price": Decimal("3004.5")},
            ),
            snapshot(),
        )

        assert stored_event.sequence == 1
        assert stored_event.payload == {"trigger_price": "3004.5"}
        assert stored_snapshot.last_event_sequence == 1
        assert stored_snapshot.state["confirmed_recovery_debt"] == "0.0338"

        await store.append_event(
            event(
                "event-2",
                JournalEventType.TRADING_SUSPENDED,
                payload={"reason": "private stream disconnected"},
            )
        )

        restarted = SqliteJournalStore(database)
        await restarted.initialize()
        replay = StartupReplayService(store=restarted, reducer=reduce_event)
        rebuilt = await replay.rebuild("cycle-0001")

        assert rebuilt.last_event_sequence == 2
        assert rebuilt.state["trading_allowed"] is False
        assert rebuilt.state["suspension_reason"] == "private stream disconnected"
        assert rebuilt.replayed_event_count == 1

    asyncio.run(exercise())


def reduce_event(
    state: dict[str, object],
    journal_event: JournalEvent,
) -> dict[str, object]:
    updated = dict(state)
    if journal_event.event_type is JournalEventType.TRADING_SUSPENDED:
        updated["trading_allowed"] = False
        updated["suspension_reason"] = journal_event.payload["reason"]
    return updated


def test_snapshot_failure_rolls_back_event_insert(tmp_path: Path) -> None:
    async def exercise() -> None:
        database = tmp_path / "journal.sqlite3"
        store = SqliteJournalStore(database)
        await store.initialize()
        with sqlite3.connect(database) as connection:
            connection.execute(
                """
                CREATE TRIGGER fail_cycle_snapshot
                BEFORE INSERT ON cycle_snapshots
                BEGIN
                    SELECT RAISE(ABORT, 'snapshot failure');
                END
                """
            )

        with pytest.raises(sqlite3.IntegrityError, match="snapshot failure"):
            await store.append_event_and_snapshot(
                event("event-rollback", JournalEventType.STRATEGY_CYCLE_CREATED),
                snapshot(),
            )

        assert await store.load_events_after("cycle-0001", 0) == ()
        assert await store.load_latest_snapshot("cycle-0001") is None

    asyncio.run(exercise())


def test_event_ids_are_idempotency_keys(tmp_path: Path) -> None:
    async def exercise() -> None:
        store = SqliteJournalStore(tmp_path / "journal.sqlite3")
        await store.initialize()
        first = event("same-event", JournalEventType.EXECUTION_RECEIVED)
        await store.append_event(first)

        with pytest.raises(ValueError, match="event ID already exists"):
            await store.append_event(
                PendingJournalEvent(
                    event_id=first.event_id,
                    event_type=first.event_type,
                    event_version=first.event_version,
                    cycle_id=first.cycle_id,
                    level_id=first.level_id,
                    timestamp_utc=first.timestamp_utc + timedelta(seconds=1),
                    payload=first.payload,
                    causation_id=first.causation_id,
                    correlation_id=first.correlation_id,
                )
            )

        events = await store.load_events_after("cycle-0001", 0)
        assert len(events) == 1

    asyncio.run(exercise())
