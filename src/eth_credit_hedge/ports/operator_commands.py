"""Authentication, persistence, action, and audit ports for operator commands."""

from __future__ import annotations

from typing import Protocol

from eth_credit_hedge.domain.operator_commands import (
    OperatorCommand,
    OperatorCommandAudit,
    OperatorCommandResult,
    OperatorCredential,
)


class OperatorAuthenticatorPort(Protocol):
    async def authenticate(
        self,
        operator_id: str,
        credential: OperatorCredential,
    ) -> bool: ...


class OperatorCommandPersistencePort(Protocol):
    async def load_command(self, command_id: str) -> OperatorCommand | None: ...

    async def load_result(self, command_id: str) -> OperatorCommandResult | None: ...

    async def persist_intent(self, command: OperatorCommand) -> bool: ...

    async def complete(self, result: OperatorCommandResult) -> None: ...


class OperatorActionPort(Protocol):
    async def execute(self, command: OperatorCommand) -> str: ...


class OperatorAuditPort(Protocol):
    async def record(self, event: OperatorCommandAudit) -> None: ...
