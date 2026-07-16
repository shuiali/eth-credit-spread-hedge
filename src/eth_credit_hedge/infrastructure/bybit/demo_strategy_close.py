"""Persistence-first bounded closure for the integrated demo strategy."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from eth_credit_hedge.application.emergency_flatten import EmergencyFlattenService
from eth_credit_hedge.application.execution_hash import execution_payload_hash
from eth_credit_hedge.application.kill_switch import StrategyCloseOperationsPort
from eth_credit_hedge.domain.client_order_ids import ClientOrderId, ClientOrderRole
from eth_credit_hedge.domain.execution import (
    CancelOrderRequest,
    ExchangePosition,
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
from eth_credit_hedge.domain.instruments import InstrumentSpec, OptionMarketQuote
from eth_credit_hedge.domain.option_exit import (
    OptionExitState,
    OptionSpreadExitSnapshot,
)
from eth_credit_hedge.domain.protected_execution import (
    apply_emergency_exit_execution,
)
from eth_credit_hedge.ports.account import AccountPort
from eth_credit_hedge.ports.persistence import ExecutionPersistencePort
from eth_credit_hedge.ports.trading import TradingPort


ZERO = Decimal("0")


class OptionExitStorePort(ExecutionPersistencePort, Protocol):
    async def persist_option_exit_snapshot(
        self,
        snapshot: OptionSpreadExitSnapshot,
    ) -> None: ...

    async def load_option_exit_snapshot(
        self,
        cycle_id: str,
    ) -> OptionSpreadExitSnapshot | None: ...

    async def transition_option_exit_snapshot(
        self,
        previous_version: int,
        snapshot: OptionSpreadExitSnapshot,
    ) -> None: ...


class OptionQuotePort(Protocol):
    async def get_option_chain(
        self,
        base_coin: str,
    ) -> tuple[OptionMarketQuote, ...]: ...


CloseOrderIdFactory = Callable[[ClientOrderRole, int], str]
Sleep = Callable[[float], Awaitable[None]]


class DemoStrategyCloseOperations(StrategyCloseOperationsPort):
    """Close the aggregate hedge, short option, then protective long."""

    def __init__(
        self,
        *,
        trading: TradingPort,
        account: AccountPort,
        store: OptionExitStorePort,
        quotes: OptionQuotePort,
        option_instruments: tuple[InstrumentSpec, ...],
        cycle_id: str,
        short_symbol: str,
        long_symbol: str,
        order_link_id_factory: CloseOrderIdFactory,
        clock: Callable[[], datetime],
        sleeper: Sleep = asyncio.sleep,
        maximum_attempts: int = 5,
        execution_visibility_attempts: int = 60,
        poll_interval_seconds: float = 0.25,
    ) -> None:
        if maximum_attempts <= 0:
            raise ValueError("maximum close attempts must be positive")
        if execution_visibility_attempts <= 0:
            raise ValueError("execution visibility attempts must be positive")
        if poll_interval_seconds < 0:
            raise ValueError("close poll interval cannot be negative")
        self._trading = trading
        self._account = account
        self._store = store
        self._quotes = quotes
        self._instruments = {
            instrument.symbol: instrument for instrument in option_instruments
        }
        if short_symbol not in self._instruments or long_symbol not in self._instruments:
            raise ValueError("close service requires both option instruments")
        self._cycle_id = cycle_id
        self._short_symbol = short_symbol
        self._long_symbol = long_symbol
        self._order_link_id_factory = order_link_id_factory
        self._clock = clock
        self._sleeper = sleeper
        self._maximum_attempts = maximum_attempts
        self._execution_visibility_attempts = execution_visibility_attempts
        self._poll_interval_seconds = poll_interval_seconds

    async def close_hedges(self) -> None:
        await self._cancel_pending_linear_entries()
        flatten = EmergencyFlattenService(
            trading=self._trading,
            account=self._account,
            store=self._store,
            clock=self._clock,
        )
        for _ in range(self._execution_visibility_attempts):
            await self._replay_persisted_emergency_executions(flatten)
            if not await flatten.confirm_flattened():
                break
            if await self._durable_open_hedge_quantity() == ZERO:
                await self._trading.cancel_all("linear", "ETHUSDT")
                return
            await self._sleeper(self._poll_interval_seconds)
        else:
            raise RuntimeError(
                "exchange is flat but durable hedge exposure remains"
            )
        for attempt in range(1, self._maximum_attempts + 1):
            if await flatten.confirm_flattened():
                raise RuntimeError(
                    "exchange is flat but durable hedge exposure remains"
                )
            result = await flatten.flatten_short(
                self._order_link_id_factory(
                    ClientOrderRole.EMERGENCY_CLOSE,
                    attempt,
                )
            )
            for _ in range(self._execution_visibility_attempts):
                executions = await self._trading.get_execution_history(
                    "linear",
                    "ETHUSDT",
                    result.request.order_link_id,
                )
                for execution in executions:
                    inserted = await flatten.record_fill(
                        execution,
                        received_at=self._clock(),
                        payload_hash=execution_payload_hash(execution),
                    )
                    if inserted:
                        await self._allocate_emergency_execution(execution)
                if await flatten.confirm_flattened():
                    if await self._durable_open_hedge_quantity() == ZERO:
                        await self._trading.cancel_all("linear", "ETHUSDT")
                        return
                await self._sleeper(self._poll_interval_seconds)
            if await flatten.confirm_flattened():
                raise RuntimeError(
                    "emergency close execution did not become visible"
                )
        raise RuntimeError("ETHUSDT hedge close could not be proven flat")

    async def close_option_spread(self) -> None:
        await self._cancel_owned_option_orders()
        positions = await self._account.get_positions("option")
        short_remaining, long_remaining = _option_quantities(
            positions,
            short_symbol=self._short_symbol,
            long_symbol=self._long_symbol,
        )
        snapshot = await self._store.load_option_exit_snapshot(self._cycle_id)
        if snapshot is None:
            snapshot = OptionSpreadExitSnapshot(
                cycle_id=self._cycle_id,
                short_symbol=self._short_symbol,
                long_symbol=self._long_symbol,
                state=OptionExitState.NOT_STARTED,
                short_remaining_quantity=short_remaining,
                long_remaining_quantity=long_remaining,
                active_order_link_id=None,
                version=1,
                updated_at=self._clock(),
            )
            await self._store.persist_option_exit_snapshot(snapshot)
        else:
            snapshot = await self._reconcile_snapshot(
                snapshot,
                short_remaining,
                long_remaining,
            )

        if snapshot.short_remaining_quantity > ZERO:
            snapshot = await self._close_option_leg(
                snapshot,
                symbol=self._short_symbol,
                side="Buy",
                role=ClientOrderRole.OPTION_SHORT,
                closing_state=OptionExitState.SHORT_CLOSING,
            )
        if snapshot.short_remaining_quantity != ZERO:
            raise RuntimeError("short option close was not confirmed")
        if snapshot.state is not OptionExitState.SHORT_CLOSED:
            snapshot = await self._transition(
                snapshot,
                state=OptionExitState.SHORT_CLOSED,
                active_order_link_id=None,
            )
        if snapshot.long_remaining_quantity > ZERO:
            snapshot = await self._close_option_leg(
                snapshot,
                symbol=self._long_symbol,
                side="Sell",
                role=ClientOrderRole.OPTION_LONG,
                closing_state=OptionExitState.LONG_CLOSING,
            )
        if (
            snapshot.short_remaining_quantity != ZERO
            or snapshot.long_remaining_quantity != ZERO
        ):
            raise RuntimeError("option spread close was not confirmed flat")
        if snapshot.state is not OptionExitState.CLOSED:
            await self._transition(
                snapshot,
                state=OptionExitState.CLOSED,
                active_order_link_id=None,
            )
        await self._trading.cancel_all("option")

    async def verify_strategy_closed(self) -> bool:
        positions = await self._account.get_positions("linear", "ETHUSDT")
        options = await self._account.get_positions("option")
        orders = (
            *(await self._trading.get_open_orders("linear", "ETHUSDT")),
            *(await self._trading.get_open_orders("option")),
        )
        relevant_options = {
            self._short_symbol,
            self._long_symbol,
        }
        return bool(
            not any(position.quantity > ZERO for position in positions)
            and not any(
                position.quantity > ZERO and position.symbol in relevant_options
                for position in options
            )
            and not any(
                order.symbol == "ETHUSDT" or order.symbol in relevant_options
                for order in orders
            )
        )

    async def verify_option_protected(self) -> bool:
        positions = await self._account.get_positions("option")
        short, long = _option_quantities(
            positions,
            short_symbol=self._short_symbol,
            long_symbol=self._long_symbol,
        )
        linear = await self._account.get_positions("linear", "ETHUSDT")
        return bool(not linear and short > ZERO and long >= short)

    async def leave_option_protected(self) -> bool:
        await self.close_hedges()
        await self._cancel_owned_option_orders()
        return await self.verify_option_protected()

    async def _close_option_leg(
        self,
        snapshot: OptionSpreadExitSnapshot,
        *,
        symbol: str,
        side: str,
        role: ClientOrderRole,
        closing_state: OptionExitState,
    ) -> OptionSpreadExitSnapshot:
        # Option entry uses attempts starting at 1. Close attempts occupy the
        # durable 51-99 range so they cannot reuse an entry intent ID.
        first_attempt = 51
        if snapshot.active_order_link_id is not None:
            try:
                previous_order_id = ClientOrderId.parse(
                    snapshot.active_order_link_id
                )
            except ValueError:
                previous_order_id = None
            if previous_order_id is not None and previous_order_id.role is role:
                first_attempt = previous_order_id.attempt + 1
        for attempt in range(
            first_attempt,
            first_attempt + self._maximum_attempts,
        ):
            remaining = (
                snapshot.short_remaining_quantity
                if role is ClientOrderRole.OPTION_SHORT
                else snapshot.long_remaining_quantity
            )
            if remaining == ZERO:
                return snapshot
            quote = _required_quote(await self._quotes.get_option_chain("ETH"), symbol)
            raw_price = quote.ask_price if side == "Buy" else quote.bid_price
            if raw_price is None:
                raise RuntimeError(f"no executable close quote for {symbol}")
            instrument = self._instruments[symbol]
            price = quantize_limit_price(
                raw_price,
                instrument.price_filter.tick_size,
                side=side,  # type: ignore[arg-type]
                policy=PriceQuantizationPolicy.AGGRESSIVE,
            )
            order_link_id = self._order_link_id_factory(role, attempt)
            snapshot = await self._transition(
                snapshot,
                state=closing_state,
                active_order_link_id=order_link_id,
            )
            request = PlaceOrderRequest(
                category="option",
                symbol=symbol,
                side=side,  # type: ignore[arg-type]
                order_type="Limit",
                quantity=remaining,
                order_link_id=order_link_id,
                price=price,
                time_in_force="IOC",
                reduce_only=True,
                position_idx=0,
            )
            await self._store.persist_order_intent(request, self._clock())
            acknowledgement = await self._submit(request)
            await self._store.record_acknowledgement(acknowledgement)
            for poll_attempt in range(
                1,
                self._execution_visibility_attempts + 1,
            ):
                executed_quantity = await self._record_option_executions(request)
                positions = await self._account.get_positions("option")
                short, long = _option_quantities(
                    positions,
                    short_symbol=self._short_symbol,
                    long_symbol=self._long_symbol,
                )
                observed_remaining = (
                    short
                    if role is ClientOrderRole.OPTION_SHORT
                    else long
                )
                observed_reduction = remaining - observed_remaining
                if (
                    observed_reduction > ZERO
                    and executed_quantity < observed_reduction
                ):
                    if poll_attempt == self._execution_visibility_attempts:
                        raise RuntimeError(
                            f"option close execution is not visible for {symbol}"
                        )
                    await self._sleeper(self._poll_interval_seconds)
                    continue
                snapshot = await self._transition(
                    snapshot,
                    state=closing_state,
                    short_remaining_quantity=short,
                    long_remaining_quantity=long,
                    active_order_link_id=order_link_id,
                )
                if (role is ClientOrderRole.OPTION_SHORT and short == ZERO) or (
                    role is ClientOrderRole.OPTION_LONG and long == ZERO
                ):
                    return snapshot
                await self._sleeper(self._poll_interval_seconds)
        raise RuntimeError(f"option close attempts exhausted for {symbol}")

    async def _submit(self, request: PlaceOrderRequest) -> OrderRequestAck:
        try:
            return await self._trading.place_order(request)
        except UncertainOrderOutcomeError:
            discovered = await self._trading.get_order_by_link_id(
                request.category,
                request.symbol,
                request.order_link_id,
            )
            if discovered is None:
                raise
            return OrderRequestAck(
                request_kind=OrderRequestKind.PLACE,
                order_id=discovered.order_id,
                order_link_id=discovered.order_link_id,
                acknowledged_at=self._clock(),
            )

    async def _record_option_executions(
        self,
        request: PlaceOrderRequest,
    ) -> Decimal:
        return await self._record_option_executions_by_identity(
            request.symbol,
            request.order_link_id,
        )

    async def _record_option_executions_by_identity(
        self,
        symbol: str,
        order_link_id: str,
    ) -> Decimal:
        executions = await self._trading.get_execution_history(
            "option",
            symbol,
            order_link_id,
        )
        for execution in executions:
            await self._store.record_execution(
                execution,
                self._clock(),
                execution_payload_hash(execution),
            )
        return sum((execution.quantity for execution in executions), ZERO)

    async def _allocate_emergency_execution(
        self,
        execution: ExecutionUpdate,
    ) -> None:
        close_id = ClientOrderId.parse(execution.order_link_id)
        remaining = execution.quantity
        snapshots = sorted(
            (
                snapshot
                for snapshot in await self._store.load_all_protection_snapshots()
                if _same_strategy_cycle(
                    ClientOrderId.parse(snapshot.entry_order_link_id),
                    close_id,
                )
            ),
            key=lambda snapshot: ClientOrderId.parse(
                snapshot.entry_order_link_id
            ).level,
        )
        for snapshot in snapshots:
            allocated = min(snapshot.open_quantity, remaining)
            if allocated == ZERO:
                continue
            updated = apply_emergency_exit_execution(
                snapshot,
                execution,
                updated_at=self._clock(),
                allocated_quantity=allocated,
            )
            await self._store.transition_protection_snapshot(
                snapshot.version,
                updated,
            )
            remaining -= allocated
            if remaining == ZERO:
                break
        if remaining != ZERO:
            raise RuntimeError("emergency execution exceeds durable hedge lots")

    async def _replay_persisted_emergency_executions(
        self,
        flatten: EmergencyFlattenService,
    ) -> None:
        active_identities = {
            (
                entry_id.strategy_instance,
                entry_id.cycle,
            )
            for snapshot in await self._store.load_all_protection_snapshots()
            if snapshot.open_quantity > ZERO
            for entry_id in (ClientOrderId.parse(snapshot.entry_order_link_id),)
        }
        if not active_identities:
            return
        requests = sorted(
            await self._store.load_all_order_intents(),
            key=lambda request: request.order_link_id,
        )
        for request in requests:
            try:
                request_id = ClientOrderId.parse(request.order_link_id)
            except ValueError:
                continue
            if (
                request_id.role is not ClientOrderRole.EMERGENCY_CLOSE
                or (
                    request_id.strategy_instance,
                    request_id.cycle,
                )
                not in active_identities
            ):
                continue
            executions = sorted(
                await self._trading.get_execution_history(
                    "linear",
                    "ETHUSDT",
                    request.order_link_id,
                ),
                key=lambda execution: (
                    execution.executed_at,
                    execution.execution_id,
                ),
            )
            for execution in executions:
                inserted = await flatten.record_fill(
                    execution,
                    received_at=self._clock(),
                    payload_hash=execution_payload_hash(execution),
                )
                if inserted:
                    await self._allocate_emergency_execution(execution)

    async def _durable_open_hedge_quantity(self) -> Decimal:
        protections = await self._store.load_all_protection_snapshots()
        protected_entry_ids = {
            snapshot.entry_order_link_id for snapshot in protections
        }
        protected = sum(
            (snapshot.open_quantity for snapshot in protections),
            ZERO,
        )
        unprotected = sum(
            (
                snapshot.filled_quantity
                for snapshot in await self._store.load_all_entry_snapshots()
                if snapshot.order_link_id not in protected_entry_ids
                and snapshot.state is not LiveExecutionState.ERROR
            ),
            ZERO,
        )
        return protected + unprotected

    async def _reconcile_snapshot(
        self,
        snapshot: OptionSpreadExitSnapshot,
        short_remaining: Decimal,
        long_remaining: Decimal,
    ) -> OptionSpreadExitSnapshot:
        if (
            short_remaining > snapshot.short_remaining_quantity
            or long_remaining > snapshot.long_remaining_quantity
        ):
            raise RuntimeError("option exposure increased during close reconciliation")
        if snapshot.active_order_link_id is not None:
            try:
                active = ClientOrderId.parse(snapshot.active_order_link_id)
            except ValueError:
                active = None
            if active is not None and active.role in (
                ClientOrderRole.OPTION_SHORT,
                ClientOrderRole.OPTION_LONG,
            ):
                if active.role is ClientOrderRole.OPTION_SHORT:
                    symbol = self._short_symbol
                    observed_reduction = (
                        snapshot.short_remaining_quantity - short_remaining
                    )
                else:
                    symbol = self._long_symbol
                    observed_reduction = (
                        snapshot.long_remaining_quantity - long_remaining
                    )
                if observed_reduction > ZERO:
                    executed = await self._record_option_executions_by_identity(
                        symbol,
                        snapshot.active_order_link_id,
                    )
                    if executed < observed_reduction:
                        raise RuntimeError(
                            f"option close execution is not visible for {symbol}"
                        )
        state = snapshot.state
        if short_remaining == ZERO and long_remaining == ZERO:
            state = OptionExitState.CLOSED
        elif short_remaining == ZERO:
            state = OptionExitState.SHORT_CLOSED
        return await self._transition(
            snapshot,
            state=state,
            short_remaining_quantity=short_remaining,
            long_remaining_quantity=long_remaining,
            active_order_link_id=(
                None
                if short_remaining == ZERO and long_remaining == ZERO
                else snapshot.active_order_link_id
            ),
        )

    async def _transition(
        self,
        snapshot: OptionSpreadExitSnapshot,
        *,
        state: OptionExitState,
        short_remaining_quantity: Decimal | None = None,
        long_remaining_quantity: Decimal | None = None,
        active_order_link_id: str | None,
    ) -> OptionSpreadExitSnapshot:
        updated = replace(
            snapshot,
            state=state,
            short_remaining_quantity=(
                snapshot.short_remaining_quantity
                if short_remaining_quantity is None
                else short_remaining_quantity
            ),
            long_remaining_quantity=(
                snapshot.long_remaining_quantity
                if long_remaining_quantity is None
                else long_remaining_quantity
            ),
            active_order_link_id=active_order_link_id,
            version=snapshot.version + 1,
            updated_at=self._clock(),
        )
        await self._store.transition_option_exit_snapshot(snapshot.version, updated)
        return updated

    async def _cancel_pending_linear_entries(self) -> None:
        for order in await self._trading.get_open_orders("linear", "ETHUSDT"):
            try:
                role = ClientOrderId.parse(order.order_link_id).role
            except ValueError:
                continue
            if role is ClientOrderRole.HEDGE_ENTRY:
                await self._trading.cancel_order(
                    CancelOrderRequest(
                        category="linear",
                        symbol="ETHUSDT",
                        order_link_id=order.order_link_id,
                    )
                )

    async def _cancel_owned_option_orders(self) -> None:
        for order in await self._trading.get_open_orders("option"):
            if order.symbol not in {self._short_symbol, self._long_symbol}:
                continue
            try:
                ClientOrderId.parse(order.order_link_id)
            except ValueError:
                continue
            await self._trading.cancel_order(
                CancelOrderRequest(
                    category="option",
                    symbol=order.symbol,
                    order_link_id=order.order_link_id,
                )
            )


def _option_quantities(
    positions: tuple[ExchangePosition, ...],
    *,
    short_symbol: str,
    long_symbol: str,
) -> tuple[Decimal, Decimal]:
    short = sum(
        (
            position.quantity
            for position in positions
            if position.symbol == short_symbol and position.side == "Sell"
        ),
        ZERO,
    )
    long = sum(
        (
            position.quantity
            for position in positions
            if position.symbol == long_symbol and position.side == "Buy"
        ),
        ZERO,
    )
    return short, long


def _required_quote(
    quotes: tuple[OptionMarketQuote, ...],
    symbol: str,
) -> OptionMarketQuote:
    for quote in quotes:
        if quote.symbol == symbol:
            return quote
    raise RuntimeError(f"option quote is unavailable for {symbol}")


def _same_strategy_cycle(left: ClientOrderId, right: ClientOrderId) -> bool:
    return bool(
        left.strategy_instance == right.strategy_instance
        and left.cycle == right.cycle
    )


__all__ = ["DemoStrategyCloseOperations"]
