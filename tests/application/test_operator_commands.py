"""Operator commands authenticate, audit, persist, and deduplicate."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from eth_credit_hedge.application.operator_commands import (
    OperatorActionRegistry,
    OperatorCommandService,
)
from eth_credit_hedge.domain.operator_commands import (
    OperatorCommand,
    OperatorCommandType,
    OperatorCredential,
)
from eth_credit_hedge.infrastructure.persistence.file_operator_command_store import (
    FileOperatorCommandStore,
)


NOW = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)


class Authenticator:
    async def authenticate(
        self,
        operator_id: str,
        credential: OperatorCredential,
    ) -> bool:
        return operator_id == "operator-1" and credential.reveal() == "valid-token"


class AuditRecorder:
    def __init__(self) -> None:
        self.events = []

    async def record(self, event) -> None:
        self.events.append(event)


def command(command_type: OperatorCommandType, index: int) -> OperatorCommand:
    return OperatorCommand(
        command_id=f"command-{index}",
        command_type=command_type,
        operator_id="operator-1",
        reason="operator test",
        issued_at_utc=NOW,
    )


def registry(calls: list[tuple[OperatorCommandType, str]]) -> OperatorActionRegistry:
    handlers = {}
    for command_type in OperatorCommandType:
        async def handle(
            value: OperatorCommand,
            expected: OperatorCommandType = command_type,
        ) -> str:
            assert value.command_type is expected
            calls.append((expected, value.command_id))
            return f"{expected.value} completed"

        handlers[command_type] = handle
    return OperatorActionRegistry(handlers)


def test_every_declared_command_dispatches_once_and_completed_retry_is_idempotent(
    tmp_path,
) -> None:
    calls = []
    audit = AuditRecorder()
    service = OperatorCommandService(
        authenticator=Authenticator(),
        store=FileOperatorCommandStore(tmp_path / "operator-commands.json"),
        actions=registry(calls),
        audit=audit,
        clock=lambda: NOW,
    )

    results = [
        asyncio.run(
            service.execute(
                command(command_type, index),
                credential=OperatorCredential("valid-token"),
            )
        )
        for index, command_type in enumerate(OperatorCommandType, start=1)
    ]
    duplicate = asyncio.run(
        service.execute(
            command(OperatorCommandType.SOFT_PAUSE, 1),
            credential=OperatorCredential("valid-token"),
        )
    )

    assert len(calls) == len(OperatorCommandType)
    assert duplicate == results[0]
    assert len(audit.events) == len(OperatorCommandType)
    assert all(event.outcome == "COMPLETED" for event in audit.events)


def test_authentication_failure_is_audited_without_persisting_or_logging_secret(
    tmp_path,
) -> None:
    calls = []
    audit = AuditRecorder()
    store = FileOperatorCommandStore(tmp_path / "operator-commands.json")
    service = OperatorCommandService(
        authenticator=Authenticator(),
        store=store,
        actions=registry(calls),
        audit=audit,
        clock=lambda: NOW,
    )
    value = command(OperatorCommandType.SOFT_PAUSE, 1)
    credential = OperatorCredential("wrong-secret-token")

    with pytest.raises(PermissionError, match="authentication failed"):
        asyncio.run(service.execute(value, credential=credential))

    assert calls == []
    assert asyncio.run(store.load_command(value.command_id)) is None
    assert audit.events[0].outcome == "DENIED"
    assert "wrong-secret-token" not in repr(credential)
    assert "wrong-secret-token" not in str(audit.events[0])


def test_started_but_incomplete_command_is_ambiguous_and_not_repeated(tmp_path) -> None:
    calls = []
    store = FileOperatorCommandStore(tmp_path / "operator-commands.json")
    value = command(OperatorCommandType.FLATTEN_STRATEGY, 1)
    assert asyncio.run(store.persist_intent(value))
    service = OperatorCommandService(
        authenticator=Authenticator(),
        store=store,
        actions=registry(calls),
        audit=AuditRecorder(),
        clock=lambda: NOW,
    )

    with pytest.raises(RuntimeError, match="outcome is unknown"):
        asyncio.run(
            service.execute(
                value,
                credential=OperatorCredential("valid-token"),
            )
        )

    assert calls == []
