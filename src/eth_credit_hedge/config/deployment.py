"""Isolated deployment profiles and fail-closed startup checks."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from eth_credit_hedge.config.schema import RuntimeEnvironment
from eth_credit_hedge.domain.risk import RiskLimits


_PROFILE_FILENAMES = (
    "local_exact.toml",
    "local_simulated.toml",
    "demo.toml",
    "shadow_mainnet.toml",
    "production_pilot.toml",
    "production.toml",
)
_LOCAL_ENVIRONMENTS = {
    RuntimeEnvironment.LOCAL_EXACT,
    RuntimeEnvironment.LOCAL_SIMULATED,
}
_MUTATING_ENVIRONMENTS = {
    RuntimeEnvironment.DEMO,
    RuntimeEnvironment.PRODUCTION_PILOT,
    RuntimeEnvironment.PRODUCTION,
}
_EXPECTED_ENDPOINTS: dict[RuntimeEnvironment, tuple[str | None, str | None]] = {
    RuntimeEnvironment.LOCAL_EXACT: (None, None),
    RuntimeEnvironment.LOCAL_SIMULATED: (None, None),
    RuntimeEnvironment.DEMO: (
        "https://api-demo.bybit.com",
        "wss://stream-demo.bybit.com/v5/private",
    ),
    RuntimeEnvironment.SHADOW_MAINNET: (
        "https://api.bybit.com",
        "wss://stream.bybit.com/v5/private",
    ),
    RuntimeEnvironment.PRODUCTION_PILOT: (
        "https://api.bybit.com",
        "wss://stream.bybit.com/v5/private",
    ),
    RuntimeEnvironment.PRODUCTION: (
        "https://api.bybit.com",
        "wss://stream.bybit.com/v5/private",
    ),
}


@dataclass(frozen=True, slots=True)
class EnvironmentProfile:
    environment: RuntimeEnvironment
    database_path: Path
    rest_base_url: str | None
    private_websocket_url: str | None
    credential_key_env: str | None
    credential_secret_env: str | None
    external_order_mutations_enabled: bool
    required_execution_schema_version: int
    required_journal_schema_version: int
    maximum_clock_drift_ms: int
    maximum_market_data_age_ms: int
    risk_limits: RiskLimits

    def __post_init__(self) -> None:
        environment = RuntimeEnvironment(self.environment)
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "database_path", Path(self.database_path))
        if not str(self.database_path):
            raise ValueError("database path cannot be empty")
        expected_rest, expected_websocket = _EXPECTED_ENDPOINTS[environment]
        if self.rest_base_url != expected_rest:
            raise ValueError("environment and REST URL conflict")
        if self.private_websocket_url != expected_websocket:
            raise ValueError("environment and private WebSocket URL conflict")
        credentials = (self.credential_key_env, self.credential_secret_env)
        if environment in _LOCAL_ENVIRONMENTS:
            if credentials != (None, None):
                raise ValueError("local environments cannot bind credentials")
        elif any(value is None or not value.strip() for value in credentials):
            raise ValueError("external environment requires a credential scope")
        if (
            self.external_order_mutations_enabled
            and environment not in _MUTATING_ENVIRONMENTS
        ):
            raise ValueError(
                f"{environment.value} cannot enable external order mutations"
            )
        for field_name in (
            "required_execution_schema_version",
            "required_journal_schema_version",
            "maximum_clock_drift_ms",
            "maximum_market_data_age_ms",
        ):
            value = getattr(self, field_name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{field_name.replace('_', ' ')} cannot be negative")


@dataclass(frozen=True, slots=True)
class StartupState:
    execution_schema_version: int
    journal_schema_version: int
    kill_switch_available: bool
    clock_drift_ms: int
    reconciliation_complete: bool
    credentials_available: bool
    database_available: bool


class StartupRefusedError(RuntimeError):
    def __init__(self, reasons: tuple[str, ...]) -> None:
        self.reasons = reasons
        super().__init__("startup refused: " + "; ".join(reasons))


def validate_startup(profile: EnvironmentProfile, state: StartupState) -> None:
    """Refuse startup until every Plan 7 prerequisite is observed safe."""

    reasons: list[str] = []
    if state.execution_schema_version < profile.required_execution_schema_version:
        reasons.append("execution database migrations are pending")
    elif state.execution_schema_version > profile.required_execution_schema_version:
        reasons.append("execution database schema is newer than this runtime")
    if state.journal_schema_version < profile.required_journal_schema_version:
        reasons.append("journal database migrations are pending")
    elif state.journal_schema_version > profile.required_journal_schema_version:
        reasons.append("journal database schema is newer than this runtime")
    if not state.kill_switch_available:
        reasons.append("kill switch is unavailable")
    if abs(state.clock_drift_ms) > profile.maximum_clock_drift_ms:
        reasons.append("clock drift exceeds configured maximum")
    if not state.reconciliation_complete:
        reasons.append("startup reconciliation is incomplete")
    if profile.credential_key_env is not None and not state.credentials_available:
        reasons.append("required credential scope is unavailable")
    if not state.database_available:
        reasons.append("database is unavailable")
    if reasons:
        raise StartupRefusedError(tuple(reasons))


def load_all_environment_profiles() -> tuple[EnvironmentProfile, ...]:
    directory = Path(__file__).with_name("environments")
    return tuple(
        load_environment_profile(directory / filename)
        for filename in _PROFILE_FILENAMES
    )


def load_environment_profile(path: Path) -> EnvironmentProfile:
    with path.open("rb") as source:
        raw: Any = tomllib.load(source)
    if not isinstance(raw, dict):
        raise ValueError("environment profile must contain a TOML table")
    risk = raw.get("risk_limits")
    if not isinstance(risk, dict):
        raise ValueError("environment profile requires finite risk limits")
    try:
        limits = RiskLimits(
            maximum_perp_quantity=Decimal(str(risk["maximum_perp_quantity"])),
            maximum_perp_notional=Decimal(str(risk["maximum_perp_notional"])),
            maximum_margin_usage=Decimal(str(risk["maximum_margin_usage"])),
            minimum_liquidation_distance=Decimal(
                str(risk["minimum_liquidation_distance"])
            ),
            maximum_recovery_debt=Decimal(str(risk["maximum_recovery_debt"])),
            maximum_projected_stop_loss=Decimal(
                str(risk["maximum_projected_stop_loss"])
            ),
            maximum_realized_cycle_loss=Decimal(
                str(risk["maximum_realized_cycle_loss"])
            ),
            maximum_daily_realized_loss=Decimal(
                str(risk["maximum_daily_realized_loss"])
            ),
            maximum_entries_per_level=int(risk["maximum_entries_per_level"]),
            maximum_active_levels=int(risk["maximum_active_levels"]),
            maximum_order_requests_per_minute=int(
                risk["maximum_order_requests_per_minute"]
            ),
            maximum_reconciliation_failures=int(
                risk["maximum_reconciliation_failures"]
            ),
        )
        return EnvironmentProfile(
            environment=RuntimeEnvironment(str(raw["environment"])),
            database_path=Path(str(raw["database_path"])),
            rest_base_url=_optional_text(raw.get("rest_base_url")),
            private_websocket_url=_optional_text(raw.get("private_websocket_url")),
            credential_key_env=_optional_text(raw.get("credential_key_env")),
            credential_secret_env=_optional_text(raw.get("credential_secret_env")),
            external_order_mutations_enabled=bool(
                raw["external_order_mutations_enabled"]
            ),
            required_execution_schema_version=int(
                raw["required_execution_schema_version"]
            ),
            required_journal_schema_version=int(
                raw["required_journal_schema_version"]
            ),
            maximum_clock_drift_ms=int(raw["maximum_clock_drift_ms"]),
            maximum_market_data_age_ms=int(raw["maximum_market_data_age_ms"]),
            risk_limits=limits,
        )
    except KeyError as exc:
        raise ValueError(f"environment profile is missing {exc.args[0]}") from exc


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None
