"""Complete one-level entry, protection, and exit orchestration."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from eth_credit_hedge.application.one_level_entry import OneLevelEntryService
from eth_credit_hedge.application.one_level_lifecycle import (
    OneLevelLifecycleService,
)
from eth_credit_hedge.application.protective_exits import ProtectiveExitService
from eth_credit_hedge.backtesting.simulated_exchange import (
    ExecutionModelConfig,
    SimulatedExchange,
)
from eth_credit_hedge.domain.execution import LiveExecutionState, PlaceOrderRequest
from eth_credit_hedge.domain.instruments import (
    InstrumentSpec,
    LotSizeFilter,
    PriceFilter,
)
from eth_credit_hedge.infrastructure.persistence.sqlite_execution_store import (
    SqliteExecutionStore,
)


NOW = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
ENTRY_ID = "ECH-D3-C0001-L01-ENTRY-A01-A001"
STOP_ID = "ECH-D3-C0001-L01-STOP-A01-A002"
TP_ID = "ECH-D3-C0001-L01-TP-A01-A003"


def instrument() -> InstrumentSpec:
    return InstrumentSpec(
        symbol="ETHUSDT",
        category="linear",
        status="Trading",
        base_coin="ETH",
        quote_coin="USDT",
        settle_coin="USDT",
        price_filter=PriceFilter(
            tick_size=Decimal("0.01"),
            min_price=Decimal("1"),
            max_price=Decimal("100000"),
        ),
        lot_size_filter=LotSizeFilter(
            qty_step=Decimal("0.01"),
            min_order_qty=Decimal("0.01"),
            max_order_qty=Decimal("100"),
            max_market_order_qty=Decimal("50"),
            min_notional=Decimal("5"),
        ),
        contract_multiplier=Decimal("1"),
        delivery_time_utc=None,
    )


def model(*, partial: bool, duplicate: bool) -> ExecutionModelConfig:
    return ExecutionModelConfig(
        acknowledgement_delay_ms=0,
        visibility_delay_ms=0,
        fill_delay_ms=0,
        partial_fill_probability=Decimal("1") if partial else Decimal("0"),
        rejection_probability=Decimal("0"),
        maker_fee_rate=Decimal("0.0002"),
        taker_fee_rate=Decimal("0.00055"),
        stop_slippage_bps=Decimal("1"),
        entry_slippage_bps=Decimal("1"),
        duplicate_execution_probability=(
            Decimal("1") if duplicate else Decimal("0")
        ),
    )


async def run_lifecycle(
    path: Path,
    *,
    partial: bool,
    duplicate: bool,
    externally_submitted: bool = False,
) -> tuple[SimulatedExchange, SqliteExecutionStore, LiveExecutionState]:
    exchange = SimulatedExchange(
        instrument=instrument(),
        initial_price=Decimal("3000"),
        config=model(partial=partial, duplicate=duplicate),
        seed=7,
        start_time_utc=NOW,
    )
    store = SqliteExecutionStore(path)
    await store.initialize()

    async def advance(_: float) -> None:
        exchange.advance_time(1)

    entry = OneLevelEntryService(
        trading=exchange,
        store=store,
        clock=lambda: exchange.current_time_utc,
    )
    exits = ProtectiveExitService(
        trading=exchange,
        account=exchange,
        store=store,
        clock=lambda: exchange.current_time_utc,
        sleeper=advance,
        visibility_attempts=3,
        visibility_interval_seconds=0,
    )
    lifecycle = OneLevelLifecycleService(
        trading=exchange,
        account=exchange,
        store=store,
        entry_service=entry,
        exit_service=exits,
        instrument=instrument(),
        clock=lambda: exchange.current_time_utc,
        sleeper=advance,
        fill_attempts=5,
        fill_interval_seconds=0,
    )
    request = PlaceOrderRequest(
        category="linear",
        symbol="ETHUSDT",
        side="Sell",
        order_type="Market",
        quantity=Decimal("0.02") if partial else Decimal("0.01"),
        order_link_id=ENTRY_ID,
        time_in_force="IOC",
    )
    if externally_submitted:
        submitted = await entry.submit_entry(request)
        opened = await lifecycle.protect_submitted_entry(
            submitted,
            stop_order_link_id=STOP_ID,
            take_profit_order_link_id=TP_ID,
            stop_rate=Decimal("0.01"),
            take_profit_price=Decimal("2990"),
        )
    else:
        opened = await lifecycle.open_and_protect(
            request,
            stop_order_link_id=STOP_ID,
            take_profit_order_link_id=TP_ID,
            stop_rate=Decimal("0.01"),
            take_profit_price=Decimal("2990"),
            reference_price=Decimal("3000"),
        )

    assert opened.entry.state is LiveExecutionState.ACTIVE_UNPROTECTED
    assert opened.protection.state is LiveExecutionState.ACTIVE_PROTECTED
    assert opened.entry.filled_quantity == opened.protection.open_quantity
    assert await entry.reconcile_position(
        await exchange.get_positions("linear", "ETHUSDT")
    )
    stop_intent = await store.load_order_intent(STOP_ID)
    tp_intent = await store.load_order_intent(TP_ID)
    assert stop_intent is not None and stop_intent.reduce_only
    assert stop_intent.close_on_trigger
    assert tp_intent is not None and tp_intent.reduce_only

    exchange.advance_market(Decimal("2989"), elapsed_ms=1)
    closed = await lifecycle.await_exit(ENTRY_ID)

    assert closed.state is LiveExecutionState.CLOSED_TP
    assert closed.open_quantity == Decimal("0")
    assert not await exchange.get_positions("linear", "ETHUSDT")
    assert not await exchange.get_open_orders("linear", "ETHUSDT")
    return exchange, store, closed.state


def test_full_fill_lifecycle_closes_from_authoritative_executions(
    tmp_path: Path,
) -> None:
    exchange, store, state = asyncio.run(
        run_lifecycle(
            tmp_path / "full.sqlite3",
            partial=False,
            duplicate=False,
        )
    )

    assert state is LiveExecutionState.CLOSED_TP
    assert asyncio.run(store.execution_count()) == 2
    assert exchange.metrics.maximum_unprotected_ms == 0


def test_partial_and_duplicate_deliveries_remain_idempotent(tmp_path: Path) -> None:
    _, store, state = asyncio.run(
        run_lifecycle(
            tmp_path / "partial.sqlite3",
            partial=True,
            duplicate=True,
        )
    )

    assert state is LiveExecutionState.CLOSED_TP
    assert asyncio.run(store.execution_count()) == 4


def test_coordinator_submitted_entry_can_continue_through_protection(
    tmp_path: Path,
) -> None:
    _, store, state = asyncio.run(
        run_lifecycle(
            tmp_path / "coordinator.sqlite3",
            partial=False,
            duplicate=False,
            externally_submitted=True,
        )
    )

    assert state is LiveExecutionState.CLOSED_TP
    assert asyncio.run(store.execution_count()) == 2
