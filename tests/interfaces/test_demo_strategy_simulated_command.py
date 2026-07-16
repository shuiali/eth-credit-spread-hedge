"""Command-level acceptance for the integrated demo composition root."""

from __future__ import annotations

import asyncio
import socket
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from eth_credit_hedge.application.kill_switch import KillSwitchController
from eth_credit_hedge.application.demo_strategy_runtime import (
    run_simulated_strategy_command,
)
from eth_credit_hedge.application.demo_runtime_journal import DemoRuntimeJournal
from eth_credit_hedge.application.live_strategy_coordinator import (
    LiveStrategyCoordinator,
)
from eth_credit_hedge.backtesting.simulated_exchange import (
    ExecutionModelConfig,
    SimulatedExchange,
)
from eth_credit_hedge.domain.client_order_ids import ClientOrderId, ClientOrderRole
from eth_credit_hedge.domain.control import KillSwitchMode
from eth_credit_hedge.domain.execution import PlaceOrderRequest
from eth_credit_hedge.domain.instruments import (
    InstrumentSpec,
    LotSizeFilter,
    OptionMarketQuote,
    PriceFilter,
)
from eth_credit_hedge.domain.live_option_execution import (
    OptionSpreadExecutionSnapshot,
)
from eth_credit_hedge.domain.live_execution import EntryExecutionSnapshot
from eth_credit_hedge.domain.option_position import OptionPositionState
from eth_credit_hedge.infrastructure.persistence.file_kill_switch_store import (
    FileKillSwitchStore,
)
from eth_credit_hedge.infrastructure.persistence.sqlite_execution_store import (
    SqliteExecutionStore,
)
from eth_credit_hedge.interfaces.demo_strategy_runner import parse_command


NOW = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
SHORT = "ETH-31AUG26-3010-P-USDT"
LONG = "ETH-31AUG26-2990-P-USDT"


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
            qty_step=Decimal("0.001"),
            min_order_qty=Decimal("0.001"),
            max_order_qty=Decimal("100"),
            max_market_order_qty=Decimal("50"),
            min_notional=Decimal("5"),
        ),
        contract_multiplier=Decimal("1"),
        delivery_time_utc=None,
    )


def option_quote(symbol: str, bid: str, ask: str) -> OptionMarketQuote:
    return OptionMarketQuote(
        symbol=symbol,
        timestamp_utc=NOW,
        bid_price=Decimal(bid),
        bid_size=Decimal("1"),
        ask_price=Decimal(ask),
        ask_size=Decimal("1"),
        mark_price=(Decimal(bid) + Decimal(ask)) / 2,
        underlying_price=Decimal("3000"),
        index_price=Decimal("3000"),
        bid_iv=None,
        ask_iv=None,
        mark_iv=None,
        delta=None,
        gamma=None,
        vega=None,
        theta=None,
    )


def option_snapshot() -> OptionSpreadExecutionSnapshot:
    return OptionSpreadExecutionSnapshot(
        cycle_id="SIM-CYCLE-1",
        state=OptionPositionState.OPEN,
        long_symbol=LONG,
        short_symbol=SHORT,
        expiry_time_utc=NOW + timedelta(days=30),
        requested_quantity=Decimal("0.01"),
        expected_net_credit=Decimal("0.05"),
        long_order_link_id=str(
            ClientOrderId(
                "SIM",
                1,
                0,
                ClientOrderRole.OPTION_LONG,
                1,
                "A001",
            )
        ),
        short_order_link_id=str(
            ClientOrderId(
                "SIM",
                1,
                0,
                ClientOrderRole.OPTION_SHORT,
                1,
                "A002",
            )
        ),
        long_order_id="option-long",
        short_order_id="option-short",
        long_filled_quantity=Decimal("0.01"),
        short_filled_quantity=Decimal("0.01"),
        long_notional=Decimal("0.05"),
        short_notional=Decimal("0.10"),
        long_fees=Decimal("0"),
        short_fees=Decimal("0"),
        opened_time_utc=NOW,
        version=5,
        updated_at=NOW,
    )


def exchange(
    *,
    faulted: bool = False,
    initial_wallet_balance: Decimal = Decimal("100000"),
) -> SimulatedExchange:
    return SimulatedExchange(
        instrument=instrument(),
        initial_price=Decimal("3020"),
        config=ExecutionModelConfig(
            acknowledgement_delay_ms=0,
            visibility_delay_ms=0,
            fill_delay_ms=0,
            partial_fill_probability=Decimal("1") if faulted else Decimal("0"),
            rejection_probability=Decimal("0"),
            maker_fee_rate=Decimal("0"),
            taker_fee_rate=Decimal("0"),
            stop_slippage_bps=Decimal("0"),
            entry_slippage_bps=Decimal("0"),
            duplicate_execution_probability=(
                Decimal("1") if faulted else Decimal("0")
            ),
            uncertain_ack_probability=(
                Decimal("1") if faulted else Decimal("0")
            ),
        ),
        seed=42,
        start_time_utc=NOW,
        initial_wallet_balance=initial_wallet_balance,
        option_quotes=(
            option_quote(SHORT, "10", "10.2"),
            option_quote(LONG, "5", "5.2"),
        ),
    )


class FailingEntryStore(SqliteExecutionStore):
    def __init__(self, path: Path, *, fail_after_persist: bool) -> None:
        super().__init__(path)
        self.fail_after_persist = fail_after_persist
        self.failed = False

    async def persist_entry_intent(
        self,
        request: PlaceOrderRequest,
        snapshot: EntryExecutionSnapshot,
        persisted_at: datetime,
    ) -> None:
        if self.failed:
            await super().persist_entry_intent(request, snapshot, persisted_at)
            return
        self.failed = True
        if self.fail_after_persist:
            await super().persist_entry_intent(request, snapshot, persisted_at)
        raise RuntimeError("injected entry persistence failure")


async def wait_for(predicate: object) -> None:
    for _ in range(3000):
        if predicate():  # type: ignore[operator]
            return
        await asyncio.sleep(0.001)
    raise AssertionError("simulated command condition timed out")


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def open_command(*, run_seconds: int = 1):
    return parse_command(
        [
            "run",
            "--cycle-mode",
            "OPEN_NEW",
            "--short-symbol",
            SHORT,
            "--long-symbol",
            LONG,
            "--option-quantity",
            "0.01",
            "--min-net-credit",
            "0.01",
            "--max-entry-deviation-bps",
            "100",
            "--run-seconds",
            str(run_seconds),
            "--shutdown-policy",
            "CLOSE_ALL",
            "--health-port",
            str(free_port()),
        ],
        environ={
            "ETH_HEDGE_ENVIRONMENT": "DEMO",
            "RUN_BYBIT_DEMO_MUTATIONS": "FULL_STRATEGY_DEMO",
        },
    )


async def run_once(
    path: Path,
    *,
    level_count: int = 1,
    faulted: bool = False,
    disconnected: bool = False,
) -> dict[str, object]:
    simulated = exchange(faulted=faulted)
    command = parse_command(
        [
            "run",
            "--cycle-mode",
            "OPEN_NEW",
            "--short-symbol",
            SHORT,
            "--long-symbol",
            LONG,
            "--option-quantity",
            "0.01",
            "--min-net-credit",
            "0.01",
            "--max-entry-deviation-bps",
            "100",
            "--run-seconds",
            "3",
            "--shutdown-policy",
            "CLOSE_ALL",
            "--health-port",
            str(free_port()),
        ],
        environ={
            "ETH_HEDGE_ENVIRONMENT": "DEMO",
            "RUN_BYBIT_DEMO_MUTATIONS": "FULL_STRATEGY_DEMO",
        },
    )

    async def drive(
        coordinator: LiveStrategyCoordinator,
        journal: DemoRuntimeJournal,
    ) -> None:
        del coordinator

        async def wait_runtime(predicate: object) -> None:
            if not faulted:
                await wait_for(predicate)
                return
            for _ in range(3000):
                if predicate():  # type: ignore[operator]
                    return
                simulated.advance_time(1)
                await asyncio.sleep(0.001)
            raise AssertionError("faulted runtime condition timed out")

        if disconnected:
            simulated.set_public_connected(False)
            simulated.advance_market(Decimal("3015"), elapsed_ms=1)
            simulated.advance_market(Decimal("3009"), elapsed_ms=1)
            simulated.set_public_connected(True)
            simulated.advance_market(Decimal("3009"), elapsed_ms=1)
            await asyncio.sleep(0.05)
            assert journal.state.level(1).attempts == 0
        simulated.advance_market(Decimal("3020"), elapsed_ms=1)
        await wait_runtime(lambda: journal.state.level(1).armed)
        simulated.advance_market(Decimal("3009"), elapsed_ms=1)
        await wait_runtime(lambda: journal.state.level(1).active_quantity > 0)
        if disconnected:
            simulated.set_private_connected(False)
        simulated.advance_market(Decimal("3020"), elapsed_ms=1)
        await wait_runtime(
            lambda: journal.state.level(1).confirmed_debt > 0
            and journal.state.level(1).active_entry_order_link_id is None
        )
        if disconnected:
            simulated.set_private_connected(True)
        simulated.advance_market(Decimal("3020"), elapsed_ms=1)
        await wait_runtime(lambda: journal.state.level(1).armed)
        simulated.advance_market(Decimal("3009"), elapsed_ms=1)
        await wait_runtime(
            lambda: journal.state.level(1).attempts == 2
            and journal.state.level(1).active_quantity > 0
        )
        simulated.advance_market(Decimal("2989"), elapsed_ms=1)
        await wait_runtime(lambda: journal.state.level(1).state.value == "PAID")

    async def drive_multiple(
        coordinator: LiveStrategyCoordinator,
        journal: DemoRuntimeJournal,
    ) -> None:
        del coordinator
        simulated.advance_market(Decimal("3020"), elapsed_ms=1)
        await wait_for(lambda: all(level.armed for level in journal.state.levels))
        simulated.advance_market(Decimal("3009"), elapsed_ms=1)
        await wait_for(
            lambda: journal.state.level(1).active_quantity > 0
        )
        simulated.advance_market(Decimal("2999"), elapsed_ms=1)
        await wait_for(lambda: journal.state.level(1).state.value == "PAID")
        simulated.advance_market(Decimal("2999"), elapsed_ms=1)
        await wait_for(lambda: journal.state.level(2).active_quantity > 0)
        simulated.advance_market(Decimal("2989"), elapsed_ms=1)
        await wait_for(
            lambda: all(level.state.value == "PAID" for level in journal.state.levels)
        )

    return await run_simulated_strategy_command(
        command,
        exchange=simulated,
        option=option_snapshot(),
        state_directory=path,
        price_driver=drive if level_count == 1 else drive_multiple,
        level_count=level_count,
    )


def test_real_command_composition_is_deterministic_and_flat(tmp_path: Path) -> None:
    first = asyncio.run(run_once(tmp_path / "first"))
    second = asyncio.run(run_once(tmp_path / "second"))
    assert first["event_log"] == second["event_log"]
    assert first["event_digest"] == second["event_digest"]
    assert first["final_level_states"] == ["PAID"]
    assert Decimal(str(first["final_recovery_debt"])) == Decimal("0")
    assert first["final_linear_position_count"] == 0
    assert first["final_linear_order_count"] == 0


def test_real_command_composition_runs_multiple_baseline_levels(
    tmp_path: Path,
) -> None:
    result = asyncio.run(run_once(tmp_path / "multiple", level_count=2))
    assert result["final_level_states"] == ["PAID", "PAID"]
    assert result["final_linear_position_count"] == 0


def test_command_converges_with_partial_duplicate_and_uncertain_events(
    tmp_path: Path,
) -> None:
    result = asyncio.run(run_once(tmp_path / "faulted", faulted=True))
    assert result["final_level_states"] == ["PAID"]
    assert result["final_recovery_debt"] in {"0", "0.000", "0.00"}
    assert result["final_linear_position_count"] == 0


def test_command_fences_public_reconnect_and_recovers_missed_private_fill(
    tmp_path: Path,
) -> None:
    result = asyncio.run(
        run_once(tmp_path / "disconnect", disconnected=True)
    )
    assert result["final_level_states"] == ["PAID"]
    assert result["final_linear_position_count"] == 0


def test_command_restart_restores_live_protection_and_exit(tmp_path: Path) -> None:
    async def exercise() -> None:
        simulated = exchange()
        open_command = parse_command(
            [
                "run",
                "--cycle-mode",
                "OPEN_NEW",
                "--short-symbol",
                SHORT,
                "--long-symbol",
                LONG,
                "--option-quantity",
                "0.01",
                "--min-net-credit",
                "0.01",
                "--max-entry-deviation-bps",
                "100",
                "--run-seconds",
                "1",
                "--health-port",
                str(free_port()),
            ],
            environ={
                "ETH_HEDGE_ENVIRONMENT": "DEMO",
                "RUN_BYBIT_DEMO_MUTATIONS": "FULL_STRATEGY_DEMO",
            },
        )

        async def open_and_hold(
            coordinator: LiveStrategyCoordinator,
            journal: DemoRuntimeJournal,
        ) -> None:
            del coordinator
            simulated.advance_market(Decimal("3020"), elapsed_ms=1)
            await wait_for(lambda: journal.state.level(1).armed)
            simulated.advance_market(Decimal("3009"), elapsed_ms=1)
            await wait_for(lambda: journal.state.level(1).active_quantity > 0)

        first = await run_simulated_strategy_command(
            open_command,
            exchange=simulated,
            option=option_snapshot(),
            state_directory=tmp_path / "restart",
            price_driver=open_and_hold,
            preserve_state_on_timeout=True,
        )
        assert first["final_linear_position_count"] == 1
        assert first["final_linear_order_count"] == 2

        restore_command = parse_command(
            [
                "run",
                "--cycle-mode",
                "RESTORE_ONLY",
                "--cycle-id",
                "SIM-CYCLE-1",
                "--run-seconds",
                "2",
                "--health-port",
                str(free_port()),
            ],
            environ={
                "ETH_HEDGE_ENVIRONMENT": "DEMO",
                "RUN_BYBIT_DEMO_MUTATIONS": "FULL_STRATEGY_DEMO",
            },
        )

        async def close_after_restart(
            coordinator: LiveStrategyCoordinator,
            journal: DemoRuntimeJournal,
        ) -> None:
            del coordinator
            await wait_for(lambda: journal.state.level(1).active_quantity > 0)
            await wait_for(
                lambda: any(
                    event_id.startswith("sim-event-100")
                    for event_id in journal.state.processed_event_ids
                )
            )
            simulated.advance_market(Decimal("2989"), elapsed_ms=1)
            await wait_for(lambda: journal.state.level(1).state.value == "PAID")

        restored = await run_simulated_strategy_command(
            restore_command,
            exchange=simulated,
            option=option_snapshot(),
            state_directory=tmp_path / "restart",
            price_driver=close_after_restart,
        )
        assert restored["final_level_states"] == ["PAID"]
        assert restored["final_linear_position_count"] == 0
        assert restored["final_linear_order_count"] == 0

    asyncio.run(exercise())


def test_timer_uses_shared_close_path_for_live_hedge(tmp_path: Path) -> None:
    async def exercise() -> None:
        simulated = exchange()
        command = parse_command(
            [
                "run",
                "--cycle-mode",
                "OPEN_NEW",
                "--short-symbol",
                SHORT,
                "--long-symbol",
                LONG,
                "--option-quantity",
                "0.01",
                "--min-net-credit",
                "0.01",
                "--max-entry-deviation-bps",
                "100",
                "--run-seconds",
                "1",
                "--health-port",
                str(free_port()),
            ],
            environ={
                "ETH_HEDGE_ENVIRONMENT": "DEMO",
                "RUN_BYBIT_DEMO_MUTATIONS": "FULL_STRATEGY_DEMO",
            },
        )

        async def open_and_wait_for_timer(
            coordinator: LiveStrategyCoordinator,
            journal: DemoRuntimeJournal,
        ) -> None:
            del coordinator
            simulated.advance_market(Decimal("3020"), elapsed_ms=1)
            await wait_for(lambda: journal.state.level(1).armed)
            simulated.advance_market(Decimal("3009"), elapsed_ms=1)
            await wait_for(lambda: journal.state.level(1).active_quantity > 0)

        result = await run_simulated_strategy_command(
            command,
            exchange=simulated,
            option=option_snapshot(),
            state_directory=tmp_path / "timer-close",
            price_driver=open_and_wait_for_timer,
        )
        assert result["close_verified"] is True
        assert result["final_linear_position_count"] == 0
        assert result["final_linear_order_count"] == 0

    asyncio.run(exercise())


@pytest.mark.parametrize("fail_after_persist", [False, True])
def test_database_failure_before_or_after_intent_closes_safely(
    tmp_path: Path,
    fail_after_persist: bool,
) -> None:
    async def exercise() -> None:
        simulated = exchange()
        state_directory = tmp_path / f"database-{fail_after_persist}"
        store = FailingEntryStore(
            state_directory / "execution.sqlite3",
            fail_after_persist=fail_after_persist,
        )

        async def trigger_entry_failure(
            coordinator: LiveStrategyCoordinator,
            journal: DemoRuntimeJournal,
        ) -> None:
            del coordinator
            simulated.advance_market(Decimal("3020"), elapsed_ms=1)
            await wait_for(lambda: journal.state.level(1).armed)
            simulated.advance_market(Decimal("3009"), elapsed_ms=1)
            await asyncio.Event().wait()

        with pytest.raises(RuntimeError, match="failed and was closed"):
            await run_simulated_strategy_command(
                open_command(),
                exchange=simulated,
                option=option_snapshot(),
                state_directory=state_directory,
                price_driver=trigger_entry_failure,
                execution_store_override=store,
            )
        assert not await simulated.get_positions("linear", "ETHUSDT")
        assert not await simulated.get_open_orders("linear", "ETHUSDT")
        assert len(await store.load_all_order_intents()) == int(fail_after_persist)

    asyncio.run(exercise())


def test_authoritative_risk_veto_prevents_entry(tmp_path: Path) -> None:
    async def exercise() -> None:
        simulated = exchange(initial_wallet_balance=Decimal("1"))

        async def cross_with_insufficient_margin(
            coordinator: LiveStrategyCoordinator,
            journal: DemoRuntimeJournal,
        ) -> None:
            del coordinator
            simulated.advance_market(Decimal("3020"), elapsed_ms=1)
            await wait_for(lambda: journal.state.level(1).armed)
            simulated.advance_market(Decimal("3009"), elapsed_ms=1)
            await asyncio.sleep(0.05)
            assert journal.state.level(1).attempts == 0

        result = await run_simulated_strategy_command(
            open_command(),
            exchange=simulated,
            option=option_snapshot(),
            state_directory=tmp_path / "risk-veto",
            price_driver=cross_with_insufficient_margin,
        )
        assert result["final_level_attempts"] == [0]
        assert result["final_linear_position_count"] == 0

    asyncio.run(exercise())


def test_persisted_kill_switch_vetoes_simulated_startup(tmp_path: Path) -> None:
    async def exercise() -> None:
        state_directory = tmp_path / "kill-veto"
        controller = KillSwitchController(
            store=FileKillSwitchStore(state_directory / "kill-switch.json"),
            clock=lambda: NOW,
        )
        await controller.initialize()
        await controller.activate(
            KillSwitchMode.SOFT_PAUSE,
            reason="operator pause",
            requested_by="test",
        )
        simulated = exchange()

        async def unused_driver(
            coordinator: LiveStrategyCoordinator,
            journal: DemoRuntimeJournal,
        ) -> None:
            del coordinator, journal

        with pytest.raises(RuntimeError, match="kill switch is not RUNNING"):
            await run_simulated_strategy_command(
                open_command(),
                exchange=simulated,
                option=option_snapshot(),
                state_directory=state_directory,
                price_driver=unused_driver,
            )
        assert not simulated.event_log

    asyncio.run(exercise())


def test_direct_runtime_cancellation_uses_shared_close_path(tmp_path: Path) -> None:
    async def exercise() -> None:
        simulated = exchange()
        opened = asyncio.Event()

        async def open_and_hold(
            coordinator: LiveStrategyCoordinator,
            journal: DemoRuntimeJournal,
        ) -> None:
            del coordinator
            simulated.advance_market(Decimal("3020"), elapsed_ms=1)
            await wait_for(lambda: journal.state.level(1).armed)
            simulated.advance_market(Decimal("3009"), elapsed_ms=1)
            await wait_for(lambda: journal.state.level(1).active_quantity > 0)
            opened.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(
            run_simulated_strategy_command(
                open_command(run_seconds=30),
                exchange=simulated,
                option=option_snapshot(),
                state_directory=tmp_path / "cancel-close",
                price_driver=open_and_hold,
            )
        )
        await asyncio.wait_for(opened.wait(), timeout=2)
        task.cancel()
        with pytest.raises(RuntimeError, match="failed and was closed"):
            await task
        assert not await simulated.get_positions("linear", "ETHUSDT")
        assert not await simulated.get_open_orders("linear", "ETHUSDT")

    asyncio.run(exercise())
