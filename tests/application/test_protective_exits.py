"""Exchange-hosted stop, TP, and cancel/fill-race acceptance tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from eth_credit_hedge.application.protective_exits import (
    ProtectionNotConfirmedError,
    ProtectiveExitService,
)
from eth_credit_hedge.domain.execution import (
    CancelOrderRequest,
    ExchangeOrder,
    ExchangePosition,
    ExecutionUpdate,
    LiveExecutionState,
    OrderRequestAck,
    OrderRequestKind,
    PlaceOrderRequest,
    UncertainOrderOutcomeError,
)
from eth_credit_hedge.domain.instrument_rules import PriceQuantizationPolicy
from eth_credit_hedge.domain.instruments import (
    InstrumentSpec,
    LotSizeFilter,
    PriceFilter,
)
from eth_credit_hedge.domain.live_execution import (
    EntryExecutionSnapshot,
    apply_entry_execution,
    transition_entry_snapshot,
)
from eth_credit_hedge.infrastructure.persistence.sqlite_execution_store import (
    SqliteExecutionStore,
)


NOW = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
ENTRY_ID = "ECH-01-C0001-L01-ENTRY-A01-9F3C"
STOP_ID = "ECH-01-C0001-L01-STOP-A01-9F3C"
TP_ID = "ECH-01-C0001-L01-TP-A01-9F3C"
REPLACEMENT_STOP_ID = "ECH-01-C0001-L01-STOP-A02-ABCD"


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


def entry_request() -> PlaceOrderRequest:
    return PlaceOrderRequest(
        category="linear",
        symbol="ETHUSDT",
        side="Sell",
        order_type="Limit",
        quantity=Decimal("0.010"),
        order_link_id=ENTRY_ID,
        price=Decimal("3000"),
        time_in_force="IOC",
    )


def exit_execution(
    *,
    execution_id: str,
    order_link_id: str,
    quantity: str,
    price: str,
    fee: str,
) -> ExecutionUpdate:
    role = "stop" if order_link_id == STOP_ID else "tp"
    return ExecutionUpdate(
        execution_id=execution_id,
        order_id=f"{role}-exchange-order",
        order_link_id=order_link_id,
        symbol="ETHUSDT",
        side="Buy",
        price=Decimal(price),
        quantity=Decimal(quantity),
        fee=Decimal(fee),
        is_maker=False,
        executed_at=NOW + timedelta(seconds=5),
    )


async def seed_filled_entry(store: SqliteExecutionStore) -> EntryExecutionSnapshot:
    request = entry_request()
    initial = EntryExecutionSnapshot.for_intent(request, NOW)
    await store.persist_entry_intent(request, initial, NOW)
    submitted = transition_entry_snapshot(
        initial,
        LiveExecutionState.ENTRY_SUBMITTED,
        updated_at=NOW,
    )
    await store.transition_entry_snapshot(initial.version, submitted)
    ack = OrderRequestAck(
        request_kind=OrderRequestKind.PLACE,
        order_id="entry-exchange-order",
        order_link_id=ENTRY_ID,
        acknowledged_at=NOW,
    )
    acknowledged = transition_entry_snapshot(
        submitted,
        LiveExecutionState.ENTRY_ACKNOWLEDGED,
        entry_order_id=ack.order_id,
        updated_at=NOW,
    )
    await store.record_acknowledgement_and_snapshot(
        submitted.version,
        ack,
        acknowledged,
    )
    execution = ExecutionUpdate(
        execution_id="entry-execution",
        order_id=ack.order_id,
        order_link_id=ENTRY_ID,
        symbol="ETHUSDT",
        side="Sell",
        price=Decimal("3000"),
        quantity=Decimal("0.010"),
        fee=Decimal("0.003"),
        is_maker=False,
        executed_at=NOW + timedelta(seconds=1),
    )
    filled = apply_entry_execution(
        acknowledged,
        execution,
        updated_at=NOW + timedelta(seconds=1),
    )
    await store.record_execution_and_snapshot(
        acknowledged.version,
        execution,
        NOW + timedelta(seconds=1),
        "a" * 64,
        filled,
    )
    return filled


def exchange_order(request: PlaceOrderRequest) -> ExchangeOrder:
    return ExchangeOrder(
        category=request.category,
        order_id=(
            "stop-exchange-order"
            if "-STOP-" in request.order_link_id
            else "tp-exchange-order"
        ),
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


class FakeTradingAdapter:
    def __init__(self, store: SqliteExecutionStore) -> None:
        self.store = store
        self.requests: list[PlaceOrderRequest] = []
        self.cancel_requests: list[CancelOrderRequest] = []
        self.visibility: dict[str, list[ExchangeOrder | None]] = {}
        self.open_order_responses: list[tuple[ExchangeOrder, ...]] = [()]
        self.place_error: Exception | None = None
        self.uncertain_order_link_ids: set[str] = set()

    async def place_order(self, request: PlaceOrderRequest) -> OrderRequestAck:
        assert await self.store.load_order_intent(request.order_link_id) == request
        self.requests.append(request)
        if self.place_error is not None:
            raise self.place_error
        if request.order_link_id in self.uncertain_order_link_ids:
            raise UncertainOrderOutcomeError(
                order_link_id=request.order_link_id,
                operation="place order",
            )
        return OrderRequestAck(
            request_kind=OrderRequestKind.PLACE,
            order_id=exchange_order(request).order_id,
            order_link_id=request.order_link_id,
            acknowledged_at=NOW,
        )

    async def get_order_by_link_id(
        self,
        category: str,
        symbol: str,
        order_link_id: str,
    ) -> ExchangeOrder | None:
        responses = self.visibility.get(order_link_id, [])
        if not responses:
            return None
        if len(responses) == 1:
            return responses[0]
        return responses.pop(0)

    async def get_open_orders(
        self,
        category: str,
        symbol: str | None = None,
    ) -> tuple[ExchangeOrder, ...]:
        if len(self.open_order_responses) == 1:
            return self.open_order_responses[0]
        return self.open_order_responses.pop(0)

    async def cancel_order(self, request: CancelOrderRequest) -> OrderRequestAck:
        self.cancel_requests.append(request)
        return OrderRequestAck(
            request_kind=OrderRequestKind.CANCEL,
            order_id=f"cancel-{request.order_link_id}",
            order_link_id=request.order_link_id,
            acknowledged_at=NOW,
        )


class FakeAccountAdapter:
    def __init__(self, positions: tuple[ExchangePosition, ...]) -> None:
        self.positions = positions

    async def get_positions(
        self,
        category: str,
        symbol: str | None = None,
    ) -> tuple[ExchangePosition, ...]:
        return self.positions


async def no_wait(_: float) -> None:
    return None


def run_service_test(
    tmp_path: Path,
    exercise: Callable[
        [SqliteExecutionStore, FakeTradingAdapter, ProtectiveExitService],
        object,
    ],
) -> object:
    async def run() -> object:
        store = SqliteExecutionStore(tmp_path / "execution.sqlite3")
        await store.initialize()
        await seed_filled_entry(store)
        trading = FakeTradingAdapter(store)
        service = ProtectiveExitService(
            trading=trading,
            account=FakeAccountAdapter(()),
            store=store,
            clock=lambda: NOW,
            sleeper=no_wait,
            visibility_attempts=3,
        )
        result = exercise(store, trading, service)
        if hasattr(result, "__await__"):
            return await result  # type: ignore[misc,no-any-return]
        return result

    return asyncio.run(run())


def test_stop_uses_actual_entry_and_is_confirmed_before_protected(
    tmp_path: Path,
) -> None:
    async def exercise(
        store: SqliteExecutionStore,
        trading: FakeTradingAdapter,
        service: ProtectiveExitService,
    ) -> object:
        expected = PlaceOrderRequest(
            category="linear",
            symbol="ETHUSDT",
            side="Buy",
            order_type="Market",
            quantity=Decimal("0.010"),
            order_link_id=STOP_ID,
            reduce_only=True,
            trigger_price=Decimal("3004.5"),
            trigger_direction=1,
            trigger_by="LastPrice",
            close_on_trigger=True,
            position_idx=0,
        )
        trading.visibility[STOP_ID] = [None, exchange_order(expected)]

        snapshot = await service.install_stop(
            ENTRY_ID,
            instrument(),
            STOP_ID,
            stop_rate=Decimal("0.0015"),
        )

        assert trading.requests == [expected]
        assert snapshot.state is LiveExecutionState.ACTIVE_PROTECTED
        assert snapshot.stop_order_id == "stop-exchange-order"
        assert snapshot.stop_trigger_price == Decimal("3004.5")
        return snapshot

    run_service_test(tmp_path, exercise)


def test_missing_stop_visibility_blocks_and_reconciles(tmp_path: Path) -> None:
    async def exercise(
        store: SqliteExecutionStore,
        trading: FakeTradingAdapter,
        service: ProtectiveExitService,
    ) -> object:
        with pytest.raises(ProtectionNotConfirmedError):
            await service.install_stop(
                ENTRY_ID,
                instrument(),
                STOP_ID,
                stop_rate=Decimal("0.0015"),
            )
        snapshot = await store.load_protection_snapshot(ENTRY_ID)
        assert snapshot is not None
        assert snapshot.state is LiveExecutionState.RECONCILING
        return snapshot

    run_service_test(tmp_path, exercise)


def test_rejected_stop_request_blocks_and_reconciles(tmp_path: Path) -> None:
    async def exercise(
        store: SqliteExecutionStore,
        trading: FakeTradingAdapter,
        service: ProtectiveExitService,
    ) -> object:
        trading.place_error = RuntimeError("insufficient margin")
        with pytest.raises(RuntimeError, match="insufficient margin"):
            await service.install_stop(
                ENTRY_ID,
                instrument(),
                STOP_ID,
                stop_rate=Decimal("0.0015"),
            )
        snapshot = await store.load_protection_snapshot(ENTRY_ID)
        assert snapshot is not None
        assert snapshot.state is LiveExecutionState.RECONCILING
        return snapshot

    run_service_test(tmp_path, exercise)


def test_uncertain_stop_that_exists_is_confirmed_without_retry(tmp_path: Path) -> None:
    async def exercise(
        store: SqliteExecutionStore,
        trading: FakeTradingAdapter,
        service: ProtectiveExitService,
    ) -> object:
        expected = PlaceOrderRequest(
            category="linear",
            symbol="ETHUSDT",
            side="Buy",
            order_type="Market",
            quantity=Decimal("0.010"),
            order_link_id=STOP_ID,
            reduce_only=True,
            trigger_price=Decimal("3004.5"),
            trigger_direction=1,
            trigger_by="LastPrice",
            close_on_trigger=True,
        )
        trading.uncertain_order_link_ids.add(STOP_ID)
        trading.visibility[STOP_ID] = [exchange_order(expected)]

        snapshot = await service.install_stop(
            ENTRY_ID,
            instrument(),
            STOP_ID,
            stop_rate=Decimal("0.0015"),
        )

        assert snapshot.state is LiveExecutionState.ACTIVE_PROTECTED
        assert len(trading.requests) == 1
        return snapshot

    run_service_test(tmp_path, exercise)


def test_tp_is_reduce_only_for_confirmed_open_quantity(tmp_path: Path) -> None:
    async def exercise(
        store: SqliteExecutionStore,
        trading: FakeTradingAdapter,
        service: ProtectiveExitService,
    ) -> object:
        stop_request = PlaceOrderRequest(
            category="linear",
            symbol="ETHUSDT",
            side="Buy",
            order_type="Market",
            quantity=Decimal("0.010"),
            order_link_id=STOP_ID,
            reduce_only=True,
            trigger_price=Decimal("3004.5"),
            trigger_direction=1,
            trigger_by="LastPrice",
            close_on_trigger=True,
        )
        trading.visibility[STOP_ID] = [exchange_order(stop_request)]
        protected = await service.install_stop(
            ENTRY_ID,
            instrument(),
            STOP_ID,
            stop_rate=Decimal("0.0015"),
        )
        tp_request = PlaceOrderRequest(
            category="linear",
            symbol="ETHUSDT",
            side="Buy",
            order_type="Limit",
            quantity=Decimal("0.010"),
            order_link_id=TP_ID,
            price=Decimal("2900"),
            time_in_force="GTC",
            reduce_only=True,
        )
        trading.visibility[TP_ID] = [exchange_order(tp_request)]

        with_tp = await service.install_take_profit(
            protected,
            instrument(),
            TP_ID,
            desired_price=Decimal("2900.04"),
            price_policy=PriceQuantizationPolicy.PASSIVE,
        )

        assert trading.requests[-1] == tp_request
        assert with_tp.tp_order_id == "tp-exchange-order"
        return with_tp

    run_service_test(tmp_path, exercise)


def test_partial_tp_then_stop_is_idempotent_and_uses_actual_debt(
    tmp_path: Path,
) -> None:
    async def exercise(
        store: SqliteExecutionStore,
        trading: FakeTradingAdapter,
        service: ProtectiveExitService,
    ) -> object:
        stop_request = PlaceOrderRequest(
            category="linear",
            symbol="ETHUSDT",
            side="Buy",
            order_type="Market",
            quantity=Decimal("0.010"),
            order_link_id=STOP_ID,
            reduce_only=True,
            trigger_price=Decimal("3004.5"),
            trigger_direction=1,
            trigger_by="LastPrice",
            close_on_trigger=True,
        )
        tp_request = PlaceOrderRequest(
            category="linear",
            symbol="ETHUSDT",
            side="Buy",
            order_type="Limit",
            quantity=Decimal("0.010"),
            order_link_id=TP_ID,
            price=Decimal("2900"),
            reduce_only=True,
        )
        trading.visibility[STOP_ID] = [exchange_order(stop_request)]
        protected = await service.install_stop(
            ENTRY_ID, instrument(), STOP_ID, stop_rate=Decimal("0.0015")
        )
        trading.visibility[TP_ID] = [exchange_order(tp_request)]
        await service.install_take_profit(
            protected,
            instrument(),
            TP_ID,
            desired_price=Decimal("2900"),
        )

        partial = await service.apply_exit_execution(
            exit_execution(
                execution_id="tp-execution",
                order_link_id=TP_ID,
                quantity="0.004",
                price="2900",
                fee="0.001",
            ),
            received_at=NOW + timedelta(seconds=6),
            payload_hash="b" * 64,
        )
        duplicate = await service.apply_exit_execution(
            exit_execution(
                execution_id="tp-execution",
                order_link_id=TP_ID,
                quantity="0.004",
                price="2900",
                fee="0.001",
            ),
            received_at=NOW + timedelta(seconds=7),
            payload_hash="b" * 64,
        )
        stopped = await service.apply_exit_execution(
            exit_execution(
                execution_id="stop-execution",
                order_link_id=STOP_ID,
                quantity="0.006",
                price="3005",
                fee="0.002",
            ),
            received_at=NOW + timedelta(seconds=8),
            payload_hash="c" * 64,
        )

        assert partial.state is LiveExecutionState.EXIT_PARTIALLY_FILLED
        assert duplicate == partial
        assert stopped.state is LiveExecutionState.CANCEL_PENDING
        assert stopped.pending_terminal_state is LiveExecutionState.CLOSED_STOP
        assert stopped.open_quantity == Decimal("0")
        assert stopped.realized_pnl == Decimal("0.364")
        assert stopped.confirmed_recovery_debt == Decimal("0.0338")
        return stopped

    run_service_test(tmp_path, exercise)


def test_cancel_fill_race_closes_only_after_orders_and_position_reconcile(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        store = SqliteExecutionStore(tmp_path / "execution.sqlite3")
        await store.initialize()
        await seed_filled_entry(store)
        trading = FakeTradingAdapter(store)
        service = ProtectiveExitService(
            trading=trading,
            account=FakeAccountAdapter(()),
            store=store,
            clock=lambda: NOW,
            sleeper=no_wait,
            visibility_attempts=2,
        )
        stop_request = PlaceOrderRequest(
            category="linear",
            symbol="ETHUSDT",
            side="Buy",
            order_type="Market",
            quantity=Decimal("0.010"),
            order_link_id=STOP_ID,
            reduce_only=True,
            trigger_price=Decimal("3004.5"),
            trigger_direction=1,
            trigger_by="LastPrice",
            close_on_trigger=True,
        )
        tp_request = PlaceOrderRequest(
            category="linear",
            symbol="ETHUSDT",
            side="Buy",
            order_type="Limit",
            quantity=Decimal("0.010"),
            order_link_id=TP_ID,
            price=Decimal("2900"),
            reduce_only=True,
        )
        stop_order = exchange_order(stop_request)
        trading.visibility[STOP_ID] = [stop_order]
        protected = await service.install_stop(
            ENTRY_ID, instrument(), STOP_ID, stop_rate=Decimal("0.0015")
        )
        trading.visibility[TP_ID] = [exchange_order(tp_request)]
        await service.install_take_profit(
            protected, instrument(), TP_ID, desired_price=Decimal("2900")
        )
        await service.apply_exit_execution(
            exit_execution(
                execution_id="tp-full",
                order_link_id=TP_ID,
                quantity="0.010",
                price="2900",
                fee="0.001",
            ),
            received_at=NOW + timedelta(seconds=6),
            payload_hash="d" * 64,
        )
        trading.open_order_responses = [(stop_order,), ()]

        reconciled = await service.reconcile_after_exit(ENTRY_ID)

        assert reconciled.state is LiveExecutionState.CLOSED_TP
        assert trading.cancel_requests == [
            CancelOrderRequest(
                category="linear",
                symbol="ETHUSDT",
                order_link_id=STOP_ID,
            )
        ]

    asyncio.run(run())


def test_position_mismatch_after_exit_suspends_in_reconciling(tmp_path: Path) -> None:
    async def run() -> None:
        store = SqliteExecutionStore(tmp_path / "execution.sqlite3")
        await store.initialize()
        await seed_filled_entry(store)
        trading = FakeTradingAdapter(store)
        mismatched = ExchangePosition(
            category="linear",
            symbol="ETHUSDT",
            side="Sell",
            quantity=Decimal("0.001"),
            average_price=Decimal("3000"),
            mark_price=Decimal("2999"),
            unrealized_pnl=Decimal("0"),
            updated_at=NOW,
        )
        service = ProtectiveExitService(
            trading=trading,
            account=FakeAccountAdapter((mismatched,)),
            store=store,
            clock=lambda: NOW,
            sleeper=no_wait,
        )
        stop_request = PlaceOrderRequest(
            category="linear",
            symbol="ETHUSDT",
            side="Buy",
            order_type="Market",
            quantity=Decimal("0.010"),
            order_link_id=STOP_ID,
            reduce_only=True,
            trigger_price=Decimal("3004.5"),
            trigger_direction=1,
            trigger_by="LastPrice",
            close_on_trigger=True,
        )
        trading.visibility[STOP_ID] = [exchange_order(stop_request)]
        await service.install_stop(
            ENTRY_ID, instrument(), STOP_ID, stop_rate=Decimal("0.0015")
        )
        await service.apply_exit_execution(
            exit_execution(
                execution_id="stop-full",
                order_link_id=STOP_ID,
                quantity="0.010",
                price="3005",
                fee="0.002",
            ),
            received_at=NOW + timedelta(seconds=6),
            payload_hash="e" * 64,
        )

        snapshot = await service.reconcile_after_exit(ENTRY_ID)

        assert snapshot.state is LiveExecutionState.RECONCILING

    asyncio.run(run())


def test_missing_stop_is_replaced_with_new_attempt_and_survives_restart(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        database = tmp_path / "execution.sqlite3"
        store = SqliteExecutionStore(database)
        await store.initialize()
        await seed_filled_entry(store)
        trading = FakeTradingAdapter(store)
        service = ProtectiveExitService(
            trading=trading,
            account=FakeAccountAdapter(()),
            store=store,
            clock=lambda: NOW,
            sleeper=no_wait,
        )
        stop_request = PlaceOrderRequest(
            category="linear",
            symbol="ETHUSDT",
            side="Buy",
            order_type="Market",
            quantity=Decimal("0.010"),
            order_link_id=STOP_ID,
            reduce_only=True,
            trigger_price=Decimal("3004.5"),
            trigger_direction=1,
            trigger_by="LastPrice",
            close_on_trigger=True,
        )
        tp_request = PlaceOrderRequest(
            category="linear",
            symbol="ETHUSDT",
            side="Buy",
            order_type="Limit",
            quantity=Decimal("0.010"),
            order_link_id=TP_ID,
            price=Decimal("2900"),
            reduce_only=True,
        )
        trading.visibility[STOP_ID] = [exchange_order(stop_request)]
        protected = await service.install_stop(
            ENTRY_ID, instrument(), STOP_ID, stop_rate=Decimal("0.0015")
        )
        trading.visibility[TP_ID] = [exchange_order(tp_request)]
        with_tp = await service.install_take_profit(
            protected,
            instrument(),
            TP_ID,
            desired_price=Decimal("2900"),
        )
        replacement = PlaceOrderRequest(
            category="linear",
            symbol="ETHUSDT",
            side="Buy",
            order_type="Market",
            quantity=Decimal("0.010"),
            order_link_id=REPLACEMENT_STOP_ID,
            reduce_only=True,
            trigger_price=Decimal("3004.5"),
            trigger_direction=1,
            trigger_by="LastPrice",
            close_on_trigger=True,
        )
        trading.visibility[REPLACEMENT_STOP_ID] = [exchange_order(replacement)]

        repaired = await service.restore_stop(
            with_tp,
            instrument(),
            REPLACEMENT_STOP_ID,
            stop_rate=Decimal("0.0015"),
        )

        assert repaired.state is LiveExecutionState.ACTIVE_PROTECTED
        assert repaired.stop_order_link_id == REPLACEMENT_STOP_ID
        assert repaired.stop_order_id == "stop-exchange-order"
        assert repaired.tp_order_link_id == TP_ID
        restarted = SqliteExecutionStore(database)
        await restarted.initialize()
        assert len(await restarted.load_all_order_intents()) == 4
        assert await restarted.load_all_entry_snapshots() == (
            await store.load_entry_snapshot(ENTRY_ID),
        )
        assert await restarted.load_all_protection_snapshots() == (repaired,)

    asyncio.run(run())
