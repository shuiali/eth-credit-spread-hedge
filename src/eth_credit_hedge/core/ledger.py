"""Exact hedge event and P&L accounting."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from eth_credit_hedge.core.credit_spread import ZERO
from eth_credit_hedge.core.credit_spread import CreditSpread
from eth_credit_hedge.core.virtual_levels import HedgeLevel, LevelState


class LedgerEventType(str, Enum):
    ENTRY = "ENTRY"
    FLOOR_ENTRY = "FLOOR_ENTRY"
    TP = "TP"
    STOP = "STOP"
    BREAKEVEN = "BREAKEVEN"
    LOCKED = "LOCKED"


@dataclass(frozen=True, slots=True)
class LedgerEvent:
    sequence: int
    tick_index: int
    event_type: LedgerEventType
    level_id: int
    price: Decimal
    quantity: Decimal
    realized_pnl: Decimal
    level_state: LevelState
    attempt: int
    projected_stop_loss: Decimal = ZERO
    zone_profit_component: Decimal = ZERO
    recovery_profit_component: Decimal = ZERO
    recovery_allocations: dict[int, Decimal] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        """Return the stable version-one persistence representation."""
        return {
            "event_version": 1,
            "sequence": self.sequence,
            "tick_index": self.tick_index,
            "event_type": self.event_type.value,
            "level_id": self.level_id,
            "price": str(self.price),
            "quantity": str(self.quantity),
            "realized_pnl": str(self.realized_pnl),
            "level_state": self.level_state.value,
            "attempt": self.attempt,
            "projected_stop_loss": str(self.projected_stop_loss),
            "zone_profit_component": str(self.zone_profit_component),
            "recovery_profit_component": str(self.recovery_profit_component),
            "recovery_allocations": {
                str(level_id): str(amount)
                for level_id, amount in sorted(self.recovery_allocations.items())
            },
        }

    def to_json(self) -> str:
        """Serialize without whitespace while preserving the declared key order."""
        return json.dumps(self.to_dict(), separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class AccountingSnapshot:
    tick_index: int
    event_sequence: int | None
    price: Decimal
    option_terminal_value_pnl: Decimal
    realized_hedge_pnl: Decimal
    open_hedge_pnl: Decimal
    combined_terminal_value_pnl: Decimal
    gross_stop_losses: Decimal
    gross_tp_profits: Decimal
    outstanding_recovery_debt: Decimal
    remaining_premium_stop_budget: Decimal
    incremental_pnl_since_entry: Decimal


@dataclass(frozen=True, slots=True)
class LevelSnapshot:
    level_id: int
    entry_price: Decimal
    tp_price: Decimal
    stop_price: Decimal
    option_budget: Decimal
    state: LevelState
    attempts: int
    active_quantity: Decimal
    active_is_floor: bool
    entry_armed: bool
    recovery_debt: Decimal
    recovery_tps_remaining: int
    realized_stop_losses: Decimal
    realized_tp_profit: Decimal
    stop_loss_history: tuple[Decimal, ...]


@dataclass(frozen=True, slots=True)
class StrategyMetrics:
    combined_pnl: Decimal
    minimum_combined_pnl: Decimal
    floor_pass: bool
    number_of_entries: int
    number_of_stops: int
    number_of_tps: int
    reentry_count: int
    floor_entry_count: int
    breakeven_exit_count: int
    maximum_quantity: Decimal
    premium_budget_consumed: Decimal
    remaining_premium_stop_budget: Decimal
    locked_levels: tuple[int, ...]
    outstanding_recovery_debt: Decimal


@dataclass(frozen=True, slots=True)
class StrategyResult:
    input_prices: tuple[Decimal, ...]
    prices: tuple[Decimal, ...]
    snapshots: tuple[AccountingSnapshot, ...]
    events: tuple[LedgerEvent, ...]
    levels: tuple[LevelSnapshot, ...]
    metrics: StrategyMetrics


class Ledger:
    """Append-only event ledger with exact realized hedge totals."""

    def __init__(self) -> None:
        self.events: list[LedgerEvent] = []
        self.realized_hedge_pnl = ZERO
        self.gross_stop_losses = ZERO
        self.gross_tp_profits = ZERO
        self.maximum_quantity = ZERO
        self.entries = 0
        self.stops = 0
        self.tps = 0
        self.reentries = 0
        self.floor_entries = 0
        self.breakeven_exits = 0

    def record_entry(
        self,
        level: HedgeLevel,
        tick_index: int,
        quantity: Decimal,
        projected_stop_loss: Decimal,
    ) -> LedgerEvent:
        return self._record_open(
            tick_index=tick_index,
            event_type=LedgerEventType.ENTRY,
            level=level,
            quantity=quantity,
            projected_stop_loss=projected_stop_loss,
        )

    def record_floor_entry(
        self,
        level: HedgeLevel,
        tick_index: int,
        quantity: Decimal,
        projected_stop_loss: Decimal,
    ) -> LedgerEvent:
        self.floor_entries += 1
        return self._record_open(
            tick_index=tick_index,
            event_type=LedgerEventType.FLOOR_ENTRY,
            level=level,
            quantity=quantity,
            projected_stop_loss=projected_stop_loss,
        )

    def record_breakeven(
        self,
        level: HedgeLevel,
        tick_index: int,
        quantity: Decimal,
    ) -> LedgerEvent:
        self.breakeven_exits += 1
        return self._append(
            tick_index=tick_index,
            event_type=LedgerEventType.BREAKEVEN,
            level=level,
            price=level.entry_price,
            quantity=quantity,
        )

    def record_stop(
        self,
        level: HedgeLevel,
        tick_index: int,
        quantity: Decimal,
        loss: Decimal,
    ) -> LedgerEvent:
        self.stops += 1
        self.gross_stop_losses += loss
        self.realized_hedge_pnl -= loss
        return self._append(
            tick_index=tick_index,
            event_type=LedgerEventType.STOP,
            level=level,
            price=level.stop_price,
            quantity=quantity,
            realized_pnl=-loss,
        )

    def record_tp(
        self,
        level: HedgeLevel,
        tick_index: int,
        quantity: Decimal,
        profit: Decimal,
        zone_component: Decimal,
        recovery_component: Decimal = ZERO,
        recovery_allocations: dict[int, Decimal] | None = None,
    ) -> LedgerEvent:
        self.tps += 1
        self.gross_tp_profits += profit
        self.realized_hedge_pnl += profit
        return self._append(
            tick_index=tick_index,
            event_type=LedgerEventType.TP,
            level=level,
            price=level.tp_price,
            quantity=quantity,
            realized_pnl=profit,
            zone_profit_component=zone_component,
            recovery_profit_component=recovery_component,
            recovery_allocations=recovery_allocations or {},
        )

    def record_locked(
        self,
        level: HedgeLevel,
        tick_index: int,
        quantity: Decimal,
        projected_stop_loss: Decimal,
    ) -> LedgerEvent:
        return self._append(
            tick_index=tick_index,
            event_type=LedgerEventType.LOCKED,
            level=level,
            price=level.entry_price,
            quantity=quantity,
            projected_stop_loss=projected_stop_loss,
        )

    def snapshot(
        self,
        *,
        spread: CreditSpread,
        levels: list[HedgeLevel],
        price: Decimal,
        tick_index: int,
        event_sequence: int | None = None,
    ) -> AccountingSnapshot:
        option_pnl = spread.expiry_pnl(price)
        open_pnl = sum(
            (
                level.active_quantity * (level.entry_price - price)
                for level in levels
                if level.state is LevelState.ACTIVE
            ),
            ZERO,
        )
        combined = option_pnl + self.realized_hedge_pnl + open_pnl
        outstanding_debt = sum((level.recovery_debt for level in levels), ZERO)
        return AccountingSnapshot(
            tick_index=tick_index,
            event_sequence=event_sequence,
            price=price,
            option_terminal_value_pnl=option_pnl,
            realized_hedge_pnl=self.realized_hedge_pnl,
            open_hedge_pnl=open_pnl,
            combined_terminal_value_pnl=combined,
            gross_stop_losses=self.gross_stop_losses,
            gross_tp_profits=self.gross_tp_profits,
            outstanding_recovery_debt=outstanding_debt,
            remaining_premium_stop_budget=spread.premium_credit
            - self.gross_stop_losses,
            incremental_pnl_since_entry=combined - spread.expiry_pnl(spread.spot),
        )

    def build_result(
        self,
        *,
        spread: CreditSpread,
        levels: list[HedgeLevel],
        prices: list[Decimal],
        snapshots: list[AccountingSnapshot],
    ) -> StrategyResult:
        if not snapshots:
            raise ValueError("at least one accounting snapshot is required")
        locked_levels = tuple(
            level.level_id for level in levels if level.state is LevelState.LOCKED
        )
        level_snapshots = tuple(
            LevelSnapshot(
                level_id=level.level_id,
                entry_price=level.entry_price,
                tp_price=level.tp_price,
                stop_price=level.stop_price,
                option_budget=level.option_budget,
                state=level.state,
                attempts=level.attempts,
                active_quantity=level.active_quantity,
                active_is_floor=level.active_is_floor,
                entry_armed=level.entry_armed,
                recovery_debt=level.recovery_debt,
                recovery_tps_remaining=level.recovery_tps_remaining,
                realized_stop_losses=level.realized_stop_losses,
                realized_tp_profit=level.realized_tp_profit,
                stop_loss_history=tuple(level.stop_loss_history),
            )
            for level in levels
        )
        minimum_combined = min(
            snapshot.combined_terminal_value_pnl for snapshot in snapshots
        )
        final = snapshots[-1]
        metrics = StrategyMetrics(
            combined_pnl=final.combined_terminal_value_pnl,
            minimum_combined_pnl=minimum_combined,
            floor_pass=minimum_combined >= ZERO,
            number_of_entries=self.entries,
            number_of_stops=self.stops,
            number_of_tps=self.tps,
            reentry_count=self.reentries,
            floor_entry_count=self.floor_entries,
            breakeven_exit_count=self.breakeven_exits,
            maximum_quantity=self.maximum_quantity,
            premium_budget_consumed=self.gross_stop_losses,
            remaining_premium_stop_budget=spread.premium_credit
            - self.gross_stop_losses,
            locked_levels=locked_levels,
            outstanding_recovery_debt=final.outstanding_recovery_debt,
        )
        return StrategyResult(
            input_prices=tuple(prices),
            prices=tuple(snapshot.price for snapshot in snapshots),
            snapshots=tuple(snapshots),
            events=tuple(self.events),
            levels=level_snapshots,
            metrics=metrics,
        )

    def _record_open(
        self,
        *,
        tick_index: int,
        event_type: LedgerEventType,
        level: HedgeLevel,
        quantity: Decimal,
        projected_stop_loss: Decimal,
    ) -> LedgerEvent:
        self.entries += 1
        if level.attempts > 1:
            self.reentries += 1
        self.maximum_quantity = max(self.maximum_quantity, quantity)
        return self._append(
            tick_index=tick_index,
            event_type=event_type,
            level=level,
            price=level.entry_price,
            quantity=quantity,
            projected_stop_loss=projected_stop_loss,
        )

    def _append(
        self,
        *,
        tick_index: int,
        event_type: LedgerEventType,
        level: HedgeLevel,
        price: Decimal,
        quantity: Decimal,
        realized_pnl: Decimal = ZERO,
        projected_stop_loss: Decimal = ZERO,
        zone_profit_component: Decimal = ZERO,
        recovery_profit_component: Decimal = ZERO,
        recovery_allocations: dict[int, Decimal] | None = None,
    ) -> LedgerEvent:
        event = LedgerEvent(
            sequence=len(self.events) + 1,
            tick_index=tick_index,
            event_type=event_type,
            level_id=level.level_id,
            price=price,
            quantity=quantity,
            realized_pnl=realized_pnl,
            level_state=level.state,
            attempt=level.attempts,
            projected_stop_loss=projected_stop_loss,
            zone_profit_component=zone_profit_component,
            recovery_profit_component=recovery_profit_component,
            recovery_allocations=dict(recovery_allocations or {}),
        )
        self.events.append(event)
        return event
