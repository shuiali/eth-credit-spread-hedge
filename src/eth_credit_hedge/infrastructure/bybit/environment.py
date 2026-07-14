"""Immutable Bybit demo endpoint and credential binding."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from eth_credit_hedge.infrastructure.bybit.auth import ApiCredentials


@dataclass(frozen=True, slots=True)
class BybitDemoProfile:
    """Credentials sealed to Bybit's demo REST and private WebSocket hosts."""

    credentials: ApiCredentials

    REST_BASE_URL: ClassVar[str] = "https://api-demo.bybit.com"
    PRIVATE_WEBSOCKET_URL: ClassVar[str] = (
        "wss://stream-demo.bybit.com/v5/private"
    )

    def __post_init__(self) -> None:
        if not isinstance(self.credentials, ApiCredentials):
            raise TypeError("credentials must be ApiCredentials")

    @property
    def rest_base_url(self) -> str:
        return self.REST_BASE_URL

    @property
    def private_websocket_url(self) -> str:
        return self.PRIVATE_WEBSOCKET_URL
