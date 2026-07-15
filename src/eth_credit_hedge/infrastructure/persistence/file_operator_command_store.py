"""Atomic durable idempotency store for low-volume operator commands."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from eth_credit_hedge.domain.operator_commands import (
    OperatorCommand,
    OperatorCommandResult,
    OperatorCommandType,
)


class FileOperatorCommandStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    async def load_command(self, command_id: str) -> OperatorCommand | None:
        record = self._records().get(command_id)
        if record is None:
            return None
        return _parse_command(_object(record, "command"))

    async def load_result(self, command_id: str) -> OperatorCommandResult | None:
        record = self._records().get(command_id)
        if record is None or record.get("result") is None:
            return None
        return _parse_result(_object(record, "result"))

    async def persist_intent(self, command: OperatorCommand) -> bool:
        records = self._records()
        if command.command_id in records:
            return False
        records[command.command_id] = {
            "command": _command_payload(command),
            "result": None,
        }
        self._write(records)
        return True

    async def complete(self, result: OperatorCommandResult) -> None:
        records = self._records()
        record = records.get(result.command_id)
        if record is None:
            raise RuntimeError("operator command intent is not persisted")
        existing = record.get("result")
        payload = _result_payload(result)
        if existing is not None and existing != payload:
            raise RuntimeError("operator command already has a different result")
        record["result"] = payload
        self._write(records)

    def _records(self) -> dict[str, dict[str, object]]:
        if not self.path.exists():
            return {}
        try:
            raw: Any = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("operator command file must contain an object")
            records: dict[str, dict[str, object]] = {}
            for key, value in raw.items():
                if not isinstance(key, str) or not isinstance(value, dict):
                    raise ValueError("operator command records are invalid")
                records[key] = value
            return records
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("operator command store is unreadable") from exc

    def _write(self, records: dict[str, dict[str, object]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                records,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        temporary.replace(self.path)


def _command_payload(command: OperatorCommand) -> dict[str, object]:
    return {
        "command_id": command.command_id,
        "command_type": command.command_type.value,
        "operator_id": command.operator_id,
        "reason": command.reason,
        "issued_at_utc": command.issued_at_utc.isoformat(),
    }


def _result_payload(result: OperatorCommandResult) -> dict[str, object]:
    return {
        "command_id": result.command_id,
        "command_type": result.command_type.value,
        "outcome": result.outcome,
        "detail": result.detail,
        "completed_at_utc": result.completed_at_utc.isoformat(),
    }


def _parse_command(raw: dict[str, object]) -> OperatorCommand:
    try:
        return OperatorCommand(
            command_id=str(raw["command_id"]),
            command_type=OperatorCommandType(str(raw["command_type"])),
            operator_id=str(raw["operator_id"]),
            reason=str(raw["reason"]),
            issued_at_utc=datetime.fromisoformat(str(raw["issued_at_utc"])),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("persisted operator command is invalid") from exc


def _parse_result(raw: dict[str, object]) -> OperatorCommandResult:
    try:
        return OperatorCommandResult(
            command_id=str(raw["command_id"]),
            command_type=OperatorCommandType(str(raw["command_type"])),
            outcome=str(raw["outcome"]),
            detail=str(raw["detail"]),
            completed_at_utc=datetime.fromisoformat(
                str(raw["completed_at_utc"])
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("persisted operator command result is invalid") from exc


def _object(parent: dict[str, object], name: str) -> dict[str, object]:
    value = parent.get(name)
    if not isinstance(value, dict):
        raise RuntimeError(f"persisted operator {name} is invalid")
    return value
