"""Drive one persisted entry through protection and an authoritative exit."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from eth_credit_hedge.application.one_level_entry import OneLevelEntryService
from eth_credit_hedge.application.execution_hash import execution_payload_hash
from eth_credit_hedge.application.protective_exits import ProtectiveExitService
from eth_credit_hedge.domain.execution import (
    LiveExecutionState,
    PlaceOrderRequest,
)
from eth_credit_hedge.domain.instrument_rules import normalize_and_validate_order
from eth_credit_hedge.domain.instruments import InstrumentSpec
from eth_credit_hedge.domain.live_execution import EntryExecutionSnapshot
from eth_credit_hedge.domain.protected_execution import (
    ProtectionSnapshot,
    protection_position_matches,
)
from eth_credit_hedge.ports.account import AccountPort
from eth_credit_hedge.ports.persistence import ExecutionPersistencePort
from eth_credit_hedge.ports.trading import TradingPort


_TERMINAL_ORDER_STATUSES = frozenset(
    {"Cancelled", "Deactivated", "Filled", "PartiallyFilledCanceled", "Rejected"}
)


class EntryFillNotConfirmedError(RuntimeError):
    """An entry did not reach a terminal, execution-proven active quantity."""


class ExitFillNotConfirmedError(RuntimeError):
    """Neither protective exit proved the position closed before its deadline."""


class LifecyclePositionMismatchError(RuntimeError):
    """Durable execution quantity differs from the exchange position."""


@dataclass(frozen=True, slots=True)
class ProtectedOneLevel:
    entry: EntryExecutionSnapshot
    protection: ProtectionSnapshot


class OneLevelLifecycleService:
    """Orchestrate existing services without treating acknowledgements as fills."""

    def __init__(
        self,
        *,
        trading: TradingPort,
        account: AccountPort,
        store: ExecutionPersistencePort,
        entry_service: OneLevelEntryService,
        exit_service: ProtectiveExitService,
        instrument: InstrumentSpec,
        clock: Callable[[], datetime],
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        fill_attempts: int = 30,
        fill_interval_seconds: float = 0.25,
    ) -> None:
        if (
            instrument.category != "linear"
            or instrument.symbol != "ETHUSDT"
            or instrument.status != "Trading"
        ):
            raise ValueError("lifecycle requires Trading ETHUSDT linear")
        if fill_attempts <= 0:
            raise ValueError("fill attempts must be positive")
        if fill_interval_seconds < 0:
            raise ValueError("fill interval cannot be negative")
        self._trading = trading
        self._account = account
        self._store = store
        self._entry = entry_service
        self._exits = exit_service
        self._instrument = instrument
        self._clock = clock
        self._sleeper = sleeper
        self._fill_attempts = fill_attempts
        self._fill_interval_seconds = fill_interval_seconds

    async def open_and_protect(
        self,
        request: PlaceOrderRequest,
        *,
        stop_order_link_id: str,
        take_profit_order_link_id: str,
        stop_rate: Decimal,
        take_profit_price: Decimal,
        reference_price: Decimal,
    ) -> ProtectedOneLevel:
        validation = normalize_and_validate_order(
            self._instrument,
            side="Sell",
            quantity=request.quantity,
            price=None,
            is_market=True,
            reference_price=reference_price,
        )
        if (
            request.category != "linear"
            or request.symbol != "ETHUSDT"
            or request.side != "Sell"
            or request.order_type != "Market"
            or request.reduce_only
            or request.position_idx != 0
            or not validation.accepted
            or validation.normalized_quantity != request.quantity
        ):
            reasons = validation.errors or ("entry request is not a valid one-way short",)
            raise ValueError("; ".join(reasons))

        entry = await self._entry.submit_entry(request)
        return await self.protect_submitted_entry(
            entry,
            stop_order_link_id=stop_order_link_id,
            take_profit_order_link_id=take_profit_order_link_id,
            stop_rate=stop_rate,
            take_profit_price=take_profit_price,
        )

    async def protect_submitted_entry(
        self,
        entry: EntryExecutionSnapshot,
        *,
        stop_order_link_id: str,
        take_profit_order_link_id: str,
        stop_rate: Decimal,
        take_profit_price: Decimal,
    ) -> ProtectedOneLevel:
        entry = await self._await_entry_fill(entry)
        positions = await self._account.get_positions("linear", "ETHUSDT")
        if not await self._entry.reconcile_position(positions):
            raise LifecyclePositionMismatchError(
                "entry executions do not match the ETHUSDT position"
            )

        protection = await self._exits.install_stop(
            entry.order_link_id,
            self._instrument,
            stop_order_link_id,
            stop_rate=stop_rate,
        )
        protection = await self._exits.install_take_profit(
            protection,
            self._instrument,
            take_profit_order_link_id,
            desired_price=take_profit_price,
        )
        positions = await self._account.get_positions("linear", "ETHUSDT")
        if not protection_position_matches(protection, positions):
            raise LifecyclePositionMismatchError(
                "protected quantity does not match the ETHUSDT position"
            )
        return ProtectedOneLevel(entry=entry, protection=protection)

    async def await_exit(
        self,
        entry_order_link_id: str,
    ) -> ProtectionSnapshot:
        snapshot = await self._required_protection(entry_order_link_id)
        exit_ids = {snapshot.stop_order_link_id, snapshot.tp_order_link_id}
        for attempt in range(self._fill_attempts):
            executions = await self._trading.get_execution_history(
                "linear",
                "ETHUSDT",
            )
            for execution in sorted(
                (
                    execution
                    for execution in executions
                    if execution.order_link_id in exit_ids
                ),
                key=lambda value: (value.executed_at, value.execution_id),
            ):
                snapshot = await self._exits.apply_exit_execution(
                    execution,
                    received_at=self._clock(),
                    payload_hash=execution_payload_hash(execution),
                )
            if snapshot.state is LiveExecutionState.CANCEL_PENDING:
                snapshot = await self._exits.reconcile_after_exit(
                    entry_order_link_id
                )
            if snapshot.state in (
                LiveExecutionState.CLOSED_TP,
                LiveExecutionState.CLOSED_STOP,
            ):
                return snapshot
            if attempt + 1 < self._fill_attempts:
                await self._sleeper(self._fill_interval_seconds)
        raise ExitFillNotConfirmedError(
            f"protective exit for {entry_order_link_id} was not confirmed"
        )

    async def _await_entry_fill(
        self,
        snapshot: EntryExecutionSnapshot,
    ) -> EntryExecutionSnapshot:
        for attempt in range(self._fill_attempts):
            executions = await self._trading.get_execution_history(
                "linear",
                "ETHUSDT",
                snapshot.order_link_id,
            )
            for execution in sorted(
                executions,
                key=lambda value: (value.executed_at, value.execution_id),
            ):
                snapshot = await self._entry.apply_execution(
                    execution,
                    received_at=self._clock(),
                    payload_hash=execution_payload_hash(execution),
                )
            if snapshot.state is LiveExecutionState.ACTIVE_UNPROTECTED:
                return snapshot

            order = await self._trading.get_order_by_link_id(
                "linear",
                "ETHUSDT",
                snapshot.order_link_id,
            )
            if order is not None and order.status in _TERMINAL_ORDER_STATUSES:
                if order.cumulative_filled_quantity < snapshot.filled_quantity:
                    raise EntryFillNotConfirmedError(
                        "exchange order reports less fill than the durable snapshot"
                    )
                if order.cumulative_filled_quantity == snapshot.filled_quantity:
                    if snapshot.state is LiveExecutionState.ENTRY_PARTIALLY_FILLED:
                        return await self._entry.finalize_partial_fill(
                            snapshot.order_link_id
                        )
                    if snapshot.filled_quantity == Decimal("0"):
                        raise EntryFillNotConfirmedError(
                            f"entry {snapshot.order_link_id} ended without a fill"
                        )
            if attempt + 1 < self._fill_attempts:
                await self._sleeper(self._fill_interval_seconds)
        raise EntryFillNotConfirmedError(
            f"entry {snapshot.order_link_id} fill was not confirmed"
        )

    async def _required_protection(
        self,
        entry_order_link_id: str,
    ) -> ProtectionSnapshot:
        snapshot = await self._store.load_protection_snapshot(entry_order_link_id)
        if snapshot is None:
            raise ValueError("protection snapshot does not exist")
        return snapshot
