"""Compact, parseable client order IDs for idempotent submissions."""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from enum import Enum


class ClientOrderRole(str, Enum):
    OPTION_LONG = "OPTION_LONG"
    OPTION_SHORT = "OPTION_SHORT"
    HEDGE_ENTRY = "HEDGE_ENTRY"
    HEDGE_TP = "HEDGE_TP"
    HEDGE_STOP = "HEDGE_STOP"
    EMERGENCY_CLOSE = "EMERGENCY_CLOSE"


_ROLE_TO_TOKEN = {
    ClientOrderRole.OPTION_LONG: "OL",
    ClientOrderRole.OPTION_SHORT: "OS",
    ClientOrderRole.HEDGE_ENTRY: "ENTRY",
    ClientOrderRole.HEDGE_TP: "TP",
    ClientOrderRole.HEDGE_STOP: "STOP",
    ClientOrderRole.EMERGENCY_CLOSE: "EC",
}
_TOKEN_TO_ROLE = {token: role for role, token in _ROLE_TO_TOKEN.items()}
_COMPONENT_PATTERN = re.compile(r"^[A-Za-z0-9]{1,4}$")
_NONCE_PATTERN = re.compile(r"^[0-9A-F]{4,6}$")
_CLIENT_ID_PATTERN = re.compile(
    r"^ECH-(?P<instance>[A-Za-z0-9]{1,4})-"
    r"C(?P<cycle>[0-9]{4})-L(?P<level>[0-9]{2})-"
    r"(?P<role>OL|OS|ENTRY|TP|STOP|EC)-"
    r"A(?P<attempt>[0-9]{2})-(?P<nonce>[0-9A-F]{4,6})$"
)


def _bounded_integer(value: int, maximum: int, field_name: str) -> None:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"{field_name} must be between 0 and {maximum}")


@dataclass(frozen=True, slots=True)
class ClientOrderId:
    strategy_instance: str
    cycle: int
    level: int
    role: ClientOrderRole
    attempt: int
    nonce: str

    def __post_init__(self) -> None:
        if _COMPONENT_PATTERN.fullmatch(self.strategy_instance) is None:
            raise ValueError("strategy instance must be 1-4 alphanumeric characters")
        _bounded_integer(self.cycle, 9_999, "cycle")
        _bounded_integer(self.level, 99, "level")
        _bounded_integer(self.attempt, 99, "attempt")
        try:
            role = ClientOrderRole(self.role)
        except ValueError as exc:
            raise ValueError("unknown client order role") from exc
        nonce = self.nonce.upper()
        if _NONCE_PATTERN.fullmatch(nonce) is None:
            raise ValueError("nonce must contain 4-6 hexadecimal characters")
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "nonce", nonce)
        if len(str(self)) > 36:
            raise ValueError("client order ID cannot exceed 36 characters")

    @classmethod
    def new(
        cls,
        strategy_instance: str,
        cycle: int,
        level: int,
        role: ClientOrderRole,
        attempt: int,
    ) -> ClientOrderId:
        return cls(
            strategy_instance=strategy_instance,
            cycle=cycle,
            level=level,
            role=role,
            attempt=attempt,
            nonce=secrets.token_hex(3).upper(),
        )

    @classmethod
    def parse(cls, value: str) -> ClientOrderId:
        match = _CLIENT_ID_PATTERN.fullmatch(value)
        if match is None:
            raise ValueError("invalid client order ID")
        return cls(
            strategy_instance=match.group("instance"),
            cycle=int(match.group("cycle")),
            level=int(match.group("level")),
            role=_TOKEN_TO_ROLE[match.group("role")],
            attempt=int(match.group("attempt")),
            nonce=match.group("nonce"),
        )

    def __str__(self) -> str:
        return (
            f"ECH-{self.strategy_instance}-C{self.cycle:04d}-"
            f"L{self.level:02d}-{_ROLE_TO_TOKEN[self.role]}-"
            f"A{self.attempt:02d}-{self.nonce}"
        )
