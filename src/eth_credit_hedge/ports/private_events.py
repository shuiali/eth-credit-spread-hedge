"""Exchange-neutral authenticated websocket event port."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from eth_credit_hedge.domain.execution import (
    ExchangePosition,
    ExecutionUpdateBatch,
    OrderUpdateBatch,
    PrivateConnectionEvent,
)


PrivateStreamEvent = (
    OrderUpdateBatch
    | ExecutionUpdateBatch
    | tuple[ExchangePosition, ...]
    | PrivateConnectionEvent
)


class PrivateEventPort(Protocol):
    @property
    def new_entries_blocked(self) -> bool: ...

    def stream_events(self) -> AsyncIterator[PrivateStreamEvent]: ...

    def mark_reconciled(
        self,
        connection_generation: int,
    ) -> None: ...
