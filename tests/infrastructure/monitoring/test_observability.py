"""Secret-safe logs, required metrics, and actionable alert routing."""

from __future__ import annotations

import asyncio
import io
import json
from datetime import datetime, timezone
from decimal import Decimal

from eth_credit_hedge.domain.operations import OperationalSnapshot
from eth_credit_hedge.infrastructure.monitoring.alerts import (
    AlertCode,
    AlertDispatcher,
    AlertObservation,
    AlertPolicy,
    AlertSeverity,
    evaluate_alerts,
)
from eth_credit_hedge.infrastructure.monitoring.metrics import render_prometheus
from eth_credit_hedge.infrastructure.monitoring.structured_logging import (
    SecretSafeJsonLogger,
    StructuredLogEvent,
)


NOW = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)


def operational_snapshot() -> OperationalSnapshot:
    return OperationalSnapshot(
        service_running=True,
        cycle_id="cycle-1",
        market_data_age_ms=250,
        maximum_market_data_age_ms=1000,
        public_connected=True,
        private_connected=True,
        database_available=True,
        reconciliation_complete=True,
        reconciliation_state="MATCHED",
        open_cycles=1,
        active_levels=2,
        open_hedge_quantity=Decimal("0.02"),
        unprotected_quantity=Decimal("0"),
        protection_missing=False,
        recovery_debt=Decimal("3"),
        remaining_stop_budget=Decimal("7"),
        daily_pnl=Decimal("-1.5"),
        order_rejections=2,
        duplicate_executions=4,
        restart_reconciliations=1,
        risk_lock_active=False,
        last_risk_reasons=(),
    )


def test_structured_log_has_fixed_fields_and_redacts_configured_secrets() -> None:
    stream = io.StringIO()
    logger = SecretSafeJsonLogger(
        stream,
        secrets=("api-key-value", "api-secret-value"),
    )

    logger.write(
        StructuredLogEvent(
            timestamp=NOW,
            service="hedge-service",
            cycle_id="cycle-1",
            level_id=1,
            client_order_id="client-1",
            exchange_order_id="exchange-1",
            execution_id="execution-1",
            correlation_id="correlation-1",
            event="OrderRejected",
            message="key api-key-value secret api-secret-value",
        )
    )

    encoded = stream.getvalue()
    payload = json.loads(encoded)
    assert "api-key-value" not in encoded
    assert "api-secret-value" not in encoded
    assert payload["message"] == "key [REDACTED] secret [REDACTED]"
    assert tuple(payload) == (
        "timestamp",
        "service",
        "cycle_id",
        "level_id",
        "client_order_id",
        "exchange_order_id",
        "execution_id",
        "correlation_id",
        "event",
        "message",
    )


def test_prometheus_output_contains_every_required_metric() -> None:
    output = render_prometheus(operational_snapshot())

    for name in (
        "market_data_age_ms",
        "public_connected",
        "private_connected",
        "reconciliation_complete",
        "open_cycles",
        "active_levels",
        "open_hedge_quantity",
        "unprotected_quantity",
        "recovery_debt",
        "remaining_stop_budget",
        "daily_pnl",
        "order_rejections_total",
        "duplicate_executions_total",
        "restart_reconciliations_total",
    ):
        assert f"eth_credit_hedge_{name} " in output


def policy() -> AlertPolicy:
    return AlertPolicy(
        maximum_market_data_age_ms=1000,
        maximum_option_quote_age_ms=2000,
        maximum_pending_order_age_ms=3000,
        large_slippage=Decimal("5"),
        maximum_recovery_debt=Decimal("10"),
        debt_warning_ratio=Decimal("0.8"),
        expiry_warning_hours=24,
    )


def test_immediate_and_warning_alerts_are_classified_and_deduplicated() -> None:
    observation = AlertObservation(
        unprotected_quantity=Decimal("0.01"),
        unknown_position=True,
        risk_violation=True,
        database_available=False,
        authentication_succeeded=False,
        dangerous_reconciliation=True,
        kill_switch_triggered=True,
        market_data_age_ms=1500,
        option_quote_age_ms=2500,
        pending_order_age_ms=3500,
        stop_slippage=Decimal("6"),
        recovery_debt=Decimal("9"),
        hours_to_expiry=12,
    )
    alerts = evaluate_alerts(observation, policy())

    assert {alert.code for alert in alerts if alert.severity is AlertSeverity.IMMEDIATE} == {
        AlertCode.UNPROTECTED_POSITION,
        AlertCode.UNKNOWN_POSITION,
        AlertCode.RISK_VIOLATION,
        AlertCode.DATABASE_FAILURE,
        AlertCode.AUTHENTICATION_FAILURE,
        AlertCode.DANGEROUS_RECONCILIATION,
        AlertCode.KILL_SWITCH,
    }
    assert {alert.code for alert in alerts if alert.severity is AlertSeverity.WARNING} == {
        AlertCode.STALE_DATA,
        AlertCode.STALE_OPTION_QUOTE,
        AlertCode.ORDER_PENDING_TOO_LONG,
        AlertCode.LARGE_SLIPPAGE,
        AlertCode.DEBT_NEAR_LIMIT,
        AlertCode.EXPIRY_APPROACHING,
    }

    class Recorder:
        def __init__(self) -> None:
            self.alerts = []

        async def send(self, alert) -> None:
            self.alerts.append(alert)

    recorder = Recorder()
    dispatcher = AlertDispatcher(recorder)
    asyncio.run(dispatcher.dispatch(observation, policy()))
    asyncio.run(dispatcher.dispatch(observation, policy()))
    assert len(recorder.alerts) == len(alerts)
