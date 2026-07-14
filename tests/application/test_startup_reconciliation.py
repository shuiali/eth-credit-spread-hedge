"""Startup order, position, and protection reconciliation rules."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from eth_credit_hedge.application.read_only_reconciliation import (
    ExpectedPosition,
    PrivateAccountSnapshot,
)
from eth_credit_hedge.application.startup_reconciliation import (
    LocalExecutionRecoveryState,
    StartupReconciliationService,
    evaluate_startup_reconciliation,
)
from eth_credit_hedge.application.startup_replay import StartupReplayService
from eth_credit_hedge.domain.execution import (
    ExchangeOrder,
    ExchangePosition,
    ExecutionUpdate,
    LiveExecutionState,
    PlaceOrderRequest,
    WalletState,
)
from eth_credit_hedge.domain.live_execution import EntryExecutionSnapshot
from eth_credit_hedge.domain.journal import JournalEventType
from eth_credit_hedge.domain.protected_execution import ProtectionSnapshot
from eth_credit_hedge.domain.reconciliation import (
    ReconciliationStatus,
    RepairActionKind,
    StateDifferenceKind,
)
from eth_credit_hedge.infrastructure.persistence.sqlite_journal_store import (
    SqliteJournalStore,
)


NOW = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
ENTRY_ID = "ECH-01-C0001-L01-ENTRY-A01-9F3C"
STOP_ID = "ECH-01-C0001-L01-STOP-A01-9F3C"
TP_ID = "ECH-01-C0001-L01-TP-A01-9F3C"


def entry_intent() -> PlaceOrderRequest:
    return PlaceOrderRequest(
        category="linear",
        symbol="ETHUSDT",
        side="Sell",
        order_type="Limit",
        quantity=Decimal("0.010"),
        order_link_id=ENTRY_ID,
        price=Decimal("3000"),
        time_in_force="IOC",
    )


def stop_intent() -> PlaceOrderRequest:
    return PlaceOrderRequest(
        category="linear",
        symbol="ETHUSDT",
        side="Buy",
        order_type="Market",
        quantity=Decimal("0.010"),
        order_link_id=STOP_ID,
        reduce_only=True,
        trigger_price=Decimal("3004.5"),
        trigger_direction=1,
        trigger_by="LastPrice",
    )


def tp_intent() -> PlaceOrderRequest:
    return PlaceOrderRequest(
        category="linear",
        symbol="ETHUSDT",
        side="Buy",
        order_type="Limit",
        quantity=Decimal("0.010"),
        order_link_id=TP_ID,
        price=Decimal("2900"),
        reduce_only=True,
    )


def entry_snapshot() -> EntryExecutionSnapshot:
    return EntryExecutionSnapshot(
        order_link_id=ENTRY_ID,
        state=LiveExecutionState.ACTIVE_UNPROTECTED,
        target_quantity=Decimal("0.010"),
        entry_order_id="entry-order",
        filled_quantity=Decimal("0.010"),
        entry_notional=Decimal("30"),
        entry_fees=Decimal("0.003"),
        version=4,
        updated_at=NOW,
    )


def protection_snapshot() -> ProtectionSnapshot:
    return ProtectionSnapshot(
        entry_order_link_id=ENTRY_ID,
        state=LiveExecutionState.ACTIVE_PROTECTED,
        entry_quantity=Decimal("0.010"),
        open_quantity=Decimal("0.010"),
        average_entry_price=Decimal("3000"),
        entry_fees=Decimal("0.003"),
        stop_order_link_id=STOP_ID,
        stop_order_id="stop-order",
        stop_trigger_price=Decimal("3004.5"),
        tp_order_link_id=TP_ID,
        tp_order_id="tp-order",
        tp_price=Decimal("2900"),
        tp_filled_quantity=Decimal("0"),
        stop_filled_quantity=Decimal("0"),
        exit_notional=Decimal("0"),
        exit_fees=Decimal("0"),
        confirmed_recovery_debt=Decimal("0"),
        pending_terminal_state=None,
        version=4,
        updated_at=NOW,
    )


def exchange_order(request: PlaceOrderRequest, order_id: str) -> ExchangeOrder:
    return ExchangeOrder(
        category=request.category,
        order_id=order_id,
        order_link_id=request.order_link_id,
        symbol=request.symbol,
        status="Untriggered" if request.trigger_price is not None else "New",
        side=request.side,
        order_type=request.order_type,
        price=request.price,
        quantity=request.quantity,
        cumulative_filled_quantity=Decimal("0"),
        average_price=None,
        reduce_only=request.reduce_only,
        created_at=NOW,
        updated_at=NOW,
        trigger_price=request.trigger_price,
        trigger_by=request.trigger_by,
        trigger_direction=request.trigger_direction,
        time_in_force=request.time_in_force,
        position_idx=request.position_idx,
    )


def short_position(quantity: str = "0.010") -> ExchangePosition:
    return ExchangePosition(
        category="linear",
        symbol="ETHUSDT",
        side="Sell",
        quantity=Decimal(quantity),
        average_price=Decimal("3000"),
        mark_price=Decimal("2999"),
        unrealized_pnl=Decimal("0"),
        updated_at=NOW,
    )


def local_state(*, protected: bool = True) -> LocalExecutionRecoveryState:
    return LocalExecutionRecoveryState(
        order_intents=(
            entry_intent(),
            *((stop_intent(), tp_intent()) if protected else ()),
        ),
        entry_snapshots=(entry_snapshot(),),
        protection_snapshots=(protection_snapshot(),) if protected else (),
        expected_option_positions=(),
    )


def exchange_snapshot(
    *,
    orders: tuple[ExchangeOrder, ...] | None = None,
    positions: tuple[ExchangePosition, ...] | None = None,
    option_positions: tuple[ExchangePosition, ...] = (),
    executions: tuple[ExecutionUpdate, ...] = (),
) -> PrivateAccountSnapshot:
    return PrivateAccountSnapshot(
        open_orders=(
            (
                exchange_order(stop_intent(), "stop-order"),
                exchange_order(tp_intent(), "tp-order"),
            )
            if orders is None
            else orders
        ),
        recent_orders=(exchange_order(entry_intent(), "entry-order"),),
        executions=executions,
        positions=(
            *((short_position(),) if positions is None else positions),
            *option_positions,
        ),
        wallet=WalletState(
            account_type="UNIFIED",
            total_equity=Decimal("1000"),
            total_wallet_balance=Decimal("1000"),
            total_available_balance=Decimal("900"),
            balances=(),
            updated_at=NOW,
        ),
        captured_at_utc=NOW,
    )


def test_exact_orders_position_and_protection_match() -> None:
    report = evaluate_startup_reconciliation(
        local_state(),
        exchange_snapshot(),
        strategy_instance="01",
        cycle_number=1,
    )

    assert report.status is ReconciliationStatus.MATCHED
    assert report.differences == ()
    assert report.repair_actions == ()
    assert report.trading_allowed


def test_strategy_owned_unknown_order_is_repairable_but_not_auto_imported() -> None:
    owned = PlaceOrderRequest(
        category="linear",
        symbol="ETHUSDT",
        side="Buy",
        order_type="Limit",
        quantity=Decimal("0.010"),
        order_link_id="ECH-01-C0001-L01-TP-A02-ABCD",
        price=Decimal("2899"),
        reduce_only=True,
    )
    report = evaluate_startup_reconciliation(
        local_state(),
        exchange_snapshot(
            orders=(
                exchange_order(stop_intent(), "stop-order"),
                exchange_order(tp_intent(), "tp-order"),
                exchange_order(owned, "unknown-owned-order"),
            )
        ),
        strategy_instance="01",
        cycle_number=1,
    )

    assert report.status is ReconciliationStatus.REPAIRABLE
    assert not report.trading_allowed
    assert report.repair_actions[0].kind is RepairActionKind.IMPORT_ORDER


def test_foreign_or_unparseable_unknown_order_is_ambiguous() -> None:
    foreign = replace(
        exchange_order(tp_intent(), "foreign-order"),
        order_link_id="manual-order",
    )

    report = evaluate_startup_reconciliation(
        local_state(),
        exchange_snapshot(
            orders=(
                exchange_order(stop_intent(), "stop-order"),
                exchange_order(tp_intent(), "tp-order"),
                foreign,
            )
        ),
        strategy_instance="01",
        cycle_number=1,
    )

    assert report.status is ReconciliationStatus.AMBIGUOUS
    assert not report.trading_allowed
    assert report.differences[-1].kind is StateDifferenceKind.UNKNOWN_EXCHANGE_ORDER


def test_missing_stop_is_repairable_and_position_mismatch_is_dangerous() -> None:
    missing_stop = evaluate_startup_reconciliation(
        local_state(),
        exchange_snapshot(orders=(exchange_order(tp_intent(), "tp-order"),)),
        strategy_instance="01",
        cycle_number=1,
    )
    mismatch = evaluate_startup_reconciliation(
        local_state(),
        exchange_snapshot(positions=(short_position("0.009"),)),
        strategy_instance="01",
        cycle_number=1,
    )

    assert missing_stop.status is ReconciliationStatus.REPAIRABLE
    assert any(
        action.kind is RepairActionKind.RESTORE_PROTECTION
        for action in missing_stop.repair_actions
    )
    assert mismatch.status is ReconciliationStatus.DANGEROUS
    assert mismatch.differences[-1].kind is StateDifferenceKind.POSITION_MISMATCH


def test_unknown_option_position_is_dangerous() -> None:
    unknown_option = ExchangePosition(
        category="option",
        symbol="ETH-31JUL26-3000-P-USDT",
        side="Sell",
        quantity=Decimal("0.01"),
        average_price=Decimal("60"),
        mark_price=Decimal("55"),
        unrealized_pnl=Decimal("0.05"),
        updated_at=NOW,
    )

    report = evaluate_startup_reconciliation(
        local_state(),
        exchange_snapshot(option_positions=(unknown_option,)),
        strategy_instance="01",
        cycle_number=1,
    )

    assert report.status is ReconciliationStatus.DANGEROUS
    assert report.differences[-1].kind is StateDifferenceKind.UNKNOWN_OPTION_POSITION


def test_exchange_execution_missing_locally_requires_import_before_resume() -> None:
    execution = ExecutionUpdate(
        execution_id="late-execution",
        order_id="entry-order",
        order_link_id=ENTRY_ID,
        symbol="ETHUSDT",
        side="Sell",
        price=Decimal("3000"),
        quantity=Decimal("0.010"),
        fee=Decimal("0.003"),
        is_maker=False,
        executed_at=NOW,
    )

    report = evaluate_startup_reconciliation(
        local_state(),
        exchange_snapshot(executions=(execution,)),
        strategy_instance="01",
        cycle_number=1,
    )

    assert report.status is ReconciliationStatus.REPAIRABLE
    assert not report.trading_allowed
    assert report.differences[-1].kind is StateDifferenceKind.MISSING_LOCAL_EXECUTION
    assert report.repair_actions[-1].kind is RepairActionKind.IMPORT_EXECUTION


def test_expected_option_position_can_reconcile_exactly() -> None:
    option = ExchangePosition(
        category="option",
        symbol="ETH-31JUL26-3000-P-USDT",
        side="Sell",
        quantity=Decimal("0.01"),
        average_price=Decimal("60"),
        mark_price=Decimal("55"),
        unrealized_pnl=Decimal("0.05"),
        updated_at=NOW,
    )
    local = LocalExecutionRecoveryState(
        order_intents=local_state().order_intents,
        entry_snapshots=local_state().entry_snapshots,
        protection_snapshots=local_state().protection_snapshots,
        expected_option_positions=(
            ExpectedPosition(
                category="option",
                symbol=option.symbol,
                side="Sell",
                quantity=option.quantity,
            ),
        ),
    )

    report = evaluate_startup_reconciliation(
        local,
        exchange_snapshot(option_positions=(option,)),
        strategy_instance="01",
        cycle_number=1,
    )

    assert report.status is ReconciliationStatus.MATCHED


class FakeExecutionRecoveryStore:
    def __init__(self, local: LocalExecutionRecoveryState) -> None:
        self.local = local

    async def load_all_order_intents(self) -> tuple[PlaceOrderRequest, ...]:
        return self.local.order_intents

    async def load_all_entry_snapshots(self) -> tuple[EntryExecutionSnapshot, ...]:
        return self.local.entry_snapshots

    async def load_all_protection_snapshots(
        self,
    ) -> tuple[ProtectionSnapshot, ...]:
        return self.local.protection_snapshots

    async def load_all_executions(self) -> tuple[ExecutionUpdate, ...]:
        return self.local.executions


class StaticPrivateReader:
    def __init__(self, snapshot: PrivateAccountSnapshot) -> None:
        self.snapshot = snapshot
        self.capture_calls = 0

    async def capture(self) -> PrivateAccountSnapshot:
        self.capture_calls += 1
        return self.snapshot


def test_startup_commits_resume_only_after_a_matched_exchange_query(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        journal = SqliteJournalStore(tmp_path / "journal.sqlite3")
        await journal.initialize()
        reader = StaticPrivateReader(exchange_snapshot())
        replay = StartupReplayService(
            store=journal,
            reducer=lambda state, event: state,
        )
        service = StartupReconciliationService(
            execution_store=FakeExecutionRecoveryStore(local_state()),
            journal_store=journal,
            replay_service=replay,
            private_reader=reader,
            expected_option_positions=(),
            clock=lambda: NOW,
            event_id_factory=lambda event_type: f"event-{event_type.value}",
        )

        result = await service.reconcile(
            cycle_id="cycle-0001",
            strategy_instance="01",
            cycle_number=1,
        )

        assert reader.capture_calls == 1
        assert result.report.status is ReconciliationStatus.MATCHED
        assert result.committed_snapshot.state["trading_allowed"] is True
        events = await journal.load_events_after("cycle-0001", 0)
        assert events[-1].event_type is JournalEventType.RECONCILIATION_COMPLETED

    asyncio.run(exercise())


def test_startup_durably_suspends_on_dangerous_position_mismatch(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        journal = SqliteJournalStore(tmp_path / "journal.sqlite3")
        await journal.initialize()
        replay = StartupReplayService(
            store=journal,
            reducer=lambda state, event: state,
        )
        service = StartupReconciliationService(
            execution_store=FakeExecutionRecoveryStore(local_state()),
            journal_store=journal,
            replay_service=replay,
            private_reader=StaticPrivateReader(
                exchange_snapshot(positions=(short_position("0.009"),))
            ),
            expected_option_positions=(),
            clock=lambda: NOW,
            event_id_factory=lambda event_type: f"event-{event_type.value}",
        )

        result = await service.reconcile(
            cycle_id="cycle-0001",
            strategy_instance="01",
            cycle_number=1,
        )

        assert result.report.status is ReconciliationStatus.DANGEROUS
        assert result.committed_snapshot.state["trading_allowed"] is False
        events = await journal.load_events_after("cycle-0001", 0)
        assert events[-1].event_type is JournalEventType.TRADING_SUSPENDED

    asyncio.run(exercise())
