"""Thread-safe mutable owner for the runtime's immutable operations snapshot."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
from threading import Lock

from eth_credit_hedge.application.demo_runtime_state import DemoRuntimeState
from eth_credit_hedge.domain.accounting.reconstruction import CombinedLedgerState
from eth_credit_hedge.domain.operations import OperationalSnapshot


ZERO = Decimal("0")


class MutableOperationalState:
    def __init__(
        self,
        *,
        maximum_market_data_age_ms: int,
        clock: Callable[[], datetime],
    ) -> None:
        if maximum_market_data_age_ms <= 0:
            raise ValueError("maximum market-data age must be positive")
        self._maximum_market_data_age_ms = maximum_market_data_age_ms
        self._clock = clock
        self._lock = Lock()
        self._runtime: DemoRuntimeState | None = None
        self._accounting: CombinedLedgerState | None = None
        self._last_market_at: datetime | None = None
        self._service_running = False
        self._public_connected = False
        self._private_connected = False
        self._database_available = True
        self._reconciliation_state = "NOT_STARTED"
        self._last_risk_reasons: tuple[str, ...] = ()
        self._restart_reconciliations = 0

    def update_runtime(self, runtime: DemoRuntimeState) -> None:
        with self._lock:
            self._runtime = runtime

    def update_accounting(self, accounting: CombinedLedgerState) -> None:
        with self._lock:
            self._accounting = accounting

    def mark_running(self, running: bool) -> None:
        with self._lock:
            self._service_running = running

    def mark_public(self, connected: bool, observed_at: datetime | None = None) -> None:
        with self._lock:
            self._public_connected = connected
            if observed_at is not None:
                self._last_market_at = _utc(observed_at)

    def mark_private(self, connected: bool) -> None:
        with self._lock:
            self._private_connected = connected

    def mark_reconciliation(self, matched: bool, state: str) -> None:
        with self._lock:
            self._reconciliation_state = state
            if matched:
                self._restart_reconciliations += 1

    def mark_database(self, available: bool) -> None:
        with self._lock:
            self._database_available = available

    def mark_risk(self, reasons: tuple[str, ...]) -> None:
        with self._lock:
            self._last_risk_reasons = tuple(reasons)

    def snapshot(self) -> OperationalSnapshot:
        with self._lock:
            runtime = self._runtime
            accounting = self._accounting
            now = _utc(self._clock())
            age_ms = (
                self._maximum_market_data_age_ms + 1
                if self._last_market_at is None
                else max(0, int((now - self._last_market_at).total_seconds() * 1000))
            )
            levels = () if runtime is None else runtime.levels
            active = tuple(
                level for level in levels if level.active_entry_order_link_id is not None
            )
            open_quantity = (
                sum((level.active_quantity for level in active), ZERO)
                if accounting is None
                else accounting.hedge.open_quantity
            )
            unprotected = sum(
                (
                    level.active_quantity
                    for level in active
                    if level.active_stop_order_link_id is None
                ),
                ZERO,
            )
            debt = (
                sum((level.confirmed_debt for level in levels), ZERO)
                if accounting is None
                else accounting.confirmed_recovery_debt.value
            )
            return OperationalSnapshot(
                service_running=self._service_running,
                cycle_id=None if runtime is None else runtime.cycle_id,
                market_data_age_ms=age_ms,
                maximum_market_data_age_ms=self._maximum_market_data_age_ms,
                public_connected=self._public_connected,
                private_connected=self._private_connected,
                database_available=self._database_available,
                reconciliation_complete=(
                    False if runtime is None else runtime.reconciliation_complete
                ),
                reconciliation_state=self._reconciliation_state,
                open_cycles=0 if runtime is None else 1,
                active_levels=len(active),
                open_hedge_quantity=open_quantity,
                unprotected_quantity=unprotected,
                protection_missing=unprotected > ZERO,
                recovery_debt=debt,
                remaining_stop_budget=sum(
                    (level.option_budget for level in levels),
                    ZERO,
                ),
                daily_pnl=(
                    accounting.net_combined_mark_pnl.value
                    if accounting is not None
                    else ZERO
                    if runtime is None
                    else runtime.daily_realized_pnl
                ),
                order_rejections=0,
                duplicate_executions=0,
                restart_reconciliations=self._restart_reconciliations,
                risk_lock_active=bool(self._last_risk_reasons),
                last_risk_reasons=self._last_risk_reasons,
            )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("operational timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


__all__ = ["MutableOperationalState"]
