"""M9 ordered baseline-only execution for several independent live levels."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from decimal import Decimal

from eth_credit_hedge.application.one_level_entry import OneLevelEntryService
from eth_credit_hedge.core.virtual_levels import HedgeLevel, LevelState
from eth_credit_hedge.domain.execution import (
    ExchangePosition,
    LiveExecutionState,
    PlaceOrderRequest,
)
from eth_credit_hedge.domain.instrument_rules import (
    PriceQuantizationPolicy,
    normalize_and_validate_order,
    recalculate_quantized_risk,
)
from eth_credit_hedge.domain.instruments import InstrumentSpec
from eth_credit_hedge.domain.live_execution import (
    EntryExecutionSnapshot,
    transition_entry_snapshot,
)
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
from eth_credit_hedge.ports.persistence import ExecutionPersistencePort


ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class LevelEntrySubmission:
    level_id: int
    request: PlaceOrderRequest
    snapshot: EntryExecutionSnapshot


@dataclass(frozen=True, slots=True)
class LevelEntryBlock:
    level_id: int
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MultiLevelTriggerResult:
    entries: tuple[LevelEntrySubmission, ...]
    blocked: tuple[LevelEntryBlock, ...]
    reasons: tuple[str, ...] = ()


class MultiLevelCoordinator:
    """Submit baseline quantities in crossing order; recovery is intentionally off."""

    def __init__(
        self,
        *,
        entry_service: OneLevelEntryService,
        store: ExecutionPersistencePort,
        option_position: PutCreditSpreadPosition,
        levels: tuple[HedgeLevel, ...],
        instrument: InstrumentSpec,
        risk_engine: RiskEngine,
        risk_limits: RiskLimits,
        order_link_id_factory: Callable[[int, int], str],
    ) -> None:
        normalized_levels = tuple(levels)
        if not normalized_levels:
            raise ValueError("multi-level coordinator requires at least one level")
        level_ids = [level.level_id for level in normalized_levels]
        if level_ids != list(range(1, len(normalized_levels) + 1)):
            raise ValueError("level IDs must be contiguous from 1")
        entry_prices = [level.entry_price for level in normalized_levels]
        if entry_prices != sorted(entry_prices, reverse=True):
            raise ValueError("levels must be ordered from highest entry to lowest")
        if instrument.category != "linear" or instrument.symbol != "ETHUSDT":
            raise ValueError("multi-level coordinator requires ETHUSDT linear")
        self._entry_service = entry_service
        self._store = store
        self._option_position = option_position
        self._levels = normalized_levels
        self._instrument = instrument
        self._risk_engine = risk_engine
        self._risk_limits = risk_limits
        self._order_link_id_factory = order_link_id_factory
        self._previous_price: Decimal | None = None
        self._connection_generation: int | None = None
        self._armed = {level.level_id: False for level in normalized_levels}
        self._attempts = {level.level_id: 0 for level in normalized_levels}
        self._submitted_order_ids: dict[int, str] = {}

    async def on_trigger(
        self,
        event: TriggerPriceEvent,
        market_health: MarketDataHealthResult,
        risk_state: RiskState,
    ) -> MultiLevelTriggerResult:
        if event.symbol != self._instrument.symbol:
            return MultiLevelTriggerResult((), (), ("trigger symbol does not match",))
        if event.source is not TriggerPriceSource.LAST_TRADE:
            return MultiLevelTriggerResult((), (), ("trigger source is not LAST_TRADE",))
        if not market_health.trading_allowed:
            return MultiLevelTriggerResult((), (), market_health.reasons)
        if self._connection_generation is None:
            self._reset_segment(event)
            return MultiLevelTriggerResult((), ())
        if event.connection_generation < self._connection_generation:
            return MultiLevelTriggerResult(
                (),
                (),
                ("connection generation is stale",),
            )
        if event.connection_generation > self._connection_generation:
            self._reset_segment(event)
            return MultiLevelTriggerResult(
                (),
                (),
                ("connection generation changed",),
            )

        previous = self._previous_price
        if previous is None:
            raise AssertionError("initialized segment must have a previous price")
        current = event.observed_price
        for level in self._levels:
            if (
                current >= level.entry_price
                and level.level_id not in self._submitted_order_ids
            ):
                self._armed[level.level_id] = True
        crossed = tuple(
            level
            for level in self._levels
            if self._armed[level.level_id]
            and previous > level.entry_price
            and current <= level.entry_price
        )
        self._previous_price = current

        entries: list[LevelEntrySubmission] = []
        blocked: list[LevelEntryBlock] = []
        effective_risk_state = risk_state
        for level in crossed:
            self._armed[level.level_id] = False
            reasons = self._preflight_reasons(level)
            if reasons:
                blocked.append(LevelEntryBlock(level.level_id, reasons))
                continue
            validation = normalize_and_validate_order(
                self._instrument,
                side="Sell",
                quantity=self._option_position.matched_quantity,
                price=level.entry_price,
                price_policy=PriceQuantizationPolicy.PASSIVE,
            )
            quantized = recalculate_quantized_risk(
                validation,
                self._instrument,
                entry_side="Sell",
                take_profit_price=level.tp_price,
                stop_price=level.stop_price,
                recovery_debt=ZERO,
                maximum_notional=self._risk_limits.maximum_perp_notional,
                maximum_projected_stop_loss=(
                    self._risk_limits.maximum_projected_stop_loss
                ),
            )
            if not quantized.accepted:
                blocked.append(LevelEntryBlock(level.level_id, quantized.errors))
                continue
            proposal = TradeProposal(
                symbol="ETHUSDT",
                side="Sell",
                quantity=quantized.quantity,
                price=quantized.entry_price,
                notional=quantized.notional,
                projected_stop_loss=quantized.projected_stop_loss,
                opens_new_level=True,
            )
            decision = self._risk_engine.evaluate(
                proposal,
                effective_risk_state,
                self._risk_limits,
            )
            if not decision.approved:
                blocked.append(LevelEntryBlock(level.level_id, decision.reasons))
                continue
            if decision.approved_quantity != quantized.quantity:
                raise AssertionError("approved risk quantity must be exact")

            attempt = self._attempts[level.level_id] + 1
            request = PlaceOrderRequest(
                category="linear",
                symbol="ETHUSDT",
                side="Sell",
                order_type="Market",
                quantity=quantized.quantity,
                order_link_id=self._order_link_id_factory(level.level_id, attempt),
                time_in_force="IOC",
                reduce_only=False,
                position_idx=0,
            )
            self._attempts[level.level_id] = attempt
            self._submitted_order_ids[level.level_id] = request.order_link_id
            snapshot = await self._entry_service.submit_entry(request)
            entries.append(LevelEntrySubmission(level.level_id, request, snapshot))
            effective_risk_state = replace(
                effective_risk_state,
                current_perp_quantity=(
                    effective_risk_state.current_perp_quantity + quantized.quantity
                ),
                current_perp_notional=(
                    effective_risk_state.current_perp_notional + quantized.notional
                ),
                active_levels=effective_risk_state.active_levels + 1,
                order_requests_last_minute=(
                    effective_risk_state.order_requests_last_minute + 1
                ),
            )
        return MultiLevelTriggerResult(tuple(entries), tuple(blocked))

    async def reconcile_aggregate_position(
        self,
        positions: tuple[ExchangePosition, ...],
    ) -> bool:
        snapshots: list[EntryExecutionSnapshot] = []
        for level_id in sorted(self._submitted_order_ids):
            order_link_id = self._submitted_order_ids[level_id]
            snapshot = await self._store.load_entry_snapshot(order_link_id)
            if snapshot is None:
                raise RuntimeError(f"entry snapshot disappeared for level {level_id}")
            snapshots.append(snapshot)
        expected = sum(
            (snapshot.filled_quantity for snapshot in snapshots),
            ZERO,
        )
        actual = tuple(
            position
            for position in positions
            if position.category == "linear"
            and position.symbol == "ETHUSDT"
            and position.quantity > ZERO
        )
        matches = (
            not actual
            if expected == ZERO
            else (
                len(actual) == 1
                and actual[0].side == "Sell"
                and actual[0].quantity == expected
            )
        )
        if matches:
            return True
        for snapshot in snapshots:
            if snapshot.state is LiveExecutionState.RECONCILING:
                continue
            reconciling = transition_entry_snapshot(
                snapshot,
                LiveExecutionState.RECONCILING,
                updated_at=positions[0].updated_at if positions else snapshot.updated_at,
            )
            await self._store.transition_entry_snapshot(
                snapshot.version,
                reconciling,
            )
        return False

    def _preflight_reasons(self, level: HedgeLevel) -> tuple[str, ...]:
        if level.level_id in self._submitted_order_ids:
            return ("level entry is already submitted",)
        if level.state is not LevelState.READY:
            return ("virtual level is not READY",)
        if self._option_position.state is not OptionPositionState.OPEN:
            return ("option spread is not OPEN",)
        return ()

    def _reset_segment(self, event: TriggerPriceEvent) -> None:
        self._connection_generation = event.connection_generation
        self._previous_price = event.observed_price
        for level in self._levels:
            self._armed[level.level_id] = (
                level.level_id not in self._submitted_order_ids
                and event.observed_price >= level.entry_price
            )
