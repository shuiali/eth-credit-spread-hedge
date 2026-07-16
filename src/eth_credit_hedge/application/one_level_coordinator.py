"""One-level live crossing coordinator with explicit market and risk gates."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from eth_credit_hedge.application.one_level_entry import OneLevelEntryService
from eth_credit_hedge.config import StrategyCostConfig
from eth_credit_hedge.core.virtual_levels import HedgeLevel, LevelState
from eth_credit_hedge.domain.execution import PlaceOrderRequest
from eth_credit_hedge.domain.instrument_rules import (
    PriceQuantizationPolicy,
    normalize_and_validate_order,
    recalculate_quantized_risk,
)
from eth_credit_hedge.domain.instruments import InstrumentSpec
from eth_credit_hedge.domain.live_execution import EntryExecutionSnapshot
from eth_credit_hedge.domain.market_data import (
    MarketDataHealthResult,
    TriggerPriceEvent,
    TriggerPriceSource,
)
from eth_credit_hedge.domain.option_position import (
    OptionPositionState,
    PutCreditSpreadPosition,
)
from eth_credit_hedge.domain.risk import (
    RiskEngine,
    RiskLimits,
    RiskState,
    TradeProposal,
)
from eth_credit_hedge.domain.strategy_math import (
    InstrumentRules,
    Money,
    Quantity,
    SizingStatus,
    StrategyMathEngine,
)
from eth_credit_hedge.ports.control import EntryGatePort


@dataclass(frozen=True, slots=True)
class OneLevelTriggerResult:
    triggered: bool
    reasons: tuple[str, ...]
    snapshot: EntryExecutionSnapshot | None = None
    request: PlaceOrderRequest | None = None


class OneLevelCoordinator:
    """Convert exactly one fresh downward crossing into one entry request."""

    def __init__(
        self,
        *,
        entry_service: OneLevelEntryService,
        option_position: PutCreditSpreadPosition,
        level: HedgeLevel,
        instrument: InstrumentSpec,
        risk_engine: RiskEngine,
        risk_limits: RiskLimits,
        order_link_id_factory: Callable[[], str],
        entry_gate: EntryGatePort | None = None,
        costs: StrategyCostConfig | None = None,
    ) -> None:
        if level.level_id != 1:
            raise ValueError("one-level coordinator requires level 1")
        if instrument.category != "linear" or instrument.symbol != "ETHUSDT":
            raise ValueError("one-level coordinator requires ETHUSDT linear")
        self._entry_service = entry_service
        self._option_position = option_position
        self._level = level
        self._instrument = instrument
        self._risk_engine = risk_engine
        self._risk_limits = risk_limits
        self._order_link_id_factory = order_link_id_factory
        self._entry_gate = entry_gate
        self._costs = costs or StrategyCostConfig()
        self._previous_price: Decimal | None = None
        self._connection_generation: int | None = None
        self._entry_armed = False
        self._submitted = False

    async def on_trigger(
        self,
        event: TriggerPriceEvent,
        market_health: MarketDataHealthResult,
        risk_state: RiskState,
    ) -> OneLevelTriggerResult:
        if event.symbol != self._instrument.symbol:
            return OneLevelTriggerResult(False, ("trigger symbol does not match",))
        if event.source is not TriggerPriceSource.LAST_TRADE:
            return OneLevelTriggerResult(False, ("trigger source is not LAST_TRADE",))
        if not market_health.trading_allowed:
            return OneLevelTriggerResult(False, market_health.reasons)

        if self._connection_generation is None:
            self._reset_segment(event)
            return OneLevelTriggerResult(False, ())
        if event.connection_generation < self._connection_generation:
            return OneLevelTriggerResult(False, ("connection generation is stale",))
        if event.connection_generation > self._connection_generation:
            self._reset_segment(event)
            return OneLevelTriggerResult(False, ("connection generation changed",))

        previous_price = self._previous_price
        if previous_price is None:
            raise AssertionError("initialized segment must have a previous price")
        current_price = event.observed_price
        if current_price >= self._level.entry_price and not self._submitted:
            self._entry_armed = True
        crossing = (
            self._entry_armed
            and previous_price > self._level.entry_price
            and current_price <= self._level.entry_price
        )
        self._previous_price = current_price
        if not crossing:
            return OneLevelTriggerResult(False, ())

        self._entry_armed = False
        if self._submitted:
            return OneLevelTriggerResult(False, ("entry is already submitted",))
        if self._entry_gate is not None and not self._entry_gate.entries_allowed:
            return OneLevelTriggerResult(
                False,
                ("kill switch blocks new entries",),
            )
        if self._level.state is not LevelState.READY:
            return OneLevelTriggerResult(False, ("virtual level is not READY",))
        if self._option_position.state is not OptionPositionState.OPEN:
            return OneLevelTriggerResult(False, ("option spread is not OPEN",))

        sizing = StrategyMathEngine.size_budget(
            role="BASELINE",
            zone_option_loss_budget=Money(self._level.option_budget),
            confirmed_recovery_debt=Money(Decimal("0")),
            configured_buffer=Money(self._costs.baseline_buffer_usd),
            costs=self._costs.execution_context(
                entry_price=self._level.entry_price,
                tp_price=self._level.tp_price,
                stop_price=self._level.stop_price,
            ),
            instrument=InstrumentRules(
                quantity_step=Quantity(self._instrument.lot_size_filter.qty_step),
                minimum_quantity=Quantity(
                    self._instrument.lot_size_filter.min_order_qty
                ),
                maximum_quantity=Quantity(
                    min(
                        self._instrument.lot_size_filter.max_order_qty,
                        self._instrument.lot_size_filter.max_market_order_qty
                        or self._instrument.lot_size_filter.max_order_qty,
                        self._risk_limits.maximum_perp_quantity,
                    )
                ),
                maximum_notional=Money(self._risk_limits.maximum_perp_notional),
                maximum_projected_stop_loss=Money(
                    self._risk_limits.maximum_projected_stop_loss
                ),
            ),
        )
        if sizing.status is SizingStatus.REJECTED_BY_RISK:
            return OneLevelTriggerResult(
                False,
                ("cost-aware baseline sizing rejected by finite risk limit",),
            )
        validation = normalize_and_validate_order(
            self._instrument,
            side="Sell",
            quantity=sizing.submitted_quantity.value,
            price=self._level.entry_price,
            price_policy=PriceQuantizationPolicy.PASSIVE,
        )
        quantized = recalculate_quantized_risk(
            validation,
            self._instrument,
            entry_side="Sell",
            take_profit_price=self._level.tp_price,
            stop_price=self._level.stop_price,
            maximum_notional=self._risk_limits.maximum_perp_notional,
            maximum_projected_stop_loss=(
                self._risk_limits.maximum_projected_stop_loss
            ),
        )
        if not quantized.accepted:
            return OneLevelTriggerResult(False, quantized.errors)

        proposal = TradeProposal(
            symbol=self._instrument.symbol,
            side="Sell",
            quantity=quantized.quantity,
            price=quantized.entry_price,
            notional=quantized.notional,
            projected_stop_loss=sizing.projected_net_stop_loss.value,
            opens_new_level=True,
        )
        decision = self._risk_engine.evaluate(
            proposal,
            risk_state,
            self._risk_limits,
        )
        if not decision.approved:
            return OneLevelTriggerResult(False, decision.reasons)
        if decision.approved_quantity != quantized.quantity:
            raise AssertionError("approved risk quantity must be exact")

        request = PlaceOrderRequest(
            category="linear",
            symbol=self._instrument.symbol,
            side="Sell",
            order_type="Market",
            quantity=quantized.quantity,
            order_link_id=self._order_link_id_factory(),
            time_in_force="IOC",
            reduce_only=False,
            position_idx=0,
        )
        self._submitted = True
        snapshot = await self._entry_service.submit_entry(request)
        return OneLevelTriggerResult(
            triggered=True,
            reasons=(),
            snapshot=snapshot,
            request=request,
        )

    def _reset_segment(self, event: TriggerPriceEvent) -> None:
        self._connection_generation = event.connection_generation
        self._previous_price = event.observed_price
        self._entry_armed = (
            not self._submitted and event.observed_price >= self._level.entry_price
        )
