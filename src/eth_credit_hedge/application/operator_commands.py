"""Authenticated, audited, persistence-first operator command dispatch."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime

from eth_credit_hedge.domain.operator_commands import (
    OperatorCommand,
    OperatorCommandAudit,
    OperatorCommandResult,
    OperatorCommandType,
    OperatorCredential,
)
from eth_credit_hedge.ports.operator_commands import (
    OperatorActionPort,
    OperatorAuditPort,
    OperatorAuthenticatorPort,
    OperatorCommandPersistencePort,
)


OperatorHandler = Callable[[OperatorCommand], Awaitable[str]]


class OperatorActionRegistry:
    def __init__(
        self,
        handlers: Mapping[OperatorCommandType, OperatorHandler],
    ) -> None:
        if set(handlers) != set(OperatorCommandType):
            raise ValueError("operator action registry must define every command")
        self._handlers = dict(handlers)

    async def execute(self, command: OperatorCommand) -> str:
        return await self._handlers[command.command_type](command)


class OperatorCommandService:
    def __init__(
        self,
        *,
        authenticator: OperatorAuthenticatorPort,
        store: OperatorCommandPersistencePort,
        actions: OperatorActionPort,
        audit: OperatorAuditPort,
        clock: Callable[[], datetime],
    ) -> None:
        self._authenticator = authenticator
        self._store = store
        self._actions = actions
        self._audit = audit
        self._clock = clock

    async def execute(
        self,
        command: OperatorCommand,
        *,
        credential: OperatorCredential,
    ) -> OperatorCommandResult:
        authenticated = await self._authenticator.authenticate(
            command.operator_id,
            credential,
        )
        if not authenticated:
            await self._record_audit(command, "DENIED", "authentication failed")
            raise PermissionError("operator authentication failed")

        existing = await self._store.load_command(command.command_id)
        result = await self._store.load_result(command.command_id)
        if existing is not None:
            if existing != command:
                raise ValueError("operator command ID is already bound to other input")
            if result is not None:
                return result
            raise RuntimeError(
                "operator command was started but its outcome is unknown"
            )

        inserted = await self._store.persist_intent(command)
        if not inserted:
            result = await self._store.load_result(command.command_id)
            if result is not None:
                return result
            raise RuntimeError(
                "operator command was concurrently started and its outcome is unknown"
            )
        try:
            detail = await self._actions.execute(command)
        except Exception as exc:
            await self._record_audit(
                command,
                "FAILED",
                f"{type(exc).__name__}: action failed",
            )
            raise
        completed = OperatorCommandResult(
            command_id=command.command_id,
            command_type=command.command_type,
            outcome="COMPLETED",
            detail=detail,
            completed_at_utc=self._clock(),
        )
        await self._store.complete(completed)
        await self._record_audit(command, "COMPLETED", detail)
        return completed

    async def _record_audit(
        self,
        command: OperatorCommand,
        outcome: str,
        detail: str,
    ) -> None:
        await self._audit.record(
            OperatorCommandAudit(
                command_id=command.command_id,
                command_type=command.command_type,
                operator_id=command.operator_id,
                outcome=outcome,
                timestamp_utc=self._clock(),
                detail=detail,
            )
        )
