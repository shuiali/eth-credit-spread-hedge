"""Plan 7 environment isolation and fail-closed startup validation."""

from __future__ import annotations

from dataclasses import replace

import pytest

from eth_credit_hedge.config.deployment import (
    StartupRefusedError,
    StartupState,
    load_all_environment_profiles,
    validate_startup,
)
from eth_credit_hedge.config.schema import RuntimeEnvironment


def test_six_profiles_have_separate_databases_and_credential_scopes() -> None:
    profiles = load_all_environment_profiles()

    assert tuple(profile.environment for profile in profiles) == (
        RuntimeEnvironment.LOCAL_EXACT,
        RuntimeEnvironment.LOCAL_SIMULATED,
        RuntimeEnvironment.DEMO,
        RuntimeEnvironment.SHADOW_MAINNET,
        RuntimeEnvironment.PRODUCTION_PILOT,
        RuntimeEnvironment.PRODUCTION,
    )
    assert len({profile.database_path for profile in profiles}) == 6
    external = [profile for profile in profiles if profile.credential_key_env]
    assert len({profile.credential_key_env for profile in external}) == 4
    assert len({profile.credential_secret_env for profile in external}) == 4
    assert all(profile.risk_limits.maximum_perp_quantity.is_finite() for profile in profiles)
    assert all(profile.maximum_market_data_age_ms > 0 for profile in profiles)


def test_shadow_profile_is_mainnet_bound_but_cannot_mutate_orders() -> None:
    profile = load_all_environment_profiles()[3]

    assert profile.environment is RuntimeEnvironment.SHADOW_MAINNET
    assert profile.rest_base_url == "https://api.bybit.com"
    assert not profile.external_order_mutations_enabled
    with pytest.raises(ValueError, match="environment and REST URL conflict"):
        replace(profile, rest_base_url="https://api-demo.bybit.com")
    with pytest.raises(ValueError, match="cannot enable external order mutations"):
        replace(profile, external_order_mutations_enabled=True)


def test_startup_refuses_all_unsafe_observed_state_at_once() -> None:
    profile = load_all_environment_profiles()[3]
    unsafe = StartupState(
        execution_schema_version=2,
        journal_schema_version=0,
        kill_switch_available=False,
        clock_drift_ms=profile.maximum_clock_drift_ms + 1,
        reconciliation_complete=False,
        credentials_available=False,
        database_available=False,
    )

    with pytest.raises(StartupRefusedError) as captured:
        validate_startup(profile, unsafe)

    assert captured.value.reasons == (
        "execution database migrations are pending",
        "journal database migrations are pending",
        "kill switch is unavailable",
        "clock drift exceeds configured maximum",
        "startup reconciliation is incomplete",
        "required credential scope is unavailable",
        "database is unavailable",
    )


def test_safe_local_and_shadow_states_pass_startup_validation() -> None:
    for profile in (
        load_all_environment_profiles()[0],
        load_all_environment_profiles()[3],
    ):
        validate_startup(
            profile,
            StartupState(
                execution_schema_version=profile.required_execution_schema_version,
                journal_schema_version=profile.required_journal_schema_version,
                kill_switch_available=True,
                clock_drift_ms=0,
                reconciliation_complete=True,
                credentials_available=profile.credential_key_env is not None,
                database_available=True,
            ),
        )
