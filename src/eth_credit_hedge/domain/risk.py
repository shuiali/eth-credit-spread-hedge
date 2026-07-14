"""Independent finite-limit risk gate for live trade proposals."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal


ZERO = Decimal("0")
TradeSide = Literal["Buy", "Sell"]


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


def _positive(value: Decimal, field_name: str) -> Decimal:
    normalized = _decimal(value, field_name)
    if normalized <= ZERO:
        raise ValueError(f"{field_name} must be positive")
    return normalized


def _positive_integer(value: int, field_name: str) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field_name} must be positive")


def _nonnegative_integer(value: int, field_name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{field_name} cannot be negative")


@dataclass(frozen=True, slots=True)
class TradeProposal:
    symbol: str
    side: TradeSide
    quantity: Decimal
    price: Decimal
    notional: Decimal
    projected_stop_loss: Decimal
    opens_new_level: bool

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            raise ValueError("symbol cannot be empty")
        if self.side not in ("Buy", "Sell"):
            raise ValueError("side must be Buy or Sell")
        object.__setattr__(self, "quantity", _positive(self.quantity, "quantity"))
        object.__setattr__(self, "price", _positive(self.price, "price"))
        object.__setattr__(self, "notional", _positive(self.notional, "notional"))
        object.__setattr__(
            self,
            "projected_stop_loss",
            _nonnegative(self.projected_stop_loss, "projected stop loss"),
        )
        if type(self.opens_new_level) is not bool:
            raise ValueError("opens new level must be boolean")


@dataclass(frozen=True, slots=True)
class RiskState:
    current_perp_quantity: Decimal
    current_perp_notional: Decimal
    post_trade_margin_usage: Decimal
    post_trade_liquidation_distance: Decimal
    confirmed_recovery_debt: Decimal
    realized_cycle_loss: Decimal
    daily_realized_loss: Decimal
    entries_for_level: int
    active_levels: int
    order_requests_last_minute: int
    consecutive_reconciliation_failures: int
    market_data_fresh: bool
    reconciliation_succeeded: bool

    def __post_init__(self) -> None:
        for field_name in (
            "current_perp_quantity",
            "current_perp_notional",
            "post_trade_margin_usage",
            "post_trade_liquidation_distance",
            "confirmed_recovery_debt",
            "realized_cycle_loss",
            "daily_realized_loss",
        ):
            object.__setattr__(
                self,
                field_name,
                _nonnegative(
                    getattr(self, field_name),
                    field_name.replace("_", " "),
                ),
            )
        for field_name in (
            "entries_for_level",
            "active_levels",
            "order_requests_last_minute",
            "consecutive_reconciliation_failures",
        ):
            _nonnegative_integer(
                getattr(self, field_name),
                field_name.replace("_", " "),
            )
        for field_name in ("market_data_fresh", "reconciliation_succeeded"):
            if type(getattr(self, field_name)) is not bool:
                raise ValueError(f"{field_name.replace('_', ' ')} must be boolean")


@dataclass(frozen=True, slots=True)
class RiskLimits:
    maximum_perp_quantity: Decimal
    maximum_perp_notional: Decimal
    maximum_margin_usage: Decimal
    minimum_liquidation_distance: Decimal
    maximum_recovery_debt: Decimal
    maximum_projected_stop_loss: Decimal
    maximum_realized_cycle_loss: Decimal
    maximum_daily_realized_loss: Decimal
    maximum_entries_per_level: int
    maximum_active_levels: int
    maximum_order_requests_per_minute: int
    maximum_reconciliation_failures: int

    def __post_init__(self) -> None:
        for field_name in (
            "maximum_perp_quantity",
            "maximum_perp_notional",
            "maximum_margin_usage",
            "minimum_liquidation_distance",
            "maximum_recovery_debt",
            "maximum_projected_stop_loss",
            "maximum_realized_cycle_loss",
            "maximum_daily_realized_loss",
        ):
            object.__setattr__(
                self,
                field_name,
                _positive(
                    getattr(self, field_name),
                    field_name.replace("_", " "),
                ),
            )
        for field_name in (
            "maximum_entries_per_level",
            "maximum_active_levels",
            "maximum_order_requests_per_minute",
            "maximum_reconciliation_failures",
        ):
            _positive_integer(
                getattr(self, field_name),
                field_name.replace("_", " "),
            )


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: bool
    approved_quantity: Decimal | None
    reasons: tuple[str, ...]
    requires_operator_ack: bool


class RiskEngine:
    """Veto proposals; it never resizes an approved quantity."""

    def evaluate(
        self,
        proposal: TradeProposal,
        state: RiskState,
        limits: RiskLimits,
    ) -> RiskDecision:
        reasons: list[str] = []
        if (
            state.current_perp_quantity + proposal.quantity
            > limits.maximum_perp_quantity
        ):
            reasons.append("maximum perpetual quantity exceeded")
        if (
            state.current_perp_notional + proposal.notional
            > limits.maximum_perp_notional
        ):
            reasons.append("maximum perpetual notional exceeded")
        if state.post_trade_margin_usage > limits.maximum_margin_usage:
            reasons.append("maximum margin usage exceeded")
        if (
            state.post_trade_liquidation_distance
            < limits.minimum_liquidation_distance
        ):
            reasons.append("minimum liquidation distance breached")
        if state.confirmed_recovery_debt > limits.maximum_recovery_debt:
            reasons.append("maximum recovery debt exceeded")
        if proposal.projected_stop_loss > limits.maximum_projected_stop_loss:
            reasons.append("maximum projected stop loss exceeded")
        if state.realized_cycle_loss > limits.maximum_realized_cycle_loss:
            reasons.append("maximum realized cycle loss exceeded")
        if state.daily_realized_loss > limits.maximum_daily_realized_loss:
            reasons.append("maximum daily realized loss exceeded")
        if state.entries_for_level >= limits.maximum_entries_per_level:
            reasons.append("maximum entries per level reached")
        if (
            proposal.opens_new_level
            and state.active_levels >= limits.maximum_active_levels
        ):
            reasons.append("maximum active levels reached")
        if (
            state.order_requests_last_minute
            >= limits.maximum_order_requests_per_minute
        ):
            reasons.append("maximum order request rate reached")
        if (
            state.consecutive_reconciliation_failures
            >= limits.maximum_reconciliation_failures
        ):
            reasons.append("maximum reconciliation failures reached")
        if not state.market_data_fresh:
            reasons.append("market data is stale")
        if not state.reconciliation_succeeded:
            reasons.append("reconciliation has not succeeded")

        operator_reasons = {
            "maximum realized cycle loss exceeded",
            "maximum daily realized loss exceeded",
            "reconciliation has not succeeded",
        }
        return RiskDecision(
            approved=not reasons,
            approved_quantity=proposal.quantity if not reasons else None,
            reasons=tuple(reasons),
            requires_operator_ack=any(reason in operator_reasons for reason in reasons),
        )
