"""Credit-spread hedge state machine."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from eth_credit_hedge.config import LockPolicy, RecoveryMode, StrategyConfig
from eth_credit_hedge.core.credit_spread import (
    CreditSpread,
    DecimalLike,
    ZERO,
    to_decimal,
)
from eth_credit_hedge.core.crossing_engine import (
    CrossingEngine,
    CrossingEvent,
    CrossingEventType,
    Direction,
)
from eth_credit_hedge.core.ledger import (
    AccountingSnapshot,
    Ledger,
    LedgerEvent,
    StrategyResult,
)
from eth_credit_hedge.core.virtual_levels import (
    HedgeLevel,
    LevelState,
    generate_virtual_levels,
)


class HedgeEngine:
    """Exact tick-driven short-perpetual hedge lifecycle."""

    def __init__(
        self,
        spread: CreditSpread,
        level_count: int = 1,
        recovery_mode: RecoveryMode | str = RecoveryMode.FULL_NEXT_TP,
        recovery_tp_count: int = 3,
        lock_policy: LockPolicy | str = LockPolicy.UNHEDGED,
        stop_rate: DecimalLike = "0.15",
    ) -> None:
        self.config = StrategyConfig(
            level_count=level_count,
            stop_rate=to_decimal(stop_rate),
            recovery_mode=RecoveryMode(recovery_mode),
            lock_policy=LockPolicy(lock_policy),
            recovery_tp_count=recovery_tp_count,
        )
        self.spread = spread
        self.levels = generate_virtual_levels(
            spread,
            self.config.level_count,
            self.config.stop_rate,
        )
        self.recovery_mode = self.config.recovery_mode
        self.recovery_tp_count = self.config.recovery_tp_count
        self.lock_policy = self.config.lock_policy
        self.ledger = Ledger()
        self.crossings = CrossingEngine()
        self.previous_price: Decimal | None = None

    @property
    def initial_stop_budget(self) -> Decimal:
        return self.spread.premium_credit

    @property
    def used_stop_budget(self) -> Decimal:
        return self.ledger.gross_stop_losses

    @property
    def remaining_stop_budget(self) -> Decimal:
        return self.initial_stop_budget - self.used_stop_budget

    def step(
        self,
        price: DecimalLike,
        tick_index: int,
        on_crossing: Callable[[CrossingEvent], None] | None = None,
    ) -> list[LedgerEvent]:
        current = to_decimal(price)
        if self.previous_price is None:
            self.previous_price = current
            self._arm_ready_levels(current)
            return []

        event_start = len(self.ledger.events)
        self.crossings.process_transition(
            self.previous_price,
            current,
            self,
            tick_index=tick_index,
            on_executed=on_crossing,
        )
        self.previous_price = current
        self._arm_ready_levels(current)
        return self.ledger.events[event_start:]

    def run(self, prices: list[DecimalLike]) -> list[LedgerEvent]:
        for tick_index, price in enumerate(prices):
            self.step(price, tick_index)
        return self.ledger.events

    def run_with_accounting(self, prices: list[DecimalLike]) -> StrategyResult:
        if self.previous_price is not None or self.ledger.events:
            raise RuntimeError("accounting runs require a fresh hedge engine")
        exact_prices = [to_decimal(price) for price in prices]
        if not exact_prices:
            raise ValueError("price path cannot be empty")

        snapshots: list[AccountingSnapshot] = []
        for tick_index, price in enumerate(exact_prices):

            def record_crossing(event: CrossingEvent) -> None:
                snapshots.append(
                    self.ledger.snapshot(
                        spread=self.spread,
                        levels=self.levels,
                        price=event.price,
                        tick_index=tick_index,
                        event_sequence=len(self.ledger.events),
                    )
                )

            self.step(price, tick_index, on_crossing=record_crossing)
            if (
                not snapshots
                or snapshots[-1].tick_index != tick_index
                or snapshots[-1].price != price
            ):
                snapshots.append(
                    self.ledger.snapshot(
                        spread=self.spread,
                        levels=self.levels,
                        price=price,
                        tick_index=tick_index,
                    )
                )
        return self.ledger.build_result(
            spread=self.spread,
            levels=self.levels,
            prices=exact_prices,
            snapshots=snapshots,
        )

    def eligible_crossings(
        self,
        previous_price: Decimal,
        current_price: Decimal,
        direction: Direction,
    ) -> list[CrossingEvent]:
        events: list[CrossingEvent] = []
        if direction is Direction.DOWN:
            for level in self.levels:
                if (
                    level.state is LevelState.ACTIVE
                    and previous_price > level.tp_price
                    and current_price <= level.tp_price
                ):
                    events.append(
                        CrossingEvent(
                            CrossingEventType.TP,
                            level.level_id,
                            level.tp_price,
                            priority=0,
                        )
                    )
                if (
                    level.state is LevelState.READY
                    and level.entry_armed
                    and current_price <= level.entry_price
                ):
                    events.append(
                        CrossingEvent(
                            CrossingEventType.ENTRY,
                            level.level_id,
                            level.entry_price,
                            priority=1,
                        )
                    )
        else:
            for level in self.levels:
                if (
                    level.state is LevelState.ACTIVE
                    and level.active_is_floor
                    and previous_price <= level.entry_price <= current_price
                ):
                    events.append(
                        CrossingEvent(
                            CrossingEventType.BREAKEVEN,
                            level.level_id,
                            level.entry_price,
                        )
                    )
                elif (
                    level.state is LevelState.ACTIVE
                    and previous_price < level.stop_price
                    and current_price >= level.stop_price
                ):
                    events.append(
                        CrossingEvent(
                            CrossingEventType.STOP,
                            level.level_id,
                            level.stop_price,
                        )
                    )
        return events

    def execute_crossing(self, event: CrossingEvent, tick_index: int) -> None:
        level = self.levels[event.level_id - 1]
        if event.event_type is CrossingEventType.ENTRY:
            self._open(level, tick_index)
        elif event.event_type is CrossingEventType.TP:
            self._take_profit(level, tick_index)
        elif event.event_type is CrossingEventType.STOP:
            self._stop(level, tick_index)
        else:
            self._breakeven(level, tick_index)

    def _open(self, level: HedgeLevel, tick_index: int) -> None:
        recovery_allocations = self._recovery_allocations_for(level)
        recovery_target = sum(recovery_allocations.values(), ZERO)
        quantity = (level.option_budget + recovery_target) / level.tp_distance
        projected_stop_loss = quantity * level.stop_distance
        if self.used_stop_budget + projected_stop_loss > self.initial_stop_budget:
            if self.lock_policy is LockPolicy.BREAKEVEN_FLOOR:
                self._open_floor(
                    level,
                    tick_index,
                    quantity,
                    recovery_allocations,
                    projected_stop_loss,
                )
                return
            level.state = LevelState.LOCKED
            level.entry_armed = False
            self.ledger.record_locked(
                level,
                tick_index,
                quantity,
                projected_stop_loss,
            )
            return

        level.active_quantity = quantity
        level.active_is_floor = False
        level.entry_armed = False
        level.active_recovery_allocations = recovery_allocations
        level.attempts += 1
        level.state = LevelState.ACTIVE
        self.ledger.record_entry(
            level,
            tick_index,
            quantity,
            projected_stop_loss=projected_stop_loss,
        )

    def _take_profit(self, level: HedgeLevel, tick_index: int) -> None:
        quantity = level.active_quantity
        profit = quantity * level.tp_distance
        recovery_allocations = dict(level.active_recovery_allocations)
        recovery_profit = sum(recovery_allocations.values(), ZERO)
        for source_level_id, allocation in recovery_allocations.items():
            source = self.levels[source_level_id - 1]
            source.recovery_debt -= allocation
            if source.recovery_debt < ZERO:
                raise AssertionError("recovery debt cannot become negative")
            if self.recovery_mode is RecoveryMode.DISTRIBUTED:
                source.recovery_tps_remaining -= 1
                if source.recovery_debt == ZERO:
                    source.recovery_tps_remaining = 0
        level.realized_tp_profit += profit
        level.active_quantity = ZERO
        level.active_is_floor = False
        level.entry_armed = False
        level.active_recovery_allocations = {}
        level.state = LevelState.PAID
        self.ledger.record_tp(
            level,
            tick_index,
            quantity,
            profit,
            zone_component=level.option_budget,
            recovery_component=recovery_profit,
            recovery_allocations=recovery_allocations,
        )

    def _stop(self, level: HedgeLevel, tick_index: int) -> None:
        if level.active_is_floor:
            raise AssertionError("floor hedge cannot execute a fixed stop")
        quantity = level.active_quantity
        loss = quantity * level.stop_distance
        level.realized_stop_losses += loss
        level.stop_loss_history.append(loss)
        level.recovery_debt += loss
        if (
            self.recovery_mode is RecoveryMode.DISTRIBUTED
            and level.recovery_tps_remaining == 0
        ):
            level.recovery_tps_remaining = self.recovery_tp_count
        level.active_quantity = ZERO
        level.active_recovery_allocations = {}
        level.state = LevelState.READY
        level.entry_armed = True
        self.ledger.record_stop(level, tick_index, quantity, loss)

    def _open_floor(
        self,
        level: HedgeLevel,
        tick_index: int,
        quantity: Decimal,
        recovery_allocations: dict[int, Decimal],
        projected_stop_loss: Decimal,
    ) -> None:
        level.active_quantity = quantity
        level.active_is_floor = True
        level.entry_armed = False
        level.active_recovery_allocations = recovery_allocations
        level.attempts += 1
        level.state = LevelState.ACTIVE
        self.ledger.record_floor_entry(
            level,
            tick_index,
            quantity,
            projected_stop_loss,
        )

    def _breakeven(self, level: HedgeLevel, tick_index: int) -> None:
        if not level.active_is_floor:
            raise AssertionError("breakeven exit requires a floor hedge")
        quantity = level.active_quantity
        level.active_quantity = ZERO
        level.active_is_floor = False
        level.active_recovery_allocations = {}
        level.state = LevelState.READY
        level.entry_armed = True
        self.ledger.record_breakeven(level, tick_index, quantity)

    def _arm_ready_levels(self, price: Decimal) -> None:
        for level in self.levels:
            if level.state is LevelState.READY and price >= level.entry_price:
                level.entry_armed = True

    def _recovery_allocations_for(self, target: HedgeLevel) -> dict[int, Decimal]:
        if self.recovery_mode is RecoveryMode.FULL_NEXT_TP:
            if target.recovery_debt > ZERO:
                return {target.level_id: target.recovery_debt}
            return {}

        for source in self.levels:
            if source.level_id > target.level_id:
                break
            if source.recovery_debt > ZERO and source.recovery_tps_remaining > 0:
                allocation = min(
                    source.recovery_debt,
                    source.recovery_debt / Decimal(source.recovery_tps_remaining),
                )
                return {source.level_id: allocation}
        return {}
