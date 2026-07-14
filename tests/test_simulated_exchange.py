"""Seeded realistic exchange, gaps, costs, duplicates, and port behavior."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from eth_credit_hedge.backtesting.simulated_exchange import (
    ExecutionModelConfig,
    SimulatedExchange,
    SimulatedOrderRejectedError,
    replay_simulated_financials,
)
from eth_credit_hedge.backtesting.fault_injection import FaultInjector
from eth_credit_hedge.application.one_level_entry import OneLevelEntryService
from eth_credit_hedge.domain.execution import LiveExecutionState
from eth_credit_hedge.domain.execution import PlaceOrderRequest
from eth_credit_hedge.domain.instruments import (
    InstrumentSpec,
    LotSizeFilter,
    PriceFilter,
)
from eth_credit_hedge.infrastructure.persistence.sqlite_execution_store import (
    SqliteExecutionStore,
)


NOW = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
ENTRY_ID = "ECH-01-C0001-L01-ENTRY-A01-A001"
STOP_ID = "ECH-01-C0001-L01-STOP-A01-B001"
TP_ID = "ECH-01-C0001-L01-TP-A01-C001"


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


def config(**changes: object) -> ExecutionModelConfig:
    values: dict[str, object] = {
        "acknowledgement_delay_ms": 10,
        "visibility_delay_ms": 50,
        "fill_delay_ms": 100,
        "partial_fill_probability": Decimal("0"),
        "rejection_probability": Decimal("0"),
        "duplicate_execution_probability": Decimal("0"),
        "reorder_probability": Decimal("0"),
        "uncertain_ack_probability": Decimal("0"),
        "partial_fill_fraction": Decimal("0.5"),
        "maker_fee_rate": Decimal("0.0002"),
        "taker_fee_rate": Decimal("0.00055"),
        "perp_spread_bps": Decimal("0"),
        "stop_slippage_bps": Decimal("0"),
        "entry_slippage_bps": Decimal("0"),
    }
    values.update(changes)
    return ExecutionModelConfig(**values)  # type: ignore[arg-type]


def market_entry(quantity: str = "0.010") -> PlaceOrderRequest:
    return PlaceOrderRequest(
        category="linear",
        symbol="ETHUSDT",
        side="Sell",
        order_type="Market",
        quantity=Decimal(quantity),
        order_link_id=ENTRY_ID,
        time_in_force="IOC",
    )


def stop_order(quantity: str = "0.010") -> PlaceOrderRequest:
    return PlaceOrderRequest(
        category="linear",
        symbol="ETHUSDT",
        side="Buy",
        order_type="Market",
        quantity=Decimal(quantity),
        order_link_id=STOP_ID,
        time_in_force="GTC",
        reduce_only=True,
        trigger_price=Decimal("3004.5"),
        trigger_direction=1,
        trigger_by="LastPrice",
    )


def tp_order(quantity: str = "0.010") -> PlaceOrderRequest:
    return PlaceOrderRequest(
        category="linear",
        symbol="ETHUSDT",
        side="Buy",
        order_type="Limit",
        quantity=Decimal(quantity),
        price=Decimal("2990"),
        order_link_id=TP_ID,
        time_in_force="GTC",
        reduce_only=True,
    )


def make_exchange(
    model: ExecutionModelConfig,
    seed: int = 7,
    fault_injector: FaultInjector | None = None,
) -> SimulatedExchange:
    return SimulatedExchange(
        instrument=instrument(),
        initial_price=Decimal("3000"),
        config=model,
        seed=seed,
        start_time_utc=NOW,
        fault_injector=fault_injector,
    )


def test_ack_visibility_and_fill_delays_are_distinct() -> None:
    exchange = make_exchange(config())

    ack = asyncio.run(exchange.place_order(market_entry()))

    assert ack.order_link_id == ENTRY_ID
    assert asyncio.run(exchange.get_open_orders("linear", "ETHUSDT")) == ()
    exchange.advance_time(40)
    assert len(asyncio.run(exchange.get_open_orders("linear", "ETHUSDT"))) == 1
    assert asyncio.run(exchange.get_execution_history("linear", "ETHUSDT")) == ()
    exchange.advance_time(50)
    executions = asyncio.run(
        exchange.get_execution_history("linear", "ETHUSDT")
    )
    assert len(executions) == 1
    assert executions[0].quantity == Decimal("0.010")


def test_downward_entry_gap_and_upward_stop_use_first_available_prices() -> None:
    exchange = make_exchange(
        config(
            acknowledgement_delay_ms=0,
            visibility_delay_ms=0,
            fill_delay_ms=100,
            perp_spread_bps=Decimal("2"),
            entry_slippage_bps=Decimal("10"),
            stop_slippage_bps=Decimal("20"),
        )
    )
    exchange.register_trigger_price(ENTRY_ID, Decimal("3100"))
    asyncio.run(exchange.place_order(market_entry()))

    exchange.advance_market(Decimal("2950"), elapsed_ms=100)
    entry_cost = exchange.execution_costs[0]

    assert entry_cost.trigger_price == Decimal("3100")
    assert entry_cost.first_available_price == Decimal("2949.4100")
    assert entry_cost.fill_price == Decimal("2946.4605900")
    assert entry_cost.gap_slippage == Decimal("153.5394100")

    asyncio.run(exchange.place_order(stop_order()))
    exchange.advance_market(Decimal("3120"), elapsed_ms=100)
    stop_cost = exchange.execution_costs[-1]

    assert stop_cost.trigger_price == Decimal("3004.5")
    assert stop_cost.first_available_price == Decimal("3120.6240")
    assert stop_cost.fill_price == Decimal("3126.8652480")
    assert stop_cost.gap_slippage == Decimal("122.3652480")
    assert asyncio.run(exchange.get_positions("linear", "ETHUSDT")) == ()


def test_partial_duplicate_and_reordered_delivery_is_seed_reproducible() -> None:
    model = config(
        acknowledgement_delay_ms=0,
        visibility_delay_ms=0,
        fill_delay_ms=0,
        partial_fill_probability=Decimal("1"),
        partial_fill_fraction=Decimal("0.4"),
        duplicate_execution_probability=Decimal("1"),
        reorder_probability=Decimal("1"),
    )

    def run_once() -> tuple[tuple[str, ...], tuple[str, ...]]:
        exchange = make_exchange(model, seed=123)
        asyncio.run(exchange.place_order(market_entry()))
        exchange.advance_market(Decimal("2999"), elapsed_ms=1)
        delivered = exchange.drain_execution_events()
        exchange.advance_market(Decimal("2998"), elapsed_ms=1)
        delivered += exchange.drain_execution_events()
        return (
            tuple(event.execution_id for event in delivered),
            tuple(item.to_json() for item in exchange.event_log),
        )

    first = run_once()
    second = run_once()

    assert first == second
    assert len(first[0]) == 4
    assert len(set(first[0])) == 2


def test_seeded_rejection_is_known_and_creates_no_order() -> None:
    exchange = make_exchange(
        config(rejection_probability=Decimal("1")),
        seed=99,
    )

    with pytest.raises(SimulatedOrderRejectedError):
        asyncio.run(exchange.place_order(market_entry()))

    assert asyncio.run(exchange.get_open_orders("linear", "ETHUSDT")) == ()
    assert exchange.metrics.rejection_count == 1


def test_fees_funding_and_slippage_are_separate_metrics() -> None:
    exchange = make_exchange(
        config(
            acknowledgement_delay_ms=0,
            visibility_delay_ms=0,
            fill_delay_ms=0,
            entry_slippage_bps=Decimal("5"),
        )
    )
    asyncio.run(exchange.place_order(market_entry()))
    exchange.apply_funding(Decimal("0.0001"))

    assert exchange.metrics.entry_fees > Decimal("0")
    assert exchange.metrics.entry_slippage > Decimal("0")
    assert exchange.metrics.funding != Decimal("0")
    assert exchange.metrics.gross_trading_pnl == Decimal("0")
    assert exchange.metrics.net_hedge_pnl < Decimal("0")
    assert exchange.metrics.mode == "SIMULATED"


def test_tp_and_stop_fees_and_unprotected_time_are_separate() -> None:
    take_profit_exchange = make_exchange(
        config(acknowledgement_delay_ms=0, visibility_delay_ms=0, fill_delay_ms=0)
    )
    asyncio.run(take_profit_exchange.place_order(market_entry()))
    take_profit_exchange.advance_time(250)
    assert take_profit_exchange.metrics.maximum_unprotected_ms == 250
    asyncio.run(take_profit_exchange.place_order(tp_order()))
    take_profit_exchange.advance_market(Decimal("2980"), elapsed_ms=1)

    stop_exchange = make_exchange(
        config(acknowledgement_delay_ms=0, visibility_delay_ms=0, fill_delay_ms=0)
    )
    asyncio.run(stop_exchange.place_order(market_entry()))
    asyncio.run(stop_exchange.place_order(stop_order()))
    stop_exchange.advance_market(Decimal("3010"), elapsed_ms=1)

    assert take_profit_exchange.metrics.tp_fees > Decimal("0")
    assert take_profit_exchange.metrics.stop_fees == Decimal("0")
    assert stop_exchange.metrics.stop_fees > Decimal("0")
    assert stop_exchange.metrics.tp_fees == Decimal("0")
    assert stop_exchange.metrics.maximum_unprotected_ms == 0

    replayed = replay_simulated_financials(take_profit_exchange.event_log)
    assert replayed.gross_trading_pnl == take_profit_exchange.metrics.gross_trading_pnl
    assert replayed.entry_fees == take_profit_exchange.metrics.entry_fees
    assert replayed.tp_fees == take_profit_exchange.metrics.tp_fees
    assert replayed.stop_fees == take_profit_exchange.metrics.stop_fees
    assert replayed.funding == take_profit_exchange.metrics.funding
    assert replayed.net_hedge_pnl == take_profit_exchange.metrics.net_hedge_pnl


def test_event_log_digest_is_reproducible_by_seed() -> None:
    def run() -> str:
        exchange = make_exchange(
            config(
                acknowledgement_delay_ms=0,
                visibility_delay_ms=0,
                fill_delay_ms=0,
                partial_fill_probability=Decimal("1"),
            ),
            seed=47,
        )
        asyncio.run(exchange.place_order(market_entry()))
        exchange.advance_market(Decimal("2990"), elapsed_ms=1)
        return exchange.event_log_digest

    assert run() == run()
    assert len(run()) == 64


def test_partial_fill_then_disconnect_recovers_all_executions_by_rest() -> None:
    exchange = make_exchange(
        config(
            acknowledgement_delay_ms=0,
            visibility_delay_ms=0,
            fill_delay_ms=0,
            partial_fill_probability=Decimal("1"),
            partial_fill_fraction=Decimal("0.4"),
        )
    )
    asyncio.run(exchange.place_order(market_entry()))
    exchange.set_private_connected(False)
    exchange.advance_market(Decimal("2990"), elapsed_ms=1)

    assert exchange.drain_execution_events() == ()
    history = asyncio.run(
        exchange.get_execution_history("linear", "ETHUSDT", ENTRY_ID)
    )
    assert sum((execution.quantity for execution in history), Decimal("0")) == Decimal(
        "0.010"
    )


def test_uncertain_ack_discovers_same_order_without_resubmission(tmp_path) -> None:
    exchange = make_exchange(
        config(
            acknowledgement_delay_ms=0,
            visibility_delay_ms=0,
            uncertain_ack_probability=Decimal("1"),
        )
    )
    store = SqliteExecutionStore(tmp_path / "execution.sqlite3")
    asyncio.run(store.initialize())
    service = OneLevelEntryService(
        trading=exchange,
        store=store,
        clock=lambda: NOW,
    )

    snapshot = asyncio.run(service.submit_entry(market_entry()))

    assert snapshot.state is LiveExecutionState.ENTRY_ACKNOWLEDGED
    accepted = [
        event for event in exchange.event_log if event.event_type == "ORDER_ACCEPTED"
    ]
    assert len(accepted) == 1


def test_duplicate_reordered_private_fills_are_counted_once(tmp_path) -> None:
    exchange = make_exchange(
        config(
            acknowledgement_delay_ms=0,
            visibility_delay_ms=0,
            fill_delay_ms=0,
            partial_fill_probability=Decimal("1"),
            partial_fill_fraction=Decimal("0.4"),
            duplicate_execution_probability=Decimal("1"),
            reorder_probability=Decimal("1"),
        ),
        seed=321,
    )
    store = SqliteExecutionStore(tmp_path / "execution.sqlite3")
    asyncio.run(store.initialize())
    service = OneLevelEntryService(
        trading=exchange,
        store=store,
        clock=lambda: NOW,
    )
    asyncio.run(service.submit_entry(market_entry()))
    exchange.advance_market(Decimal("2999"), elapsed_ms=1)

    for execution in exchange.drain_execution_events():
        asyncio.run(
            service.apply_execution(
                execution,
                received_at=execution.executed_at,
                payload_hash=hashlib.sha256(
                    execution.execution_id.encode()
                ).hexdigest(),
            )
        )

    snapshot = asyncio.run(store.load_entry_snapshot(ENTRY_ID))
    assert snapshot is not None
    assert snapshot.filled_quantity == Decimal("0.010")
    assert asyncio.run(store.execution_count()) == 2


def test_private_disconnect_hides_events_but_rest_history_recovers_them() -> None:
    exchange = make_exchange(
        config(
            acknowledgement_delay_ms=0,
            visibility_delay_ms=0,
            fill_delay_ms=0,
        )
    )
    exchange.set_private_connected(False)
    asyncio.run(exchange.place_order(market_entry()))

    assert exchange.drain_execution_events() == ()
    history = asyncio.run(
        exchange.get_execution_history("linear", "ETHUSDT", ENTRY_ID)
    )
    assert len(history) == 1


class FailingIntentStore(SqliteExecutionStore):
    async def persist_entry_intent(self, *args, **kwargs) -> None:
        raise sqlite3.OperationalError("injected database outage")


def test_database_failure_before_intent_blocks_exchange_mutation(tmp_path) -> None:
    exchange = make_exchange(config())
    store = FailingIntentStore(tmp_path / "execution.sqlite3")
    asyncio.run(store.initialize())
    service = OneLevelEntryService(
        trading=exchange,
        store=store,
        clock=lambda: NOW,
    )

    with pytest.raises(sqlite3.OperationalError, match="injected database outage"):
        asyncio.run(service.submit_entry(market_entry()))

    assert exchange.event_log == ()
