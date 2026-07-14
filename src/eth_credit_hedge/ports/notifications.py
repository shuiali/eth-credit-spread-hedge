"""Notification delivery boundary for operational alerts."""

from __future__ import annotations

from typing import Protocol


class Notification(Protocol):
    @property
    def code(self) -> object: ...


class NotificationPort(Protocol):
    async def send(self, notification: Notification) -> None: ...
