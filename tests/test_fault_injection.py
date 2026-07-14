"""Deterministic process-crash and public-network fault scenarios."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from eth_credit_hedge.application.one_level_entry import OneLevelEntryService
from eth_credit_hedge.backtesting.fault_injection import (
    FaultInjector,
    FaultRule,
    InjectedProcessCrash,
)
from eth_credit_hedge.domain.execution import LiveExecutionState
from eth_credit_hedge.domain.execution import PlaceOrderRequest
from eth_credit_hedge.domain.instruments import (
    InstrumentSpec,
    LotSizeFilter,
    PriceFilter,
)
from eth_credit_hedge.backtesting.simulated_exchange import (
    ExecutionModelConfig,
    SimulatedExchange,
)
from eth_credit_hedge.infrastructure.persistence.sqlite_execution_store import (
    SqliteExecutionStore,
)


NOW = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
ENTRY_ID = "ECH-01-C0001-L01-ENTRY-A01-A001"


def make_exchange(
    *,
    fault_injector: FaultInjector | None = None,
) -> SimulatedExchange:
    return SimulatedExchange(
        instrument=InstrumentSpec(
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
        ),
        initial_price=Decimal("3000"),
        config=ExecutionModelConfig(
            acknowledgement_delay_ms=0,
            visibility_delay_ms=0,
            fill_delay_ms=100,
            partial_fill_probability=Decimal("0"),
            rejection_probability=Decimal("0"),
            maker_fee_rate=Decimal("0.0002"),
            taker_fee_rate=Decimal("0.00055"),
            stop_slippage_bps=Decimal("0"),
            entry_slippage_bps=Decimal("0"),
        ),
        seed=4,
        start_time_utc=NOW,
        fault_injector=fault_injector,
    )


def market_entry() -> PlaceOrderRequest:
    return PlaceOrderRequest(
        category="linear",
        symbol="ETHUSDT",
        side="Sell",
        order_type="Market",
        quantity=Decimal("0.010"),
        order_link_id=ENTRY_ID,
        time_in_force="IOC",
    )


def test_fault_rule_fires_only_on_declared_occurrence() -> None:
    injector = FaultInjector((FaultRule("checkpoint", occurrence=2),))

    injector.checkpoint("checkpoint")
    with pytest.raises(InjectedProcessCrash, match="checkpoint"):
        injector.checkpoint("checkpoint")
    injector.checkpoint("checkpoint")


def test_crash_after_exchange_acceptance_leaves_recoverable_persisted_intent(
    tmp_path,
) -> None:
    injector = FaultInjector((FaultRule("after_order_accepted"),))
    exchange = make_exchange(fault_injector=injector)
    store = SqliteExecutionStore(tmp_path / "execution.sqlite3")
    asyncio.run(store.initialize())
    service = OneLevelEntryService(
        trading=exchange,
        store=store,
        clock=lambda: exchange.current_time_utc,
    )

    with pytest.raises(InjectedProcessCrash):
        asyncio.run(service.submit_entry(market_entry()))

    snapshot = asyncio.run(store.load_entry_snapshot(ENTRY_ID))
    discovered = asyncio.run(
        exchange.get_order_by_link_id("linear", "ETHUSDT", ENTRY_ID)
    )
    assert snapshot is not None
    assert snapshot.state is LiveExecutionState.ENTRY_SUBMITTED
    assert discovered is not None
    assert len([event for event in exchange.event_log if event.event_type == "ORDER_ACCEPTED"]) == 1


def test_public_disconnect_creates_stale_interval_and_outage_gap() -> None:
    exchange = make_exchange()

    exchange.set_public_connected(False)
    exchange.advance_market(Decimal("2800"), elapsed_ms=2500)
    exchange.set_public_connected(True)
    exchange.advance_market(Decimal("2750"), elapsed_ms=1)

    assert exchange.metrics.maximum_stale_duration_ms == 2500
    assert exchange.metrics.public_disconnect_count == 1
    assert exchange.latest_published_price == Decimal("2750")
