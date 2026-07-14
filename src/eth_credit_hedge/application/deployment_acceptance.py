"""Fail-closed shadow, pilot, release, and gradual-rollout gates."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import IntEnum

from eth_credit_hedge.config.deployment import EnvironmentProfile
from eth_credit_hedge.config.schema import RuntimeEnvironment


ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class AcceptanceReport:
    accepted: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ShadowAcceptanceMetrics:
    recorded_intents: int
    reproduced_intents: int
    contradictory_transitions: int
    stale_data_approved_intents: int
    invalid_quantized_intents: int
    nondeterministic_risk_decisions: int
    expiry_cutoff_violations: int

    def __post_init__(self) -> None:
        for field_name in (
            "recorded_intents",
            "reproduced_intents",
            "contradictory_transitions",
            "stale_data_approved_intents",
            "invalid_quantized_intents",
            "nondeterministic_risk_decisions",
            "expiry_cutoff_violations",
        ):
            value = getattr(self, field_name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{field_name.replace('_', ' ')} cannot be negative")
        if self.recorded_intents == 0:
            raise ValueError("shadow acceptance requires at least one recorded intent")
        if self.reproduced_intents > self.recorded_intents:
            raise ValueError("reproduced intents cannot exceed recorded intents")


def evaluate_shadow_acceptance(metrics: ShadowAcceptanceMetrics) -> AcceptanceReport:
    reasons: list[str] = []
    if metrics.reproduced_intents != metrics.recorded_intents:
        reasons.append("not all shadow intents reproduce offline")
    if metrics.contradictory_transitions:
        reasons.append("shadow run contains contradictory transitions")
    if metrics.stale_data_approved_intents:
        reasons.append("stale data approved a shadow intent")
    if metrics.invalid_quantized_intents:
        reasons.append("shadow run contains invalid quantization")
    if metrics.nondeterministic_risk_decisions:
        reasons.append("shadow risk decisions are not deterministic")
    if metrics.expiry_cutoff_violations:
        reasons.append("shadow run violated the expiry cutoff")
    return AcceptanceReport(not reasons, tuple(reasons))


@dataclass(frozen=True, slots=True)
class ReleaseEvidence:
    versioned_migration: bool
    changelog_updated: bool
    configuration_diff_reviewed: bool
    risk_reviewed: bool
    rollback_tested: bool
    demo_smoke_passed: bool
    shadow_replay_passed: bool
    operator_approved: bool
    legal_eligibility_confirmed: bool


@dataclass(frozen=True, slots=True)
class RollbackEvidence:
    kill_switch_engaged: bool
    target_artifact_available: bool
    configuration_snapshot_available: bool
    database_backup_verified: bool
    execution_schema_compatible: bool
    journal_schema_compatible: bool
    event_replay_passed: bool
    reconciliation_matched: bool
    smoke_tests_passed: bool


def evaluate_rollback_acceptance(evidence: RollbackEvidence) -> AcceptanceReport:
    checks = (
        (evidence.kill_switch_engaged, "kill switch is not engaged"),
        (evidence.target_artifact_available, "target artifact is unavailable"),
        (
            evidence.configuration_snapshot_available,
            "target configuration snapshot is unavailable",
        ),
        (evidence.database_backup_verified, "database backup is not verified"),
        (
            evidence.execution_schema_compatible,
            "execution schema is incompatible with rollback target",
        ),
        (
            evidence.journal_schema_compatible,
            "journal schema is incompatible with rollback target",
        ),
        (evidence.event_replay_passed, "event replay has not passed"),
        (evidence.reconciliation_matched, "reconciliation is not MATCHED"),
        (evidence.smoke_tests_passed, "rollback smoke tests have not passed"),
    )
    reasons = tuple(message for passed, message in checks if not passed)
    return AcceptanceReport(not reasons, reasons)


@dataclass(frozen=True, slots=True)
class PilotConfiguration:
    option_spread_count: int
    virtual_level_count: int
    distributed_recovery_enabled: bool
    automatic_scaling_enabled: bool
    configured_quantity: Decimal
    smallest_practical_quantity: Decimal
    monitoring_available: bool
    manual_intervention_available: bool
    recovery_enabled: bool = False

    def __post_init__(self) -> None:
        for field_name in ("option_spread_count", "virtual_level_count"):
            value = getattr(self, field_name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{field_name.replace('_', ' ')} must be positive")
        for field_name in ("configured_quantity", "smallest_practical_quantity"):
            value = Decimal(getattr(self, field_name))
            if not value.is_finite() or value <= ZERO:
                raise ValueError(f"{field_name.replace('_', ' ')} must be positive")
            object.__setattr__(self, field_name, value)


def evaluate_pilot_acceptance(
    profile: EnvironmentProfile,
    evidence: ReleaseEvidence,
    configuration: PilotConfiguration,
) -> AcceptanceReport:
    reasons = _release_reasons(evidence)
    if profile.environment is not RuntimeEnvironment.PRODUCTION_PILOT:
        reasons.append("pilot requires the PRODUCTION_PILOT profile")
    if not profile.external_order_mutations_enabled:
        reasons.append("pilot profile does not permit controlled order mutations")
    if configuration.option_spread_count != 1:
        reasons.append("pilot must use exactly one option spread")
    if configuration.virtual_level_count != 1:
        reasons.append("pilot must use exactly one virtual level")
    if configuration.distributed_recovery_enabled:
        reasons.append("distributed recovery must remain disabled")
    if configuration.recovery_enabled:
        reasons.append("recovery must remain disabled for the initial pilot")
    if configuration.automatic_scaling_enabled:
        reasons.append("automatic scaling must remain disabled")
    if configuration.configured_quantity != configuration.smallest_practical_quantity:
        reasons.append("pilot must use the smallest practical quantity")
    if not configuration.monitoring_available:
        reasons.append("operator monitoring is unavailable")
    if not configuration.manual_intervention_available:
        reasons.append("manual intervention is unavailable")
    return AcceptanceReport(not reasons, tuple(reasons))


def _release_reasons(evidence: ReleaseEvidence) -> list[str]:
    checks = (
        (evidence.versioned_migration, "versioned migration evidence is missing"),
        (evidence.changelog_updated, "changelog is not updated"),
        (
            evidence.configuration_diff_reviewed,
            "configuration diff is not reviewed",
        ),
        (evidence.risk_reviewed, "risk review is not complete"),
        (evidence.rollback_tested, "rollback has not been tested"),
        (evidence.demo_smoke_passed, "demo smoke test has not passed"),
        (evidence.shadow_replay_passed, "shadow replay has not passed"),
        (evidence.operator_approved, "operator approval is missing"),
        (
            evidence.legal_eligibility_confirmed,
            "legal and account eligibility are unconfirmed",
        ),
    )
    return [message for passed, message in checks if not passed]


class RolloutStage(IntEnum):
    ONE_LEVEL = 1
    MULTIPLE_BASELINE_LEVELS = 2
    SAME_LEVEL_RECOVERY = 3
    CAPPED_QUANTITY_INCREASE = 4
    ADDITIONAL_CYCLES = 5


@dataclass(frozen=True, slots=True)
class RolloutConfiguration:
    stage: RolloutStage
    maximum_notional: Decimal
    level_count: int
    recovery_enabled: bool
    cycle_limit: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "stage", RolloutStage(self.stage))
        notional = Decimal(self.maximum_notional)
        if not notional.is_finite() or notional <= ZERO:
            raise ValueError("rollout maximum notional must be positive")
        object.__setattr__(self, "maximum_notional", notional)
        if self.level_count <= 0 or self.cycle_limit <= 0:
            raise ValueError("rollout level and cycle limits must be positive")
        if self.stage is RolloutStage.ONE_LEVEL and (
            self.level_count != 1 or self.recovery_enabled or self.cycle_limit != 1
        ):
            raise ValueError("one-level rollout must have one level, cycle, and no recovery")
        if self.stage is RolloutStage.MULTIPLE_BASELINE_LEVELS and (
            self.level_count <= 1 or self.recovery_enabled
        ):
            raise ValueError("multiple baseline rollout requires levels and no recovery")
        if self.stage >= RolloutStage.SAME_LEVEL_RECOVERY and not self.recovery_enabled:
            raise ValueError("recovery rollout stages require same-level recovery")


def validate_rollout_transition(
    previous: RolloutConfiguration,
    candidate: RolloutConfiguration,
) -> AcceptanceReport:
    reasons: list[str] = []
    if candidate.stage != previous.stage + 1:
        reasons.append("rollout must advance exactly one declared stage")
    if candidate.maximum_notional < previous.maximum_notional:
        reasons.append("rollout maximum notional cannot decrease in this workflow")
    complexity_increased = (
        candidate.level_count > previous.level_count
        or (candidate.recovery_enabled and not previous.recovery_enabled)
        or candidate.cycle_limit > previous.cycle_limit
    )
    if (
        complexity_increased
        and candidate.maximum_notional > previous.maximum_notional
    ):
        reasons.append("complexity and notional cannot increase in the same release")
    return AcceptanceReport(not reasons, tuple(reasons))
