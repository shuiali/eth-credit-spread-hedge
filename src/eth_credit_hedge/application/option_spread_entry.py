"""Persistence-first, protective-long-first option spread entry."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from eth_credit_hedge.application.execution_hash import execution_payload_hash
from eth_credit_hedge.domain.client_order_ids import ClientOrderId, ClientOrderRole
from eth_credit_hedge.domain.execution import (
    OrderRequestAck,
    OrderRequestKind,
    PlaceOrderRequest,
    UncertainOrderOutcomeError,
)
from eth_credit_hedge.domain.live_option_execution import (
    OptionSpreadExecutionSnapshot,
    acknowledge_option_order,
    apply_option_execution,
    finalize_protective_long,
    finalize_short_premium,
    mark_option_execution_error,
    start_short_premium,
)
from eth_credit_hedge.domain.option_lifecycle import OptionEntryPolicy
from eth_credit_hedge.domain.option_position import OptionPositionState
from eth_credit_hedge.ports.persistence import ExecutionPersistencePort
from eth_credit_hedge.ports.trading import TradingPort


_TERMINAL_ORDER_STATUSES = frozenset(
    {"Cancelled", "Deactivated", "Filled", "PartiallyFilledCanceled", "Rejected"}
)


class OptionSpreadNotOpenedError(RuntimeError):
    """The option legs did not produce an execution-proven credit spread."""


@dataclass(frozen=True, slots=True)
class OptionSpreadEntryPlan:
    cycle_id: str
    long_symbol: str
    short_symbol: str
    expiry_time_utc: datetime
    quantity: Decimal
    long_limit_price: Decimal
    short_limit_price: Decimal
    expected_net_credit: Decimal
    long_order_link_id: str
    short_order_link_id: str

    def __post_init__(self) -> None:
        if not self.cycle_id.strip():
            raise ValueError("option cycle ID cannot be empty")
        _validate_put_pair(self.long_symbol, self.short_symbol)
        for field_name in (
            "quantity",
            "long_limit_price",
            "short_limit_price",
            "expected_net_credit",
        ):
            value = Decimal(str(getattr(self, field_name)))
            if not value.is_finite() or value <= 0:
                raise ValueError(f"{field_name.replace('_', ' ')} must be positive")
            object.__setattr__(self, field_name, value)
        if self.expiry_time_utc.tzinfo is None or self.expiry_time_utc.utcoffset() is None:
            raise ValueError("option expiry must be timezone-aware")
        if ClientOrderId.parse(self.long_order_link_id).role is not (
            ClientOrderRole.OPTION_LONG
        ):
            raise ValueError("long client ID must have OPTION_LONG role")
        if ClientOrderId.parse(self.short_order_link_id).role is not (
            ClientOrderRole.OPTION_SHORT
        ):
            raise ValueError("short client ID must have OPTION_SHORT role")


class OptionSpreadEntryService:
    def __init__(
        self,
        *,
        trading: TradingPort,
        store: ExecutionPersistencePort,
        clock: Callable[[], datetime],
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        fill_attempts: int = 40,
        fill_interval_seconds: float = 0.25,
    ) -> None:
        if fill_attempts <= 0:
            raise ValueError("fill attempts must be positive")
        if fill_interval_seconds < 0:
            raise ValueError("fill interval cannot be negative")
        self._trading = trading
        self._store = store
        self._clock = clock
        self._sleeper = sleeper
        self._fill_attempts = fill_attempts
        self._fill_interval_seconds = fill_interval_seconds

    async def open_spread(
        self,
        plan: OptionSpreadEntryPlan,
        policy: OptionEntryPolicy,
    ) -> OptionSpreadExecutionSnapshot:
        long_request = PlaceOrderRequest(
            category="option",
            symbol=plan.long_symbol,
            side="Buy",
            order_type="Limit",
            quantity=plan.quantity,
            order_link_id=plan.long_order_link_id,
            price=plan.long_limit_price,
            time_in_force="IOC",
            position_idx=0,
        )
        snapshot = OptionSpreadExecutionSnapshot.for_long_intent(
            long_request,
            cycle_id=plan.cycle_id,
            short_symbol=plan.short_symbol,
            expiry_time_utc=plan.expiry_time_utc,
            expected_net_credit=plan.expected_net_credit,
            persisted_at=self._clock(),
        )
        await self._store.persist_option_long_intent(
            long_request,
            snapshot,
            snapshot.updated_at,
        )
        try:
            snapshot = await self._submit(snapshot, long_request)
            snapshot = await self._await_leg(snapshot, long_request, policy)
        except Exception as exc:
            await self._mark_error(snapshot)
            raise OptionSpreadNotOpenedError(str(exc)) from exc
        if snapshot.state is not OptionPositionState.LONG_PROTECTION_FILLED:
            raise OptionSpreadNotOpenedError("protective long did not fully fill")

        short_request = PlaceOrderRequest(
            category="option",
            symbol=plan.short_symbol,
            side="Sell",
            order_type="Limit",
            quantity=snapshot.long_filled_quantity,
            order_link_id=plan.short_order_link_id,
            price=plan.short_limit_price,
            time_in_force="IOC",
            position_idx=0,
        )
        with_short = start_short_premium(
            snapshot,
            short_request,
            updated_at=self._clock(),
        )
        await self._store.persist_option_short_intent(
            snapshot.version,
            short_request,
            with_short,
            with_short.updated_at,
        )
        snapshot = with_short
        try:
            snapshot = await self._submit(snapshot, short_request)
            snapshot = await self._await_leg(snapshot, short_request, policy)
        except Exception as exc:
            await self._mark_error(snapshot)
            raise OptionSpreadNotOpenedError(str(exc)) from exc
        if snapshot.state is not OptionPositionState.OPEN:
            raise OptionSpreadNotOpenedError("short fill did not open a full spread")
        return snapshot

    async def _submit(
        self,
        snapshot: OptionSpreadExecutionSnapshot,
        request: PlaceOrderRequest,
    ) -> OptionSpreadExecutionSnapshot:
        try:
            acknowledgement = await self._trading.place_order(request)
        except UncertainOrderOutcomeError:
            discovered = await self._trading.get_order_by_link_id(
                request.category,
                request.symbol,
                request.order_link_id,
            )
            if discovered is None:
                raise
            acknowledgement = OrderRequestAck(
                request_kind=OrderRequestKind.PLACE,
                order_id=discovered.order_id,
                order_link_id=discovered.order_link_id,
                acknowledged_at=self._clock(),
            )
        acknowledged = acknowledge_option_order(
            snapshot,
            acknowledgement,
            updated_at=self._clock(),
        )
        await self._store.record_option_acknowledgement_and_snapshot(
            snapshot.version,
            acknowledgement,
            acknowledged,
        )
        return acknowledged

    async def _await_leg(
        self,
        snapshot: OptionSpreadExecutionSnapshot,
        request: PlaceOrderRequest,
        policy: OptionEntryPolicy,
    ) -> OptionSpreadExecutionSnapshot:
        for attempt in range(self._fill_attempts):
            executions = await self._trading.get_execution_history(
                "option",
                request.symbol,
                request.order_link_id,
            )
            for execution in sorted(
                executions,
                key=lambda value: (value.executed_at, value.execution_id),
            ):
                if await self._store.has_execution(execution.execution_id):
                    continue
                updated = apply_option_execution(
                    snapshot,
                    execution,
                    updated_at=self._clock(),
                )
                inserted = await self._store.record_option_execution_and_snapshot(
                    snapshot.version,
                    execution,
                    self._clock(),
                    execution_payload_hash(execution),
                    updated,
                )
                snapshot = (
                    updated
                    if inserted
                    else await self._required_snapshot(snapshot.cycle_id)
                )
            order = await self._trading.get_order_by_link_id(
                "option",
                request.symbol,
                request.order_link_id,
            )
            if order is not None and order.status in _TERMINAL_ORDER_STATUSES:
                filled = (
                    snapshot.long_filled_quantity
                    if request.side == "Buy"
                    else snapshot.short_filled_quantity
                )
                if order.cumulative_filled_quantity > filled:
                    pass
                elif order.cumulative_filled_quantity < filled:
                    raise ValueError("option order reports less fill than local state")
                else:
                    finalized = (
                        finalize_protective_long(
                            snapshot,
                            policy,
                            updated_at=self._clock(),
                        )
                        if request.side == "Buy"
                        else finalize_short_premium(
                            snapshot,
                            policy,
                            updated_at=self._clock(),
                        )
                    )
                    await self._store.transition_option_spread_snapshot(
                        snapshot.version,
                        finalized,
                    )
                    return finalized
            if attempt + 1 < self._fill_attempts:
                await self._sleeper(self._fill_interval_seconds)
        raise TimeoutError(f"option leg {request.order_link_id} did not settle")

    async def _mark_error(
        self,
        snapshot: OptionSpreadExecutionSnapshot,
    ) -> None:
        current = await self._required_snapshot(snapshot.cycle_id)
        failed = mark_option_execution_error(current, updated_at=self._clock())
        if failed is not current:
            await self._store.transition_option_spread_snapshot(
                current.version,
                failed,
            )

    async def _required_snapshot(
        self,
        cycle_id: str,
    ) -> OptionSpreadExecutionSnapshot:
        snapshot = await self._store.load_option_spread_snapshot(cycle_id)
        if snapshot is None:
            raise RuntimeError("option spread snapshot disappeared")
        return snapshot


def _validate_put_pair(long_symbol: str, short_symbol: str) -> None:
    long_parts = long_symbol.split("-")
    short_parts = short_symbol.split("-")
    if (
        len(long_parts) != 5
        or len(short_parts) != 5
        or long_parts[0] != "ETH"
        or short_parts[0] != "ETH"
        or long_parts[1] != short_parts[1]
        or long_parts[3:] != ["P", "USDT"]
        or short_parts[3:] != ["P", "USDT"]
    ):
        raise ValueError("option plan requires same-expiry ETH USDT puts")
    if Decimal(short_parts[2]) <= Decimal(long_parts[2]):
        raise ValueError("short put strike must exceed long put strike")
