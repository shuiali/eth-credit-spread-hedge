"""Deterministic alert evaluation and active-alert deduplication."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from eth_credit_hedge.ports.notifications import NotificationPort


ZERO = Decimal("0")
ONE = Decimal("1")


class AlertSeverity(str, Enum):
    IMMEDIATE = "IMMEDIATE"
    WARNING = "WARNING"


class AlertCode(str, Enum):
    UNPROTECTED_POSITION = "UNPROTECTED_POSITION"
    UNKNOWN_POSITION = "UNKNOWN_POSITION"
    RISK_VIOLATION = "RISK_VIOLATION"
    DATABASE_FAILURE = "DATABASE_FAILURE"
    AUTHENTICATION_FAILURE = "AUTHENTICATION_FAILURE"
    DANGEROUS_RECONCILIATION = "DANGEROUS_RECONCILIATION"
    KILL_SWITCH = "KILL_SWITCH"
    STALE_DATA = "STALE_DATA"
    STALE_OPTION_QUOTE = "STALE_OPTION_QUOTE"
    ORDER_PENDING_TOO_LONG = "ORDER_PENDING_TOO_LONG"
    LARGE_SLIPPAGE = "LARGE_SLIPPAGE"
    DEBT_NEAR_LIMIT = "DEBT_NEAR_LIMIT"
    EXPIRY_APPROACHING = "EXPIRY_APPROACHING"


@dataclass(frozen=True, slots=True)
class Alert:
    code: AlertCode
    severity: AlertSeverity
    message: str


@dataclass(frozen=True, slots=True)
class AlertPolicy:
    maximum_market_data_age_ms: int
    maximum_option_quote_age_ms: int
    maximum_pending_order_age_ms: int
    large_slippage: Decimal
    maximum_recovery_debt: Decimal
    debt_warning_ratio: Decimal
    expiry_warning_hours: int

    def __post_init__(self) -> None:
        for field_name in (
            "maximum_market_data_age_ms",
            "maximum_option_quote_age_ms",
            "maximum_pending_order_age_ms",
            "expiry_warning_hours",
        ):
            value = getattr(self, field_name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{field_name.replace('_', ' ')} cannot be negative")
        for field_name in ("large_slippage", "maximum_recovery_debt"):
            value = Decimal(getattr(self, field_name))
            if not value.is_finite() or value <= ZERO:
                raise ValueError(f"{field_name.replace('_', ' ')} must be positive")
            object.__setattr__(self, field_name, value)
        ratio = Decimal(self.debt_warning_ratio)
        if not ratio.is_finite() or not ZERO < ratio <= ONE:
            raise ValueError("debt warning ratio must be in (0, 1]")
        object.__setattr__(self, "debt_warning_ratio", ratio)


@dataclass(frozen=True, slots=True)
class AlertObservation:
    unprotected_quantity: Decimal
    unknown_position: bool
    risk_violation: bool
    database_available: bool
    authentication_succeeded: bool
    dangerous_reconciliation: bool
    kill_switch_triggered: bool
    market_data_age_ms: int
    option_quote_age_ms: int
    pending_order_age_ms: int
    stop_slippage: Decimal
    recovery_debt: Decimal
    hours_to_expiry: int


def evaluate_alerts(
    observation: AlertObservation,
    policy: AlertPolicy,
) -> tuple[Alert, ...]:
    alerts: list[Alert] = []

    def add(condition: bool, code: AlertCode, severity: AlertSeverity, message: str) -> None:
        if condition:
            alerts.append(Alert(code, severity, message))

    add(
        observation.unprotected_quantity > ZERO,
        AlertCode.UNPROTECTED_POSITION,
        AlertSeverity.IMMEDIATE,
        "confirmed position is not protected",
    )
    add(observation.unknown_position, AlertCode.UNKNOWN_POSITION, AlertSeverity.IMMEDIATE, "unknown exchange position detected")
    add(observation.risk_violation, AlertCode.RISK_VIOLATION, AlertSeverity.IMMEDIATE, "risk limit violation detected")
    add(not observation.database_available, AlertCode.DATABASE_FAILURE, AlertSeverity.IMMEDIATE, "database is unavailable")
    add(not observation.authentication_succeeded, AlertCode.AUTHENTICATION_FAILURE, AlertSeverity.IMMEDIATE, "exchange authentication failed")
    add(observation.dangerous_reconciliation, AlertCode.DANGEROUS_RECONCILIATION, AlertSeverity.IMMEDIATE, "dangerous reconciliation state detected")
    add(observation.kill_switch_triggered, AlertCode.KILL_SWITCH, AlertSeverity.IMMEDIATE, "kill switch activated")
    add(observation.market_data_age_ms > policy.maximum_market_data_age_ms, AlertCode.STALE_DATA, AlertSeverity.WARNING, "market data exceeded freshness limit")
    add(observation.option_quote_age_ms > policy.maximum_option_quote_age_ms, AlertCode.STALE_OPTION_QUOTE, AlertSeverity.WARNING, "option quote exceeded freshness limit")
    add(observation.pending_order_age_ms > policy.maximum_pending_order_age_ms, AlertCode.ORDER_PENDING_TOO_LONG, AlertSeverity.WARNING, "order remained pending too long")
    add(observation.stop_slippage > policy.large_slippage, AlertCode.LARGE_SLIPPAGE, AlertSeverity.WARNING, "stop slippage exceeded warning limit")
    add(observation.recovery_debt >= policy.maximum_recovery_debt * policy.debt_warning_ratio, AlertCode.DEBT_NEAR_LIMIT, AlertSeverity.WARNING, "recovery debt is near its limit")
    add(observation.hours_to_expiry <= policy.expiry_warning_hours, AlertCode.EXPIRY_APPROACHING, AlertSeverity.WARNING, "option expiry is approaching")
    return tuple(alerts)


class AlertDispatcher:
    def __init__(self, notifications: NotificationPort) -> None:
        self._notifications = notifications
        self._active: set[AlertCode] = set()

    async def dispatch(
        self,
        observation: AlertObservation,
        policy: AlertPolicy,
    ) -> tuple[Alert, ...]:
        alerts = evaluate_alerts(observation, policy)
        current = {alert.code for alert in alerts}
        for alert in alerts:
            if alert.code not in self._active:
                await self._notifications.send(alert)
        self._active = current
        return alerts
