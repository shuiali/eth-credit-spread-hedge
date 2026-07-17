"""Atomic SQLite persistence for the authoritative, replayable accounting ledger."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from eth_credit_hedge.domain.accounting.errors import (
    AccountingContractError,
    DuplicateAccountingIdentifierError,
)
from eth_credit_hedge.domain.accounting.events import (
    AccountingEvent,
    FeeRecorded,
    FundingRecorded,
    HedgeExecutionRecorded,
    MigratedFromLegacySnapshot,
    OptionExecutionRecorded,
    OptionQuoteRecorded,
    RecoveryAllocationRecorded,
    RecoveryDebtChanged,
    ReferencePriceRecorded,
    canonical_event_json,
    event_from_dict,
    event_to_dict,
)
from eth_credit_hedge.domain.accounting.reconciliation import (
    AccountingReconciliationReport,
)
from eth_credit_hedge.domain.accounting.reconstruction import (
    CombinedLedgerReconstructor,
    CombinedLedgerState,
)
from eth_credit_hedge.domain.strategy_math.units import Price


_MIGRATION = """
CREATE TABLE IF NOT EXISTS accounting_schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounting_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    event_json TEXT NOT NULL,
    execution_id TEXT UNIQUE,
    funding_id TEXT UNIQUE,
    fee_id TEXT UNIQUE,
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS option_lots (
    snapshot_sequence INTEGER NOT NULL REFERENCES accounting_events(sequence),
    leg TEXT NOT NULL,
    symbol TEXT,
    open_quantity TEXT NOT NULL,
    realized_pnl TEXT NOT NULL,
    entry_fees TEXT NOT NULL,
    exit_fees TEXT NOT NULL,
    PRIMARY KEY(snapshot_sequence, leg)
);

CREATE TABLE IF NOT EXISTS hedge_lots (
    snapshot_sequence INTEGER NOT NULL REFERENCES accounting_events(sequence),
    lot_id TEXT NOT NULL,
    open_quantity TEXT NOT NULL,
    gross_realized_pnl TEXT NOT NULL,
    entry_fees TEXT NOT NULL,
    exit_fees TEXT NOT NULL,
    funding_pnl TEXT NOT NULL,
    debt_increment TEXT NOT NULL,
    PRIMARY KEY(snapshot_sequence, lot_id)
);

CREATE TABLE IF NOT EXISTS fee_records (
    fee_id TEXT PRIMARY KEY,
    event_sequence INTEGER NOT NULL REFERENCES accounting_events(sequence),
    owner TEXT NOT NULL,
    amount TEXT NOT NULL,
    currency TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS funding_records (
    funding_id TEXT PRIMARY KEY,
    event_sequence INTEGER NOT NULL REFERENCES accounting_events(sequence),
    symbol TEXT NOT NULL,
    position_quantity TEXT NOT NULL,
    rate TEXT NOT NULL,
    amount TEXT NOT NULL,
    allocations_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reference_prices (
    event_sequence INTEGER NOT NULL REFERENCES accounting_events(sequence),
    reference_type TEXT NOT NULL,
    symbol TEXT,
    price TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    PRIMARY KEY(event_sequence, reference_type)
);

CREATE TABLE IF NOT EXISTS slippage_attributions (
    snapshot_sequence INTEGER PRIMARY KEY REFERENCES accounting_events(sequence),
    amount TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounting_snapshots (
    last_sequence INTEGER PRIMARY KEY REFERENCES accounting_events(sequence),
    event_count INTEGER NOT NULL,
    ledger_digest TEXT NOT NULL,
    snapshot_digest TEXT NOT NULL,
    state_json TEXT NOT NULL,
    replay_events_json TEXT NOT NULL,
    hedge_mark TEXT,
    hedge_liquidation TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recovery_allocations (
    event_sequence INTEGER PRIMARY KEY REFERENCES accounting_events(sequence),
    amount TEXT NOT NULL,
    reason TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounting_reconciliation_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ledger_digest TEXT NOT NULL,
    trading_allowed INTEGER NOT NULL,
    differences_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);

INSERT OR IGNORE INTO accounting_schema_migrations(version, applied_at)
VALUES (1, strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'));
"""


@dataclass(frozen=True, slots=True)
class AccountingReplayResult:
    state: CombinedLedgerState
    event_count: int
    last_sequence: int
    full_replay_digest: str
    snapshot_tail_digest: str
    snapshot_digest_matches: bool


class SqliteAccountingStore:
    """Persist only canonical events and reconstructor projections in one transaction."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize)

    async def append_event_and_snapshot(
        self,
        event: AccountingEvent,
        state: CombinedLedgerState,
        *,
        hedge_mark: Price | None = None,
        hedge_liquidation: Price | None = None,
    ) -> int:
        return await asyncio.to_thread(
            self._append_event_and_snapshot,
            event,
            state,
            hedge_mark,
            hedge_liquidation,
        )

    async def append_events_and_snapshot(
        self,
        events: tuple[AccountingEvent, ...],
        state: CombinedLedgerState,
        *,
        hedge_mark: Price | None = None,
        hedge_liquidation: Price | None = None,
    ) -> tuple[int, ...]:
        return await asyncio.to_thread(
            self._append_events_and_snapshot,
            events,
            state,
            hedge_mark,
            hedge_liquidation,
        )

    async def load_events_after(self, sequence: int) -> tuple[AccountingEvent, ...]:
        if sequence < 0:
            raise ValueError("accounting event sequence cannot be negative")
        return await asyncio.to_thread(self._load_events_after, sequence)

    async def replay(
        self,
        reconstructor: CombinedLedgerReconstructor,
    ) -> AccountingReplayResult:
        return await asyncio.to_thread(self._replay, reconstructor)

    async def persist_reconciliation_result(
        self,
        report: AccountingReconciliationReport,
        ledger_digest: str,
        recorded_at: datetime,
    ) -> None:
        await asyncio.to_thread(
            self._persist_reconciliation_result,
            report,
            ledger_digest,
            recorded_at,
        )

    async def legacy_migration_pending(self) -> bool:
        return await asyncio.to_thread(self._legacy_migration_pending)

    async def schema_version(self) -> int:
        return await asyncio.to_thread(self._schema_version)

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_MIGRATION)

    def _append_event_and_snapshot(
        self,
        event: AccountingEvent,
        state: CombinedLedgerState,
        hedge_mark: Price | None,
        hedge_liquidation: Price | None,
    ) -> int:
        if not isinstance(state, CombinedLedgerState):
            raise AccountingContractError("accounting projection must be CombinedLedgerState")
        with self._connect() as connection:
            sequence, inserted = self._insert_event(connection, event)
            if not inserted:
                return sequence
            events = _load_events(connection, 0)
            _write_event_detail(connection, sequence, event)
            _write_projection(connection, sequence, state)
            _write_snapshot(
                connection,
                sequence,
                events,
                state,
                hedge_mark,
                hedge_liquidation,
            )
            return sequence

    def _append_events_and_snapshot(
        self,
        events: tuple[AccountingEvent, ...],
        state: CombinedLedgerState,
        hedge_mark: Price | None,
        hedge_liquidation: Price | None,
    ) -> tuple[int, ...]:
        if not events:
            return ()
        with self._connect() as connection:
            sequences: list[int] = []
            inserted: list[tuple[int, AccountingEvent]] = []
            for event in events:
                sequence, created = self._insert_event(connection, event)
                sequences.append(sequence)
                if created:
                    inserted.append((sequence, event))
            if not inserted:
                return tuple(sequences)
            persisted = _load_events(connection, 0)
            for sequence, event in inserted:
                _write_event_detail(connection, sequence, event)
                _write_projection(connection, sequence, state)
            _write_snapshot(
                connection, max(sequence for sequence, _ in inserted), persisted,
                state, hedge_mark, hedge_liquidation,
            )
            return tuple(sequences)

    def _insert_event(
        self,
        connection: sqlite3.Connection,
        event: AccountingEvent,
    ) -> tuple[int, bool]:
        canonical = canonical_event_json(event)
        existing = connection.execute(
            "SELECT sequence, event_json FROM accounting_events WHERE event_id = ?",
            (event.event_id,),
        ).fetchone()
        if existing is not None:
            if str(existing["event_json"]) == canonical:
                return int(existing["sequence"]), False
            raise DuplicateAccountingIdentifierError(f"conflicting event ID: {event.event_id}")
        execution_id, funding_id, fee_id = _identifiers(event)
        for column, identifier, label in (
            ("execution_id", execution_id, "execution"),
            ("funding_id", funding_id, "funding"),
            ("fee_id", fee_id, "fee"),
        ):
            if identifier is None:
                continue
            row = connection.execute(
                f"SELECT sequence, event_json FROM accounting_events WHERE {column} = ?",
                (identifier,),
            ).fetchone()
            if row is None:
                continue
            prior = event_from_dict(json.loads(str(row["event_json"])))
            if _same_identifier_content(prior, event):
                return int(row["sequence"]), False
            raise DuplicateAccountingIdentifierError(f"conflicting {label} ID: {identifier}")
        cursor = connection.execute(
            """
            INSERT INTO accounting_events(
                event_id, event_type, event_json, execution_id, funding_id, fee_id, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                (
                    event.migration_kind.value
                    if isinstance(event, MigratedFromLegacySnapshot)
                    else type(event).__name__
                ),
                canonical,
                execution_id,
                funding_id,
                fee_id,
                event.timestamp.isoformat(),
            ),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("accounting event insert did not return a sequence")
        return int(cursor.lastrowid), True

    def _load_events_after(self, sequence: int) -> tuple[AccountingEvent, ...]:
        with self._connect() as connection:
            return _load_events(connection, sequence)

    def _replay(self, reconstructor: CombinedLedgerReconstructor) -> AccountingReplayResult:
        with self._connect() as connection:
            events = _load_events(connection, 0)
            snapshot = connection.execute(
                "SELECT * FROM accounting_snapshots ORDER BY last_sequence DESC LIMIT 1"
            ).fetchone()
        if not events:
            state = reconstructor.reconstruct(())
            return AccountingReplayResult(state, 0, 0, state.ledger_digest, state.ledger_digest, True)
        if snapshot is None:
            raise AccountingContractError("accounting events exist without a replay snapshot")
        hedge_mark = _price_or_none(snapshot["hedge_mark"])
        hedge_liquidation = _price_or_none(snapshot["hedge_liquidation"])
        full_state = reconstructor.reconstruct(
            events,
            hedge_mark=hedge_mark,
            hedge_liquidation=hedge_liquidation,
        )
        prefix_payload = json.loads(str(snapshot["replay_events_json"]))
        if not isinstance(prefix_payload, list) or not all(
            isinstance(item, dict) for item in prefix_payload
        ):
            raise AccountingContractError("persisted accounting replay snapshot is invalid")
        prefix = tuple(event_from_dict(item) for item in prefix_payload)
        last_sequence = int(snapshot["last_sequence"])
        with self._connect() as connection:
            event_rows = _load_event_rows(connection, 0)
        tail = tuple(event for sequence, event in event_rows if sequence > last_sequence)
        snapshot_tail_state = reconstructor.reconstruct(
            (*prefix, *tail),
            hedge_mark=hedge_mark,
            hedge_liquidation=hedge_liquidation,
        )
        stored_state = _parse_state(str(snapshot["state_json"]))
        snapshot_digest_matches = (
            _state_digest(stored_state) == str(snapshot["snapshot_digest"])
            and stored_state == snapshot_tail_state.to_dict()
            and str(snapshot["ledger_digest"]) == snapshot_tail_state.ledger_digest
        )
        return AccountingReplayResult(
            state=full_state,
            event_count=len(events),
            last_sequence=max((sequence for sequence, _ in event_rows), default=0),
            full_replay_digest=full_state.ledger_digest,
            snapshot_tail_digest=snapshot_tail_state.ledger_digest,
            snapshot_digest_matches=snapshot_digest_matches,
        )

    def _persist_reconciliation_result(
        self,
        report: AccountingReconciliationReport,
        ledger_digest: str,
        recorded_at: datetime,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO accounting_reconciliation_results(
                    ledger_digest, trading_allowed, differences_json, recorded_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    ledger_digest,
                    int(report.trading_allowed),
                    json.dumps(
                        [
                            {"kind": item.kind.value, "detail": item.detail}
                            for item in report.differences
                        ],
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    recorded_at.isoformat(),
                ),
            )

    def _legacy_migration_pending(self) -> bool:
        with self._connect() as connection:
            migrated = connection.execute(
                "SELECT 1 FROM accounting_events WHERE event_type = ? LIMIT 1",
                ("MIGRATED_FROM_LEGACY_SNAPSHOT",),
            ).fetchone()
            latest = connection.execute(
                """
                SELECT trading_allowed FROM accounting_reconciliation_results
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
        return migrated is not None and (latest is None or not bool(latest["trading_allowed"]))

    def _schema_version(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT MAX(version) AS version FROM accounting_schema_migrations"
            ).fetchone()
        return 0 if row is None or row["version"] is None else int(row["version"])

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection


def _identifiers(event: AccountingEvent) -> tuple[str | None, str | None, str | None]:
    if isinstance(event, (OptionExecutionRecorded, HedgeExecutionRecorded)):
        return event.execution.execution_id, None, None
    if isinstance(event, FundingRecorded):
        return None, event.funding_id, None
    if isinstance(event, FeeRecorded):
        return None, None, event.fee_id
    return None, None, None


def _same_identifier_content(left: AccountingEvent, right: AccountingEvent) -> bool:
    if isinstance(left, OptionExecutionRecorded) and isinstance(right, OptionExecutionRecorded):
        return (left.execution, left.leg) == (right.execution, right.leg)
    if isinstance(left, HedgeExecutionRecorded) and isinstance(right, HedgeExecutionRecorded):
        return (
            left.execution,
            left.lot_id,
            left.attempt,
            left.role,
            left.exit_reason,
            left.reference_type,
            left.reference_price,
        ) == (
            right.execution,
            right.lot_id,
            right.attempt,
            right.role,
            right.exit_reason,
            right.reference_type,
            right.reference_price,
        )
    if isinstance(left, FundingRecorded) and isinstance(right, FundingRecorded):
        return (
            left.symbol,
            left.position_quantity,
            left.rate,
            left.amount,
            left.allocations,
            left.timestamp,
        ) == (
            right.symbol,
            right.position_quantity,
            right.rate,
            right.amount,
            right.allocations,
            right.timestamp,
        )
    if isinstance(left, FeeRecorded) and isinstance(right, FeeRecorded):
        return (left.owner, left.amount, left.currency, left.timestamp) == (
            right.owner,
            right.amount,
            right.currency,
            right.timestamp,
        )
    return False


def _load_events(connection: sqlite3.Connection, sequence: int) -> tuple[AccountingEvent, ...]:
    return tuple(event for _, event in _load_event_rows(connection, sequence))


def _load_event_rows(
    connection: sqlite3.Connection,
    sequence: int,
) -> tuple[tuple[int, AccountingEvent], ...]:
    rows = connection.execute(
        "SELECT sequence, event_json FROM accounting_events WHERE sequence > ? ORDER BY sequence",
        (sequence,),
    ).fetchall()
    return tuple(
        (int(row["sequence"]), event_from_dict(json.loads(str(row["event_json"]))))
        for row in rows
    )


def _write_event_detail(
    connection: sqlite3.Connection,
    sequence: int,
    event: AccountingEvent,
) -> None:
    if isinstance(event, (OptionExecutionRecorded, HedgeExecutionRecorded)):
        owner = "OPTION" if isinstance(event, OptionExecutionRecorded) else "HEDGE"
        connection.execute(
            "INSERT INTO fee_records(fee_id, event_sequence, owner, amount, currency) VALUES (?, ?, ?, ?, ?)",
            (
                event.execution.execution_id,
                sequence,
                owner,
                str(event.execution.fee.value),
                event.execution.fee_currency,
            ),
        )
        if isinstance(event, HedgeExecutionRecorded) and event.reference_price is not None:
            if event.reference_type is None:
                raise AssertionError("hedge reference price requires its type")
            connection.execute(
                "INSERT INTO reference_prices(event_sequence, reference_type, symbol, price, timestamp) VALUES (?, ?, ?, ?, ?)",
                (
                    sequence,
                    event.reference_type.value,
                    event.symbol,
                    str(event.reference_price.value),
                    event.timestamp.isoformat(),
                ),
            )
    elif isinstance(event, FeeRecorded):
        connection.execute(
            "INSERT INTO fee_records(fee_id, event_sequence, owner, amount, currency) VALUES (?, ?, ?, ?, ?)",
            (event.fee_id, sequence, event.owner.value, str(event.amount.value), event.currency),
        )
    elif isinstance(event, FundingRecorded):
        if event.symbol is None:
            raise AccountingContractError("funding record requires a symbol")
        connection.execute(
            """
            INSERT INTO funding_records(
                funding_id, event_sequence, symbol, position_quantity, rate, amount, allocations_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.funding_id,
                sequence,
                event.symbol,
                str(event.position_quantity.value),
                str(event.rate),
                str(event.amount.value),
                json.dumps(
                    [
                        {"lot_id": allocation.lot_id, "amount": str(allocation.amount.value)}
                        for allocation in event.allocations
                    ],
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            ),
        )
    elif isinstance(event, ReferencePriceRecorded):
        connection.execute(
            "INSERT INTO reference_prices(event_sequence, reference_type, symbol, price, timestamp) VALUES (?, ?, ?, ?, ?)",
            (
                sequence,
                event.reference_type.value,
                event.symbol,
                str(event.price.value),
                event.timestamp.isoformat(),
            ),
        )
    elif isinstance(event, OptionQuoteRecorded):
        for reference_type, price in (
            ("OPTION_BID", event.bid),
            ("OPTION_ASK", event.ask),
            ("OPTION_MARK", event.mark),
        ):
            connection.execute(
                "INSERT INTO reference_prices(event_sequence, reference_type, symbol, price, timestamp) VALUES (?, ?, ?, ?, ?)",
                (
                    sequence,
                    reference_type,
                    event.symbol,
                    str(price.value),
                    event.timestamp.isoformat(),
                ),
            )
    elif isinstance(event, RecoveryDebtChanged) and event.actual_recovery_allocation.value:
        connection.execute(
            "INSERT INTO recovery_allocations(event_sequence, amount, reason) VALUES (?, ?, ?)",
            (sequence, str(event.actual_recovery_allocation.value), event.reason),
        )
    elif isinstance(event, RecoveryAllocationRecorded) and event.allocated_amount.value:
        connection.execute(
            "INSERT INTO recovery_allocations(event_sequence, amount, reason) VALUES (?, ?, ?)",
            (
                sequence,
                str(event.allocated_amount.value),
                f"actual recovery allocation:{event.target.cycle_id}:"
                f"{event.target.level_id}:{event.target.attempt}",
            ),
        )


def _write_projection(
    connection: sqlite3.Connection,
    sequence: int,
    state: CombinedLedgerState,
) -> None:
    for leg in (state.option.long, state.option.short):
        connection.execute(
            """
            INSERT INTO option_lots(
                snapshot_sequence, leg, symbol, open_quantity, realized_pnl, entry_fees, exit_fees
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sequence,
                leg.side.value,
                leg.symbol,
                str(leg.open_quantity),
                str(leg.realized_pnl.value),
                str(leg.entry_fees.value),
                str(leg.exit_fees.value),
            ),
        )
    for lot in state.hedge.lots:
        connection.execute(
            """
            INSERT INTO hedge_lots(
                snapshot_sequence, lot_id, open_quantity, gross_realized_pnl,
                entry_fees, exit_fees, funding_pnl, debt_increment
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sequence,
                lot.lot_id,
                str(lot.open_quantity),
                str(lot.gross_realized_pnl.value),
                str(lot.entry_fees.value),
                str(lot.exit_fees.value),
                str(lot.allocated_funding.value),
                str(lot.debt_increment.value),
            ),
        )
    connection.execute(
        "INSERT INTO slippage_attributions(snapshot_sequence, amount) VALUES (?, ?)",
        (sequence, str(state.slippage_attribution.value)),
    )


def _write_snapshot(
    connection: sqlite3.Connection,
    sequence: int,
    events: tuple[AccountingEvent, ...],
    state: CombinedLedgerState,
    hedge_mark: Price | None,
    hedge_liquidation: Price | None,
) -> None:
    state_json = json.dumps(state.to_dict(), separators=(",", ":"), sort_keys=True)
    connection.execute(
        """
        INSERT INTO accounting_snapshots(
            last_sequence, event_count, ledger_digest, snapshot_digest, state_json,
            replay_events_json, hedge_mark, hedge_liquidation, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            sequence,
            len(events),
            state.ledger_digest,
            _state_digest(json.loads(state_json)),
            state_json,
            json.dumps(
                [event_to_dict(event) for event in events],
                separators=(",", ":"),
                sort_keys=True,
            ),
            None if hedge_mark is None else str(hedge_mark.value),
            None if hedge_liquidation is None else str(hedge_liquidation.value),
            state.as_of.isoformat(),
        ),
    )


def _state_digest(state: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(state, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()


def _parse_state(payload: str) -> dict[str, object]:
    parsed = json.loads(payload)
    if not isinstance(parsed, dict):
        raise AccountingContractError("persisted accounting state must be an object")
    return parsed


def _price_or_none(value: object) -> Price | None:
    return None if value is None else Price(Decimal(str(value)))
