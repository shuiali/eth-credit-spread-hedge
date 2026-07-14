"""Confirmed-debt-only local FULL_NEXT_TP recovery planning and settlement."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum

from eth_credit_hedge.core.virtual_levels import HedgeLevel
from eth_credit_hedge.domain.instrument_rules import (
    PriceQuantizationPolicy,
    ceil_to_step,
    normalize_and_validate_order,
    recalculate_quantized_risk,
)
from eth_credit_hedge.domain.instruments import InstrumentSpec
from eth_credit_hedge.domain.risk import RiskEngine, RiskLimits, RiskState, TradeProposal


ZERO = Decimal("0")


def _decimal(value: Decimal, field_name: str) -> Decimal:
    try:
        normalized = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be finite") from exc
    if not normalized.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return normalized


def _nonnegative(value: Decimal, field_name: str) -> Decimal:
    normalized = _decimal(value, field_name)
    if normalized < ZERO:
        raise ValueError(f"{field_name} cannot be negative")
    return normalized


def _utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


class LockedLevelAction(str, Enum):
    CLOSE_OPTION_STRATEGY = "CLOSE_OPTION_STRATEGY"


@dataclass(frozen=True, slots=True)
class RecoveryDebtState:
    projected_debt: Decimal
    confirmed_debt: Decimal
    allocated_debt: Decimal
    remaining_debt: Decimal

    def __post_init__(self) -> None:
        for field_name in (
            "projected_debt",
            "confirmed_debt",
            "allocated_debt",
            "remaining_debt",
        ):
            object.__setattr__(
                self,
                field_name,
                _nonnegative(
                    getattr(self, field_name),
                    field_name.replace("_", " "),
                ),
            )
        if self.confirmed_debt != self.allocated_debt + self.remaining_debt:
            raise ValueError(
                "confirmed debt must equal allocated plus remaining debt"
            )

    @classmethod
    def empty(
        cls,
        *,
        projected_debt: Decimal = ZERO,
    ) -> RecoveryDebtState:
        return cls(
            projected_debt=projected_debt,
            confirmed_debt=ZERO,
            allocated_debt=ZERO,
            remaining_debt=ZERO,
        )


@dataclass(frozen=True, slots=True)
class RecoveryDebtSnapshot:
    level_id: int
    debt: RecoveryDebtState
    version: int
    updated_at: datetime

    def __post_init__(self) -> None:
        if type(self.level_id) is not int or self.level_id <= 0:
            raise ValueError("level ID must be positive")
        if type(self.version) is not int or self.version <= 0:
            raise ValueError("debt snapshot version must be positive")
        object.__setattr__(self, "updated_at", _utc(self.updated_at, "update time"))


def add_confirmed_stop_debt(
    state: RecoveryDebtState,
    actual_stop_debt: Decimal,
) -> RecoveryDebtState:
    debt = _nonnegative(actual_stop_debt, "actual stop debt")
    return replace(
        state,
        confirmed_debt=state.confirmed_debt + debt,
        remaining_debt=state.remaining_debt + debt,
    )


def allocate_confirmed_debt(state: RecoveryDebtState) -> RecoveryDebtState:
    if state.allocated_debt != ZERO:
        raise ValueError("recovery debt is already allocated to an active hedge")
    return replace(
        state,
        allocated_debt=state.remaining_debt,
        remaining_debt=ZERO,
    )


def settle_recovery_take_profit(
    state: RecoveryDebtState,
    *,
    realized_take_profit: Decimal,
    zone_budget: Decimal,
) -> RecoveryDebtState:
    if state.allocated_debt == ZERO:
        raise ValueError("recovery TP requires explicitly allocated debt")
    realized = _decimal(realized_take_profit, "realized take profit")
    zone = _nonnegative(zone_budget, "zone budget")
    recovery_profit = max(realized - zone, ZERO)
    paid = min(recovery_profit, state.allocated_debt)
    unpaid_allocation = state.allocated_debt - paid
    return replace(
        state,
        confirmed_debt=state.confirmed_debt - paid,
        allocated_debt=ZERO,
        remaining_debt=state.remaining_debt + unpaid_allocation,
    )


def release_allocated_debt(state: RecoveryDebtState) -> RecoveryDebtState:
    if state.allocated_debt == ZERO:
        return state
    return replace(
        state,
        allocated_debt=ZERO,
        remaining_debt=state.remaining_debt + state.allocated_debt,
    )


@dataclass(frozen=True, slots=True)
class RecoveryEntryPlan:
    approved: bool
    raw_desired_quantity: Decimal
    entry_price: Decimal
    quantity: Decimal | None
    expected_take_profit: Decimal
    allocated_debt: Decimal
    reasons: tuple[str, ...]
    locked_action: LockedLevelAction | None


class SameLevelRecoveryPlanner:
    """Plan local FULL_NEXT_TP recovery; no cross-level allocation exists here."""

    def __init__(self, risk_engine: RiskEngine) -> None:
        self._risk_engine = risk_engine

    def plan(
        self,
        level: HedgeLevel,
        debt: RecoveryDebtState,
        instrument: InstrumentSpec,
        risk_state: RiskState,
        limits: RiskLimits,
    ) -> RecoveryEntryPlan:
        if debt.allocated_debt != ZERO:
            raise ValueError("cannot plan while recovery debt is already allocated")
        if risk_state.confirmed_recovery_debt < debt.confirmed_debt:
            raise ValueError("risk state underreports the level recovery debt")
        if instrument.category != "linear" or instrument.symbol != "ETHUSDT":
            raise ValueError("recovery requires ETHUSDT linear")
        if level.tp_distance <= ZERO:
            raise ValueError("recovery level requires positive TP distance")
        raw_desired = (
            level.option_budget + debt.confirmed_debt
        ) / level.tp_distance
        rounded_up = ceil_to_step(
            raw_desired,
            instrument.lot_size_filter.qty_step,
        )
        validation = normalize_and_validate_order(
            instrument,
            side="Sell",
            quantity=rounded_up,
            price=level.entry_price,
            price_policy=PriceQuantizationPolicy.PASSIVE,
        )
        quantized = recalculate_quantized_risk(
            validation,
            instrument,
            entry_side="Sell",
            take_profit_price=level.tp_price,
            stop_price=level.stop_price,
            recovery_debt=debt.confirmed_debt,
            maximum_notional=limits.maximum_perp_notional,
            maximum_projected_stop_loss=limits.maximum_projected_stop_loss,
        )
        reasons = list(quantized.errors)
        required_profit = level.option_budget + debt.confirmed_debt
        if quantized.tp_profit < required_profit:
            reasons.append(
                "quantized TP profit does not cover zone budget and confirmed debt"
            )
        if not reasons:
            decision = self._risk_engine.evaluate(
                TradeProposal(
                    symbol="ETHUSDT",
                    side="Sell",
                    quantity=quantized.quantity,
                    price=quantized.entry_price,
                    notional=quantized.notional,
                    projected_stop_loss=quantized.projected_stop_loss,
                    opens_new_level=True,
                ),
                risk_state,
                limits,
            )
            reasons.extend(decision.reasons)
        approved = not reasons
        return RecoveryEntryPlan(
            approved=approved,
            raw_desired_quantity=raw_desired,
            entry_price=quantized.entry_price,
            quantity=quantized.quantity if approved else None,
            expected_take_profit=quantized.tp_profit,
            allocated_debt=debt.confirmed_debt if approved else ZERO,
            reasons=tuple(reasons),
            locked_action=(
                None if approved else LockedLevelAction.CLOSE_OPTION_STRATEGY
            ),
        )
