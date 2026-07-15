"""M9 ordered baseline levels, aggregate position, independent exits, restart."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from eth_credit_hedge.application.multi_level_execution import (
    MultiLevelCoordinator,
)
from eth_credit_hedge.application.one_level_entry import OneLevelEntryService
from eth_credit_hedge.application.protective_exits import ProtectiveExitService
from eth_credit_hedge.core.virtual_levels import HedgeLevel
from eth_credit_hedge.domain.execution import (
    ExchangeOrder,
    ExchangePosition,
    ExecutionUpdate,
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


def entry_id(level_id: int) -> str:
    return f"ECH-01-C0001-L{level_id:02d}-ENTRY-A01-A00{level_id}"


def stop_id(level_id: int) -> str:
    return f"ECH-01-C0001-L{level_id:02d}-STOP-A01-B00{level_id}"


def tp_id(level_id: int) -> str:
    return f"ECH-01-C0001-L{level_id:02d}-TP-A01-C00{level_id}"


def option_position() -> PutCreditSpreadPosition:
    expiry = datetime(2026, 7, 31, 8, tzinfo=timezone.utc)
    short = OptionContract(
        symbol="ETH-31JUL26-3100-P-USDT",
        base_coin="ETH",
        quote_coin="USDT",
        settle_coin="USDT",
        option_type="Put",
        strike=Decimal("3100"),
        expiry_time_utc=expiry,
        contract_multiplier=Decimal("1"),
    )
    long = replace(
        short,
        symbol="ETH-31JUL26-2800-P-USDT",
        strike=Decimal("2800"),
    )
    return PutCreditSpreadPosition(
        short_put=OptionLegPosition(
            contract=short,
            side="Short",
            requested_quantity=Decimal("0.010"),
            filled_quantity=Decimal("0.010"),
            average_entry_price=Decimal("90"),
            fees_paid=Decimal("0"),
        ),
        long_put=OptionLegPosition(
            contract=long,
            side="Long",
            requested_quantity=Decimal("0.010"),
            filled_quantity=Decimal("0.010"),
            average_entry_price=Decimal("30"),
            fees_paid=Decimal("0"),
        ),
        state=OptionPositionState.OPEN,
    )


def levels() -> tuple[HedgeLevel, ...]:
    return tuple(
        HedgeLevel(
            level_id=level_id,
            entry_price=entry,
            tp_price=entry - Decimal("100"),
            stop_price=entry * Decimal("1.0015"),
            option_budget=Decimal("1"),
        )
        for level_id, entry in enumerate(
            (Decimal("3100"), Decimal("3000"), Decimal("2900")),
            start=1,
        )
    )


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


def risk_limits() -> RiskLimits:
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
        maximum_active_levels=3,
        maximum_order_requests_per_minute=10,
        maximum_reconciliation_failures=3,
    )


def risk_state() -> RiskState:
    return RiskState(
        current_perp_quantity=Decimal("0"),
        current_perp_notional=Decimal("0"),
        post_trade_margin_usage=Decimal("0.1"),
        post_trade_liquidation_distance=Decimal("0.5"),
        confirmed_recovery_debt=Decimal("7"),
        realized_cycle_loss=Decimal("0"),
        daily_realized_loss=Decimal("0"),
        entries_for_level=0,
        active_levels=0,
        order_requests_last_minute=0,
        consecutive_reconciliation_failures=0,
        market_data_fresh=True,
        reconciliation_succeeded=True,
    )


def health() -> MarketDataHealthResult:
    return MarketDataHealthResult(
        trading_allowed=True,
        reasons=(),
        event_type=MarketDataEventType.MARKET_DATA_RECOVERED,
    )


def trigger(price: str) -> TriggerPriceEvent:
    return TriggerPriceEvent(
        symbol="ETHUSDT",
        source=TriggerPriceSource.LAST_TRADE,
        observed_price=Decimal(price),
        observed_timestamp=NOW,
        connection_generation=1,
    )


class FakeExchange:
    def __init__(self, store: SqliteExecutionStore) -> None:
        self.store = store
        self.requests: list[PlaceOrderRequest] = []

    async def place_order(self, request: PlaceOrderRequest) -> OrderRequestAck:
        assert await self.store.load_order_intent(request.order_link_id) == request
        self.requests.append(request)
        return OrderRequestAck(
            request_kind=OrderRequestKind.PLACE,
            order_id=f"order-{request.order_link_id}",
            order_link_id=request.order_link_id,
            acknowledged_at=NOW,
        )

    async def get_order_by_link_id(
        self,
        category: str,
        symbol: str,
        order_link_id: str,
    ) -> ExchangeOrder | None:
        request = next(
            (item for item in self.requests if item.order_link_id == order_link_id),
            None,
        )
        if request is None:
            return None
        return ExchangeOrder(
            category=request.category,
            order_id=f"order-{request.order_link_id}",
            order_link_id=request.order_link_id,
            symbol=request.symbol,
            status="Untriggered" if request.trigger_price is not None else "New",
            side=request.side,
            order_type=request.order_type,
            price=request.price,
            quantity=request.quantity,
            cumulative_filled_quantity=Decimal("0"),
            average_price=None,
            reduce_only=request.reduce_only,
            created_at=NOW,
            updated_at=NOW,
            trigger_price=request.trigger_price,
            trigger_by=request.trigger_by,
            trigger_direction=request.trigger_direction,
            time_in_force=request.time_in_force,
            position_idx=request.position_idx,
            close_on_trigger=request.close_on_trigger,
        )


class EmptyAccount:
    async def get_positions(
        self,
        category: str,
        symbol: str | None = None,
    ) -> tuple[ExchangePosition, ...]:
        return ()


async def no_wait(_: float) -> None:
    return None


def fill(level_id: int, price: str) -> ExecutionUpdate:
    return ExecutionUpdate(
        execution_id=f"entry-execution-{level_id}",
        order_id=f"order-{entry_id(level_id)}",
        order_link_id=entry_id(level_id),
        symbol="ETHUSDT",
        side="Sell",
        price=Decimal(price),
        quantity=Decimal("0.010"),
        fee=Decimal("0.001"),
        is_maker=False,
        executed_at=NOW + timedelta(seconds=level_id),
    )


def test_large_downward_segment_submits_ordered_baseline_only_entries(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        store = SqliteExecutionStore(tmp_path / "execution.sqlite3")
        await store.initialize()
        exchange = FakeExchange(store)
        entries = OneLevelEntryService(
            trading=exchange,
            store=store,
            clock=lambda: NOW,
        )
        coordinator = MultiLevelCoordinator(
            entry_service=entries,
            store=store,
            option_position=option_position(),
            levels=levels(),
            instrument=instrument(),
            risk_engine=RiskEngine(),
            risk_limits=risk_limits(),
            order_link_id_factory=lambda level_id, attempt: entry_id(level_id),
        )

        first = await coordinator.on_trigger(trigger("3200"), health(), risk_state())
        crossed = await coordinator.on_trigger(trigger("2850"), health(), risk_state())

        assert first.entries == ()
        assert [entry.level_id for entry in crossed.entries] == [1, 2, 3]
        assert [request.quantity for request in exchange.requests] == [
            Decimal("0.010"),
            Decimal("0.010"),
            Decimal("0.010"),
        ]
        assert all(request.order_type == "Market" for request in exchange.requests)
        assert all(request.price is None for request in exchange.requests)
        assert all(request.side == "Sell" for request in exchange.requests)
        assert crossed.blocked == ()

    asyncio.run(run())


def test_three_levels_fill_protect_exit_independently_and_restart(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        database = tmp_path / "execution.sqlite3"
        store = SqliteExecutionStore(database)
        await store.initialize()
        exchange = FakeExchange(store)
        entries = OneLevelEntryService(
            trading=exchange,
            store=store,
            clock=lambda: NOW,
        )
        coordinator = MultiLevelCoordinator(
            entry_service=entries,
            store=store,
            option_position=option_position(),
            levels=levels(),
            instrument=instrument(),
            risk_engine=RiskEngine(),
            risk_limits=risk_limits(),
            order_link_id_factory=lambda level_id, attempt: entry_id(level_id),
        )
        await coordinator.on_trigger(trigger("3200"), health(), risk_state())
        await coordinator.on_trigger(trigger("2850"), health(), risk_state())
        for level_id, price in enumerate(("3100", "3000", "2900"), start=1):
            await entries.apply_execution(
                fill(level_id, price),
                received_at=NOW + timedelta(seconds=level_id),
                payload_hash=str(level_id) * 64,
            )

        aggregate_position = ExchangePosition(
            category="linear",
            symbol="ETHUSDT",
            side="Sell",
            quantity=Decimal("0.030"),
            average_price=Decimal("3000"),
            mark_price=Decimal("2990"),
            unrealized_pnl=Decimal("0"),
            updated_at=NOW,
        )
        assert await coordinator.reconcile_aggregate_position((aggregate_position,))

        protection = ProtectiveExitService(
            trading=exchange,
            account=EmptyAccount(),
            store=store,
            clock=lambda: NOW,
            sleeper=no_wait,
        )
        for item in levels():
            protected = await protection.install_stop(
                entry_id(item.level_id),
                instrument(),
                stop_id(item.level_id),
                stop_rate=Decimal("0.0015"),
            )
            await protection.install_take_profit(
                protected,
                instrument(),
                tp_id(item.level_id),
                desired_price=item.tp_price,
            )

        level_two_exit = ExecutionUpdate(
            execution_id="level-2-tp",
            order_id=f"order-{tp_id(2)}",
            order_link_id=tp_id(2),
            symbol="ETHUSDT",
            side="Buy",
            price=Decimal("2900"),
            quantity=Decimal("0.010"),
            fee=Decimal("0.001"),
            is_maker=False,
            executed_at=NOW + timedelta(seconds=10),
        )
        closed_two = await protection.apply_exit_execution(
            level_two_exit,
            received_at=NOW + timedelta(seconds=10),
            payload_hash="f" * 64,
        )

        assert closed_two.open_quantity == Decimal("0")
        snapshots = await store.load_all_protection_snapshots()
        assert [snapshot.open_quantity for snapshot in snapshots] == [
            Decimal("0.010"),
            Decimal("0"),
            Decimal("0.010"),
        ]
        restarted = SqliteExecutionStore(database)
        await restarted.initialize()
        assert await restarted.load_all_entry_snapshots() == (
            await store.load_all_entry_snapshots()
        )
        assert await restarted.load_all_protection_snapshots() == snapshots

    asyncio.run(run())


def test_aggregate_position_mismatch_marks_every_active_entry_reconciling(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        store = SqliteExecutionStore(tmp_path / "execution.sqlite3")
        await store.initialize()
        exchange = FakeExchange(store)
        entries = OneLevelEntryService(
            trading=exchange,
            store=store,
            clock=lambda: NOW,
        )
        coordinator = MultiLevelCoordinator(
            entry_service=entries,
            store=store,
            option_position=option_position(),
            levels=levels(),
            instrument=instrument(),
            risk_engine=RiskEngine(),
            risk_limits=risk_limits(),
            order_link_id_factory=lambda level_id, attempt: entry_id(level_id),
        )
        await coordinator.on_trigger(trigger("3200"), health(), risk_state())
        await coordinator.on_trigger(trigger("2850"), health(), risk_state())
        for level_id, price in enumerate(("3100", "3000", "2900"), start=1):
            await entries.apply_execution(
                fill(level_id, price),
                received_at=NOW,
                payload_hash=str(level_id) * 64,
            )
        mismatch = ExchangePosition(
            category="linear",
            symbol="ETHUSDT",
            side="Sell",
            quantity=Decimal("0.029"),
            average_price=Decimal("3000"),
            mark_price=Decimal("2990"),
            unrealized_pnl=Decimal("0"),
            updated_at=NOW,
        )

        assert not await coordinator.reconcile_aggregate_position((mismatch,))
        assert all(
            snapshot.state.value == "RECONCILING"
            for snapshot in await store.load_all_entry_snapshots()
        )

    asyncio.run(run())
