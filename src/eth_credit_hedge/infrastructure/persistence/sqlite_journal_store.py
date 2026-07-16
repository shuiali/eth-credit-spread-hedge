"""SQLite event journal with atomic event-plus-snapshot commits."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from eth_credit_hedge.domain.journal import (
    CycleSnapshot,
    JournalEvent,
    JournalEventType,
    PendingJournalEvent,
    canonical_json,
    canonical_object,
)


_MIGRATION = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_journal (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    event_version INTEGER NOT NULL,
    cycle_id TEXT NOT NULL,
    level_id INTEGER,
    timestamp_utc TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    causation_id TEXT,
    correlation_id TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS event_journal_cycle_sequence
ON event_journal(cycle_id, sequence);

CREATE TABLE IF NOT EXISTS cycle_snapshots (
    cycle_id TEXT NOT NULL,
    last_event_sequence INTEGER NOT NULL REFERENCES event_journal(sequence),
    snapshot_version INTEGER NOT NULL,
    state_json TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    PRIMARY KEY(cycle_id, last_event_sequence)
);

INSERT OR IGNORE INTO schema_migrations(version, applied_at)
VALUES (3, strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'));
"""


class SqliteJournalStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize)

    async def schema_version(self) -> int:
        return await asyncio.to_thread(self._schema_version)

    async def append_event(
        self,
        event: PendingJournalEvent,
    ) -> JournalEvent:
        return await asyncio.to_thread(self._append_event, event)

    async def append_event_and_snapshot(
        self,
        event: PendingJournalEvent,
        snapshot: CycleSnapshot,
    ) -> tuple[JournalEvent, CycleSnapshot]:
        if event.cycle_id != snapshot.cycle_id:
            raise ValueError("event and snapshot cycle IDs differ")
        return await asyncio.to_thread(
            self._append_event_and_snapshot,
            event,
            snapshot,
        )

    async def load_latest_snapshot(
        self,
        cycle_id: str,
    ) -> CycleSnapshot | None:
        return await asyncio.to_thread(self._load_latest_snapshot, cycle_id)

    async def load_events_after(
        self,
        cycle_id: str,
        sequence: int,
    ) -> tuple[JournalEvent, ...]:
        if sequence < 0:
            raise ValueError("event sequence cannot be negative")
        return await asyncio.to_thread(self._load_events_after, cycle_id, sequence)

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_MIGRATION)

    def _schema_version(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT MAX(version) AS version FROM schema_migrations"
            ).fetchone()
        if row is None or row["version"] is None:
            return 0
        return int(row["version"])

    def _append_event(self, event: PendingJournalEvent) -> JournalEvent:
        try:
            with self._connect() as connection:
                return _insert_event(connection, event)
        except sqlite3.IntegrityError as exc:
            if "event_journal.event_id" in str(exc):
                raise ValueError("event ID already exists") from exc
            raise

    def _append_event_and_snapshot(
        self,
        event: PendingJournalEvent,
        snapshot: CycleSnapshot,
    ) -> tuple[JournalEvent, CycleSnapshot]:
        try:
            with self._connect() as connection:
                stored_event = _insert_event(connection, event)
                stored_snapshot = replace(
                    snapshot,
                    last_event_sequence=stored_event.sequence,
                )
                connection.execute(
                    """
                    INSERT INTO cycle_snapshots(
                        cycle_id, last_event_sequence, snapshot_version,
                        state_json, updated_at_utc
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        stored_snapshot.cycle_id,
                        stored_snapshot.last_event_sequence,
                        stored_snapshot.snapshot_version,
                        canonical_json(stored_snapshot.state),
                        stored_snapshot.updated_at_utc.isoformat(),
                    ),
                )
                return stored_event, stored_snapshot
        except sqlite3.IntegrityError as exc:
            if "event_journal.event_id" in str(exc):
                raise ValueError("event ID already exists") from exc
            raise

    def _load_latest_snapshot(self, cycle_id: str) -> CycleSnapshot | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM cycle_snapshots
                WHERE cycle_id = ?
                ORDER BY last_event_sequence DESC
                LIMIT 1
                """,
                (cycle_id,),
            ).fetchone()
        if row is None:
            return None
        return CycleSnapshot(
            cycle_id=str(row["cycle_id"]),
            last_event_sequence=int(row["last_event_sequence"]),
            state=_parse_object(str(row["state_json"])),
            snapshot_version=int(row["snapshot_version"]),
            updated_at_utc=datetime.fromisoformat(str(row["updated_at_utc"])),
        )

    def _load_events_after(
        self,
        cycle_id: str,
        sequence: int,
    ) -> tuple[JournalEvent, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM event_journal
                WHERE cycle_id = ? AND sequence > ?
                ORDER BY sequence ASC
                """,
                (cycle_id, sequence),
            ).fetchall()
        return tuple(_event_from_row(row) for row in rows)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection


def _insert_event(
    connection: sqlite3.Connection,
    event: PendingJournalEvent,
) -> JournalEvent:
    cursor = connection.execute(
        """
        INSERT INTO event_journal(
            event_id, event_type, event_version, cycle_id, level_id,
            timestamp_utc, payload_json, causation_id, correlation_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event.event_id,
            event.event_type.value,
            event.event_version,
            event.cycle_id,
            event.level_id,
            event.timestamp_utc.isoformat(),
            canonical_json(event.payload),
            event.causation_id,
            event.correlation_id,
        ),
    )
    sequence = cursor.lastrowid
    if sequence is None:
        raise RuntimeError("journal insert did not return a sequence")
    return JournalEvent(
        sequence=int(sequence),
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


def _event_from_row(row: sqlite3.Row) -> JournalEvent:
    return JournalEvent(
        sequence=int(row["sequence"]),
        event_id=str(row["event_id"]),
        event_type=JournalEventType(str(row["event_type"])),
        event_version=int(row["event_version"]),
        cycle_id=str(row["cycle_id"]),
        level_id=None if row["level_id"] is None else int(row["level_id"]),
        timestamp_utc=datetime.fromisoformat(str(row["timestamp_utc"])),
        payload=_parse_object(str(row["payload_json"])),
        causation_id=(
            None if row["causation_id"] is None else str(row["causation_id"])
        ),
        correlation_id=str(row["correlation_id"]),
    )


def _parse_object(payload: str) -> dict[str, object]:
    parsed: object = json.loads(payload)
    if not isinstance(parsed, dict):
        raise ValueError("journal JSON must be an object")
    return canonical_object(cast(dict[str, Any], parsed))
