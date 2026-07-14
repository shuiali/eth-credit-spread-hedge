"""One fresh crossing through risk, quantization, and durable submission."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from eth_credit_hedge.application.one_level_coordinator import (
    OneLevelCoordinator,
)
from eth_credit_hedge.application.one_level_entry import OneLevelEntryService
from eth_credit_hedge.core.virtual_levels import HedgeLevel
from eth_credit_hedge.domain.execution import (
    ExchangeOrder,
    OrderRequestAck,
    OrderRequestKind,
    PlaceOrderRequest,
)
from eth_credit_hedge.domain.instruments import (
    InstrumentSpec,
    LotSizeFilter,
    OptionContract,
    PriceFilter,
)
from eth_credit_hedge.domain.market_data import (
    MarketDataEventType,
    MarketDataHealthResult,
    TriggerPriceEvent,
    TriggerPriceSource,
)
from eth_credit_hedge.domain.option_position import (
    OptionLegPosition,
    OptionPositionState,
    PutCreditSpreadPosition,
)
from eth_credit_hedge.domain.risk import RiskEngine, RiskLimits, RiskState
from eth_credit_hedge.infrastructure.persistence.sqlite_execution_store import (
    SqliteExecutionStore,
)


NOW = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
ORDER_LINK_ID = "ECH-01-C0001-L01-ENTRY-A01-9F3C"


class RecordingTradingAdapter:
    def __init__(self, store: SqliteExecutionStore) -> None:
        self.store = store
        self.requests: list[PlaceOrderRequest] = []

    async def place_order(self, request: PlaceOrderRequest) -> OrderRequestAck:
        assert await self.store.load_order_intent(request.order_link_id) == request
        self.requests.append(request)
        return OrderRequestAck(
            request_kind=OrderRequestKind.PLACE,
            order_id="exchange-order-1",
            order_link_id=request.order_link_id,
            acknowledged_at=NOW,
        )

    async def get_order_by_link_id(
        self,
        category: str,
        symbol: str,
        order_link_id: str,
    ) -> ExchangeOrder | None:
        return None


def option_position(state: OptionPositionState = OptionPositionState.OPEN) -> PutCreditSpreadPosition:
    expiry = datetime(2026, 7, 31, 8, tzinfo=timezone.utc)
    short_contract = OptionContract(
        symbol="ETH-31JUL26-3000-P-USDT",
        base_coin="ETH",
        quote_coin="USDT",
        settle_coin="USDT",
        option_type="Put",
        strike=Decimal("3000"),
        expiry_time_utc=expiry,
        contract_multiplier=Decimal("1"),
    )
    long_contract = replace(
        short_contract,
        symbol="ETH-31JUL26-2900-P-USDT",
        strike=Decimal("2900"),
    )
    filled = Decimal("0.010") if state is OptionPositionState.OPEN else Decimal("0")
    short = OptionLegPosition(
        contract=short_contract,
        side="Short",
        requested_quantity=Decimal("0.010"),
        filled_quantity=filled,
        average_entry_price=Decimal("60") if filled else Decimal("0"),
        fees_paid=Decimal("0") if filled else Decimal("0"),
    )
    long = OptionLegPosition(
        contract=long_contract,
        side="Long",
        requested_quantity=Decimal("0.010"),
        filled_quantity=filled,
        average_entry_price=Decimal("30") if filled else Decimal("0"),
        fees_paid=Decimal("0") if filled else Decimal("0"),
    )
    return PutCreditSpreadPosition(short_put=short, long_put=long, state=state)


def instrument() -> InstrumentSpec:
    return InstrumentSpec(
        symbol="ETHUSDT",
        category="linear",
        status="Trading",
        base_coin="ETH",
        quote_coin="USDT",
        settle_coin="USDT",
        price_filter=PriceFilter(
            tick_size=Decimal("0.1"),
            min_price=Decimal("10"),
            max_price=Decimal("1000000"),
        ),
        lot_size_filter=LotSizeFilter(
            qty_step=Decimal("0.001"),
            min_order_qty=Decimal("0.001"),
            max_order_qty=Decimal("100"),
            max_market_order_qty=Decimal("50"),
            min_notional=Decimal("5"),
        ),
        contract_multiplier=Decimal("1"),
        delivery_time_utc=None,
    )


def level() -> HedgeLevel:
    return HedgeLevel(
        level_id=1,
        entry_price=Decimal("3000"),
        tp_price=Decimal("2900"),
        stop_price=Decimal("3004.5"),
        option_budget=Decimal("1"),
    )


def limits() -> RiskLimits:
    return RiskLimits(
        maximum_perp_quantity=Decimal("0.1"),
        maximum_perp_notional=Decimal("1000"),
        maximum_margin_usage=Decimal("0.5"),
        minimum_liquidation_distance=Decimal("0.1"),
        maximum_recovery_debt=Decimal("10"),
        maximum_projected_stop_loss=Decimal("1"),
        maximum_realized_cycle_loss=Decimal("10"),
        maximum_daily_realized_loss=Decimal("20"),
        maximum_entries_per_level=2,
        maximum_active_levels=1,
        maximum_order_requests_per_minute=10,
        maximum_reconciliation_failures=3,
    )


def risk_state() -> RiskState:
    return RiskState(
        current_perp_quantity=Decimal("0"),
        current_perp_notional=Decimal("0"),
        post_trade_margin_usage=Decimal("0.1"),
        post_trade_liquidation_distance=Decimal("0.5"),
        confirmed_recovery_debt=Decimal("0"),
        realized_cycle_loss=Decimal("0"),
        daily_realized_loss=Decimal("0"),
        entries_for_level=0,
        active_levels=0,
        order_requests_last_minute=0,
        consecutive_reconciliation_failures=0,
        market_data_fresh=True,
        reconciliation_succeeded=True,
    )


def health(allowed: bool = True) -> MarketDataHealthResult:
    return MarketDataHealthResult(
        trading_allowed=allowed,
        reasons=() if allowed else ("trigger price is stale",),
        event_type=(
            MarketDataEventType.MARKET_DATA_RECOVERED
            if allowed
            else MarketDataEventType.MARKET_DATA_STALE
        ),
    )


def trigger(price: str, generation: int = 1) -> TriggerPriceEvent:
    return TriggerPriceEvent(
        symbol="ETHUSDT",
        source=TriggerPriceSource.LAST_TRADE,
        observed_price=Decimal(price),
        observed_timestamp=NOW,
        connection_generation=generation,
    )


def make_coordinator(
    tmp_path: Path,
    *,
    position_state: OptionPositionState = OptionPositionState.OPEN,
) -> tuple[OneLevelCoordinator, RecordingTradingAdapter]:
    store = SqliteExecutionStore(tmp_path / "execution.sqlite3")
    asyncio.run(store.initialize())
    trading = RecordingTradingAdapter(store)
    entry = OneLevelEntryService(trading=trading, store=store, clock=lambda: NOW)
    coordinator = OneLevelCoordinator(
        entry_service=entry,
        option_position=option_position(position_state),
        level=level(),
        instrument=instrument(),
        risk_engine=RiskEngine(),
        risk_limits=limits(),
        order_link_id_factory=lambda: ORDER_LINK_ID,
    )
    return coordinator, trading


def test_one_new_downward_crossing_submits_one_quantized_entry(tmp_path: Path) -> None:
    coordinator, trading = make_coordinator(tmp_path)

    above = asyncio.run(
        coordinator.on_trigger(trigger("3001"), health(), risk_state())
    )
    crossing = asyncio.run(
        coordinator.on_trigger(trigger("2999"), health(), risk_state())
    )
    repeated = asyncio.run(
        coordinator.on_trigger(trigger("2998"), health(), risk_state())
    )

    assert not above.triggered
    assert crossing.triggered
    assert crossing.snapshot is not None
    assert not repeated.triggered
    assert len(trading.requests) == 1
    assert trading.requests[0] == PlaceOrderRequest(
        category="linear",
        symbol="ETHUSDT",
        side="Sell",
        order_type="Market",
        quantity=Decimal("0.010"),
        order_link_id=ORDER_LINK_ID,
        time_in_force="IOC",
        reduce_only=False,
        position_idx=0,
    )


def test_stale_crossing_is_ignored_but_next_fresh_event_can_trigger(tmp_path: Path) -> None:
    coordinator, trading = make_coordinator(tmp_path)
    asyncio.run(coordinator.on_trigger(trigger("3001"), health(), risk_state()))

    stale = asyncio.run(
        coordinator.on_trigger(trigger("2999"), health(False), risk_state())
    )
    fresh = asyncio.run(
        coordinator.on_trigger(trigger("2998"), health(), risk_state())
    )

    assert not stale.triggered
    assert stale.reasons == ("trigger price is stale",)
    assert fresh.triggered
    assert len(trading.requests) == 1


def test_risk_veto_and_unconfirmed_option_position_block_entry(tmp_path: Path) -> None:
    coordinator, trading = make_coordinator(tmp_path)
    asyncio.run(coordinator.on_trigger(trigger("3001"), health(), risk_state()))
    veto = asyncio.run(
        coordinator.on_trigger(
            trigger("2999"),
            health(),
            replace(risk_state(), post_trade_margin_usage=Decimal("0.9")),
        )
    )

    assert not veto.triggered
    assert "maximum margin usage exceeded" in veto.reasons
    assert trading.requests == []

    unconfirmed, unconfirmed_trading = make_coordinator(
        tmp_path / "planned",
        position_state=OptionPositionState.PLANNED,
    )
    asyncio.run(unconfirmed.on_trigger(trigger("3001"), health(), risk_state()))
    blocked = asyncio.run(
        unconfirmed.on_trigger(trigger("2999"), health(), risk_state())
    )
    assert not blocked.triggered
    assert blocked.reasons == ("option spread is not OPEN",)
    assert unconfirmed_trading.requests == []


def test_reconnect_does_not_infer_a_crossing_across_missing_data(tmp_path: Path) -> None:
    coordinator, trading = make_coordinator(tmp_path)
    asyncio.run(coordinator.on_trigger(trigger("3001", 1), health(), risk_state()))

    reset = asyncio.run(
        coordinator.on_trigger(trigger("2999", 2), health(), risk_state())
    )

    assert not reset.triggered
    assert reset.reasons == ("connection generation changed",)
    assert trading.requests == []
