"""Health and status endpoints share one fail-closed operational snapshot."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from eth_credit_hedge.domain.operations import OperationalSnapshot
from eth_credit_hedge.interfaces.health_api import HealthApi


def healthy() -> OperationalSnapshot:
    return OperationalSnapshot(
        service_running=True,
        cycle_id="cycle-1",
        market_data_age_ms=100,
        maximum_market_data_age_ms=1000,
        public_connected=True,
        private_connected=True,
        database_available=True,
        reconciliation_complete=True,
        reconciliation_state="MATCHED",
        open_cycles=1,
        active_levels=1,
        open_hedge_quantity=Decimal("0.01"),
        unprotected_quantity=Decimal("0"),
        protection_missing=False,
        recovery_debt=Decimal("0"),
        remaining_stop_budget=Decimal("10"),
        daily_pnl=Decimal("1.25"),
        order_rejections=0,
        duplicate_executions=0,
        restart_reconciliations=1,
        risk_lock_active=False,
        last_risk_reasons=(),
    )


def test_live_ready_and_status_routes_have_stable_payloads() -> None:
    snapshot = healthy()
    api = HealthApi(lambda: snapshot)

    live = api.handle_get("/health/live")
    ready = api.handle_get("/health/ready")
    strategy = api.handle_get("/status/strategy")
    exchange = api.handle_get("/status/exchange")
    risk = api.handle_get("/status/risk")

    assert live.status_code == 200
    assert live.payload == {"live": True}
    assert ready.status_code == 200
    assert ready.payload == {"ready": True, "reasons": []}
    assert strategy.payload["open_hedge_quantity"] == "0.01"
    assert exchange.payload["reconciliation_state"] == "MATCHED"
    assert risk.payload == {"risk_lock_active": False, "last_risk_reasons": []}


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"market_data_age_ms": 1001}, "market data is stale"),
        ({"public_connected": False}, "public stream is disconnected"),
        ({"private_connected": False}, "private stream is disconnected"),
        ({"database_available": False}, "database is unavailable"),
        ({"reconciliation_complete": False}, "reconciliation is incomplete"),
        ({"protection_missing": True}, "protection is missing"),
        ({"unprotected_quantity": Decimal("0.01")}, "position is unprotected"),
        ({"risk_lock_active": True}, "risk lock is active"),
    ],
)
def test_readiness_is_false_for_every_declared_condition(
    changes: dict[str, object],
    reason: str,
) -> None:
    response = HealthApi(lambda: replace(healthy(), **changes)).handle_get(
        "/health/ready"
    )

    assert response.status_code == 503
    assert response.payload["ready"] is False
    assert reason in response.payload["reasons"]


def test_liveness_and_unknown_route_status_codes() -> None:
    api = HealthApi(lambda: replace(healthy(), service_running=False))

    assert api.handle_get("/health/live").status_code == 503
    assert api.handle_get("/unknown").status_code == 404
