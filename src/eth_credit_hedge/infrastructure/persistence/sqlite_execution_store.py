"""Narrow SQLite store for durable entry intents and idempotent executions."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from eth_credit_hedge.domain.execution import (
    Category,
    ExecutionUpdate,
    LiveExecutionState,
    OrderRequestAck,
    OrderSide,
    OrderType,
    PlaceOrderRequest,
    TimeInForce,
    TriggerBy,
)
from eth_credit_hedge.domain.live_execution import EntryExecutionSnapshot
from eth_credit_hedge.domain.protected_execution import ProtectionSnapshot
from eth_credit_hedge.domain.live_recovery import (
    RecoveryDebtSnapshot,
    RecoveryDebtState,
)


_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
_MIGRATION = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS order_intents (
    order_link_id TEXT PRIMARY KEY,
    request_json TEXT NOT NULL,
    persisted_at TEXT NOT NULL,
    exchange_order_id TEXT,
    acknowledged_at TEXT
);

CREATE TABLE IF NOT EXISTS entry_snapshots (
    order_link_id TEXT PRIMARY KEY REFERENCES order_intents(order_link_id),
    state TEXT NOT NULL,
    target_quantity TEXT NOT NULL,
    entry_order_id TEXT,
    filled_quantity TEXT NOT NULL,
    entry_notional TEXT NOT NULL,
    entry_fees TEXT NOT NULL,
    version INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS executions (
    execution_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    order_link_id TEXT NOT NULL REFERENCES order_intents(order_link_id),
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    price TEXT NOT NULL,
    quantity TEXT NOT NULL,
    fee TEXT NOT NULL,
    is_maker INTEGER,
    executed_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    payload_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS protection_snapshots (
    entry_order_link_id TEXT PRIMARY KEY REFERENCES order_intents(order_link_id),
    state TEXT NOT NULL,
    entry_quantity TEXT NOT NULL,
    open_quantity TEXT NOT NULL,
    average_entry_price TEXT NOT NULL,
    entry_fees TEXT NOT NULL,
    stop_order_link_id TEXT NOT NULL UNIQUE REFERENCES order_intents(order_link_id),
    stop_order_id TEXT,
    stop_trigger_price TEXT NOT NULL,
    tp_order_link_id TEXT UNIQUE REFERENCES order_intents(order_link_id),
    tp_order_id TEXT,
    tp_price TEXT,
    tp_filled_quantity TEXT NOT NULL,
    stop_filled_quantity TEXT NOT NULL,
    exit_notional TEXT NOT NULL,
    exit_fees TEXT NOT NULL,
    confirmed_recovery_debt TEXT NOT NULL,
    pending_terminal_state TEXT,
    version INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recovery_debts (
    level_id INTEGER PRIMARY KEY,
    projected_debt TEXT NOT NULL,
    confirmed_debt TEXT NOT NULL,
    allocated_debt TEXT NOT NULL,
    remaining_debt TEXT NOT NULL,
    version INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

INSERT OR IGNORE INTO schema_migrations(version, applied_at)
VALUES (1, strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'));

INSERT OR IGNORE INTO schema_migrations(version, applied_at)
VALUES (2, strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'));

INSERT OR IGNORE INTO schema_migrations(version, applied_at)
VALUES (4, strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'));
"""


class ConcurrentSnapshotUpdateError(RuntimeError):
    """The persisted snapshot changed after it was read."""


class SqliteExecutionStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize)

    async def persist_order_intent(
        self,
        request: PlaceOrderRequest,
        persisted_at: datetime,
    ) -> None:
        persisted = _utc(persisted_at, "intent persistence time")
        await asyncio.to_thread(self._persist_order_intent, request, persisted)

    async def persist_entry_intent(
        self,
        request: PlaceOrderRequest,
        snapshot: EntryExecutionSnapshot,
        persisted_at: datetime,
    ) -> None:
        if request.order_link_id != snapshot.order_link_id:
            raise ValueError("request and snapshot client IDs differ")
        persisted = _utc(persisted_at, "intent persistence time")
        await asyncio.to_thread(
            self._persist_entry_intent,
            request,
            snapshot,
            persisted,
        )

    async def load_order_intent(
        self,
        order_link_id: str,
    ) -> PlaceOrderRequest | None:
        return await asyncio.to_thread(self._load_order_intent, order_link_id)

    async def load_all_order_intents(self) -> tuple[PlaceOrderRequest, ...]:
        return await asyncio.to_thread(self._load_all_order_intents)

    async def load_entry_snapshot(
        self,
        order_link_id: str,
    ) -> EntryExecutionSnapshot | None:
        return await asyncio.to_thread(self._load_entry_snapshot, order_link_id)

    async def load_all_entry_snapshots(
        self,
    ) -> tuple[EntryExecutionSnapshot, ...]:
        return await asyncio.to_thread(self._load_all_entry_snapshots)

    async def transition_entry_snapshot(
        self,
        previous_version: int,
        snapshot: EntryExecutionSnapshot,
    ) -> None:
        await asyncio.to_thread(
            self._transition_entry_snapshot,
            previous_version,
            snapshot,
        )

    async def record_acknowledgement_and_snapshot(
        self,
        previous_version: int,
        acknowledgement: OrderRequestAck,
        snapshot: EntryExecutionSnapshot,
    ) -> None:
        if acknowledgement.order_link_id != snapshot.order_link_id:
            raise ValueError("acknowledgement and snapshot client IDs differ")
        await asyncio.to_thread(
            self._record_acknowledgement_and_snapshot,
            previous_version,
            acknowledgement,
            snapshot,
        )

    async def record_acknowledgement(
        self,
        acknowledgement: OrderRequestAck,
    ) -> None:
        await asyncio.to_thread(self._record_acknowledgement, acknowledgement)

    async def record_execution_and_snapshot(
        self,
        previous_version: int,
        execution: ExecutionUpdate,
        received_at: datetime,
        payload_hash: str,
        snapshot: EntryExecutionSnapshot,
    ) -> bool:
        if execution.order_link_id != snapshot.order_link_id:
            raise ValueError("execution and snapshot client IDs differ")
        received = _utc(received_at, "execution receive time")
        if _SHA256_PATTERN.fullmatch(payload_hash) is None:
            raise ValueError("payload hash must be a SHA-256 hexadecimal digest")
        return await asyncio.to_thread(
            self._record_execution_and_snapshot,
            previous_version,
            execution,
            received,
            payload_hash.lower(),
            snapshot,
        )

    async def persist_protection_intent(
        self,
        request: PlaceOrderRequest,
        snapshot: ProtectionSnapshot,
        persisted_at: datetime,
    ) -> None:
        if request.order_link_id != snapshot.stop_order_link_id:
            raise ValueError("request and stop snapshot client IDs differ")
        persisted = _utc(persisted_at, "protection persistence time")
        await asyncio.to_thread(
            self._persist_protection_intent,
            request,
            snapshot,
            persisted,
        )

    async def persist_take_profit_intent(
        self,
        previous_version: int,
        request: PlaceOrderRequest,
        snapshot: ProtectionSnapshot,
        persisted_at: datetime,
    ) -> None:
        if request.order_link_id != snapshot.tp_order_link_id:
            raise ValueError("request and TP snapshot client IDs differ")
        persisted = _utc(persisted_at, "TP persistence time")
        await asyncio.to_thread(
            self._persist_take_profit_intent,
            previous_version,
            request,
            snapshot,
            persisted,
        )

    async def persist_replacement_stop_intent(
        self,
        previous_version: int,
        request: PlaceOrderRequest,
        snapshot: ProtectionSnapshot,
        persisted_at: datetime,
    ) -> None:
        if request.order_link_id != snapshot.stop_order_link_id:
            raise ValueError("request and replacement stop client IDs differ")
        persisted = _utc(persisted_at, "replacement stop persistence time")
        await asyncio.to_thread(
            self._persist_replacement_stop_intent,
            previous_version,
            request,
            snapshot,
            persisted,
        )

    async def load_protection_snapshot(
        self,
        entry_order_link_id: str,
    ) -> ProtectionSnapshot | None:
        return await asyncio.to_thread(
            self._load_protection_snapshot,
            entry_order_link_id,
        )

    async def load_protection_snapshot_by_exit_id(
        self,
        order_link_id: str,
    ) -> ProtectionSnapshot | None:
        return await asyncio.to_thread(
            self._load_protection_snapshot_by_exit_id,
            order_link_id,
        )

    async def load_all_protection_snapshots(
        self,
    ) -> tuple[ProtectionSnapshot, ...]:
        return await asyncio.to_thread(self._load_all_protection_snapshots)

    async def transition_protection_snapshot(
        self,
        previous_version: int,
        snapshot: ProtectionSnapshot,
    ) -> None:
        await asyncio.to_thread(
            self._transition_protection_snapshot,
            previous_version,
            snapshot,
        )

    async def record_exit_execution_and_snapshot(
        self,
        previous_version: int,
        execution: ExecutionUpdate,
        received_at: datetime,
        payload_hash: str,
        snapshot: ProtectionSnapshot,
    ) -> bool:
        received = _utc(received_at, "execution receive time")
        if _SHA256_PATTERN.fullmatch(payload_hash) is None:
            raise ValueError("payload hash must be a SHA-256 hexadecimal digest")
        return await asyncio.to_thread(
            self._record_exit_execution_and_snapshot,
            previous_version,
            execution,
            received,
            payload_hash.lower(),
            snapshot,
        )

    async def has_execution(self, execution_id: str) -> bool:
        return await asyncio.to_thread(self._has_execution, execution_id)

    async def load_all_executions(self) -> tuple[ExecutionUpdate, ...]:
        return await asyncio.to_thread(self._load_all_executions)

    async def persist_recovery_debt_snapshot(
        self,
        snapshot: RecoveryDebtSnapshot,
    ) -> None:
        await asyncio.to_thread(self._persist_recovery_debt_snapshot, snapshot)

    async def load_recovery_debt_snapshot(
        self,
        level_id: int,
    ) -> RecoveryDebtSnapshot | None:
        return await asyncio.to_thread(self._load_recovery_debt_snapshot, level_id)

    async def transition_recovery_debt_snapshot(
        self,
        previous_version: int,
        snapshot: RecoveryDebtSnapshot,
    ) -> None:
        await asyncio.to_thread(
            self._transition_recovery_debt_snapshot,
            previous_version,
            snapshot,
        )

    async def execution_count(self) -> int:
        return await asyncio.to_thread(self._execution_count)

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_MIGRATION)

    def _persist_order_intent(
        self,
        request: PlaceOrderRequest,
        persisted_at: datetime,
    ) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO order_intents(
                        order_link_id, request_json, persisted_at
                    ) VALUES (?, ?, ?)
                    """,
                    (
                        request.order_link_id,
                        _serialize_request(request),
                        persisted_at.isoformat(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("order intent is already persisted") from exc

    def _persist_entry_intent(
        self,
        request: PlaceOrderRequest,
        snapshot: EntryExecutionSnapshot,
        persisted_at: datetime,
    ) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO order_intents(
                        order_link_id, request_json, persisted_at
                    ) VALUES (?, ?, ?)
                    """,
                    (
                        request.order_link_id,
                        _serialize_request(request),
                        persisted_at.isoformat(),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO entry_snapshots(
                        order_link_id, state, target_quantity, entry_order_id,
                        filled_quantity, entry_notional, entry_fees, version,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    _snapshot_values(snapshot),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("entry intent is already persisted") from exc

    def _load_order_intent(
        self,
        order_link_id: str,
    ) -> PlaceOrderRequest | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT request_json FROM order_intents WHERE order_link_id = ?",
                (order_link_id,),
            ).fetchone()
        if row is None:
            return None
        return _deserialize_request(str(row["request_json"]))

    def _load_all_order_intents(self) -> tuple[PlaceOrderRequest, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT request_json FROM order_intents
                ORDER BY persisted_at, order_link_id
                """
            ).fetchall()
        return tuple(
            _deserialize_request(str(row["request_json"])) for row in rows
        )

    def _load_entry_snapshot(
        self,
        order_link_id: str,
    ) -> EntryExecutionSnapshot | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entry_snapshots WHERE order_link_id = ?",
                (order_link_id,),
            ).fetchone()
        return None if row is None else _snapshot_from_row(row)

    def _load_all_entry_snapshots(
        self,
    ) -> tuple[EntryExecutionSnapshot, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM entry_snapshots
                ORDER BY order_link_id
                """
            ).fetchall()
        return tuple(_snapshot_from_row(row) for row in rows)

    def _transition_entry_snapshot(
        self,
        previous_version: int,
        snapshot: EntryExecutionSnapshot,
    ) -> None:
        with self._connect() as connection:
            _update_snapshot(connection, previous_version, snapshot)

    def _record_acknowledgement_and_snapshot(
        self,
        previous_version: int,
        acknowledgement: OrderRequestAck,
        snapshot: EntryExecutionSnapshot,
    ) -> None:
        with self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE order_intents
                SET exchange_order_id = ?, acknowledged_at = ?
                WHERE order_link_id = ?
                """,
                (
                    acknowledgement.order_id,
                    acknowledgement.acknowledged_at.isoformat(),
                    acknowledgement.order_link_id,
                ),
            ).rowcount
            if updated != 1:
                raise ValueError("acknowledgement has no persisted intent")
            _update_snapshot(connection, previous_version, snapshot)

    def _record_acknowledgement(
        self,
        acknowledgement: OrderRequestAck,
    ) -> None:
        with self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE order_intents
                SET exchange_order_id = ?, acknowledged_at = ?
                WHERE order_link_id = ?
                """,
                (
                    acknowledgement.order_id,
                    acknowledgement.acknowledged_at.isoformat(),
                    acknowledgement.order_link_id,
                ),
            ).rowcount
            if updated != 1:
                raise ValueError("acknowledgement has no persisted intent")

    def _record_execution_and_snapshot(
        self,
        previous_version: int,
        execution: ExecutionUpdate,
        received_at: datetime,
        payload_hash: str,
        snapshot: EntryExecutionSnapshot,
    ) -> bool:
        with self._connect() as connection:
            inserted = connection.execute(
                """
                INSERT OR IGNORE INTO executions(
                    execution_id, order_id, order_link_id, symbol, side,
                    price, quantity, fee, is_maker, executed_at, received_at,
                    payload_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    execution.execution_id,
                    execution.order_id,
                    execution.order_link_id,
                    execution.symbol,
                    execution.side,
                    str(execution.price),
                    str(execution.quantity),
                    str(execution.fee),
                    (
                        None
                        if execution.is_maker is None
                        else int(execution.is_maker)
                    ),
                    execution.executed_at.isoformat(),
                    received_at.isoformat(),
                    payload_hash,
                ),
            ).rowcount
            if inserted == 0:
                return False
            _update_snapshot(connection, previous_version, snapshot)
            return True

    def _persist_protection_intent(
        self,
        request: PlaceOrderRequest,
        snapshot: ProtectionSnapshot,
        persisted_at: datetime,
    ) -> None:
        try:
            with self._connect() as connection:
                _insert_order_intent(connection, request, persisted_at)
                connection.execute(
                    """
                    INSERT INTO protection_snapshots(
                        entry_order_link_id, state, entry_quantity,
                        open_quantity, average_entry_price, entry_fees,
                        stop_order_link_id, stop_order_id, stop_trigger_price,
                        tp_order_link_id, tp_order_id, tp_price,
                        tp_filled_quantity, stop_filled_quantity,
                        exit_notional, exit_fees, confirmed_recovery_debt,
                        pending_terminal_state, version, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    _protection_values(snapshot),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("protection intent is already persisted") from exc

    def _persist_take_profit_intent(
        self,
        previous_version: int,
        request: PlaceOrderRequest,
        snapshot: ProtectionSnapshot,
        persisted_at: datetime,
    ) -> None:
        try:
            with self._connect() as connection:
                _insert_order_intent(connection, request, persisted_at)
                _update_protection_snapshot(connection, previous_version, snapshot)
        except sqlite3.IntegrityError as exc:
            raise ValueError("TP intent is already persisted") from exc

    def _persist_replacement_stop_intent(
        self,
        previous_version: int,
        request: PlaceOrderRequest,
        snapshot: ProtectionSnapshot,
        persisted_at: datetime,
    ) -> None:
        try:
            with self._connect() as connection:
                _insert_order_intent(connection, request, persisted_at)
                _update_protection_snapshot(connection, previous_version, snapshot)
        except sqlite3.IntegrityError as exc:
            raise ValueError("replacement stop intent is already persisted") from exc

    def _load_protection_snapshot(
        self,
        entry_order_link_id: str,
    ) -> ProtectionSnapshot | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM protection_snapshots
                WHERE entry_order_link_id = ?
                """,
                (entry_order_link_id,),
            ).fetchone()
        return None if row is None else _protection_from_row(row)

    def _load_protection_snapshot_by_exit_id(
        self,
        order_link_id: str,
    ) -> ProtectionSnapshot | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM protection_snapshots
                WHERE stop_order_link_id = ? OR tp_order_link_id = ?
                """,
                (order_link_id, order_link_id),
            ).fetchone()
        return None if row is None else _protection_from_row(row)

    def _load_all_protection_snapshots(
        self,
    ) -> tuple[ProtectionSnapshot, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM protection_snapshots
                ORDER BY entry_order_link_id
                """
            ).fetchall()
        return tuple(_protection_from_row(row) for row in rows)

    def _transition_protection_snapshot(
        self,
        previous_version: int,
        snapshot: ProtectionSnapshot,
    ) -> None:
        with self._connect() as connection:
            _update_protection_snapshot(connection, previous_version, snapshot)

    def _record_exit_execution_and_snapshot(
        self,
        previous_version: int,
        execution: ExecutionUpdate,
        received_at: datetime,
        payload_hash: str,
        snapshot: ProtectionSnapshot,
    ) -> bool:
        with self._connect() as connection:
            inserted = _insert_execution_if_new(
                connection,
                execution,
                received_at,
                payload_hash,
            )
            if not inserted:
                return False
            _update_protection_snapshot(connection, previous_version, snapshot)
            return True

    def _has_execution(self, execution_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
        return row is not None

    def _load_all_executions(self) -> tuple[ExecutionUpdate, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM executions
                ORDER BY executed_at, execution_id
                """
            ).fetchall()
        return tuple(_execution_from_row(row) for row in rows)

    def _persist_recovery_debt_snapshot(
        self,
        snapshot: RecoveryDebtSnapshot,
    ) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO recovery_debts(
                        level_id, projected_debt, confirmed_debt,
                        allocated_debt, remaining_debt, version, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    _recovery_debt_values(snapshot),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("recovery debt snapshot already exists") from exc

    def _load_recovery_debt_snapshot(
        self,
        level_id: int,
    ) -> RecoveryDebtSnapshot | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM recovery_debts WHERE level_id = ?",
                (level_id,),
            ).fetchone()
        return None if row is None else _recovery_debt_from_row(row)

    def _transition_recovery_debt_snapshot(
        self,
        previous_version: int,
        snapshot: RecoveryDebtSnapshot,
    ) -> None:
        if snapshot.version != previous_version + 1:
            raise ValueError("new debt version must increment exactly once")
        with self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE recovery_debts
                SET projected_debt = ?, confirmed_debt = ?,
                    allocated_debt = ?, remaining_debt = ?, version = ?,
                    updated_at = ?
                WHERE level_id = ? AND version = ?
                """,
                (
                    str(snapshot.debt.projected_debt),
                    str(snapshot.debt.confirmed_debt),
                    str(snapshot.debt.allocated_debt),
                    str(snapshot.debt.remaining_debt),
                    snapshot.version,
                    snapshot.updated_at.isoformat(),
                    snapshot.level_id,
                    previous_version,
                ),
            ).rowcount
            if updated != 1:
                raise ConcurrentSnapshotUpdateError(
                    "recovery debt version changed before transaction commit"
                )

    def _execution_count(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM executions").fetchone()
        if row is None:
            raise RuntimeError("execution count query returned no row")
        return int(row["count"])

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection


def _update_snapshot(
    connection: sqlite3.Connection,
    previous_version: int,
    snapshot: EntryExecutionSnapshot,
) -> None:
    if snapshot.version != previous_version + 1:
        raise ValueError("new snapshot version must increment exactly once")
    updated = connection.execute(
        """
        UPDATE entry_snapshots
        SET state = ?, target_quantity = ?, entry_order_id = ?,
            filled_quantity = ?, entry_notional = ?, entry_fees = ?,
            version = ?, updated_at = ?
        WHERE order_link_id = ? AND version = ?
        """,
        (
            snapshot.state.value,
            str(snapshot.target_quantity),
            snapshot.entry_order_id,
            str(snapshot.filled_quantity),
            str(snapshot.entry_notional),
            str(snapshot.entry_fees),
            snapshot.version,
            snapshot.updated_at.isoformat(),
            snapshot.order_link_id,
            previous_version,
        ),
    ).rowcount
    if updated != 1:
        raise ConcurrentSnapshotUpdateError(
            "entry snapshot version changed before transaction commit"
        )


def _insert_order_intent(
    connection: sqlite3.Connection,
    request: PlaceOrderRequest,
    persisted_at: datetime,
) -> None:
    connection.execute(
        """
        INSERT INTO order_intents(order_link_id, request_json, persisted_at)
        VALUES (?, ?, ?)
        """,
        (
            request.order_link_id,
            _serialize_request(request),
            persisted_at.isoformat(),
        ),
    )


def _insert_execution_if_new(
    connection: sqlite3.Connection,
    execution: ExecutionUpdate,
    received_at: datetime,
    payload_hash: str,
) -> bool:
    inserted = connection.execute(
        """
        INSERT OR IGNORE INTO executions(
            execution_id, order_id, order_link_id, symbol, side,
            price, quantity, fee, is_maker, executed_at, received_at,
            payload_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            execution.execution_id,
            execution.order_id,
            execution.order_link_id,
            execution.symbol,
            execution.side,
            str(execution.price),
            str(execution.quantity),
            str(execution.fee),
            None if execution.is_maker is None else int(execution.is_maker),
            execution.executed_at.isoformat(),
            received_at.isoformat(),
            payload_hash,
        ),
    ).rowcount
    return inserted == 1


def _update_protection_snapshot(
    connection: sqlite3.Connection,
    previous_version: int,
    snapshot: ProtectionSnapshot,
) -> None:
    if snapshot.version != previous_version + 1:
        raise ValueError("new protection version must increment exactly once")
    updated = connection.execute(
        """
        UPDATE protection_snapshots
        SET state = ?, entry_quantity = ?, open_quantity = ?,
            average_entry_price = ?, entry_fees = ?,
            stop_order_link_id = ?, stop_order_id = ?,
            stop_trigger_price = ?, tp_order_link_id = ?, tp_order_id = ?,
            tp_price = ?, tp_filled_quantity = ?, stop_filled_quantity = ?,
            exit_notional = ?, exit_fees = ?, confirmed_recovery_debt = ?,
            pending_terminal_state = ?, version = ?, updated_at = ?
        WHERE entry_order_link_id = ? AND version = ?
        """,
        (
            snapshot.state.value,
            str(snapshot.entry_quantity),
            str(snapshot.open_quantity),
            str(snapshot.average_entry_price),
            str(snapshot.entry_fees),
            snapshot.stop_order_link_id,
            snapshot.stop_order_id,
            str(snapshot.stop_trigger_price),
            snapshot.tp_order_link_id,
            snapshot.tp_order_id,
            None if snapshot.tp_price is None else str(snapshot.tp_price),
            str(snapshot.tp_filled_quantity),
            str(snapshot.stop_filled_quantity),
            str(snapshot.exit_notional),
            str(snapshot.exit_fees),
            str(snapshot.confirmed_recovery_debt),
            (
                None
                if snapshot.pending_terminal_state is None
                else snapshot.pending_terminal_state.value
            ),
            snapshot.version,
            snapshot.updated_at.isoformat(),
            snapshot.entry_order_link_id,
            previous_version,
        ),
    ).rowcount
    if updated != 1:
        raise ConcurrentSnapshotUpdateError(
            "protection snapshot version changed before transaction commit"
        )


def _protection_values(snapshot: ProtectionSnapshot) -> tuple[object, ...]:
    return (
        snapshot.entry_order_link_id,
        snapshot.state.value,
        str(snapshot.entry_quantity),
        str(snapshot.open_quantity),
        str(snapshot.average_entry_price),
        str(snapshot.entry_fees),
        snapshot.stop_order_link_id,
        snapshot.stop_order_id,
        str(snapshot.stop_trigger_price),
        snapshot.tp_order_link_id,
        snapshot.tp_order_id,
        None if snapshot.tp_price is None else str(snapshot.tp_price),
        str(snapshot.tp_filled_quantity),
        str(snapshot.stop_filled_quantity),
        str(snapshot.exit_notional),
        str(snapshot.exit_fees),
        str(snapshot.confirmed_recovery_debt),
        (
            None
            if snapshot.pending_terminal_state is None
            else snapshot.pending_terminal_state.value
        ),
        snapshot.version,
        snapshot.updated_at.isoformat(),
    )


def _protection_from_row(row: sqlite3.Row) -> ProtectionSnapshot:
    pending = row["pending_terminal_state"]
    tp_price = row["tp_price"]
    return ProtectionSnapshot(
        entry_order_link_id=str(row["entry_order_link_id"]),
        state=LiveExecutionState(str(row["state"])),
        entry_quantity=Decimal(str(row["entry_quantity"])),
        open_quantity=Decimal(str(row["open_quantity"])),
        average_entry_price=Decimal(str(row["average_entry_price"])),
        entry_fees=Decimal(str(row["entry_fees"])),
        stop_order_link_id=str(row["stop_order_link_id"]),
        stop_order_id=(
            None if row["stop_order_id"] is None else str(row["stop_order_id"])
        ),
        stop_trigger_price=Decimal(str(row["stop_trigger_price"])),
        tp_order_link_id=(
            None
            if row["tp_order_link_id"] is None
            else str(row["tp_order_link_id"])
        ),
        tp_order_id=(
            None if row["tp_order_id"] is None else str(row["tp_order_id"])
        ),
        tp_price=None if tp_price is None else Decimal(str(tp_price)),
        tp_filled_quantity=Decimal(str(row["tp_filled_quantity"])),
        stop_filled_quantity=Decimal(str(row["stop_filled_quantity"])),
        exit_notional=Decimal(str(row["exit_notional"])),
        exit_fees=Decimal(str(row["exit_fees"])),
        confirmed_recovery_debt=Decimal(
            str(row["confirmed_recovery_debt"])
        ),
        pending_terminal_state=(
            None if pending is None else LiveExecutionState(str(pending))
        ),
        version=int(row["version"]),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
    )


def _recovery_debt_values(
    snapshot: RecoveryDebtSnapshot,
) -> tuple[object, ...]:
    return (
        snapshot.level_id,
        str(snapshot.debt.projected_debt),
        str(snapshot.debt.confirmed_debt),
        str(snapshot.debt.allocated_debt),
        str(snapshot.debt.remaining_debt),
        snapshot.version,
        snapshot.updated_at.isoformat(),
    )


def _recovery_debt_from_row(row: sqlite3.Row) -> RecoveryDebtSnapshot:
    return RecoveryDebtSnapshot(
        level_id=int(row["level_id"]),
        debt=RecoveryDebtState(
            projected_debt=Decimal(str(row["projected_debt"])),
            confirmed_debt=Decimal(str(row["confirmed_debt"])),
            allocated_debt=Decimal(str(row["allocated_debt"])),
            remaining_debt=Decimal(str(row["remaining_debt"])),
        ),
        version=int(row["version"]),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
    )


def _snapshot_values(snapshot: EntryExecutionSnapshot) -> tuple[object, ...]:
    return (
        snapshot.order_link_id,
        snapshot.state.value,
        str(snapshot.target_quantity),
        snapshot.entry_order_id,
        str(snapshot.filled_quantity),
        str(snapshot.entry_notional),
        str(snapshot.entry_fees),
        snapshot.version,
        snapshot.updated_at.isoformat(),
    )


def _execution_from_row(row: sqlite3.Row) -> ExecutionUpdate:
    maker = row["is_maker"]
    return ExecutionUpdate(
        execution_id=str(row["execution_id"]),
        order_id=str(row["order_id"]),
        order_link_id=str(row["order_link_id"]),
        symbol=str(row["symbol"]),
        side=cast(OrderSide, str(row["side"])),
        price=Decimal(str(row["price"])),
        quantity=Decimal(str(row["quantity"])),
        fee=Decimal(str(row["fee"])),
        is_maker=None if maker is None else bool(maker),
        executed_at=datetime.fromisoformat(str(row["executed_at"])),
    )


def _snapshot_from_row(row: sqlite3.Row) -> EntryExecutionSnapshot:
    return EntryExecutionSnapshot(
        order_link_id=str(row["order_link_id"]),
        state=LiveExecutionState(str(row["state"])),
        target_quantity=Decimal(str(row["target_quantity"])),
        entry_order_id=(
            None if row["entry_order_id"] is None else str(row["entry_order_id"])
        ),
        filled_quantity=Decimal(str(row["filled_quantity"])),
        entry_notional=Decimal(str(row["entry_notional"])),
        entry_fees=Decimal(str(row["entry_fees"])),
        version=int(row["version"]),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
    )


def _serialize_request(request: PlaceOrderRequest) -> str:
    return json.dumps(
        {
            "category": request.category,
            "symbol": request.symbol,
            "side": request.side,
            "order_type": request.order_type,
            "quantity": str(request.quantity),
            "order_link_id": request.order_link_id,
            "price": None if request.price is None else str(request.price),
            "time_in_force": request.time_in_force,
            "reduce_only": request.reduce_only,
            "trigger_price": (
                None if request.trigger_price is None else str(request.trigger_price)
            ),
            "trigger_direction": request.trigger_direction,
            "trigger_by": request.trigger_by,
            "position_idx": request.position_idx,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _deserialize_request(payload: str) -> PlaceOrderRequest:
    parsed: object = json.loads(payload)
    if not isinstance(parsed, dict):
        raise ValueError("persisted order intent must be a JSON object")
    data = cast(dict[str, Any], parsed)
    return PlaceOrderRequest(
        category=cast(Category, _required_string(data, "category")),
        symbol=_required_string(data, "symbol"),
        side=cast(OrderSide, _required_string(data, "side")),
        order_type=cast(OrderType, _required_string(data, "order_type")),
        quantity=Decimal(_required_string(data, "quantity")),
        order_link_id=_required_string(data, "order_link_id"),
        price=_optional_decimal_value(data.get("price")),
        time_in_force=cast(
            TimeInForce,
            _required_string(data, "time_in_force"),
        ),
        reduce_only=_required_bool(data, "reduce_only"),
        trigger_price=_optional_decimal_value(data.get("trigger_price")),
        trigger_direction=_optional_int(data.get("trigger_direction")),
        trigger_by=cast(
            TriggerBy | None,
            _optional_string(data.get("trigger_by")),
        ),
        position_idx=_required_int(data, "position_idx"),
    )


def _required_string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ValueError(f"persisted {key} must be a string")
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("persisted optional value must be a string")
    return value


def _optional_decimal_value(value: object) -> Decimal | None:
    text = _optional_string(value)
    return None if text is None else Decimal(text)


def _required_bool(data: dict[str, Any], key: str) -> bool:
    value = data.get(key)
    if type(value) is not bool:
        raise ValueError(f"persisted {key} must be a boolean")
    return value


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise ValueError("persisted optional value must be an integer")
    return value


def _required_int(data: dict[str, Any], key: str) -> int:
    value = _optional_int(data.get(key))
    if value is None:
        raise ValueError(f"persisted {key} must be an integer")
    return value


def _utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)
