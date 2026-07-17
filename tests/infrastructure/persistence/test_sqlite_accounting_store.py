"""M2.5 atomic persistence and exact restart replay tests."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from eth_credit_hedge.application.accounting_reconciliation import (
    AccountingReconciliationService,
    LegacyAccountingMigrator,
)
from eth_credit_hedge.domain.accounting.errors import DuplicateAccountingIdentifierError
from eth_credit_hedge.domain.accounting.events import (
    AccountingEvent,
    EventSource,
    HedgeExecutionRecorded,
    MigratedFromLegacySnapshot,
    OptionExecutionRecorded,
    event_from_dict,
)
from eth_credit_hedge.domain.accounting.reconciliation import (
    AccountingDifferenceKind,
    AccountingExchangeState,
)
from eth_credit_hedge.domain.accounting.reconstruction import CombinedLedgerReconstructor
from eth_credit_hedge.domain.strategy_math.units import Money, Quantity
from eth_credit_hedge.infrastructure.persistence.sqlite_accounting_store import (
    SqliteAccountingStore,
)


D = Decimal
NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)
ROOT = Path(__file__).parents[3]
FIXTURES = ROOT / "tests" / "fixtures" / "accounting"


def _events(filename: str) -> tuple[AccountingEvent, ...]:
    return tuple(
        event_from_dict(json.loads(line))
        for line in (FIXTURES / filename).read_text(encoding="utf-8").splitlines()
        if line
    )


async def _persist(
    store: SqliteAccountingStore,
    events: tuple[AccountingEvent, ...],
) -> None:
    reconstructor = CombinedLedgerReconstructor()
    recorded: list[AccountingEvent] = []
    for event in events:
        recorded.append(event)
        await store.append_event_and_snapshot(
            event,
            reconstructor.reconstruct(recorded),
            hedge_mark=None,
            hedge_liquidation=None,
        )


def test_atomic_event_projection_and_snapshot_rollback(tmp_path: Path) -> None:
    async def exercise() -> None:
        database = tmp_path / "accounting.sqlite3"
        store = SqliteAccountingStore(database)
        await store.initialize()
        quote = _events("m2_4_option_quotes.jsonl")[0]
        with sqlite3.connect(database) as connection:
            connection.execute(
                """
                CREATE TRIGGER fail_accounting_snapshot
                BEFORE INSERT ON accounting_snapshots
                BEGIN
                    SELECT RAISE(ABORT, 'snapshot failure');
                END
                """
            )
        with pytest.raises(sqlite3.IntegrityError, match="snapshot failure"):
            await store.append_event_and_snapshot(
                quote,
                CombinedLedgerReconstructor().reconstruct((quote,)),
                hedge_mark=None,
                hedge_liquidation=None,
            )
        assert await store.load_events_after(0) == ()
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT COUNT(*) FROM option_lots").fetchone()[0] == 0

    asyncio.run(exercise())


def test_duplicates_recovery_funding_and_restart_replay_are_exact(tmp_path: Path) -> None:
    async def exercise() -> None:
        database = tmp_path / "accounting.sqlite3"
        all_events = (*_events("m2_4_option_quotes.jsonl"), *_events("m2_4_combined_events.jsonl"))
        store = SqliteAccountingStore(database)
        await store.initialize()
        await _persist(store, all_events[:6])

        restarted = SqliteAccountingStore(database)
        await restarted.initialize()
        recovered = (
            *all_events[:6],
            *(replace(event, source=EventSource.REST_RECOVERY) for event in all_events[6:]),
        )
        await _persist(restarted, recovered)
        replay = await restarted.replay(CombinedLedgerReconstructor())

        assert replay.event_count == len(all_events)
        assert replay.full_replay_digest == replay.snapshot_tail_digest
        assert replay.snapshot_digest_matches
        assert replay.state.confirmed_recovery_debt == Money(D("4"))
        assert replay.state.funding_pnl == Money(D("0"))
        assert replay.state.net_combined_mark_pnl == Money(D("-3"))

        duplicate = replace(all_events[2], event_id="rest-recovery-option-fill")
        duplicate_state = CombinedLedgerReconstructor().reconstruct((*all_events, duplicate))
        duplicate_sequence = await restarted.append_event_and_snapshot(
            duplicate,
            duplicate_state,
            hedge_mark=None,
            hedge_liquidation=None,
        )
        assert duplicate_sequence == 3
        assert len(await restarted.load_events_after(0)) == len(all_events)
        with pytest.raises(DuplicateAccountingIdentifierError, match="execution ID"):
            await restarted.append_event_and_snapshot(
                replace(
                    duplicate,
                    execution=replace(duplicate.execution, fee=Money(D("2"))),
                ),
                duplicate_state,
                hedge_mark=None,
                hedge_liquidation=None,
            )

        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT COUNT(*) FROM funding_records").fetchone()[0] == 2
            assert connection.execute("SELECT COUNT(*) FROM fee_records").fetchone()[0] == 6
            assert connection.execute("SELECT COUNT(*) FROM recovery_allocations").fetchone()[0] == 1
            assert connection.execute("SELECT COUNT(*) FROM reference_prices").fetchone()[0] == 7

    asyncio.run(exercise())


def test_digest_mismatch_and_unknown_external_state_block_trading(tmp_path: Path) -> None:
    async def exercise() -> None:
        database = tmp_path / "accounting.sqlite3"
        events = (*_events("m2_4_option_quotes.jsonl"), *_events("m2_4_combined_events.jsonl"))
        store = SqliteAccountingStore(database)
        await store.initialize()
        await _persist(store, events)
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE accounting_snapshots SET ledger_digest = 'wrong'")

        service = AccountingReconciliationService(
            store=store,
            reconstructor=CombinedLedgerReconstructor(),
            state_reader=StaticAccountingReader(None),
            clock=lambda: NOW,
        )
        result = await service.reconcile()
        kinds = {difference.kind for difference in result.report.differences}
        assert not result.report.trading_allowed
        assert AccountingDifferenceKind.REPLAY_DIGEST_MISMATCH in kinds
        assert AccountingDifferenceKind.UNKNOWN_EXTERNAL_STATE in kinds

    asyncio.run(exercise())


def test_authoritative_service_matches_or_blocks_orders_and_executions(tmp_path: Path) -> None:
    async def exercise() -> None:
        events = (*_events("m2_4_option_quotes.jsonl"), *_events("m2_4_combined_events.jsonl"))
        store = SqliteAccountingStore(tmp_path / "accounting.sqlite3")
        await store.initialize()
        await _persist(store, events)
        state = (await store.replay(CombinedLedgerReconstructor())).state
        executions = tuple(
            event
            for event in events
            if isinstance(event, (OptionExecutionRecorded, HedgeExecutionRecorded))
        )
        matching = AccountingExchangeState(
            option_quantities={
                state.option.long.symbol: Quantity(state.option.long.open_quantity),
                state.option.short.symbol: Quantity(state.option.short.open_quantity),
            },
            hedge_short_quantity=D("0"),
            total_fees=Money(D("6")),
            funding_pnl=Money(D("0")),
            order_ids=frozenset(event.execution.order_id for event in executions),
            execution_ids=frozenset(event.execution.execution_id for event in executions),
        )
        service = AccountingReconciliationService(
            store=store,
            reconstructor=CombinedLedgerReconstructor(),
            state_reader=StaticAccountingReader(matching),
            clock=lambda: NOW,
        )
        assert (await service.reconcile()).report.trading_allowed

        missing_execution = AccountingExchangeState(
            option_quantities=matching.option_quantities,
            hedge_short_quantity=D("0"),
            total_fees=Money(D("6")),
            funding_pnl=Money(D("0")),
            order_ids=matching.order_ids,
            execution_ids=frozenset(),
        )
        blocked = AccountingReconciliationService(
            store=store,
            reconstructor=CombinedLedgerReconstructor(),
            state_reader=StaticAccountingReader(missing_execution),
            clock=lambda: NOW,
        )
        assert any(
            difference.kind is AccountingDifferenceKind.EXECUTION_MISMATCH
            for difference in (await blocked.reconcile()).report.differences
        )

        unknown_position = AccountingExchangeState(
            option_quantities={
                **matching.option_quantities,
                "ETH-UNKNOWN-P": Quantity(D("1")),
            },
            hedge_short_quantity=D("0"),
            total_fees=Money(D("6")),
            funding_pnl=Money(D("0")),
            order_ids=matching.order_ids,
            execution_ids=matching.execution_ids,
        )
        position_blocked = AccountingReconciliationService(
            store=store,
            reconstructor=CombinedLedgerReconstructor(),
            state_reader=StaticAccountingReader(unknown_position),
            clock=lambda: NOW,
        )
        assert any(
            difference.kind is AccountingDifferenceKind.OPTION_POSITION_MISMATCH
            for difference in (await position_blocked.reconcile()).report.differences
        )

    asyncio.run(exercise())


def test_fee_mismatch_blocks_and_explicit_legacy_migration_requires_reconciliation(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        database = tmp_path / "accounting.sqlite3"
        events = (*_events("m2_4_option_quotes.jsonl"), *_events("m2_4_combined_events.jsonl"))
        store = SqliteAccountingStore(database)
        await store.initialize()
        await _persist(store, events)
        state = (await store.replay(CombinedLedgerReconstructor())).state
        service = AccountingReconciliationService(
            store=store,
            reconstructor=CombinedLedgerReconstructor(),
            state_reader=StaticAccountingReader(
                AccountingExchangeState(
                    option_quantities={},
                    hedge_short_quantity=D("0"),
                    total_fees=Money(D("5")),
                    funding_pnl=Money(D("0")),
                )
            ),
            clock=lambda: NOW,
        )
        result = await service.reconcile()
        assert state.ledger_digest == result.state.ledger_digest
        assert any(
            difference.kind is AccountingDifferenceKind.FEE_MISMATCH
            for difference in result.report.differences
        )

        migrated = SqliteAccountingStore(tmp_path / "migration.sqlite3")
        await migrated.initialize()
        migration = MigratedFromLegacySnapshot(
            event_id="legacy-snapshot-1",
            event_version=1,
            cycle_id="legacy-cycle",
            timestamp=NOW,
            source=EventSource.MIGRATION,
            correlation_id="legacy-correlation",
            legacy_snapshot_type="ProtectionSnapshot",
            legacy_snapshot_key="legacy-entry-order",
            legacy_payload_digest="a" * 64,
        )
        migrated_state = await LegacyAccountingMigrator(
            store=migrated,
            reconstructor=CombinedLedgerReconstructor(),
        ).migrate(migration)
        migration_service = AccountingReconciliationService(
            store=migrated,
            reconstructor=CombinedLedgerReconstructor(),
            state_reader=StaticAccountingReader(
                AccountingExchangeState(
                    option_quantities={},
                    hedge_short_quantity=D("0"),
                    total_fees=Money(D("0")),
                    funding_pnl=Money(D("0")),
                )
            ),
            clock=lambda: NOW,
        )
        migration_result = await migration_service.reconcile()
        assert migrated_state.ledger_digest == migration_result.state.ledger_digest
        assert not migration_result.report.trading_allowed
        assert any(
            difference.kind
            is AccountingDifferenceKind.LEGACY_MIGRATION_REQUIRES_RECONCILIATION
            for difference in migration_result.report.differences
        )
        assert migration.execution_id is None
        assert migration.migration_kind.value == "MIGRATED_FROM_LEGACY_SNAPSHOT"

    asyncio.run(exercise())


class StaticAccountingReader:
    def __init__(self, state: AccountingExchangeState | None) -> None:
        self.state = state

    async def capture_accounting_state(self) -> AccountingExchangeState | None:
        return self.state
