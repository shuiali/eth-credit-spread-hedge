"""Durable exchange-hosted stop, TP, and sibling-exit reconciliation."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from decimal import Decimal

from eth_credit_hedge.domain.execution import (
    CancelOrderRequest,
    ExchangeOrder,
    ExecutionUpdate,
    LiveExecutionState,
    OrderRequestAck,
    OrderRequestKind,
    PlaceOrderRequest,
    UncertainOrderOutcomeError,
)
from eth_credit_hedge.domain.instrument_rules import (
    PriceQuantizationPolicy,
    quantize_limit_price,
)
from eth_credit_hedge.domain.instruments import InstrumentSpec
from eth_credit_hedge.domain.protected_execution import (
    ProtectionSnapshot,
    add_take_profit_intent,
    apply_exit_execution,
    confirm_exit_reconciliation,
    confirm_stop,
    confirm_take_profit,
    mark_protection_reconciling,
    protection_position_matches,
    replace_stop_intent,
)
from eth_credit_hedge.ports.account import AccountPort
from eth_credit_hedge.ports.persistence import ExecutionPersistencePort
from eth_credit_hedge.ports.trading import TradingPort


class ProtectionNotConfirmedError(RuntimeError):
    """A protective order was not proven visible before its deadline."""


class ProtectiveExitService:
    def __init__(
        self,
        *,
        trading: TradingPort,
        account: AccountPort,
        store: ExecutionPersistencePort,
        clock: Callable[[], datetime],
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        visibility_attempts: int = 3,
        visibility_interval_seconds: float = 0.25,
    ) -> None:
        if visibility_attempts <= 0:
            raise ValueError("visibility attempts must be positive")
        if visibility_interval_seconds < 0:
            raise ValueError("visibility interval cannot be negative")
        self._trading = trading
        self._account = account
        self._store = store
        self._clock = clock
        self._sleeper = sleeper
        self._visibility_attempts = visibility_attempts
        self._visibility_interval_seconds = visibility_interval_seconds

    async def install_stop(
        self,
        entry_order_link_id: str,
        instrument: InstrumentSpec,
        stop_order_link_id: str,
        *,
        stop_rate: Decimal,
    ) -> ProtectionSnapshot:
        self._validate_instrument(instrument)
        if stop_rate <= 0:
            raise ValueError("stop rate must be positive")
        entry = await self._store.load_entry_snapshot(entry_order_link_id)
        if entry is None:
            raise ValueError("entry snapshot does not exist")
        average_price = entry.average_entry_price
        if average_price is None:
            raise ValueError("entry has no confirmed average price")
        trigger_price = quantize_limit_price(
            average_price * (Decimal("1") + stop_rate),
            instrument.price_filter.tick_size,
            side="Buy",
            policy=PriceQuantizationPolicy.AGGRESSIVE,
        )
        request = PlaceOrderRequest(
            category="linear",
            symbol="ETHUSDT",
            side="Buy",
            order_type="Market",
            quantity=entry.filled_quantity,
            order_link_id=stop_order_link_id,
            time_in_force="IOC",
            reduce_only=True,
            trigger_price=trigger_price,
            trigger_direction=1,
            trigger_by="LastPrice",
            position_idx=0,
            close_on_trigger=True,
        )
        snapshot = ProtectionSnapshot.for_stop_intent(
            entry,
            stop_order_link_id=stop_order_link_id,
            stop_trigger_price=trigger_price,
            persisted_at=self._clock(),
        )
        await self._store.persist_protection_intent(
            request,
            snapshot,
            snapshot.updated_at,
        )
        try:
            visible = await self._place_and_confirm_visible(request)
        except Exception:
            await self._set_reconciling(snapshot)
            raise
        protected = confirm_stop(
            snapshot,
            order_id=visible.order_id,
            updated_at=self._clock(),
        )
        await self._store.transition_protection_snapshot(
            snapshot.version,
            protected,
        )
        return protected

    async def install_take_profit(
        self,
        snapshot: ProtectionSnapshot,
        instrument: InstrumentSpec,
        order_link_id: str,
        *,
        desired_price: Decimal,
        price_policy: PriceQuantizationPolicy = PriceQuantizationPolicy.PASSIVE,
    ) -> ProtectionSnapshot:
        self._validate_instrument(instrument)
        tp_price = quantize_limit_price(
            desired_price,
            instrument.price_filter.tick_size,
            side="Buy",
            policy=price_policy,
        )
        request = PlaceOrderRequest(
            category="linear",
            symbol="ETHUSDT",
            side="Buy",
            order_type="Limit",
            quantity=snapshot.open_quantity,
            order_link_id=order_link_id,
            price=tp_price,
            time_in_force="GTC",
            reduce_only=True,
            position_idx=0,
        )
        with_intent = add_take_profit_intent(
            snapshot,
            order_link_id=order_link_id,
            price=tp_price,
            updated_at=self._clock(),
        )
        await self._store.persist_take_profit_intent(
            snapshot.version,
            request,
            with_intent,
            with_intent.updated_at,
        )
        try:
            visible = await self._place_and_confirm_visible(request)
        except Exception:
            await self._set_reconciling(with_intent)
            raise
        confirmed = confirm_take_profit(
            with_intent,
            order_id=visible.order_id,
            updated_at=self._clock(),
        )
        await self._store.transition_protection_snapshot(
            with_intent.version,
            confirmed,
        )
        return confirmed

    async def restore_stop(
        self,
        snapshot: ProtectionSnapshot,
        instrument: InstrumentSpec,
        order_link_id: str,
        *,
        stop_rate: Decimal,
    ) -> ProtectionSnapshot:
        self._validate_instrument(instrument)
        if stop_rate <= 0:
            raise ValueError("stop rate must be positive")
        trigger_price = quantize_limit_price(
            snapshot.average_entry_price * (Decimal("1") + stop_rate),
            instrument.price_filter.tick_size,
            side="Buy",
            policy=PriceQuantizationPolicy.AGGRESSIVE,
        )
        request = PlaceOrderRequest(
            category="linear",
            symbol="ETHUSDT",
            side="Buy",
            order_type="Market",
            quantity=snapshot.open_quantity,
            order_link_id=order_link_id,
            time_in_force="IOC",
            reduce_only=True,
            trigger_price=trigger_price,
            trigger_direction=1,
            trigger_by="LastPrice",
            position_idx=0,
            close_on_trigger=True,
        )
        with_intent = replace_stop_intent(
            snapshot,
            order_link_id=order_link_id,
            trigger_price=trigger_price,
            updated_at=self._clock(),
        )
        await self._store.persist_replacement_stop_intent(
            snapshot.version,
            request,
            with_intent,
            with_intent.updated_at,
        )
        try:
            visible = await self._place_and_confirm_visible(request)
        except Exception:
            await self._set_reconciling(with_intent)
            raise
        protected = confirm_stop(
            with_intent,
            order_id=visible.order_id,
            updated_at=self._clock(),
        )
        await self._store.transition_protection_snapshot(
            with_intent.version,
            protected,
        )
        return protected

    async def apply_exit_execution(
        self,
        execution: ExecutionUpdate,
        *,
        received_at: datetime,
        payload_hash: str,
    ) -> ProtectionSnapshot:
        snapshot = await self._store.load_protection_snapshot_by_exit_id(
            execution.order_link_id
        )
        if snapshot is None:
            raise ValueError("execution has no persisted protection snapshot")
        if await self._store.has_execution(execution.execution_id):
            return snapshot
        updated = apply_exit_execution(
            snapshot,
            execution,
            updated_at=received_at,
        )
        inserted = await self._store.record_exit_execution_and_snapshot(
            snapshot.version,
            execution,
            received_at,
            payload_hash,
            updated,
        )
        if inserted:
            return updated
        restored = await self._store.load_protection_snapshot(
            snapshot.entry_order_link_id
        )
        if restored is None:
            raise RuntimeError("protection snapshot disappeared after duplicate")
        return restored

    async def reconcile_after_exit(
        self,
        entry_order_link_id: str,
    ) -> ProtectionSnapshot:
        snapshot = await self._store.load_protection_snapshot(entry_order_link_id)
        if snapshot is None:
            raise ValueError("protection snapshot does not exist")
        if snapshot.state is not LiveExecutionState.CANCEL_PENDING:
            raise ValueError("exit reconciliation requires CANCEL_PENDING state")

        open_orders = await self._trading.get_open_orders("linear", "ETHUSDT")
        exit_ids = {snapshot.stop_order_link_id, snapshot.tp_order_link_id}
        for order in open_orders:
            if order.order_link_id not in exit_ids:
                continue
            try:
                await self._trading.cancel_order(
                    CancelOrderRequest(
                        category="linear",
                        symbol="ETHUSDT",
                        order_link_id=order.order_link_id,
                    )
                )
            except UncertainOrderOutcomeError:
                pass

        remaining_orders = await self._trading.get_open_orders(
            "linear",
            "ETHUSDT",
        )
        positions = await self._account.get_positions("linear", "ETHUSDT")
        unexplained_exit = any(
            order.order_link_id in exit_ids for order in remaining_orders
        )
        if unexplained_exit or not protection_position_matches(snapshot, positions):
            return await self._set_reconciling(snapshot)

        closed = confirm_exit_reconciliation(
            snapshot,
            updated_at=self._clock(),
        )
        await self._store.transition_protection_snapshot(
            snapshot.version,
            closed,
        )
        return closed

    async def _place_and_confirm_visible(
        self,
        request: PlaceOrderRequest,
    ) -> ExchangeOrder:
        discovered: ExchangeOrder | None = None
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
        await self._store.record_acknowledgement(acknowledgement)
        if discovered is not None:
            self._validate_visible_order(discovered, request)
            return discovered

        for attempt in range(self._visibility_attempts):
            order = await self._trading.get_order_by_link_id(
                request.category,
                request.symbol,
                request.order_link_id,
            )
            if order is not None:
                self._validate_visible_order(order, request)
                return order
            if attempt + 1 < self._visibility_attempts:
                await self._sleeper(self._visibility_interval_seconds)
        raise ProtectionNotConfirmedError(
            f"order {request.order_link_id} was not confirmed visible"
        )

    async def _set_reconciling(
        self,
        snapshot: ProtectionSnapshot,
    ) -> ProtectionSnapshot:
        reconciling = mark_protection_reconciling(
            snapshot,
            updated_at=self._clock(),
        )
        if reconciling is not snapshot:
            await self._store.transition_protection_snapshot(
                snapshot.version,
                reconciling,
            )
        return reconciling

    @staticmethod
    def _validate_instrument(instrument: InstrumentSpec) -> None:
        if (
            instrument.category != "linear"
            or instrument.symbol != "ETHUSDT"
            or instrument.status != "Trading"
        ):
            raise ValueError("protection requires Trading ETHUSDT linear")

    @staticmethod
    def _validate_visible_order(
        order: ExchangeOrder,
        request: PlaceOrderRequest,
    ) -> None:
        if (
            order.order_link_id != request.order_link_id
            or order.symbol != request.symbol
            or order.side != request.side
            or order.order_type != request.order_type
            or order.quantity != request.quantity
            or order.price != request.price
            or order.trigger_price != request.trigger_price
            or order.trigger_by != request.trigger_by
            or order.trigger_direction != request.trigger_direction
            or order.time_in_force != request.time_in_force
            or order.position_idx != request.position_idx
            or order.close_on_trigger != request.close_on_trigger
            or not order.reduce_only
            or order.status not in {"New", "Untriggered", "PartiallyFilled"}
        ):
            raise ProtectionNotConfirmedError(
                f"order {request.order_link_id} does not match persisted intent"
            )
