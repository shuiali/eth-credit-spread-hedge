"""Fixed-schema JSON logging with explicit runtime secret redaction."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TextIO


@dataclass(frozen=True, slots=True)
class StructuredLogEvent:
    timestamp: datetime
    service: str
    cycle_id: str | None
    level_id: int | None
    client_order_id: str | None
    exchange_order_id: str | None
    execution_id: str | None
    correlation_id: str | None
    event: str
    message: str

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("log timestamp must be timezone-aware")
        object.__setattr__(self, "timestamp", self.timestamp.astimezone(timezone.utc))
        for value, name in (
            (self.service, "service"),
            (self.event, "event"),
            (self.message, "message"),
        ):
            if not value.strip():
                raise ValueError(f"log {name} cannot be empty")
        if self.level_id is not None and self.level_id <= 0:
            raise ValueError("log level ID must be positive")


class SecretSafeJsonLogger:
    def __init__(self, stream: TextIO, *, secrets: tuple[str, ...] = ()) -> None:
        self._stream = stream
        self._secrets = tuple(
            secret for secret in dict.fromkeys(secrets) if len(secret) >= 4
        )

    def write(self, event: StructuredLogEvent) -> None:
        payload: dict[str, object] = {
            "timestamp": event.timestamp.isoformat(),
            "service": event.service,
            "cycle_id": event.cycle_id,
            "level_id": event.level_id,
            "client_order_id": event.client_order_id,
            "exchange_order_id": event.exchange_order_id,
            "execution_id": event.execution_id,
            "correlation_id": event.correlation_id,
            "event": event.event,
            "message": event.message,
        }
        redacted = {
            key: self._redact(value) if isinstance(value, str) else value
            for key, value in payload.items()
        }
        self._stream.write(
            json.dumps(redacted, allow_nan=False, separators=(",", ":")) + "\n"
        )
        self._stream.flush()

    def _redact(self, value: str) -> str:
        redacted = value
        for secret in self._secrets:
            redacted = redacted.replace(secret, "[REDACTED]")
        return redacted
