"""Long-lived multi-level coordinator for the integrated demo runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from eth_credit_hedge.application.demo_runtime_journal import DemoRuntimeJournal
from eth_credit_hedge.application.demo_runtime_state import LiveHedgeRole
from eth_credit_hedge.application.hedge_lot_allocation import (
    AllocationReconciliationError,
    HedgeLotAllocationService,
)
from eth_credit_hedge.application.net_position_allocator import HedgeLot
from eth_credit_hedge.application.one_level_lifecycle import (
    OneLevelLifecycleService,
)
from eth_credit_hedge.application.runtime_risk_state import (
    RuntimeRiskStateBuilder,
)
from eth_credit_hedge.application.same_level_recovery import (
    SameLevelRecoveryService,
)
from eth_credit_hedge.config import StrategyCostConfig
from eth_credit_hedge.core.virtual_levels import HedgeLevel, LevelState
from eth_credit_hedge.domain.client_order_ids import ClientOrderRole
from eth_credit_hedge.domain.accounting.events import HedgeRole as AccountingHedgeRole
from eth_credit_hedge.domain.accounting.hedge_ledger import HedgeLotSnapshot
from eth_credit_hedge.domain.accounting.reconstruction import CombinedLedgerState
from eth_credit_hedge.domain.execution import LiveExecutionState, PlaceOrderRequest
from eth_credit_hedge.domain.instrument_rules import (
    PriceQuantizationPolicy,
    ceil_to_step,
    normalize_and_validate_order,
    recalculate_quantized_risk,
)
from eth_credit_hedge.domain.instruments import InstrumentSpec
from eth_credit_hedge.domain.journal import JournalEventType
from eth_credit_hedge.domain.live_recovery import RecoveryDebtState, SameLevelRecoveryPlanner
from eth_credit_hedge.domain.market_data import TriggerPriceEvent, TriggerPriceSource
from eth_credit_hedge.domain.risk import RiskEngine, RiskLimits, TradeProposal
from eth_credit_hedge.domain.strategy_math import (
    EntryPercentStopConfig,
    InstrumentRules,
    ExpirationOptionValuation,
    Money,
    Price,
    PriceStepFractionStopConfig,
    Quantity,
    Rate,
    SizingStatus,
    StopGeometryEngine,
    StopMode,
    StrategyMathEngine,
)
from eth_credit_hedge.ports.account import AccountPort
from eth_credit_hedge.ports.control import EntryGatePort
from eth_credit_hedge.ports.persistence import ExecutionPersistencePort


ZERO = Decimal("0")


class _TaskSpawner(Protocol):
    def __call__(
        self,
        coroutine: Coroutine[Any, Any, None],
    ) -> asyncio.Task[None]: ...


OrderLinkIdFactory = Callable[[int, ClientOrderRole, int], str]
LifecycleFactory = Callable[[], OneLevelLifecycleService]
Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class LiveTriggerResult:
    scheduled_levels: tuple[int, ...]
    blocked: tuple[tuple[int, tuple[str, ...]], ...]
    reasons: tuple[str, ...] = ()


class LiveStrategyCoordinator:
    """Own baseline, same-level recovery, protection, and exit transitions."""

    def __init__(
        self,
        *,
        journal: DemoRuntimeJournal,
        account: AccountPort,
        store: ExecutionPersistencePort,
        instrument: InstrumentSpec,
        risk_engine: RiskEngine,
        risk_limits: RiskLimits,
        risk_state_builder: RuntimeRiskStateBuilder,
        recovery_service: SameLevelRecoveryService,
        recovery_planner: SameLevelRecoveryPlanner,
        lifecycle_factory: LifecycleFactory,
        order_link_id_factory: OrderLinkIdFactory,
        task_spawner: _TaskSpawner,
        clock: Callable[[], datetime],
        entry_gate: EntryGatePort | None = None,
        sleeper: Sleep = asyncio.sleep,
        exit_poll_interval_seconds: float = 0.25,
        costs: StrategyCostConfig | None = None,
        math_engine: StrategyMathEngine | None = None,
        accounting_refresh: Callable[[], Awaitable[CombinedLedgerState]] | None = None,
        allocation_service: HedgeLotAllocationService | None = None,
    ) -> None:
        if instrument.category != "linear" or instrument.symbol != "ETHUSDT":
            raise ValueError("live coordinator requires ETHUSDT linear")
        for level in journal.state.levels:
            stop = (
                EntryPercentStopConfig(Rate(level.stop_parameter))
                if level.stop_mode is StopMode.ENTRY_PERCENT
                else PriceStepFractionStopConfig(Rate(level.stop_parameter))
            )
            expected = StopGeometryEngine.stop_price(
                Price(level.entry_price),
                Price(level.entry_price - level.take_profit_price),
                stop,
            )
            if level.stop_price != expected.value:
                raise ValueError(
                    "level stop distance does not match its explicit stop mode"
                )
        if exit_poll_interval_seconds < 0:
            raise ValueError("exit poll interval cannot be negative")
        self._journal = journal
        self._account = account
        self._store = store
        self._instrument = instrument
        self._risk_engine = risk_engine
        self._risk_limits = risk_limits
        self._risk_builder = risk_state_builder
        self._recovery = recovery_service
        self._recovery_planner = recovery_planner
        self._lifecycle_factory = lifecycle_factory
        self._order_link_id_factory = order_link_id_factory
        self._spawn = task_spawner
        self._clock = clock
        self._entry_gate = entry_gate
        self._sleeper = sleeper
        self._exit_poll_interval_seconds = exit_poll_interval_seconds
        self._costs = costs or StrategyCostConfig()
        self._math_engine = math_engine or StrategyMathEngine(
            ExpirationOptionValuation()
        )
        self._accounting_refresh = accounting_refresh
        self._allocation = allocation_service
        self._previous_price: Decimal | None = None
        self._connection_generation: int | None = None
        self._pending_levels: set[int] = set()
        self._deferred_levels: set[int] = set()
        self._entry_lock = asyncio.Lock()

    async def on_trigger(self, event: TriggerPriceEvent) -> LiveTriggerResult:
        if event.symbol != self._instrument.symbol:
            return LiveTriggerResult((), (), ("trigger symbol does not match",))
        if event.source is not TriggerPriceSource.LAST_TRADE:
            return LiveTriggerResult((), (), ("trigger source is not LAST_TRADE",))
        if self._entry_gate is not None and not self._entry_gate.entries_allowed:
            return LiveTriggerResult((), (), ("kill switch blocks new entries",))
        state = self._journal.state
        if not state.reconciliation_complete or state.suspended_reason is not None:
            return LiveTriggerResult((), (), ("runtime is not reconciled",))
        if self._allocation is not None and not any(
            level.active_quantity > ZERO for level in state.levels
        ):
            positions = await self._account.get_positions("linear", "ETHUSDT")
            try:
                await self._allocation.reconcile_exchange_position(positions)
            except AllocationReconciliationError as error:
                await self._journal.append(
                    JournalEventType.TRADING_SUSPENDED,
                    payload={"reason": f"hedge lot reconciliation fault: {error}"},
                )
                return LiveTriggerResult((), (), (str(error),))
        if self._connection_generation is None:
            await self._reset_segment(event)
            return LiveTriggerResult((), ())
        if event.connection_generation < self._connection_generation:
            return LiveTriggerResult((), (), ("connection generation is stale",))
        if event.connection_generation > self._connection_generation:
            await self._reset_segment(event)
            return LiveTriggerResult(
                (),
                (),
                ("connection generation changed; crossing fenced",),
            )
        previous = self._previous_price
        if previous is None:
            raise AssertionError("initialized segment requires a previous price")
        current = event.observed_price
        await self._arm_eligible(current, event.connection_generation)
        crossed = tuple(
            level
            for level in self._journal.state.levels
            if level.armed
            and level.state is LevelState.READY
            and level.active_entry_order_link_id is None
            and level.level_id not in self._pending_levels
            and current <= level.entry_price
            and (
                previous > level.entry_price
                or level.level_id in self._deferred_levels
            )
        )
        self._previous_price = current

        scheduled: list[int] = []
        blocked: list[tuple[int, tuple[str, ...]]] = []
        reserved_quantity = ZERO
        reserved_notional = ZERO
        for level in crossed:
            if current <= level.take_profit_price:
                blocked.append(
                    (
                        level.level_id,
                        ("price gap is already through the level take profit",),
                    )
                )
                self._deferred_levels.discard(level.level_id)
                continue
            if any(
                higher.level_id < level.level_id
                and higher.active_entry_order_link_id is not None
                for higher in self._journal.state.levels
            ):
                blocked.append(
                    (
                        level.level_id,
                        ("higher-level exit must settle before the next entry",),
                    )
                )
                self._deferred_levels.add(level.level_id)
                continue
            positions, wallet = await asyncio.gather(
                self._account.get_positions("linear", "ETHUSDT"),
                self._account.get_wallet_state(),
            )
            hedge_level = _hedge_level(level)
            ledger_state = await self._require_accounting_projection()
            level_debt = ledger_state.debt_for_level(
                self._journal.state.cycle_id,
                level.level_id,
            ).value
            role = (
                LiveHedgeRole.RECOVERY
                if level_debt > ZERO
                else LiveHedgeRole.BASELINE
            )
            if role is LiveHedgeRole.BASELINE:
                sizing = self._math_engine.size_budget(
                    role="BASELINE",
                    zone_option_loss_budget=Money(level.option_budget),
                    confirmed_recovery_debt=Money(ZERO),
                    configured_buffer=Money(self._costs.baseline_buffer_usd),
                    costs=self._costs.execution_context(
                        entry_price=level.entry_price,
                        tp_price=level.take_profit_price,
                        stop_price=level.stop_price,
                    ),
                    instrument=InstrumentRules(
                        quantity_step=Quantity(
                            self._instrument.lot_size_filter.qty_step
                        ),
                        minimum_quantity=Quantity(
                            self._instrument.lot_size_filter.min_order_qty
                        ),
                        maximum_quantity=Quantity(
                            min(
                                self._instrument.lot_size_filter.max_order_qty,
                                self._instrument.lot_size_filter.max_market_order_qty
                                or self._instrument.lot_size_filter.max_order_qty,
                                self._risk_limits.maximum_perp_quantity,
                            )
                        ),
                        maximum_notional=Money(
                            self._risk_limits.maximum_perp_notional
                        ),
                        maximum_projected_stop_loss=Money(
                            self._risk_limits.maximum_projected_stop_loss
                        ),
                    ),
                )
                quantized = recalculate_quantized_risk(
                    normalize_and_validate_order(
                        self._instrument,
                        side="Sell",
                        quantity=sizing.submitted_quantity.value,
                        price=level.entry_price,
                        price_policy=PriceQuantizationPolicy.PASSIVE,
                    ),
                    self._instrument,
                    entry_side="Sell",
                    take_profit_price=level.take_profit_price,
                    stop_price=level.stop_price,
                    maximum_notional=self._risk_limits.maximum_perp_notional,
                    maximum_projected_stop_loss=(
                        self._risk_limits.maximum_projected_stop_loss
                    ),
                )
                sizing_errors = list(quantized.errors)
                if sizing.status is SizingStatus.REJECTED_BY_RISK:
                    sizing_errors.append(
                        "cost-aware baseline sizing rejected by finite risk limit"
                    )
                if sizing.undercoverage.value > ZERO:
                    sizing_errors.append(
                        "quantized net TP profit undercovers baseline budget"
                    )
                if sizing_errors:
                    blocked.append((level.level_id, tuple(sizing_errors)))
                    continue
                risk_state = self._risk_builder.build(
                    runtime=self._journal.state,
                    positions=positions,
                    wallet=wallet,
                    level_id=level.level_id,
                    proposed_notional=quantized.notional + reserved_notional,
                    last_market_event_at_utc=event.observed_timestamp,
                    now_utc=self._clock(),
                    accounting=ledger_state,
                )
                risk_state = replace(
                    risk_state,
                    current_perp_quantity=(
                        risk_state.current_perp_quantity + reserved_quantity
                    ),
                    current_perp_notional=(
                        risk_state.current_perp_notional + reserved_notional
                    ),
                )
                decision = self._risk_engine.evaluate(
                    TradeProposal(
                        symbol="ETHUSDT",
                        side="Sell",
                        quantity=quantized.quantity,
                        price=quantized.entry_price,
                        notional=quantized.notional,
                        projected_stop_loss=sizing.projected_net_stop_loss.value,
                        opens_new_level=True,
                    ),
                    risk_state,
                    self._risk_limits,
                )
                if not decision.approved:
                    blocked.append((level.level_id, decision.reasons))
                    continue
                attempt = level.attempts + 1
                entry_id = self._order_link_id_factory(
                    level.level_id,
                    ClientOrderRole.HEDGE_ENTRY,
                    attempt,
                )
                await self._journal.append(
                    JournalEventType.HEDGE_ENTRY_INTENT_CREATED,
                    level_id=level.level_id,
                    payload={
                        "order_link_id": entry_id,
                        "role": role.value,
                        "allocated_debt": "0",
                        "raw_quantity": str(sizing.raw_quantity.value),
                        "submitted_quantity": str(sizing.submitted_quantity.value),
                        "net_tp_profit_per_unit": str(
                            sizing.net_tp_profit_per_unit.value
                        ),
                        "net_stop_loss_per_unit": str(
                            sizing.net_stop_loss_per_unit.value
                        ),
                        "overcoverage": str(sizing.overcoverage.value),
                        "undercoverage": str(sizing.undercoverage.value),
                        **_geometry_payload(level),
                    },
                )
                request = PlaceOrderRequest(
                    category="linear",
                    symbol="ETHUSDT",
                    side="Sell",
                    order_type="Market",
                    quantity=quantized.quantity,
                    order_link_id=entry_id,
                    time_in_force="IOC",
                    reduce_only=False,
                    position_idx=0,
                )
                self._schedule_level(
                    level.level_id,
                    self._run_baseline(
                        hedge_level,
                        request,
                        attempt=attempt,
                    ),
                )
                reserved_quantity += quantized.quantity
                reserved_notional += quantized.notional
            else:
                debt = _ledger_recovery_debt_state(
                    ledger_state,
                    cycle_id=self._journal.state.cycle_id,
                    level_id=level.level_id,
                )
                candidate_quantity = ceil_to_step(
                    (
                        level.option_budget + debt.confirmed_debt
                    ) / (level.entry_price - level.take_profit_price),
                    self._instrument.lot_size_filter.qty_step,
                )
                candidate_notional = candidate_quantity * level.entry_price
                risk_state = self._risk_builder.build(
                    runtime=self._journal.state,
                    positions=positions,
                    wallet=wallet,
                    level_id=level.level_id,
                    proposed_notional=candidate_notional + reserved_notional,
                    last_market_event_at_utc=event.observed_timestamp,
                    now_utc=self._clock(),
                    accounting=ledger_state,
                )
                risk_state = replace(
                    risk_state,
                    current_perp_quantity=(
                        risk_state.current_perp_quantity + reserved_quantity
                    ),
                    current_perp_notional=(
                        risk_state.current_perp_notional + reserved_notional
                    ),
                )
                plan = self._recovery_planner.plan(
                    hedge_level,
                    debt,
                    self._instrument,
                    risk_state,
                    self._risk_limits,
                )
                if not plan.approved or plan.quantity is None:
                    blocked.append((level.level_id, plan.reasons))
                    continue
                attempt = level.attempts + 1
                entry_id = self._order_link_id_factory(
                    level.level_id,
                    ClientOrderRole.HEDGE_ENTRY,
                    attempt,
                )
                self._schedule_level(
                    level.level_id,
                    self._run_recovery(
                        hedge_level,
                        entry_id=entry_id,
                        attempt=attempt,
                        risk_state=risk_state,
                        debt=debt,
                    ),
                )
                reserved_quantity += plan.quantity
                reserved_notional += plan.quantity * plan.entry_price
            scheduled.append(level.level_id)
            self._deferred_levels.discard(level.level_id)
        return LiveTriggerResult(tuple(scheduled), tuple(blocked))

    async def restore_active_levels(self) -> None:
        for level in self._journal.state.levels:
            if level.active_entry_order_link_id is None:
                continue
            if level.active_stop_order_link_id is None:
                raise RuntimeError(
                    f"active level {level.level_id} is not durably protected"
                )
            self._schedule_level(
                level.level_id,
                self._monitor_exit(
                    level.level_id,
                    level.active_entry_order_link_id,
                    self._lifecycle_factory(),
                ),
            )

    async def _reset_segment(self, event: TriggerPriceEvent) -> None:
        self._connection_generation = event.connection_generation
        self._previous_price = event.observed_price
        await self._arm_eligible(
            event.observed_price,
            event.connection_generation,
        )

    async def _arm_eligible(self, price: Decimal, generation: int) -> None:
        for level in self._journal.state.levels:
            if (
                price >= level.entry_price
                and level.state is LevelState.READY
                and level.active_entry_order_link_id is None
                and not level.armed
            ):
                await self._journal.append(
                    JournalEventType.VIRTUAL_LEVEL_ARMED,
                    level_id=level.level_id,
                    payload={"connection_generation": generation},
                )

    def _schedule_level(
        self,
        level_id: int,
        coroutine: Coroutine[Any, Any, None],
    ) -> None:
        self._pending_levels.add(level_id)

        async def supervised() -> None:
            try:
                await coroutine
            finally:
                self._pending_levels.discard(level_id)

        self._spawn(supervised())

    async def _run_baseline(
        self,
        level: HedgeLevel,
        request: PlaceOrderRequest,
        *,
        attempt: int,
    ) -> None:
        await self._register_lot(
            level=level,
            attempt=attempt,
            role=LiveHedgeRole.BASELINE,
            entry_order_link_id=request.order_link_id,
        )
        lifecycle = self._lifecycle_factory()
        async with self._entry_lock:
            protected = await lifecycle.open_and_protect(
                request,
                stop_order_link_id=self._order_link_id_factory(
                    level.level_id,
                    ClientOrderRole.HEDGE_STOP,
                    attempt,
                ),
                take_profit_order_link_id=self._order_link_id_factory(
                    level.level_id,
                    ClientOrderRole.HEDGE_TP,
                    attempt,
                ),
                stop_distance=level.stop_distance,
                take_profit_price=level.tp_price,
                reference_price=level.entry_price,
            )
            await self._synchronize_allocation(protected.protection)
            await self._record_protection(level.level_id, protected.protection)
        await self._monitor_exit(level.level_id, request.order_link_id, lifecycle)

    async def _run_recovery(
        self,
        level: HedgeLevel,
        *,
        entry_id: str,
        attempt: int,
        risk_state: Any,
        debt: RecoveryDebtState,
    ) -> None:
        await self._register_lot(
            level=level,
            attempt=attempt,
            role=LiveHedgeRole.RECOVERY,
            entry_order_link_id=entry_id,
        )
        async def before_submission(plan: Any) -> None:
            sizing = plan.sizing
            if sizing is None:
                raise RuntimeError("recovery plan is missing cost-aware sizing")
            await self._journal.append(
                JournalEventType.HEDGE_ENTRY_INTENT_CREATED,
                level_id=level.level_id,
                payload={
                    "order_link_id": entry_id,
                    "role": LiveHedgeRole.RECOVERY.value,
                    "allocated_debt": str(plan.allocated_debt),
                    "raw_quantity": str(sizing.raw_quantity.value),
                    "submitted_quantity": str(sizing.submitted_quantity.value),
                    "net_tp_profit_per_unit": str(
                        sizing.net_tp_profit_per_unit.value
                    ),
                    "net_stop_loss_per_unit": str(
                        sizing.net_stop_loss_per_unit.value
                    ),
                    "overcoverage": str(sizing.overcoverage.value),
                    "undercoverage": str(sizing.undercoverage.value),
                    **_geometry_payload(level),
                },
            )

        lifecycle = self._lifecycle_factory()
        async with self._entry_lock:
            submission = await self._recovery.submit_recovery_from_ledger(
                level=level,
                instrument=self._instrument,
                risk_state=risk_state,
                limits=self._risk_limits,
                order_link_id=entry_id,
                debt=debt,
                before_persisted_submission=before_submission,
            )
            if submission.entry_snapshot is None:
                raise RuntimeError("approved recovery was not submitted")
            protected = await lifecycle.protect_submitted_entry(
                submission.entry_snapshot,
                stop_order_link_id=self._order_link_id_factory(
                    level.level_id,
                    ClientOrderRole.HEDGE_STOP,
                    attempt,
                ),
                take_profit_order_link_id=self._order_link_id_factory(
                    level.level_id,
                    ClientOrderRole.HEDGE_TP,
                    attempt,
                ),
                stop_distance=level.stop_distance,
                take_profit_price=level.tp_price,
            )
            await self._synchronize_allocation(protected.protection)
            await self._record_protection(level.level_id, protected.protection)
        await self._monitor_exit(level.level_id, entry_id, lifecycle)

    async def _record_protection(self, level_id: int, protection: Any) -> None:
        if protection.tp_order_link_id is None:
            raise RuntimeError("confirmed protection is missing take profit")
        await self._journal.append(
            JournalEventType.PROTECTION_CONFIRMED,
            level_id=level_id,
            payload={
                "entry_order_link_id": protection.entry_order_link_id,
                "stop_order_link_id": protection.stop_order_link_id,
                "take_profit_order_link_id": protection.tp_order_link_id,
                "quantity": str(protection.open_quantity),
                "average_entry_price": str(protection.average_entry_price),
            },
        )

    async def _monitor_exit(
        self,
        level_id: int,
        entry_order_link_id: str,
        lifecycle: OneLevelLifecycleService,
    ) -> None:
        while True:
            snapshot = await lifecycle.poll_exit(entry_order_link_id)
            if snapshot is not None:
                break
            await self._sleeper(self._exit_poll_interval_seconds)
        state_before = self._journal.state.level(level_id)
        await self._synchronize_allocation(snapshot)
        ledger_state = await self._require_accounting_projection()
        lot = _ledger_lot_for_level_attempt(
            ledger_state,
            cycle_id=self._journal.state.cycle_id,
            level_id=level_id,
            attempt=state_before.attempts,
            role=state_before.active_role,
        )
        realized = lot.net_realized_pnl.value
        remaining_debt = ledger_state.debt_for_level(
            self._journal.state.cycle_id,
            level_id,
        ).value
        if snapshot.state is LiveExecutionState.CLOSED_STOP:
            actual_debt = lot.debt_increment.value
            await self._journal.append(
                JournalEventType.STOP_RECEIVED,
                level_id=level_id,
                payload={
                    "realized_pnl": str(realized),
                    "actual_stop_debt": str(actual_debt),
                    "confirmed_debt": str(remaining_debt),
                    **_geometry_payload(state_before),
                },
                event_id=f"exit:{snapshot.stop_order_link_id}:{snapshot.version}",
            )
            return
        if snapshot.state is not LiveExecutionState.CLOSED_TP:
            raise RuntimeError("lifecycle returned a non-terminal exit")
        await self._journal.append(
            JournalEventType.TAKE_PROFIT_RECEIVED,
            level_id=level_id,
            payload={
                "realized_pnl": str(realized),
                "remaining_debt": str(remaining_debt),
                **_geometry_payload(state_before),
            },
            event_id=f"exit:{snapshot.tp_order_link_id}:{snapshot.version}",
        )

    async def _register_lot(
        self,
        *,
        level: HedgeLevel,
        attempt: int,
        role: LiveHedgeRole,
        entry_order_link_id: str,
    ) -> None:
        if self._allocation is None:
            return
        lot_id = f"{self._journal.state.cycle_id}:L{level.level_id:02d}:A{attempt:02d}"
        await self._allocation.register(
            HedgeLot(
                lot_id=lot_id,
                cycle_id=self._journal.state.cycle_id,
                level_id=level.level_id,
                attempt=attempt,
                entry_order_link_id=entry_order_link_id,
                role=role,
                accounting_lot_id=lot_id,
            )
        )

    async def _synchronize_allocation(self, protection: Any) -> None:
        if self._allocation is None:
            return
        lot = next(
            (
                value
                for value in self._allocation.allocator.lots
                if value.entry_order_link_id == protection.entry_order_link_id
            ),
            None,
        )
        if lot is None:
            raise AllocationReconciliationError("protection has no registered hedge lot")
        if protection.tp_order_link_id is None:
            raise AllocationReconciliationError("protection is missing take-profit ownership")
        await self._allocation.bind_protection(
            lot.lot_id,
            take_profit_order_link_id=protection.tp_order_link_id,
            stop_order_link_id=protection.stop_order_link_id,
        )
        await self._allocation.apply_confirmed_executions(
            await self._store.load_all_executions()
        )
        await self._allocation.reconcile_exchange_position(
            await self._account.get_positions("linear", "ETHUSDT")
        )

    async def _require_accounting_projection(self) -> CombinedLedgerState:
        if self._accounting_refresh is None:
            raise RuntimeError(
                "ledger-owned lifecycle requires accounting projection refresh"
            )
        return await self._accounting_refresh()


def _hedge_level(level: Any) -> HedgeLevel:
    return HedgeLevel(
        level_id=level.level_id,
        entry_price=level.entry_price,
        tp_price=level.take_profit_price,
        stop_price=level.stop_price,
        option_budget=level.option_budget,
        spacing_mode=level.spacing_mode,
        stop_mode=level.stop_mode,
        stop_parameter=level.stop_parameter,
        state=level.state,
        attempts=level.attempts,
        active_quantity=level.active_quantity,
        entry_armed=level.armed,
        recovery_debt=level.confirmed_debt,
    )


def _ledger_recovery_debt_state(
    state: CombinedLedgerState,
    *,
    cycle_id: str,
    level_id: int,
) -> RecoveryDebtState:
    debt = state.debt_for_level(cycle_id, level_id).value
    return RecoveryDebtState(
        projected_debt=debt,
        confirmed_debt=debt,
        allocated_debt=ZERO,
        remaining_debt=debt,
    )


def _ledger_lot_for_level_attempt(
    state: CombinedLedgerState,
    *,
    cycle_id: str,
    level_id: int,
    attempt: int,
    role: LiveHedgeRole | None,
) -> HedgeLotSnapshot:
    expected_role = (
        AccountingHedgeRole.RECOVERY
        if role is LiveHedgeRole.RECOVERY
        else AccountingHedgeRole.BASELINE
    )
    matches = tuple(
        lot
        for lot in state.hedge.lots
        if lot.cycle_id == cycle_id
        and lot.level_id == level_id
        and lot.attempt == attempt
        and lot.role is expected_role
    )
    if len(matches) != 1:
        raise RuntimeError(
            "ledger projection does not identify exactly one lifecycle hedge lot"
        )
    return matches[0]


def _geometry_payload(level: Any) -> dict[str, str]:
    take_profit_price = (
        level.take_profit_price
        if hasattr(level, "take_profit_price")
        else level.tp_price
    )
    return {
        "entry_price": str(level.entry_price),
        "take_profit_price": str(take_profit_price),
        "take_profit_distance": str(
            level.entry_price - take_profit_price
        ),
        "spacing_mode": level.spacing_mode.value,
        "stop_price": str(level.stop_price),
        "stop_distance": str(level.stop_price - level.entry_price),
        "stop_mode": level.stop_mode.value,
        "stop_parameter": str(level.stop_parameter),
    }


__all__ = ["LiveStrategyCoordinator", "LiveTriggerResult"]
