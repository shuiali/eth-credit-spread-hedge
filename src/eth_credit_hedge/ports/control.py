"""Control-plane boundaries for entry gates and durable kill-switch state."""

from __future__ import annotations

from typing import Protocol

from eth_credit_hedge.domain.control import KillSwitchState


class EntryGatePort(Protocol):
    @property
    def entries_allowed(self) -> bool: ...


class KillSwitchPersistencePort(Protocol):
    async def load(self) -> KillSwitchState | None: ...

    async def save(
        self,
        state: KillSwitchState,
        *,
        expected_version: int | None,
    ) -> None: ...
