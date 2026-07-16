"""Concrete demo closure preserves short-before-long option ordering."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from eth_credit_hedge.domain.client_order_ids import ClientOrderId, ClientOrderRole
from eth_credit_hedge.domain.execution import (
    ExchangeOrder,
    ExchangePosition,
    ExecutionUpdate,
    LiveExecutionState,
    OrderRequestAck,
    OrderRequestKind,
    PlaceOrderRequest,
    UncertainOrderOutcomeError,
)
from eth_credit_hedge.domain.instruments import (
    InstrumentSpec,
    LotSizeFilter,
    OptionMarketQuote,
    PriceFilter,
)
from eth_credit_hedge.domain.live_execution import EntryExecutionSnapshot
from eth_credit_hedge.domain.option_exit import OptionExitState
from eth_credit_hedge.domain.protected_execution import ProtectionSnapshot
from eth_credit_hedge.infrastructure.bybit.demo_strategy_close import (
    DemoStrategyCloseOperations,
)
from eth_credit_hedge.infrastructure.persistence.sqlite_execution_store import (
    SqliteExecutionStore,
)


NOW = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
SHORT = "ETH-31JUL26-3000-P-USDT"
LONG = "ETH-31JUL26-2900-P-USDT"


def option_instrument(symbol: str) -> InstrumentSpec:
    return InstrumentSpec(
        symbol=symbol,
        category="option",
        status="Trading",
        base_coin="ETH",
        quote_coin="USDT",
        settle_coin="USDT",
        price_filter=PriceFilter(
            tick_size=Decimal("0.1"),
            min_price=Decimal("0.1"),
            max_price=Decimal("10000"),
        ),
        lot_size_filter=LotSizeFilter(
            qty_step=Decimal("0.1"),
            min_order_qty=Decimal("0.1"),
            max_order_qty=Decimal("100"),
            max_market_order_qty=Decimal("100"),
            min_notional=Decimal("0.1"),
        ),
        contract_multiplier=Decimal("1"),
        delivery_time_utc=datetime(2026, 7, 31, 8, tzinfo=timezone.utc),
    )


def position(symbol: str, side: str) -> ExchangePosition:
    return ExchangePosition(
        category="option",
        symbol=symbol,
        side=side,  # type: ignore[arg-type]
        quantity=Decimal("0.1"),
        average_price=Decimal("10"),
        mark_price=Decimal("10"),
        unrealized_pnl=Decimal("0"),
        updated_at=NOW,
    )


class FakeClosingExchange:
    def __init__(
        self,
        *,
        partial: bool = False,
        fail_after_first_option: bool = False,
        reject_first_option: bool = False,
        uncertain_first_option: bool = False,
        no_option_fills: bool = False,
        execution_visibility_delay_reads: int = 0,
    ) -> None:
        self.positions = [position(SHORT, "Sell"), position(LONG, "Buy")]
        self.requests: list[PlaceOrderRequest] = []
        self.executions: list[ExecutionUpdate] = []
        self.partial = partial
        self.fail_after_first_option = fail_after_first_option
        self.reject_first_option = reject_first_option
        self.uncertain_first_option = uncertain_first_option
        self.no_option_fills = no_option_fills
        self.execution_visibility_delay_reads = execution_visibility_delay_reads
        self.failed_once = False
        self.rejected_once = False
        self.uncertain_once = False
        self.option_requests = 0
        self.orders: dict[str, ExchangeOrder] = {}

    async def get_positions(
        self,
        category: str,
        symbol: str | None = None,
    ) -> tuple[ExchangePosition, ...]:
        if (
            category == "option"
            and self.fail_after_first_option
            and self.option_requests > 0
            and not self.failed_once
        ):
            self.failed_once = True
            raise RuntimeError("injected option position read failure")
        return tuple(
            value
            for value in self.positions
            if value.category == category
            and (symbol is None or value.symbol == symbol)
        )

    async def get_open_orders(
        self,
        category: str,
        symbol: str | None = None,
    ) -> tuple[()]:
        del category, symbol
        return ()

    async def cancel_all(self, category: str, symbol: str | None = None) -> None:
        del category, symbol

    async def cancel_order(self, request: object) -> None:
        del request

    async def place_order(self, request: PlaceOrderRequest) -> OrderRequestAck:
        self.requests.append(request)
        if request.category == "option":
            self.option_requests += 1
            if self.reject_first_option and not self.rejected_once:
                self.rejected_once = True
                raise RuntimeError("injected option rejection")
        order_id = f"order-{len(self.requests)}"
        matching = next(
            (
                value
                for value in self.positions
                if value.symbol == request.symbol
            ),
            None,
        )
        fill_quantity = request.quantity
        if (
            request.category == "option"
            and self.partial
            and matching is not None
            and matching.quantity == Decimal("0.1")
        ):
            fill_quantity = Decimal("0.05")
        execution = ExecutionUpdate(
            execution_id=f"execution-{len(self.requests)}",
            order_id=order_id,
            order_link_id=request.order_link_id,
            symbol=request.symbol,
            side=request.side,
            price=request.price or Decimal("3000"),
            quantity=fill_quantity,
            fee=Decimal("0"),
            is_maker=False,
            executed_at=NOW,
        )
        self.orders[request.order_link_id] = ExchangeOrder(
            category=request.category,
            order_id=order_id,
            order_link_id=request.order_link_id,
            symbol=request.symbol,
            status="New" if self.no_option_fills else "Filled",
            side=request.side,
            order_type=request.order_type,
            price=request.price,
            quantity=request.quantity,
            cumulative_filled_quantity=(
                Decimal("0") if self.no_option_fills else fill_quantity
            ),
            average_price=(
                None if self.no_option_fills else execution.price
            ),
            reduce_only=request.reduce_only,
            created_at=NOW,
            updated_at=NOW,
            time_in_force=request.time_in_force,
            position_idx=request.position_idx,
        )
        if not self.no_option_fills:
            self.executions.append(execution)
        if request.category == "option" and not self.no_option_fills:
            updated_positions: list[ExchangePosition] = []
            for value in self.positions:
                if value.symbol != request.symbol:
                    updated_positions.append(value)
                    continue
                remaining = value.quantity - fill_quantity
                if remaining > Decimal("0"):
                    updated_positions.append(replace(value, quantity=remaining))
            self.positions = updated_positions
        acknowledgement = OrderRequestAck(
            request_kind=OrderRequestKind.PLACE,
            order_id=order_id,
            order_link_id=request.order_link_id,
            acknowledged_at=NOW,
        )
        if self.uncertain_first_option and not self.uncertain_once:
            self.uncertain_once = True
            raise UncertainOrderOutcomeError(
                order_link_id=request.order_link_id,
                operation="place order",
            )
        return acknowledgement

    async def get_execution_history(
        self,
        category: str,
        symbol: str | None = None,
        order_link_id: str | None = None,
    ) -> tuple[ExecutionUpdate, ...]:
        del category
        if self.execution_visibility_delay_reads > 0:
            self.execution_visibility_delay_reads -= 1
            return ()
        return tuple(
            value
            for value in self.executions
            if (symbol is None or value.symbol == symbol)
            and (order_link_id is None or value.order_link_id == order_link_id)
        )

    async def get_order_by_link_id(
        self,
        category: str,
        symbol: str,
        order_link_id: str,
    ) -> ExchangeOrder | None:
        order = self.orders.get(order_link_id)
        if order is None or order.category != category or order.symbol != symbol:
            return None
        return order


class FakeQuotes:
    async def get_option_chain(self, base_coin: str) -> tuple[OptionMarketQuote, ...]:
        assert base_coin == "ETH"
        return (
            OptionMarketQuote(
                symbol=SHORT,
                timestamp_utc=NOW,
                bid_price=Decimal("9.9"),
                bid_size=Decimal("1"),
                ask_price=Decimal("10.1"),
                ask_size=Decimal("1"),
                mark_price=Decimal("10"),
                underlying_price=Decimal("3000"),
                index_price=Decimal("3000"),
                bid_iv=None,
                ask_iv=None,
                mark_iv=None,
                delta=None,
                gamma=None,
                vega=None,
                theta=None,
            ),
            OptionMarketQuote(
                symbol=LONG,
                timestamp_utc=NOW,
                bid_price=Decimal("4.9"),
                bid_size=Decimal("1"),
                ask_price=Decimal("5.1"),
                ask_size=Decimal("1"),
                mark_price=Decimal("5"),
                underlying_price=Decimal("3000"),
                index_price=Decimal("3000"),
                bid_iv=None,
                ask_iv=None,
                mark_iv=None,
                delta=None,
                gamma=None,
                vega=None,
                theta=None,
            ),
        )


async def make_operations(
    path: Path,
    exchange: FakeClosingExchange,
    *,
    cycle_id: str,
    maximum_attempts: int = 5,
) -> tuple[SqliteExecutionStore, DemoStrategyCloseOperations]:
    store = SqliteExecutionStore(path)
    await store.initialize()

    def order_id(role: ClientOrderRole, attempt: int) -> str:
        return str(
            ClientOrderId(
                strategy_instance="SIM",
                cycle=1,
                level=0,
                role=role,
                attempt=attempt,
                nonce=f"{attempt + 200:04X}",
            )
        )

    return store, DemoStrategyCloseOperations(
        trading=exchange,  # type: ignore[arg-type]
        account=exchange,  # type: ignore[arg-type]
        store=store,
        quotes=FakeQuotes(),
        option_instruments=(option_instrument(SHORT), option_instrument(LONG)),
        cycle_id=cycle_id,
        short_symbol=SHORT,
        long_symbol=LONG,
        order_link_id_factory=order_id,
        clock=lambda: NOW,
        poll_interval_seconds=0,
        maximum_attempts=maximum_attempts,
    )


async def run_close(path: Path) -> None:
    exchange = FakeClosingExchange()
    store = SqliteExecutionStore(path)
    await store.initialize()

    def order_id(role: ClientOrderRole, attempt: int) -> str:
        return str(
            ClientOrderId(
                strategy_instance="SIM",
                cycle=1,
                level=0,
                role=role,
                attempt=attempt,
                nonce=f"{attempt:04X}",
            )
        )

    operations = DemoStrategyCloseOperations(
        trading=exchange,  # type: ignore[arg-type]
        account=exchange,  # type: ignore[arg-type]
        store=store,
        quotes=FakeQuotes(),
        option_instruments=(option_instrument(SHORT), option_instrument(LONG)),
        cycle_id="cycle-1",
        short_symbol=SHORT,
        long_symbol=LONG,
        order_link_id_factory=order_id,
        clock=lambda: NOW,
        poll_interval_seconds=0,
    )
    await operations.close_hedges()
    await operations.close_option_spread()
    assert await operations.verify_strategy_closed()
    assert [(request.symbol, request.side) for request in exchange.requests] == [
        (SHORT, "Buy"),
        (LONG, "Sell"),
    ]
    snapshot = await store.load_option_exit_snapshot("cycle-1")
    assert snapshot is not None and snapshot.state is OptionExitState.CLOSED
    await operations.close_option_spread()
    assert len(exchange.requests) == 2


def test_option_close_is_ordered_and_restart_idempotent(tmp_path: Path) -> None:
    asyncio.run(run_close(tmp_path / "close.sqlite3"))


def test_option_close_recovers_partial_fill_after_restart(tmp_path: Path) -> None:
    async def exercise() -> None:
        exchange = FakeClosingExchange(
            partial=True,
            fail_after_first_option=True,
        )
        store = SqliteExecutionStore(tmp_path / "partial-close.sqlite3")
        await store.initialize()

        def order_id(role: ClientOrderRole, attempt: int) -> str:
            return str(
                ClientOrderId(
                    strategy_instance="SIM",
                    cycle=1,
                    level=0,
                    role=role,
                    attempt=attempt,
                    nonce=f"{attempt + 100:04X}",
                )
            )

        operations = DemoStrategyCloseOperations(
            trading=exchange,  # type: ignore[arg-type]
            account=exchange,  # type: ignore[arg-type]
            store=store,
            quotes=FakeQuotes(),
            option_instruments=(option_instrument(SHORT), option_instrument(LONG)),
            cycle_id="cycle-partial",
            short_symbol=SHORT,
            long_symbol=LONG,
            order_link_id_factory=order_id,
            clock=lambda: NOW,
            poll_interval_seconds=0,
        )
        with pytest.raises(RuntimeError, match="injected option position"):
            await operations.close_option_spread()
        interrupted = await store.load_option_exit_snapshot("cycle-partial")
        assert interrupted is not None
        assert interrupted.state is OptionExitState.SHORT_CLOSING

        await operations.close_option_spread()
        assert await operations.verify_strategy_closed()
        closed = await store.load_option_exit_snapshot("cycle-partial")
        assert closed is not None and closed.state is OptionExitState.CLOSED
        assert [(request.symbol, request.side) for request in exchange.requests] == [
            (SHORT, "Buy"),
            (SHORT, "Buy"),
            (LONG, "Sell"),
            (LONG, "Sell"),
        ]

    asyncio.run(exercise())


def test_option_close_recovers_rejection_on_restart(tmp_path: Path) -> None:
    async def exercise() -> None:
        exchange = FakeClosingExchange(reject_first_option=True)
        store, operations = await make_operations(
            tmp_path / "rejected-close.sqlite3",
            exchange,
            cycle_id="cycle-rejected",
        )
        with pytest.raises(RuntimeError, match="injected option rejection"):
            await operations.close_option_spread()
        interrupted = await store.load_option_exit_snapshot("cycle-rejected")
        assert interrupted is not None
        assert interrupted.state is OptionExitState.SHORT_CLOSING

        await operations.close_option_spread()
        assert await operations.verify_strategy_closed()
        assert [(request.symbol, request.side) for request in exchange.requests] == [
            (SHORT, "Buy"),
            (SHORT, "Buy"),
            (LONG, "Sell"),
        ]

    asyncio.run(exercise())


def test_option_close_recovers_uncertain_acknowledgement(tmp_path: Path) -> None:
    async def exercise() -> None:
        exchange = FakeClosingExchange(uncertain_first_option=True)
        _, operations = await make_operations(
            tmp_path / "uncertain-close.sqlite3",
            exchange,
            cycle_id="cycle-uncertain",
        )
        await operations.close_option_spread()
        assert await operations.verify_strategy_closed()
        assert [(request.symbol, request.side) for request in exchange.requests] == [
            (SHORT, "Buy"),
            (LONG, "Sell"),
        ]

    asyncio.run(exercise())


def test_option_close_timeout_never_claims_flat(tmp_path: Path) -> None:
    async def exercise() -> None:
        exchange = FakeClosingExchange(no_option_fills=True)
        store, operations = await make_operations(
            tmp_path / "timeout-close.sqlite3",
            exchange,
            cycle_id="cycle-timeout",
            maximum_attempts=2,
        )
        with pytest.raises(RuntimeError, match="attempts exhausted"):
            await operations.close_option_spread()
        snapshot = await store.load_option_exit_snapshot("cycle-timeout")
        assert snapshot is not None
        assert snapshot.state is OptionExitState.SHORT_CLOSING
        assert not await operations.verify_strategy_closed()
        assert await operations.verify_option_protected()

    asyncio.run(exercise())


def test_option_close_waits_for_execution_evidence_and_restores(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        exchange = FakeClosingExchange(execution_visibility_delay_reads=60)
        store, operations = await make_operations(
            tmp_path / "delayed-evidence-close.sqlite3",
            exchange,
            cycle_id="cycle-delayed-evidence",
        )
        with pytest.raises(RuntimeError, match="execution is not visible"):
            await operations.close_option_spread()
        interrupted = await store.load_option_exit_snapshot(
            "cycle-delayed-evidence"
        )
        assert interrupted is not None
        assert interrupted.state is OptionExitState.SHORT_CLOSING
        assert interrupted.short_remaining_quantity == Decimal("0.1")
        assert interrupted.active_order_link_id is not None

        await operations.close_option_spread()
        assert await operations.verify_strategy_closed()
        closed = await store.load_option_exit_snapshot("cycle-delayed-evidence")
        assert closed is not None and closed.state is OptionExitState.CLOSED
        assert await store.execution_count() == 2

    asyncio.run(exercise())


def test_flat_restart_imports_persisted_emergency_close_execution(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        exchange = FakeClosingExchange(execution_visibility_delay_reads=3)
        store, operations = await make_operations(
            tmp_path / "emergency-restart.sqlite3",
            exchange,
            cycle_id="cycle-emergency-restart",
        )

        def order_id(role: ClientOrderRole, level: int = 2) -> str:
            return str(
                ClientOrderId(
                    strategy_instance="SIM",
                    cycle=1,
                    level=level,
                    role=role,
                    attempt=1,
                    nonce=f"{300 + level:04X}",
                )
            )

        entry_id = order_id(ClientOrderRole.HEDGE_ENTRY)
        stop_id = order_id(ClientOrderRole.HEDGE_STOP)
        emergency_id = order_id(ClientOrderRole.EMERGENCY_CLOSE, level=0)
        entry_request = PlaceOrderRequest(
            category="linear",
            symbol="ETHUSDT",
            side="Sell",
            order_type="Market",
            quantity=Decimal("0.1"),
            order_link_id=entry_id,
        )
        await store.persist_entry_intent(
            entry_request,
            EntryExecutionSnapshot(
                order_link_id=entry_id,
                state=LiveExecutionState.ACTIVE_UNPROTECTED,
                target_quantity=Decimal("0.1"),
                entry_order_id="entry-order",
                filled_quantity=Decimal("0.1"),
                entry_notional=Decimal("300"),
                entry_fees=Decimal("0.1"),
                version=1,
                updated_at=NOW,
            ),
            NOW,
        )
        stop_request = PlaceOrderRequest(
            category="linear",
            symbol="ETHUSDT",
            side="Buy",
            order_type="Market",
            quantity=Decimal("0.1"),
            order_link_id=stop_id,
            reduce_only=True,
            trigger_price=Decimal("3010"),
            trigger_direction=1,
            trigger_by="MarkPrice",
            close_on_trigger=True,
        )
        await store.persist_protection_intent(
            stop_request,
            ProtectionSnapshot(
                entry_order_link_id=entry_id,
                state=LiveExecutionState.ACTIVE_UNPROTECTED,
                entry_quantity=Decimal("0.1"),
                open_quantity=Decimal("0.1"),
                average_entry_price=Decimal("3000"),
                entry_fees=Decimal("0.1"),
                stop_order_link_id=stop_id,
                stop_order_id="stop-order",
                stop_trigger_price=Decimal("3010"),
                tp_order_link_id=None,
                tp_order_id=None,
                tp_price=None,
                tp_filled_quantity=Decimal("0"),
                stop_filled_quantity=Decimal("0"),
                exit_notional=Decimal("0"),
                exit_fees=Decimal("0"),
                confirmed_recovery_debt=Decimal("0"),
                pending_terminal_state=None,
                version=1,
                updated_at=NOW,
            ),
            NOW,
        )
        await store.persist_order_intent(
            PlaceOrderRequest(
                category="linear",
                symbol="ETHUSDT",
                side="Buy",
                order_type="Market",
                quantity=Decimal("0.1"),
                order_link_id=emergency_id,
                reduce_only=True,
            ),
            NOW,
        )
        exchange.executions.append(
            ExecutionUpdate(
                execution_id="emergency-execution",
                order_id="emergency-order",
                order_link_id=emergency_id,
                symbol="ETHUSDT",
                side="Buy",
                price=Decimal("3001"),
                quantity=Decimal("0.1"),
                fee=Decimal("0.1"),
                is_maker=False,
                executed_at=NOW,
            )
        )

        await operations.close_hedges()
        recovered = await store.load_protection_snapshot(entry_id)
        assert recovered is not None
        assert recovered.state is LiveExecutionState.ERROR
        assert recovered.open_quantity == Decimal("0")
        assert recovered.stop_filled_quantity == Decimal("0.1")
        assert await store.execution_count() == 1
        assert exchange.requests == []

        await operations.close_hedges()
        assert await store.execution_count() == 1

    asyncio.run(exercise())
