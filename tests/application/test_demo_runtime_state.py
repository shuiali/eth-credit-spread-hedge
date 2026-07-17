"""Replayable integrated demo runtime state."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from eth_credit_hedge.application.demo_runtime_state import (
    DemoLevelRuntimeState,
    DemoRuntimeState,
    demo_runtime_state_from_payload,
    demo_runtime_state_to_payload,
    reduce_demo_runtime_state,
)
from eth_credit_hedge.application.demo_runtime_journal import DemoRuntimeJournal
from eth_credit_hedge.domain.journal import (
    JournalEvent,
    JournalEventType,
)
from eth_credit_hedge.infrastructure.persistence.sqlite_journal_store import (
    SqliteJournalStore,
)


NOW = datetime(2026, 7, 15, tzinfo=timezone.utc)


def initial_state() -> DemoRuntimeState:
    return DemoRuntimeState(
        cycle_id="DEMO-1",
        short_option_symbol="ETH-31JUL26-1800-P-USDT",
        long_option_symbol="ETH-31JUL26-1750-P-USDT",
        option_quantity=Decimal("0.1"),
        levels=(
            DemoLevelRuntimeState(
                level_id=1,
                entry_price=Decimal("1800"),
                take_profit_price=Decimal("1750"),
                stop_price=Decimal("1802.7"),
                option_budget=Decimal("5"),
            ),
        ),
    )


def event(
    sequence: int,
    event_type: JournalEventType,
    payload: dict[str, object],
    *,
    event_id: str | None = None,
) -> JournalEvent:
    return JournalEvent(
        sequence=sequence,
        event_id=event_id or f"event-{sequence}",
        event_type=event_type,
        event_version=1,
        cycle_id="DEMO-1",
        level_id=1,
        timestamp_utc=NOW,
        payload=payload,
        causation_id=None,
        correlation_id="test",
    )


def test_payload_round_trip_is_exact() -> None:
    state = initial_state()
    assert demo_runtime_state_from_payload(
        demo_runtime_state_to_payload(state)
    ) == state


def test_reducer_traces_stop_recovery_and_take_profit() -> None:
    payload = demo_runtime_state_to_payload(initial_state())
    events = (
        event(
            1,
            JournalEventType.VIRTUAL_LEVEL_ARMED,
            {"connection_generation": 1},
        ),
        event(
            2,
            JournalEventType.HEDGE_ENTRY_INTENT_CREATED,
            {
                "order_link_id": "ENTRY-1",
                "role": "BASELINE",
                "allocated_debt": "0",
            },
        ),
        event(
            3,
            JournalEventType.STOP_RECEIVED,
            {
                "realized_pnl": "-0.25",
                "actual_stop_debt": "0.25",
                "confirmed_debt": "0.25",
            },
        ),
        event(
            4,
            JournalEventType.HEDGE_ENTRY_INTENT_CREATED,
            {
                "order_link_id": "ENTRY-2",
                "role": "RECOVERY",
                "allocated_debt": "0.25",
            },
        ),
        event(
            5,
            JournalEventType.TAKE_PROFIT_RECEIVED,
            {"realized_pnl": "0.30", "remaining_debt": "0"},
        ),
    )
    for current in events:
        payload = reduce_demo_runtime_state(payload, current)

    state = demo_runtime_state_from_payload(payload)
    assert state.level(1).state.value == "PAID"
    assert state.level(1).confirmed_debt == Decimal("0")
    assert state.level(1).active_entry_order_link_id is None
    assert state.daily_realized_pnl == Decimal("0.05")


def test_duplicate_public_event_is_idempotent() -> None:
    payload = demo_runtime_state_to_payload(initial_state())
    armed = event(
        1,
        JournalEventType.VIRTUAL_LEVEL_ARMED,
        {"connection_generation": 2},
        event_id="trade-1",
    )
    once = reduce_demo_runtime_state(payload, armed)
    twice = reduce_demo_runtime_state(once, armed)
    assert twice == once


def test_reducer_starts_from_cycle_created_event() -> None:
    created = JournalEvent(
        sequence=1,
        event_id="created",
        event_type=JournalEventType.STRATEGY_CYCLE_CREATED,
        event_version=1,
        cycle_id="DEMO-1",
        level_id=None,
        timestamp_utc=NOW,
        payload={"runtime_state": demo_runtime_state_to_payload(initial_state())},
        causation_id=None,
        correlation_id="test",
    )
    rebuilt = demo_runtime_state_from_payload(
        reduce_demo_runtime_state({}, created)
    )
    assert rebuilt.cycle_id == "DEMO-1"
    assert rebuilt.processed_event_ids == ("created",)


def test_runtime_journal_restores_byte_equivalent_state(tmp_path: Path) -> None:
    asyncio.run(_assert_runtime_journal_restores_byte_equivalent_state(tmp_path))


async def _assert_runtime_journal_restores_byte_equivalent_state(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.sqlite3"
    store = SqliteJournalStore(path)
    await store.initialize()
    identifiers = iter(("created", "armed", "entry"))
    journal = await DemoRuntimeJournal.create(
        store=store,
        state=initial_state(),
        clock=lambda: NOW,
        event_id_factory=lambda: next(identifiers),
    )
    await journal.append(
        JournalEventType.VIRTUAL_LEVEL_ARMED,
        level_id=1,
        payload={"connection_generation": 1},
    )
    await journal.append(
        JournalEventType.HEDGE_ENTRY_INTENT_CREATED,
        level_id=1,
        payload={
            "order_link_id": "ENTRY-1",
            "role": "BASELINE",
            "allocated_debt": "0",
        },
    )

    restored = await DemoRuntimeJournal.restore(
        store=store,
        cycle_id="DEMO-1",
        clock=lambda: NOW,
    )
    assert demo_runtime_state_to_payload(restored.state) == (
        demo_runtime_state_to_payload(journal.state)
    )
    assert restored.last_event_sequence == journal.last_event_sequence


def test_runtime_journal_does_not_append_duplicate_event(tmp_path: Path) -> None:
    asyncio.run(_assert_runtime_journal_does_not_append_duplicate_event(tmp_path))


async def _assert_runtime_journal_does_not_append_duplicate_event(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.sqlite3"
    store = SqliteJournalStore(path)
    await store.initialize()
    journal = await DemoRuntimeJournal.create(
        store=store,
        state=initial_state(),
        clock=lambda: NOW,
        event_id_factory=lambda: "created",
    )
    await journal.append(
        JournalEventType.VIRTUAL_LEVEL_ARMED,
        level_id=1,
        event_id="trade-1",
        payload={"connection_generation": 1},
    )
    sequence = journal.last_event_sequence
    await journal.append(
        JournalEventType.VIRTUAL_LEVEL_ARMED,
        level_id=1,
        event_id="trade-1",
        payload={"connection_generation": 1},
    )
    assert journal.last_event_sequence == sequence


def test_runtime_journal_serializes_concurrent_appends(tmp_path: Path) -> None:
    asyncio.run(_assert_runtime_journal_serializes_concurrent_appends(tmp_path))


async def _assert_runtime_journal_serializes_concurrent_appends(
    tmp_path: Path,
) -> None:
    store = SqliteJournalStore(tmp_path / "concurrent-runtime.sqlite3")
    await store.initialize()
    journal = await DemoRuntimeJournal.create(
        store=store,
        state=initial_state(),
        clock=lambda: NOW,
        event_id_factory=lambda: "created",
    )
    await asyncio.gather(
        journal.append(
            JournalEventType.VIRTUAL_LEVEL_ARMED,
            level_id=1,
            event_id="armed-concurrently",
            payload={"connection_generation": 1},
        ),
        journal.append(
            JournalEventType.RECONCILIATION_COMPLETED,
            event_id="reconciled-concurrently",
            payload={},
        ),
    )

    restored = await DemoRuntimeJournal.restore(
        store=store,
        cycle_id="DEMO-1",
        clock=lambda: NOW,
    )
    assert restored.last_event_sequence == 3
    assert set(restored.state.processed_event_ids) == {
        "created",
        "armed-concurrently",
        "reconciled-concurrently",
    }
