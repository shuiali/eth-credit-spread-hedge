"""Durable kill-switch control and close orchestration."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from eth_credit_hedge.application.emergency_flatten import EmergencyFlattenService
from eth_credit_hedge.domain.control import (
    KillSwitchActivatedNotification,
    KillSwitchMode,
    KillSwitchState,
)
from eth_credit_hedge.ports.control import KillSwitchPersistencePort
from eth_credit_hedge.ports.notifications import NotificationPort
from eth_credit_hedge.ports.trading import TradingPort


_ESCALATION = {
    KillSwitchMode.RUNNING: 0,
    KillSwitchMode.SOFT_PAUSE: 1,
    KillSwitchMode.STRATEGY_CLOSE: 2,
    KillSwitchMode.EMERGENCY_FLATTEN: 3,
}


class KillSwitchController:
    def __init__(
        self,
        *,
        store: KillSwitchPersistencePort,
        clock: Callable[[], datetime],
    ) -> None:
        self._store = store
        self._clock = clock
        self._state: KillSwitchState | None = None

    @property
    def state(self) -> KillSwitchState:
        if self._state is None:
            raise RuntimeError("kill switch has not been initialized")
        return self._state

    @property
    def entries_allowed(self) -> bool:
        return self.state.mode is KillSwitchMode.RUNNING

    @property
    def protection_management_allowed(self) -> bool:
        return True

    @property
    def reconciliation_allowed(self) -> bool:
        return True

    async def initialize(self) -> KillSwitchState:
        persisted = await self._store.load()
        if persisted is None:
            persisted = KillSwitchState(
                mode=KillSwitchMode.RUNNING,
                reason="initial state",
                requested_by="system",
                changed_at_utc=self._clock(),
                version=1,
            )
            await self._store.save(persisted, expected_version=None)
        self._state = persisted
        return persisted

    async def activate(
        self,
        mode: KillSwitchMode,
        *,
        reason: str,
        requested_by: str,
    ) -> KillSwitchState:
        requested = KillSwitchMode(mode)
        if requested is KillSwitchMode.RUNNING:
            raise ValueError("use reset to return the kill switch to RUNNING")
        current = self.state
        if _ESCALATION[requested] < _ESCALATION[current.mode]:
            raise ValueError("kill switch cannot de-escalate without explicit reset")
        updated = KillSwitchState(
            mode=requested,
            reason=reason,
            requested_by=requested_by,
            changed_at_utc=self._clock(),
            version=current.version + 1,
        )
        await self._store.save(updated, expected_version=current.version)
        self._state = updated
        return updated

    async def reset(
        self,
        *,
        requested_by: str,
        operator_acknowledged: bool,
    ) -> KillSwitchState:
        if not operator_acknowledged:
            raise ValueError("operator acknowledgement is required to reset")
        current = self.state
        updated = KillSwitchState(
            mode=KillSwitchMode.RUNNING,
            reason="operator acknowledged reset",
            requested_by=requested_by,
            changed_at_utc=self._clock(),
            version=current.version + 1,
        )
        await self._store.save(updated, expected_version=current.version)
        self._state = updated
        return updated


class StrategyCloseOperationsPort(Protocol):
    async def close_hedges(self) -> None: ...

    async def close_option_spread(self) -> None: ...

    async def verify_strategy_closed(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class StrategyCloseResult:
    kill_switch_state: KillSwitchState
    verified_closed: bool


class StrategyCloseService:
    def __init__(
        self,
        *,
        controller: KillSwitchController,
        trading: TradingPort,
        operations: StrategyCloseOperationsPort,
    ) -> None:
        self._controller = controller
        self._trading = trading
        self._operations = operations

    async def close(self, *, reason: str, requested_by: str) -> StrategyCloseResult:
        state = await self._controller.activate(
            KillSwitchMode.STRATEGY_CLOSE,
            reason=reason,
            requested_by=requested_by,
        )
        await self._trading.cancel_all("linear", "ETHUSDT")
        await self._trading.cancel_all("option")
        await self._operations.close_hedges()
        await self._operations.close_option_spread()
        verified = await self._operations.verify_strategy_closed()
        return StrategyCloseResult(state, verified)


@dataclass(frozen=True, slots=True)
class EmergencyKillSwitchResult:
    attempts: int
    position_confirmed_flat: bool
    reconciliation_succeeded: bool


class EmergencyKillSwitchExecutor:
    def __init__(
        self,
        *,
        controller: KillSwitchController,
        trading: TradingPort,
        flatten: EmergencyFlattenService,
        reconcile: Callable[[], Awaitable[bool]],
        notifications: NotificationPort,
        order_link_id_factory: Callable[[int], str],
        maximum_attempts: int,
    ) -> None:
        if maximum_attempts <= 0:
            raise ValueError("maximum emergency attempts must be positive")
        self._controller = controller
        self._trading = trading
        self._flatten = flatten
        self._reconcile = reconcile
        self._notifications = notifications
        self._order_link_id_factory = order_link_id_factory
        self._maximum_attempts = maximum_attempts

    async def execute(
        self,
        *,
        reason: str,
        requested_by: str,
    ) -> EmergencyKillSwitchResult:
        await self._controller.activate(
            KillSwitchMode.EMERGENCY_FLATTEN,
            reason=reason,
            requested_by=requested_by,
        )
        await self._notifications.send(
            KillSwitchActivatedNotification(
                code="KILL_SWITCH",
                severity="IMMEDIATE",
                message=f"emergency flatten activated: {reason}",
            )
        )
        await self._trading.cancel_all("linear", "ETHUSDT")
        await self._trading.cancel_all("option")
        for attempt in range(1, self._maximum_attempts + 1):
            position_flat = await self._flatten.confirm_flattened()
            if not position_flat:
                await self._flatten.flatten_short(
                    self._order_link_id_factory(attempt)
                )
                position_flat = await self._flatten.confirm_flattened()
            reconciled = await self._reconcile()
            if position_flat and reconciled:
                return EmergencyKillSwitchResult(attempt, True, True)
        raise RuntimeError("emergency flatten did not verify flat and reconciled")


__all__ = [
    "EmergencyKillSwitchExecutor",
    "EmergencyKillSwitchResult",
    "KillSwitchController",
    "KillSwitchMode",
    "KillSwitchState",
    "StrategyCloseResult",
    "StrategyCloseService",
]
