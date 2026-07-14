"""Bybit V5 signing primitives that keep credentials out of representations."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass


@dataclass(frozen=True, slots=True, repr=False)
class SecretStr:
    """A string whose normal string representations never reveal its value."""

    _value: str

    def __post_init__(self) -> None:
        if not isinstance(self._value, str):
            raise TypeError("secret value must be a string")
        if not self._value:
            raise ValueError("secret value must not be empty")

    def get_secret_value(self) -> str:
        return self._value

    def __str__(self) -> str:
        return "<redacted>"

    def __repr__(self) -> str:
        return "SecretStr('<redacted>')"


@dataclass(frozen=True, slots=True)
class ApiCredentials:
    """Immutable HMAC credentials for one exchange environment."""

    api_key: SecretStr
    api_secret: SecretStr

    def __post_init__(self) -> None:
        if not isinstance(self.api_key, SecretStr):
            raise TypeError("api_key must be SecretStr")
        if not isinstance(self.api_secret, SecretStr):
            raise TypeError("api_secret must be SecretStr")


@dataclass(frozen=True, slots=True, repr=False)
class SignedRequestHeaders:
    """Authenticated HTTP headers with an explicitly redacted representation."""

    api_key: SecretStr
    timestamp_ms: int
    signature: SecretStr
    receive_window_ms: int

    def as_http_headers(self) -> dict[str, str]:
        """Return wire headers; callers must never log the returned mapping."""
        return {
            "X-BAPI-API-KEY": self.api_key.get_secret_value(),
            "X-BAPI-TIMESTAMP": str(self.timestamp_ms),
            "X-BAPI-SIGN": self.signature.get_secret_value(),
            "X-BAPI-RECV-WINDOW": str(self.receive_window_ms),
            "Content-Type": "application/json",
        }

    def redacted(self) -> dict[str, str]:
        return {
            "X-BAPI-API-KEY": "<redacted>",
            "X-BAPI-TIMESTAMP": str(self.timestamp_ms),
            "X-BAPI-SIGN": "<redacted>",
            "X-BAPI-RECV-WINDOW": str(self.receive_window_ms),
            "Content-Type": "application/json",
        }

    def __repr__(self) -> str:
        return f"SignedRequestHeaders({self.redacted()!r})"


@dataclass(frozen=True, slots=True, repr=False)
class WebSocketAuthArgs:
    """Private WebSocket auth arguments with secret-safe representations."""

    api_key: SecretStr
    expires_at_ms: int
    signature: SecretStr

    def as_auth_args(self) -> tuple[str, int, str]:
        """Return wire args; callers must never log the returned tuple."""
        return (
            self.api_key.get_secret_value(),
            self.expires_at_ms,
            self.signature.get_secret_value(),
        )

    def __repr__(self) -> str:
        return (
            "WebSocketAuthArgs(api_key='<redacted>', "
            f"expires_at_ms={self.expires_at_ms}, signature='<redacted>')"
        )


@dataclass(frozen=True, slots=True)
class BybitV5Signer:
    """Sign exact Bybit V5 GET query strings and POST JSON bodies."""

    credentials: ApiCredentials
    receive_window_ms: int = 5000

    def __post_init__(self) -> None:
        if not isinstance(self.credentials, ApiCredentials):
            raise TypeError("credentials must be ApiCredentials")
        if self.receive_window_ms <= 0:
            raise ValueError("receive window must be positive")

    def sign_get(
        self,
        *,
        timestamp_ms: int,
        query_string: str,
    ) -> SignedRequestHeaders:
        return self._sign_http(timestamp_ms=timestamp_ms, payload=query_string)

    def sign_post(
        self,
        *,
        timestamp_ms: int,
        body: str,
    ) -> SignedRequestHeaders:
        return self._sign_http(timestamp_ms=timestamp_ms, payload=body)

    def sign_websocket_auth(self, *, expires_at_ms: int) -> WebSocketAuthArgs:
        if expires_at_ms <= 0:
            raise ValueError("WebSocket expiry timestamp must be positive")
        signature = self._hmac_hex(f"GET/realtime{expires_at_ms}")
        return WebSocketAuthArgs(
            api_key=self.credentials.api_key,
            expires_at_ms=expires_at_ms,
            signature=SecretStr(signature),
        )

    def _sign_http(
        self,
        *,
        timestamp_ms: int,
        payload: str,
    ) -> SignedRequestHeaders:
        if timestamp_ms <= 0:
            raise ValueError("timestamp must be positive")
        api_key = self.credentials.api_key.get_secret_value()
        plaintext = (
            f"{timestamp_ms}{api_key}{self.receive_window_ms}{payload}"
        )
        return SignedRequestHeaders(
            api_key=self.credentials.api_key,
            timestamp_ms=timestamp_ms,
            signature=SecretStr(self._hmac_hex(plaintext)),
            receive_window_ms=self.receive_window_ms,
        )

    def _hmac_hex(self, plaintext: str) -> str:
        secret = self.credentials.api_secret.get_secret_value().encode("utf-8")
        return hmac.new(
            secret,
            plaintext.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
