"""Integrated baseline-stop-recovery lifecycle on the real simulated adapter."""

from __future__ import annotations

import asyncio
import itertools
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from eth_credit_hedge.application.demo_runtime_journal import DemoRuntimeJournal
from eth_credit_hedge.application.demo_runtime_state import (
    DemoLevelRuntimeState,
    DemoRuntimeState,
)
from eth_credit_hedge.application.live_strategy_coordinator import (
    LiveStrategyCoordinator,
)
from eth_credit_hedge.application.one_level_entry import OneLevelEntryService
from eth_credit_hedge.application.one_level_lifecycle import (
    OneLevelLifecycleService,
)
from eth_credit_hedge.application.protective_exits import ProtectiveExitService
from eth_credit_hedge.application.runtime_risk_state import RuntimeRiskStateBuilder
from eth_credit_hedge.application.same_level_recovery import SameLevelRecoveryService
from eth_credit_hedge.backtesting.simulated_exchange import (
    ExecutionModelConfig,
    SimulatedExchange,
)
from eth_credit_hedge.domain.client_order_ids import (
    ClientOrderId,
    ClientOrderRole,
)
from eth_credit_hedge.domain.instruments import (
    InstrumentSpec,
    LotSizeFilter,
    PriceFilter,
)
from eth_credit_hedge.domain.journal import JournalEventType
from eth_credit_hedge.domain.live_recovery import SameLevelRecoveryPlanner
from eth_credit_hedge.domain.market_data import TriggerPriceEvent, TriggerPriceSource
from eth_credit_hedge.domain.risk import RiskEngine, RiskLimits
from eth_credit_hedge.domain.strategy_math import StopMode
from eth_credit_hedge.infrastructure.persistence.sqlite_execution_store import (
    SqliteExecutionStore,
)
from eth_credit_hedge.infrastructure.persistence.sqlite_journal_store import (
    SqliteJournalStore,
)


NOW = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)


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


def model() -> ExecutionModelConfig:
    return ExecutionModelConfig(
        acknowledgement_delay_ms=0,
        visibility_delay_ms=0,
        fill_delay_ms=0,
        partial_fill_probability=Decimal("0"),
        rejection_probability=Decimal("0"),
        maker_fee_rate=Decimal("0"),
        taker_fee_rate=Decimal("0"),
        stop_slippage_bps=Decimal("0"),
        entry_slippage_bps=Decimal("0"),
    )


def limits() -> RiskLimits:
    return RiskLimits(
        maximum_perp_quantity=Decimal("1"),
        maximum_perp_notional=Decimal("10000"),
        maximum_margin_usage=Decimal("0.5"),
        minimum_liquidation_distance=Decimal("0.1"),
        maximum_recovery_debt=Decimal("100"),
        maximum_projected_stop_loss=Decimal("100"),
        maximum_realized_cycle_loss=Decimal("100"),
        maximum_daily_realized_loss=Decimal("100"),
        maximum_entries_per_level=3,
        maximum_active_levels=3,
        maximum_order_requests_per_minute=20,
        maximum_reconciliation_failures=2,
    )


def trigger(exchange: SimulatedExchange, price: str, sequence: int) -> TriggerPriceEvent:
    return TriggerPriceEvent(
        symbol="ETHUSDT",
        source=TriggerPriceSource.LAST_TRADE,
        observed_price=Decimal(price),
        observed_timestamp=exchange.current_time_utc,
        connection_generation=1,
    )


async def wait_until(predicate: object) -> None:
    for _ in range(1000):
        if predicate():  # type: ignore[operator]
            return
        await asyncio.sleep(0.001)
    raise AssertionError("coordinator condition did not become true")


async def run_stop_recovery(path: Path) -> tuple[str, str]:
    exchange = SimulatedExchange(
        instrument=instrument(),
        initial_price=Decimal("3010"),
        config=model(),
        seed=19,
        start_time_utc=NOW,
    )
    execution_store = SqliteExecutionStore(path / "execution.sqlite3")
    journal_store = SqliteJournalStore(path / "journal.sqlite3")
    await execution_store.initialize()
    await journal_store.initialize()
    event_numbers = itertools.count(1)
    runtime_journal = await DemoRuntimeJournal.create(
        store=journal_store,
        state=DemoRuntimeState(
            cycle_id="SIM-CYCLE-1",
            short_option_symbol="ETH-31JUL26-3050-P-USDT",
            long_option_symbol="ETH-31JUL26-2950-P-USDT",
            option_quantity=Decimal("0.01"),
            levels=(
                DemoLevelRuntimeState(
                    level_id=1,
                    entry_price=Decimal("3000"),
                    take_profit_price=Decimal("2990"),
                    stop_price=Decimal("3001.5"),
                    option_budget=Decimal("0.1"),
                    stop_mode=StopMode.PRICE_STEP_FRACTION,
                    stop_parameter=Decimal("0.15"),
                ),
            ),
        ),
        clock=lambda: exchange.current_time_utc,
        event_id_factory=lambda: f"event-{next(event_numbers)}",
    )
    await runtime_journal.append(
        JournalEventType.RECONCILIATION_COMPLETED,
        payload={"status": "MATCHED"},
        event_id="reconciled",
    )

    async def advance(_: float) -> None:
        await asyncio.sleep(0)

    def entry_service() -> OneLevelEntryService:
        return OneLevelEntryService(
            trading=exchange,
            store=execution_store,
            clock=lambda: exchange.current_time_utc,
        )

    def lifecycle() -> OneLevelLifecycleService:
        entry = entry_service()
        exits = ProtectiveExitService(
            trading=exchange,
            account=exchange,
            store=execution_store,
            clock=lambda: exchange.current_time_utc,
            sleeper=advance,
            visibility_attempts=3,
            visibility_interval_seconds=0,
        )
        return OneLevelLifecycleService(
            trading=exchange,
            account=exchange,
            store=execution_store,
            entry_service=entry,
            exit_service=exits,
            instrument=instrument(),
            clock=lambda: exchange.current_time_utc,
            sleeper=advance,
            fill_attempts=10,
            fill_interval_seconds=0,
        )

    risk_engine = RiskEngine()
    recovery = SameLevelRecoveryService(
        entry_service=entry_service(),
        store=execution_store,
        planner=SameLevelRecoveryPlanner(risk_engine),
        clock=lambda: exchange.current_time_utc,
    )

    def order_id(level: int, role: ClientOrderRole, attempt: int) -> str:
        return str(
            ClientOrderId(
                strategy_instance="SIM",
                cycle=1,
                level=level,
                role=role,
                attempt=attempt,
                nonce=f"{level * 100 + attempt:04X}",
            )
        )

    async with asyncio.TaskGroup() as group:
        coordinator = LiveStrategyCoordinator(
            journal=runtime_journal,
            account=exchange,
            store=execution_store,
            instrument=instrument(),
            risk_engine=risk_engine,
            risk_limits=limits(),
            risk_state_builder=RuntimeRiskStateBuilder(
                maximum_market_data_age_ms=1000
            ),
            recovery_service=recovery,
            recovery_planner=SameLevelRecoveryPlanner(risk_engine),
            lifecycle_factory=lifecycle,
            order_link_id_factory=order_id,
            task_spawner=group.create_task,
            clock=lambda: exchange.current_time_utc,
            sleeper=advance,
            exit_poll_interval_seconds=0,
        )
        await coordinator.on_trigger(trigger(exchange, "3010", 1))
        exchange.advance_market(Decimal("2999"), elapsed_ms=1)
        baseline = await coordinator.on_trigger(trigger(exchange, "2999", 2))
        assert baseline.scheduled_levels == (1,)
        await wait_until(
            lambda: runtime_journal.state.level(1).active_quantity > 0
        )

        exchange.advance_market(Decimal("3010"), elapsed_ms=1)
        for _ in range(1000):
            stopped = runtime_journal.state.level(1)
            if stopped.confirmed_debt > 0 and stopped.active_entry_order_link_id is None:
                break
            await asyncio.sleep(0.001)
        else:
            snapshots = await execution_store.load_all_protection_snapshots()
            raise AssertionError(
                f"stop did not settle: state={stopped!r}, protection={snapshots!r}"
            )

        await coordinator.on_trigger(trigger(exchange, "3010", 3))
        exchange.advance_market(Decimal("2999"), elapsed_ms=1)
        recovery_result = await coordinator.on_trigger(
            trigger(exchange, "2999", 4)
        )
        assert recovery_result.scheduled_levels == (1,)
        await wait_until(
            lambda: runtime_journal.state.level(1).active_quantity > 0
        )
        exchange.advance_market(Decimal("2989"), elapsed_ms=1)
        for _ in range(1000):
            paid = runtime_journal.state.level(1)
            if paid.state.value == "PAID":
                break
            await asyncio.sleep(0.001)
        else:
            snapshots = await execution_store.load_all_protection_snapshots()
            raise AssertionError(
                f"recovery TP did not settle: state={paid!r}, protection={snapshots!r}"
            )

    restored = await DemoRuntimeJournal.restore(
        store=journal_store,
        cycle_id="SIM-CYCLE-1",
        clock=lambda: exchange.current_time_utc,
    )
    assert restored.state.level(1).confirmed_debt == Decimal("0")
    assert not await exchange.get_positions("linear", "ETHUSDT")
    assert not await exchange.get_open_orders("linear", "ETHUSDT")
    return exchange.event_log_digest, restored.state.level(1).state.value


def test_baseline_stop_same_level_recovery_tp_is_durable(tmp_path: Path) -> None:
    first = asyncio.run(run_stop_recovery(tmp_path / "first"))
    second = asyncio.run(run_stop_recovery(tmp_path / "second"))
    assert first == second
