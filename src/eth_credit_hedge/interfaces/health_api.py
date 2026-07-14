"""Dependency-free health and status HTTP interface."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from eth_credit_hedge.domain.operations import OperationalSnapshot


@dataclass(frozen=True, slots=True)
class HealthResponse:
    status_code: int
    payload: dict[str, object]

    def to_json_bytes(self) -> bytes:
        return json.dumps(
            self.payload,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()


class HealthApi:
    def __init__(
        self,
        snapshot_provider: Callable[[], OperationalSnapshot],
    ) -> None:
        self._snapshot_provider = snapshot_provider

    def handle_get(self, path: str) -> HealthResponse:
        snapshot = self._snapshot_provider()
        if path == "/health/live":
            return HealthResponse(
                200 if snapshot.service_running else 503,
                {"live": snapshot.service_running},
            )
        if path == "/health/ready":
            reasons = snapshot.readiness_reasons
            return HealthResponse(
                200 if not reasons else 503,
                {"ready": not reasons, "reasons": list(reasons)},
            )
        if path == "/status/strategy":
            return HealthResponse(
                200,
                {
                    "cycle_id": snapshot.cycle_id,
                    "open_cycles": snapshot.open_cycles,
                    "active_levels": snapshot.active_levels,
                    "open_hedge_quantity": str(snapshot.open_hedge_quantity),
                    "unprotected_quantity": str(snapshot.unprotected_quantity),
                    "recovery_debt": str(snapshot.recovery_debt),
                    "remaining_stop_budget": str(snapshot.remaining_stop_budget),
                    "daily_pnl": str(snapshot.daily_pnl),
                },
            )
        if path == "/status/exchange":
            return HealthResponse(
                200,
                {
                    "market_data_age_ms": snapshot.market_data_age_ms,
                    "public_connected": snapshot.public_connected,
                    "private_connected": snapshot.private_connected,
                    "database_available": snapshot.database_available,
                    "reconciliation_complete": snapshot.reconciliation_complete,
                    "reconciliation_state": snapshot.reconciliation_state,
                    "protection_missing": snapshot.protection_missing,
                },
            )
        if path == "/status/risk":
            return HealthResponse(
                200,
                {
                    "risk_lock_active": snapshot.risk_lock_active,
                    "last_risk_reasons": list(snapshot.last_risk_reasons),
                },
            )
        return HealthResponse(404, {"error": "not found"})


def create_health_server(
    api: HealthApi,
    *,
    host: str = "127.0.0.1",
    port: int = 8080,
) -> ThreadingHTTPServer:
    """Create, but do not start, the operator health server."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            response = api.handle_get(urlsplit(self.path).path)
            body = response.to_json_bytes()
            self.send_response(response.status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            del format, args

    return ThreadingHTTPServer((host, port), Handler)
