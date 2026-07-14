"""Order-free live shadow decisions with deterministic offline replay."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Protocol

from eth_credit_hedge.core.virtual_levels import HedgeLevel, LevelState
from eth_credit_hedge.domain.instrument_rules import (
    PriceQuantizationPolicy,
    normalize_and_validate_order,
    recalculate_quantized_risk,
)
from eth_credit_hedge.domain.instruments import InstrumentSpec
from eth_credit_hedge.domain.journal import canonical_json, canonical_object
from eth_credit_hedge.domain.market_data import (
    MarketDataHealthResult,
    TriggerPriceEvent,
    TriggerPriceSource,
)
from eth_credit_hedge.domain.risk import (
    RiskEngine,
    RiskLimits,
    RiskState,
    TradeProposal,
)


ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class ShadowIntent:
    sequence: int
    cycle_id: str
    level_id: int
    observed_at_utc: datetime
    connection_generation: int
    virtual_crossing_price: Decimal
    desired_quantity: Decimal
    quantized_quantity: Decimal | None
    risk_approved: bool
    risk_reasons: tuple[str, ...]
    hypothetical_entry_price: Decimal | None
    hypothetical_take_profit_price: Decimal | None
    hypothetical_stop_price: Decimal | None
    expected_tp_pnl: Decimal | None
    projected_stop_loss: Decimal | None

    def __post_init__(self) -> None:
        if self.sequence <= 0 or self.level_id <= 0:
            raise ValueError("shadow sequence and level ID must be positive")
        if not self.cycle_id.strip():
            raise ValueError("shadow cycle ID cannot be empty")
        if self.observed_at_utc.tzinfo is None or self.observed_at_utc.utcoffset() is None:
            raise ValueError("shadow observation timestamp must be timezone-aware")
        object.__setattr__(
            self,
            "observed_at_utc",
            self.observed_at_utc.astimezone(timezone.utc),
        )
        if self.connection_generation < 0:
            raise ValueError("connection generation cannot be negative")
        for field_name in (
            "virtual_crossing_price",
            "desired_quantity",
            "quantized_quantity",
            "hypothetical_entry_price",
            "hypothetical_take_profit_price",
            "hypothetical_stop_price",
            "expected_tp_pnl",
            "projected_stop_loss",
        ):
            value = getattr(self, field_name)
            if value is None:
                continue
            normalized = Decimal(value)
            if not normalized.is_finite() or normalized < ZERO:
                raise ValueError(f"{field_name.replace('_', ' ')} cannot be negative")
            object.__setattr__(self, field_name, normalized)
        if self.virtual_crossing_price == ZERO or self.desired_quantity == ZERO:
            raise ValueError("shadow crossing price and desired quantity must be positive")
        if self.risk_approved and self.quantized_quantity is None:
            raise ValueError("approved shadow intent requires a quantized quantity")
        object.__setattr__(self, "risk_reasons", tuple(self.risk_reasons))

    def to_dict(self) -> dict[str, object]:
        return canonical_object(
            {
                "sequence": self.sequence,
                "cycle_id": self.cycle_id,
                "level_id": self.level_id,
                "observed_at_utc": self.observed_at_utc,
                "connection_generation": self.connection_generation,
                "virtual_crossing_price": self.virtual_crossing_price,
                "desired_quantity": self.desired_quantity,
                "quantized_quantity": self.quantized_quantity,
                "risk_approved": self.risk_approved,
                "risk_reasons": self.risk_reasons,
                "hypothetical_entry_price": self.hypothetical_entry_price,
                "hypothetical_take_profit_price": (
                    self.hypothetical_take_profit_price
                ),
                "hypothetical_stop_price": self.hypothetical_stop_price,
                "expected_tp_pnl": self.expected_tp_pnl,
                "projected_stop_loss": self.projected_stop_loss,
            }
        )

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @property
    def decision_digest(self) -> str:
        return hashlib.sha256(self.to_json().encode()).hexdigest()


class ShadowIntentRecorder(Protocol):
    def append(self, intent: ShadowIntent) -> None: ...


@dataclass(frozen=True, slots=True)
class ShadowTriggerResult:
    intents: tuple[ShadowIntent, ...]
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ShadowObservation:
    event: TriggerPriceEvent
    market_health: MarketDataHealthResult
    risk_state: RiskState


class ShadowModeService:
    """Evaluate crossings without accepting or invoking a trading adapter."""

    def __init__(
        self,
        *,
        cycle_id: str,
        levels: tuple[HedgeLevel, ...],
        desired_quantity: Decimal,
        option_spread_open: bool,
        instrument: InstrumentSpec,
        risk_engine: RiskEngine,
        risk_limits: RiskLimits,
        recorder: ShadowIntentRecorder | None = None,
    ) -> None:
        if not cycle_id.strip():
            raise ValueError("shadow cycle ID cannot be empty")
        normalized_levels = tuple(levels)
        if not normalized_levels:
            raise ValueError("shadow mode requires at least one level")
        if [level.level_id for level in normalized_levels] != list(
            range(1, len(normalized_levels) + 1)
        ):
            raise ValueError("shadow level IDs must be contiguous from 1")
        if [level.entry_price for level in normalized_levels] != sorted(
            (level.entry_price for level in normalized_levels),
            reverse=True,
        ):
            raise ValueError("shadow levels must be ordered highest to lowest")
        quantity = Decimal(desired_quantity)
        if not quantity.is_finite() or quantity <= ZERO:
            raise ValueError("shadow desired quantity must be positive")
        if type(option_spread_open) is not bool:
            raise ValueError("option spread open flag must be boolean")
        if instrument.category != "linear" or instrument.symbol != "ETHUSDT":
            raise ValueError("shadow mode requires the ETHUSDT linear instrument")
        self._cycle_id = cycle_id
        self._levels = normalized_levels
        self._desired_quantity = quantity
        self._option_spread_open = option_spread_open
        self._instrument = instrument
        self._risk_engine = risk_engine
        self._risk_limits = risk_limits
        self._recorder = recorder
        self._previous_price: Decimal | None = None
        self._connection_generation: int | None = None
        self._armed = {level.level_id: False for level in normalized_levels}
        self._decided_levels: set[int] = set()
        self._sequence = 0

    def on_trigger(
        self,
        event: TriggerPriceEvent,
        market_health: MarketDataHealthResult,
        risk_state: RiskState,
    ) -> ShadowTriggerResult:
        if event.symbol != self._instrument.symbol:
            return ShadowTriggerResult((), ("trigger symbol does not match",))
        if event.source is not TriggerPriceSource.LAST_TRADE:
            return ShadowTriggerResult((), ("trigger source is not LAST_TRADE",))
        if not market_health.trading_allowed:
            return ShadowTriggerResult((), market_health.reasons)
        if self._connection_generation is None:
            self._reset_segment(event)
            return ShadowTriggerResult(())
        if event.connection_generation < self._connection_generation:
            return ShadowTriggerResult((), ("connection generation is stale",))
        if event.connection_generation > self._connection_generation:
            self._reset_segment(event)
            return ShadowTriggerResult((), ("connection generation changed",))

        previous = self._previous_price
        if previous is None:
            raise AssertionError("initialized shadow segment requires a previous price")
        current = event.observed_price
        for level in self._levels:
            if current >= level.entry_price and level.level_id not in self._decided_levels:
                self._armed[level.level_id] = True
        crossed = tuple(
            level
            for level in self._levels
            if self._armed[level.level_id]
            and previous > level.entry_price
            and current <= level.entry_price
        )
        self._previous_price = current

        intents: list[ShadowIntent] = []
        effective_state = risk_state
        for level in crossed:
            self._armed[level.level_id] = False
            intent = self._evaluate(level, event, effective_state)
            intents.append(intent)
            if self._recorder is not None:
                self._recorder.append(intent)
            if intent.risk_approved:
                self._decided_levels.add(level.level_id)
                if intent.quantized_quantity is None or intent.hypothetical_entry_price is None:
                    raise AssertionError("approved shadow intent must be fully quantified")
                effective_state = replace(
                    effective_state,
                    current_perp_quantity=(
                        effective_state.current_perp_quantity
                        + intent.quantized_quantity
                    ),
                    current_perp_notional=(
                        effective_state.current_perp_notional
                        + intent.quantized_quantity * intent.hypothetical_entry_price
                    ),
                    active_levels=effective_state.active_levels + 1,
                    order_requests_last_minute=(
                        effective_state.order_requests_last_minute + 1
                    ),
                )
        return ShadowTriggerResult(tuple(intents))

    def _evaluate(
        self,
        level: HedgeLevel,
        event: TriggerPriceEvent,
        risk_state: RiskState,
    ) -> ShadowIntent:
        reasons: tuple[str, ...] = ()
        if level.state is not LevelState.READY:
            reasons = ("virtual level is not READY",)
        elif not self._option_spread_open:
            reasons = ("option spread is not OPEN",)

        validation = normalize_and_validate_order(
            self._instrument,
            side="Sell",
            quantity=self._desired_quantity,
            price=level.entry_price,
            price_policy=PriceQuantizationPolicy.PASSIVE,
        )
        quantized = recalculate_quantized_risk(
            validation,
            self._instrument,
            entry_side="Sell",
            take_profit_price=level.tp_price,
            stop_price=level.stop_price,
            recovery_debt=risk_state.confirmed_recovery_debt,
            maximum_notional=self._risk_limits.maximum_perp_notional,
            maximum_projected_stop_loss=(
                self._risk_limits.maximum_projected_stop_loss
            ),
        )
        if not reasons and not quantized.accepted:
            reasons = quantized.errors
        approved = False
        if not reasons:
            decision = self._risk_engine.evaluate(
                TradeProposal(
                    symbol=self._instrument.symbol,
                    side="Sell",
                    quantity=quantized.quantity,
                    price=quantized.entry_price,
                    notional=quantized.notional,
                    projected_stop_loss=quantized.projected_stop_loss,
                    opens_new_level=True,
                ),
                risk_state,
                self._risk_limits,
            )
            approved = decision.approved
            reasons = decision.reasons

        self._sequence += 1
        return ShadowIntent(
            sequence=self._sequence,
            cycle_id=self._cycle_id,
            level_id=level.level_id,
            observed_at_utc=event.observed_timestamp,
            connection_generation=event.connection_generation,
            virtual_crossing_price=level.entry_price,
            desired_quantity=self._desired_quantity,
            quantized_quantity=quantized.quantity,
            risk_approved=approved,
            risk_reasons=reasons,
            hypothetical_entry_price=quantized.entry_price,
            hypothetical_take_profit_price=quantized.take_profit_price,
            hypothetical_stop_price=quantized.stop_price,
            expected_tp_pnl=quantized.tp_profit,
            projected_stop_loss=quantized.projected_stop_loss,
        )

    def _reset_segment(self, event: TriggerPriceEvent) -> None:
        self._connection_generation = event.connection_generation
        self._previous_price = event.observed_price
        for level in self._levels:
            self._armed[level.level_id] = (
                level.level_id not in self._decided_levels
                and event.observed_price >= level.entry_price
            )


def replay_shadow_observations(
    service: ShadowModeService,
    observations: Iterable[ShadowObservation],
) -> tuple[ShadowIntent, ...]:
    intents: list[ShadowIntent] = []
    for observation in observations:
        result = service.on_trigger(
            observation.event,
            observation.market_health,
            observation.risk_state,
        )
        intents.extend(result.intents)
    return tuple(intents)
