"""Prometheus text rendering for the fixed Plan 7 metric set."""

from __future__ import annotations

from eth_credit_hedge.domain.operations import OperationalSnapshot


def render_prometheus(snapshot: OperationalSnapshot) -> str:
    values: tuple[tuple[str, object], ...] = (
        ("market_data_age_ms", snapshot.market_data_age_ms),
        ("public_connected", int(snapshot.public_connected)),
        ("private_connected", int(snapshot.private_connected)),
        ("reconciliation_complete", int(snapshot.reconciliation_complete)),
        ("open_cycles", snapshot.open_cycles),
        ("active_levels", snapshot.active_levels),
        ("open_hedge_quantity", snapshot.open_hedge_quantity),
        ("unprotected_quantity", snapshot.unprotected_quantity),
        ("recovery_debt", snapshot.recovery_debt),
        ("remaining_stop_budget", snapshot.remaining_stop_budget),
        ("daily_pnl", snapshot.daily_pnl),
        ("order_rejections_total", snapshot.order_rejections),
        ("duplicate_executions_total", snapshot.duplicate_executions),
        ("restart_reconciliations_total", snapshot.restart_reconciliations),
    )
    return "".join(
        f"eth_credit_hedge_{name} {value}\n" for name, value in values
    )
