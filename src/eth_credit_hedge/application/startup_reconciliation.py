"""Conservative startup comparison of durable local and private exchange state."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from eth_credit_hedge.application.read_only_reconciliation import (
    ExpectedPosition,
    PrivateAccountSnapshot,
)
from eth_credit_hedge.domain.client_order_ids import ClientOrderId
from eth_credit_hedge.domain.execution import (
    ExchangeOrder,
    ExchangePosition,
    ExecutionUpdate,
    LiveExecutionState,
    PlaceOrderRequest,
)
from eth_credit_hedge.domain.live_execution import EntryExecutionSnapshot
from eth_credit_hedge.domain.journal import (
    CycleSnapshot,
    JournalEventType,
    PendingJournalEvent,
)
from eth_credit_hedge.domain.protected_execution import ProtectionSnapshot
from eth_credit_hedge.domain.reconciliation import (
    ReconciliationReport,
    ReconciliationStatus,
    RepairAction,
    RepairActionKind,
    StateDifference,
    StateDifferenceKind,
)
from eth_credit_hedge.ports.journal import JournalPersistencePort
from eth_credit_hedge.ports.persistence import ExecutionPersistencePort
from eth_credit_hedge.application.startup_replay import (
    StartupReplayResult,
    StartupReplayService,
)


ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class LocalExecutionRecoveryState:
    order_intents: tuple[PlaceOrderRequest, ...]
    entry_snapshots: tuple[EntryExecutionSnapshot, ...]
    protection_snapshots: tuple[ProtectionSnapshot, ...]
    expected_option_positions: tuple[ExpectedPosition, ...]
    executions: tuple[ExecutionUpdate, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "order_intents",
            "entry_snapshots",
            "protection_snapshots",
            "expected_option_positions",
            "executions",
        ):
            object.__setattr__(self, field_name, tuple(getattr(self, field_name)))
        order_ids = [request.order_link_id for request in self.order_intents]
        if len(order_ids) != len(set(order_ids)):
            raise ValueError("local order intents must have unique client IDs")
        entry_ids = [snapshot.order_link_id for snapshot in self.entry_snapshots]
        if len(entry_ids) != len(set(entry_ids)):
            raise ValueError("local entry snapshots must have unique client IDs")
        protection_ids = [
            snapshot.entry_order_link_id for snapshot in self.protection_snapshots
        ]
        if len(protection_ids) != len(set(protection_ids)):
            raise ValueError("protection snapshots must have unique entries")
        execution_ids = [execution.execution_id for execution in self.executions]
        if len(execution_ids) != len(set(execution_ids)):
            raise ValueError("local executions must have unique execution IDs")


class PrivateStateReaderPort(Protocol):
    async def capture(self) -> PrivateAccountSnapshot: ...


@dataclass(frozen=True, slots=True)
class StartupReconciliationResult:
    replay: StartupReplayResult
    local: LocalExecutionRecoveryState
    exchange: PrivateAccountSnapshot
    report: ReconciliationReport
    committed_snapshot: CycleSnapshot


class StartupReconciliationService:
    """Replay locally, query exchange, then durably record allow/suspend."""

    def __init__(
        self,
        *,
        execution_store: ExecutionPersistencePort,
        journal_store: JournalPersistencePort,
        replay_service: StartupReplayService,
        private_reader: PrivateStateReaderPort,
        expected_option_positions: tuple[ExpectedPosition, ...],
        clock: Callable[[], datetime],
        event_id_factory: Callable[[JournalEventType], str],
    ) -> None:
        self._execution_store = execution_store
        self._journal_store = journal_store
        self._replay_service = replay_service
        self._private_reader = private_reader
        self._expected_option_positions = tuple(expected_option_positions)
        self._clock = clock
        self._event_id_factory = event_id_factory

    async def reconcile(
        self,
        *,
        cycle_id: str,
        strategy_instance: str,
        cycle_number: int,
    ) -> StartupReconciliationResult:
        replay = await self._replay_service.rebuild(cycle_id)
        local = LocalExecutionRecoveryState(
            order_intents=(
                await self._execution_store.load_all_order_intents()
            ),
            entry_snapshots=(
                await self._execution_store.load_all_entry_snapshots()
            ),
            protection_snapshots=(
                await self._execution_store.load_all_protection_snapshots()
            ),
            expected_option_positions=self._expected_option_positions,
            executions=await self._execution_store.load_all_executions(),
        )
        exchange = await self._private_reader.capture()
        report = evaluate_startup_reconciliation(
            local,
            exchange,
            strategy_instance=strategy_instance,
            cycle_number=cycle_number,
        )
        committed_at = self._clock()
        state = dict(replay.state)
        state["trading_allowed"] = report.trading_allowed
        state["reconciliation_status"] = report.status.value
        state["reconciliation_differences"] = [
            {
                "kind": difference.kind.value,
                "detail": difference.detail,
                "order_link_id": difference.order_link_id,
            }
            for difference in report.differences
        ]
        state["repair_actions"] = [
            {
                "kind": action.kind.value,
                "detail": action.detail,
                "order_link_id": action.order_link_id,
            }
            for action in report.repair_actions
        ]
        event_type = (
            JournalEventType.RECONCILIATION_COMPLETED
            if report.trading_allowed
            else JournalEventType.TRADING_SUSPENDED
        )
        event = PendingJournalEvent(
            event_id=self._event_id_factory(event_type),
            event_type=event_type,
            event_version=1,
            cycle_id=cycle_id,
            level_id=None,
            timestamp_utc=committed_at,
            payload={
                "status": report.status.value,
                "trading_allowed": report.trading_allowed,
                "difference_count": len(report.differences),
            },
            causation_id=None,
            correlation_id=f"{cycle_id}-startup-reconciliation",
        )
        _, committed_snapshot = await self._journal_store.append_event_and_snapshot(
            event,
            CycleSnapshot(
                cycle_id=cycle_id,
                last_event_sequence=replay.last_event_sequence,
                state=state,
                snapshot_version=1,
                updated_at_utc=committed_at,
            ),
        )
        return StartupReconciliationResult(
            replay=replay,
            local=local,
            exchange=exchange,
            report=report,
            committed_snapshot=committed_snapshot,
        )


def evaluate_startup_reconciliation(
    local: LocalExecutionRecoveryState,
    exchange: PrivateAccountSnapshot,
    *,
    strategy_instance: str,
    cycle_number: int,
) -> ReconciliationReport:
    intents = {request.order_link_id: request for request in local.order_intents}
    open_orders = {order.order_link_id: order for order in exchange.open_orders}
    recent_orders = {order.order_link_id: order for order in exchange.recent_orders}
    differences: list[StateDifference] = []
    actions: list[RepairAction] = []
    severity = ReconciliationStatus.MATCHED

    for order in exchange.open_orders:
        if order.order_link_id in intents:
            continue
        owned = _is_owned_strategy_order(
            order,
            strategy_instance=strategy_instance,
            cycle_number=cycle_number,
        )
        differences.append(
            StateDifference(
                kind=StateDifferenceKind.UNKNOWN_EXCHANGE_ORDER,
                detail=(
                    f"exchange order {order.order_id} is not in the local journal"
                ),
                order_link_id=order.order_link_id,
            )
        )
        if owned:
            actions.append(
                RepairAction(
                    kind=RepairActionKind.IMPORT_ORDER,
                    detail="import strategy-owned order after operator review",
                    order_link_id=order.order_link_id,
                )
            )
            severity = _more_severe(severity, ReconciliationStatus.REPAIRABLE)
        else:
            severity = _more_severe(severity, ReconciliationStatus.AMBIGUOUS)

    local_execution_ids = {
        execution.execution_id for execution in local.executions
    }
    for execution in exchange.executions:
        if execution.execution_id in local_execution_ids:
            continue
        known_order = execution.order_link_id in intents
        owned_order = _is_owned_order_link_id(
            execution.order_link_id,
            strategy_instance=strategy_instance,
            cycle_number=cycle_number,
        )
        kind = (
            StateDifferenceKind.MISSING_LOCAL_EXECUTION
            if known_order or owned_order
            else StateDifferenceKind.UNKNOWN_EXCHANGE_EXECUTION
        )
        differences.append(
            StateDifference(
                kind=kind,
                detail=(
                    f"exchange execution {execution.execution_id} is absent locally"
                ),
                order_link_id=execution.order_link_id,
            )
        )
        if known_order or owned_order:
            actions.append(
                RepairAction(
                    kind=RepairActionKind.IMPORT_EXECUTION,
                    detail="persist and replay the missing exchange execution",
                    order_link_id=execution.order_link_id,
                )
            )
            severity = _more_severe(severity, ReconciliationStatus.REPAIRABLE)
        else:
            severity = _more_severe(severity, ReconciliationStatus.AMBIGUOUS)

    entry_by_id = {
        snapshot.order_link_id: snapshot for snapshot in local.entry_snapshots
    }
    for entry_snapshot in local.entry_snapshots:
        if entry_snapshot.state not in (
            LiveExecutionState.ENTRY_SUBMITTED,
            LiveExecutionState.ENTRY_ACKNOWLEDGED,
            LiveExecutionState.ENTRY_PARTIALLY_FILLED,
        ):
            continue
        if entry_snapshot.order_link_id not in open_orders and (
            entry_snapshot.order_link_id not in recent_orders
        ):
            differences.append(
                StateDifference(
                    kind=StateDifferenceKind.MISSING_EXCHANGE_ORDER,
                    detail="entry request is absent from open orders and history",
                    order_link_id=entry_snapshot.order_link_id,
                )
            )
            severity = _more_severe(severity, ReconciliationStatus.AMBIGUOUS)

    protected_entry_ids: set[str] = set()
    for protection_snapshot in local.protection_snapshots:
        protected_entry_ids.add(protection_snapshot.entry_order_link_id)
        if protection_snapshot.open_quantity == ZERO:
            continue
        stop = open_orders.get(protection_snapshot.stop_order_link_id)
        stop_intent = intents.get(protection_snapshot.stop_order_link_id)
        if stop is None:
            differences.append(
                StateDifference(
                    kind=StateDifferenceKind.MISSING_PROTECTION,
                    detail="confirmed local short has no visible exchange stop",
                    order_link_id=protection_snapshot.stop_order_link_id,
                )
            )
            actions.append(
                RepairAction(
                    kind=RepairActionKind.RESTORE_PROTECTION,
                    detail="submit and confirm a replacement reduce-only stop",
                    order_link_id=protection_snapshot.stop_order_link_id,
                )
            )
            severity = _more_severe(severity, ReconciliationStatus.REPAIRABLE)
        elif stop_intent is None or not _order_matches_intent(stop, stop_intent):
            differences.append(
                StateDifference(
                    kind=StateDifferenceKind.PROTECTION_MISMATCH,
                    detail="visible stop does not match its persisted intent",
                    order_link_id=protection_snapshot.stop_order_link_id,
                )
            )
            severity = _more_severe(severity, ReconciliationStatus.DANGEROUS)

        if protection_snapshot.tp_order_link_id is not None:
            tp = open_orders.get(protection_snapshot.tp_order_link_id)
            tp_intent = intents.get(protection_snapshot.tp_order_link_id)
            if tp is None:
                differences.append(
                    StateDifference(
                        kind=StateDifferenceKind.MISSING_TAKE_PROFIT,
                        detail="local TP is not visible on the exchange",
                        order_link_id=protection_snapshot.tp_order_link_id,
                    )
                )
                actions.append(
                    RepairAction(
                        kind=RepairActionKind.RESTORE_TAKE_PROFIT,
                        detail="submit and confirm a replacement reduce-only TP",
                        order_link_id=protection_snapshot.tp_order_link_id,
                    )
                )
                severity = _more_severe(
                    severity,
                    ReconciliationStatus.REPAIRABLE,
                )
            elif tp_intent is None or not _order_matches_intent(tp, tp_intent):
                differences.append(
                    StateDifference(
                        kind=StateDifferenceKind.PROTECTION_MISMATCH,
                        detail="visible TP does not match its persisted intent",
                        order_link_id=protection_snapshot.tp_order_link_id,
                    )
                )
                severity = _more_severe(severity, ReconciliationStatus.DANGEROUS)

    for entry_id, entry in entry_by_id.items():
        if (
            entry.filled_quantity > ZERO
            and entry_id not in protected_entry_ids
            and entry.state
            not in (LiveExecutionState.RECONCILING, LiveExecutionState.ERROR)
        ):
            differences.append(
                StateDifference(
                    kind=StateDifferenceKind.MISSING_PROTECTION,
                    detail="filled entry has no durable protection state",
                    order_link_id=entry_id,
                )
            )
            actions.append(
                RepairAction(
                    kind=RepairActionKind.RESTORE_PROTECTION,
                    detail="create and confirm stop protection",
                    order_link_id=entry_id,
                )
            )
            severity = _more_severe(severity, ReconciliationStatus.REPAIRABLE)

    expected_perp_quantity = _expected_perp_quantity(local)
    actual_perp = tuple(
        position
        for position in exchange.positions
        if position.category == "linear"
        and position.symbol == "ETHUSDT"
        and position.quantity > ZERO
    )
    if not _perp_position_matches(expected_perp_quantity, actual_perp):
        kind = (
            StateDifferenceKind.UNKNOWN_EXCHANGE_POSITION
            if expected_perp_quantity == ZERO and actual_perp
            else StateDifferenceKind.POSITION_MISMATCH
        )
        differences.append(
            StateDifference(
                kind=kind,
                detail=(
                    f"local ETHUSDT short {expected_perp_quantity} does not match "
                    f"exchange positions {tuple(p.quantity for p in actual_perp)}"
                ),
            )
        )
        severity = _more_severe(severity, ReconciliationStatus.DANGEROUS)

    if not _option_positions_match(
        local.expected_option_positions,
        exchange.positions,
    ):
        differences.append(
            StateDifference(
                kind=StateDifferenceKind.UNKNOWN_OPTION_POSITION,
                detail="exchange option positions do not match confirmed local legs",
            )
        )
        severity = _more_severe(severity, ReconciliationStatus.DANGEROUS)

    return ReconciliationReport(
        status=severity,
        differences=tuple(differences),
        repair_actions=tuple(actions),
        trading_allowed=severity is ReconciliationStatus.MATCHED,
    )


def _expected_perp_quantity(local: LocalExecutionRecoveryState) -> Decimal:
    protected = {
        snapshot.entry_order_link_id: snapshot.open_quantity
        for snapshot in local.protection_snapshots
    }
    quantity = sum(protected.values(), ZERO)
    quantity += sum(
        (
            snapshot.filled_quantity
            for snapshot in local.entry_snapshots
            if snapshot.order_link_id not in protected
        ),
        ZERO,
    )
    return quantity


def _perp_position_matches(
    expected_quantity: Decimal,
    positions: tuple[ExchangePosition, ...],
) -> bool:
    if expected_quantity == ZERO:
        return not positions
    return (
        len(positions) == 1
        and positions[0].side == "Sell"
        and positions[0].quantity == expected_quantity
    )


def _option_positions_match(
    expected: tuple[ExpectedPosition, ...],
    actual: tuple[ExchangePosition, ...],
) -> bool:
    expected_quantities = {
        (position.symbol, position.side): position.quantity for position in expected
    }
    actual_quantities = {
        (position.symbol, position.side): position.quantity
        for position in actual
        if position.category == "option" and position.quantity > ZERO
    }
    return expected_quantities == actual_quantities


def _is_owned_strategy_order(
    order: ExchangeOrder,
    *,
    strategy_instance: str,
    cycle_number: int,
) -> bool:
    return _is_owned_order_link_id(
        order.order_link_id,
        strategy_instance=strategy_instance,
        cycle_number=cycle_number,
    )


def _is_owned_order_link_id(
    order_link_id: str,
    *,
    strategy_instance: str,
    cycle_number: int,
) -> bool:
    try:
        parsed = ClientOrderId.parse(order_link_id)
    except ValueError:
        return False
    return (
        parsed.strategy_instance == strategy_instance
        and parsed.cycle == cycle_number
    )


def _order_matches_intent(
    order: ExchangeOrder,
    request: PlaceOrderRequest,
) -> bool:
    return (
        order.category == request.category
        and order.symbol == request.symbol
        and order.side == request.side
        and order.order_type == request.order_type
        and order.price == request.price
        and order.quantity == request.quantity
        and order.reduce_only == request.reduce_only
        and order.trigger_price == request.trigger_price
        and order.trigger_by == request.trigger_by
        and order.trigger_direction == request.trigger_direction
        and order.time_in_force == request.time_in_force
        and order.position_idx == request.position_idx
    )


_SEVERITY = {
    ReconciliationStatus.MATCHED: 0,
    ReconciliationStatus.REPAIRABLE: 1,
    ReconciliationStatus.AMBIGUOUS: 2,
    ReconciliationStatus.DANGEROUS: 3,
}


def _more_severe(
    current: ReconciliationStatus,
    candidate: ReconciliationStatus,
) -> ReconciliationStatus:
    return candidate if _SEVERITY[candidate] > _SEVERITY[current] else current
