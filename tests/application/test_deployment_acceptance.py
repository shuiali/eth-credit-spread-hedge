"""Shadow, pilot, release, and gradual-rollout gates fail closed."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from eth_credit_hedge.application.deployment_acceptance import (
    PilotConfiguration,
    ReleaseEvidence,
    RollbackEvidence,
    RolloutConfiguration,
    RolloutStage,
    ShadowAcceptanceMetrics,
    evaluate_pilot_acceptance,
    evaluate_rollback_acceptance,
    evaluate_shadow_acceptance,
    validate_rollout_transition,
)
from eth_credit_hedge.config.deployment import load_all_environment_profiles


def complete_release_evidence() -> ReleaseEvidence:
    return ReleaseEvidence(
        versioned_migration=True,
        changelog_updated=True,
        configuration_diff_reviewed=True,
        risk_reviewed=True,
        rollback_tested=True,
        demo_smoke_passed=True,
        shadow_replay_passed=True,
        operator_approved=True,
        legal_eligibility_confirmed=True,
    )


def pilot() -> PilotConfiguration:
    return PilotConfiguration(
        option_spread_count=1,
        virtual_level_count=1,
        distributed_recovery_enabled=False,
        automatic_scaling_enabled=False,
        configured_quantity=Decimal("0.001"),
        smallest_practical_quantity=Decimal("0.001"),
        monitoring_available=True,
        manual_intervention_available=True,
    )


def test_shadow_acceptance_requires_every_decision_to_reproduce() -> None:
    passing = ShadowAcceptanceMetrics(
        recorded_intents=100,
        reproduced_intents=100,
        contradictory_transitions=0,
        stale_data_approved_intents=0,
        invalid_quantized_intents=0,
        nondeterministic_risk_decisions=0,
        expiry_cutoff_violations=0,
    )
    failing = replace(
        passing,
        reproduced_intents=99,
        contradictory_transitions=1,
        stale_data_approved_intents=1,
        invalid_quantized_intents=1,
        nondeterministic_risk_decisions=1,
        expiry_cutoff_violations=1,
    )

    assert evaluate_shadow_acceptance(passing).accepted
    rejected = evaluate_shadow_acceptance(failing)
    assert not rejected.accepted
    assert len(rejected.reasons) == 6


def test_pilot_gate_requires_release_legal_and_small_fixed_scope() -> None:
    profile = load_all_environment_profiles()[4]

    accepted = evaluate_pilot_acceptance(
        profile,
        complete_release_evidence(),
        pilot(),
    )
    rejected = evaluate_pilot_acceptance(
        profile,
        replace(
            complete_release_evidence(),
            demo_smoke_passed=False,
            shadow_replay_passed=False,
            operator_approved=False,
            legal_eligibility_confirmed=False,
        ),
        replace(
            pilot(),
            virtual_level_count=2,
            distributed_recovery_enabled=True,
            automatic_scaling_enabled=True,
            monitoring_available=False,
            manual_intervention_available=False,
        ),
    )

    assert accepted.accepted
    assert not rejected.accepted
    assert "demo smoke test has not passed" in rejected.reasons
    assert "pilot must use exactly one virtual level" in rejected.reasons
    assert "automatic scaling must remain disabled" in rejected.reasons


def test_rollout_cannot_skip_stage_or_raise_complexity_and_notional_together() -> None:
    one = RolloutConfiguration(
        stage=RolloutStage.ONE_LEVEL,
        maximum_notional=Decimal("50"),
        level_count=1,
        recovery_enabled=False,
        cycle_limit=1,
    )
    multiple = RolloutConfiguration(
        stage=RolloutStage.MULTIPLE_BASELINE_LEVELS,
        maximum_notional=Decimal("50"),
        level_count=3,
        recovery_enabled=False,
        cycle_limit=1,
    )
    invalid_combined = replace(multiple, maximum_notional=Decimal("75"))
    skipped = replace(
        multiple,
        stage=RolloutStage.SAME_LEVEL_RECOVERY,
        recovery_enabled=True,
    )

    assert validate_rollout_transition(one, multiple).accepted
    assert not validate_rollout_transition(one, invalid_combined).accepted
    assert not validate_rollout_transition(one, skipped).accepted


def test_rollback_preflight_requires_pause_backup_compatibility_and_replay() -> None:
    complete = RollbackEvidence(
        kill_switch_engaged=True,
        target_artifact_available=True,
        configuration_snapshot_available=True,
        database_backup_verified=True,
        execution_schema_compatible=True,
        journal_schema_compatible=True,
        event_replay_passed=True,
        reconciliation_matched=True,
        smoke_tests_passed=True,
    )

    assert evaluate_rollback_acceptance(complete).accepted
    rejected = evaluate_rollback_acceptance(
        replace(
            complete,
            kill_switch_engaged=False,
            execution_schema_compatible=False,
            reconciliation_matched=False,
        )
    )
    assert rejected.reasons == (
        "kill switch is not engaged",
        "execution schema is incompatible with rollback target",
        "reconciliation is not MATCHED",
    )
