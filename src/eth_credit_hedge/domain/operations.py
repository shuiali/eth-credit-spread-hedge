"""Shared operational state for health, status, metrics, and alerts."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class OperationalSnapshot:
    service_running: bool
    cycle_id: str | None
    market_data_age_ms: int
    maximum_market_data_age_ms: int
    public_connected: bool
    private_connected: bool
    database_available: bool
    reconciliation_complete: bool
    reconciliation_state: str
    open_cycles: int
    active_levels: int
    open_hedge_quantity: Decimal
    unprotected_quantity: Decimal
    protection_missing: bool
    recovery_debt: Decimal
    remaining_stop_budget: Decimal
    daily_pnl: Decimal
    order_rejections: int
    duplicate_executions: int
    restart_reconciliations: int
    risk_lock_active: bool
    last_risk_reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        for field_name in (
            "service_running",
            "public_connected",
            "private_connected",
            "database_available",
            "reconciliation_complete",
            "protection_missing",
            "risk_lock_active",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise ValueError(f"{field_name.replace('_', ' ')} must be boolean")
        if self.cycle_id is not None and not self.cycle_id.strip():
            raise ValueError("cycle ID cannot be empty")
        if not self.reconciliation_state.strip():
            raise ValueError("reconciliation state cannot be empty")
        for field_name in (
            "market_data_age_ms",
            "maximum_market_data_age_ms",
            "open_cycles",
            "active_levels",
            "order_rejections",
            "duplicate_executions",
            "restart_reconciliations",
        ):
            value = getattr(self, field_name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{field_name.replace('_', ' ')} cannot be negative")
        for field_name in (
            "open_hedge_quantity",
            "unprotected_quantity",
            "recovery_debt",
            "remaining_stop_budget",
            "daily_pnl",
        ):
            value = Decimal(getattr(self, field_name))
            if not value.is_finite():
                raise ValueError(f"{field_name.replace('_', ' ')} must be finite")
            if field_name != "daily_pnl" and value < ZERO:
                raise ValueError(f"{field_name.replace('_', ' ')} cannot be negative")
            object.__setattr__(self, field_name, value)
        object.__setattr__(self, "last_risk_reasons", tuple(self.last_risk_reasons))

    @property
    def readiness_reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if self.market_data_age_ms > self.maximum_market_data_age_ms:
            reasons.append("market data is stale")
        if not self.public_connected:
            reasons.append("public stream is disconnected")
        if not self.private_connected:
            reasons.append("private stream is disconnected")
        if not self.database_available:
            reasons.append("database is unavailable")
        if not self.reconciliation_complete:
            reasons.append("reconciliation is incomplete")
        if self.protection_missing:
            reasons.append("protection is missing")
        if self.unprotected_quantity > ZERO:
            reasons.append("position is unprotected")
        if self.risk_lock_active:
            reasons.append("risk lock is active")
        return tuple(reasons)
