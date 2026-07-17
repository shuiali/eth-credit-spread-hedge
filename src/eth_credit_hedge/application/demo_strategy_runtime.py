"""Composition root for the integrated, bounded Bybit demo strategy."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from threading import Thread
from typing import Any, Protocol, TYPE_CHECKING

from eth_credit_hedge.application.demo_runtime_journal import DemoRuntimeJournal
from eth_credit_hedge.application.accounting_runtime import AccountingRuntime
from eth_credit_hedge.application.private_execution_accounting import (
    PrivateExecutionClassifier,
)
from eth_credit_hedge.application.demo_runtime_state import (
    DemoLevelRuntimeState,
    DemoRuntimeState,
)
from eth_credit_hedge.application.kill_switch import (
    KillSwitchController,
    StrategyCloseService,
)
from eth_credit_hedge.application.live_strategy_coordinator import (
    LiveStrategyCoordinator,
)
from eth_credit_hedge.application.one_level_entry import OneLevelEntryService
from eth_credit_hedge.application.one_level_lifecycle import (
    OneLevelLifecycleService,
)
from eth_credit_hedge.application.operational_state import MutableOperationalState
from eth_credit_hedge.application.option_spread_entry import (
    OptionSpreadEntryPlan,
    OptionSpreadEntryService,
)
from eth_credit_hedge.application.protective_exits import ProtectiveExitService
from eth_credit_hedge.application.read_only_reconciliation import (
    BybitPrivateStateReader,
    PrivateAccountSnapshot,
    evaluate_private_snapshot,
)
from eth_credit_hedge.application.runtime_risk_state import RuntimeRiskStateBuilder
from eth_credit_hedge.application.same_level_recovery import SameLevelRecoveryService
from eth_credit_hedge.config.bybit import load_bybit_demo_profile
from eth_credit_hedge.config.schema import RuntimeConfig, StrategyCostConfig
from eth_credit_hedge.core.credit_spread import CreditSpread
from eth_credit_hedge.core.virtual_levels import build_virtual_levels
from eth_credit_hedge.domain.client_order_ids import ClientOrderId, ClientOrderRole
from eth_credit_hedge.domain.control import KillSwitchMode
from eth_credit_hedge.domain.execution import (
    ExecutionUpdate,
    ExecutionUpdateBatch,
    PrivateConnectionEvent,
    PrivateConnectionState,
)
from eth_credit_hedge.domain.accounting.events import EventSource, OptionQuoteRecorded
from eth_credit_hedge.domain.accounting.reconstruction import (
    CombinedLedgerReconstructor,
    CombinedLedgerState,
)
from eth_credit_hedge.domain.accounting.reconciliation import AccountingExchangeState
from eth_credit_hedge.domain.strategy_math.units import Money, Price, Quantity
from eth_credit_hedge.domain.instrument_rules import (
    PriceQuantizationPolicy,
    quantize_limit_price,
)
from eth_credit_hedge.domain.instruments import InstrumentSpec, OptionMarketQuote
from eth_credit_hedge.domain.journal import JournalEventType
from eth_credit_hedge.domain.live_recovery import SameLevelRecoveryPlanner
from eth_credit_hedge.domain.market_data import (
    TriggerPriceRouter,
    TriggerPriceSource,
)
from eth_credit_hedge.domain.option_lifecycle import (
    OptionEntryPolicy,
    UnmatchedLongPolicy,
)
from eth_credit_hedge.domain.option_position import OptionPositionState
from eth_credit_hedge.domain.protected_execution import ProtectionSnapshot
from eth_credit_hedge.domain.risk import RiskEngine
from eth_credit_hedge.domain.strategy_math import (
    ExpirationOptionValuation,
    StopConfig,
    StrategyMathEngine,
)
from eth_credit_hedge.infrastructure.bybit.auth import BybitV5Signer
from eth_credit_hedge.infrastructure.bybit.clock import ServerClock
from eth_credit_hedge.infrastructure.bybit.demo_strategy_close import (
    DemoStrategyCloseOperations,
)
from eth_credit_hedge.infrastructure.bybit.private_rest import BybitPrivateRestClient
from eth_credit_hedge.infrastructure.bybit.private_ws import (
    BybitPrivateWebSocketClient,
)
from eth_credit_hedge.infrastructure.bybit.public_market_data import (
    BybitPublicMarketData,
)
from eth_credit_hedge.infrastructure.bybit.public_rest import BybitPublicRestClient
from eth_credit_hedge.infrastructure.monitoring.alerts import (
    AlertDispatcher,
    AlertObservation,
    AlertPolicy,
)
from eth_credit_hedge.infrastructure.monitoring.structured_logging import (
    SecretSafeJsonLogger,
    StructuredLogEvent,
)
from eth_credit_hedge.infrastructure.persistence.file_kill_switch_store import (
    FileKillSwitchStore,
)
from eth_credit_hedge.infrastructure.persistence.sqlite_execution_store import (
    SqliteExecutionStore,
)
from eth_credit_hedge.infrastructure.persistence.sqlite_accounting_store import (
    SqliteAccountingStore,
)
from eth_credit_hedge.infrastructure.persistence.sqlite_journal_store import (
    SqliteJournalStore,
)
from eth_credit_hedge.interfaces.demo_bootstrap import (
    _demo_deployment_profile,
    _expected_durable_positions,
    _journal_path,
    _kill_switch_path,
    run_demo_preflight,
)
from eth_credit_hedge.interfaces.health_api import HealthApi, create_health_server
from eth_credit_hedge.ports.notifications import NotificationPort
from eth_credit_hedge.ports.private_events import PrivateEventPort, PrivateStreamEvent

if TYPE_CHECKING:
    from eth_credit_hedge.domain.live_option_execution import (
        OptionSpreadExecutionSnapshot,
    )
    from eth_credit_hedge.interfaces.demo_strategy_runner import DemoStrategyCommand


ZERO = Decimal("0")
BPS = Decimal("10000")


class _ProtectionSnapshotReader(Protocol):
    async def load_all_protection_snapshots(
        self,
    ) -> tuple[ProtectionSnapshot, ...]: ...


class _CapturedAccountingReader:
    def __init__(
        self,
        snapshot: PrivateAccountSnapshot,
        *,
        cycle_number: int,
        strategy_instance: str,
        funding_pnl: Money,
    ) -> None:
        owned = tuple(
            execution
            for execution in snapshot.executions
            if _belongs_to_cycle(
                execution.order_link_id,
                cycle_number=cycle_number,
                strategy_instance=strategy_instance,
            )
        )
        option_totals: dict[str, Decimal] = {}
        hedge_short = ZERO
        for position in snapshot.positions:
            if position.quantity <= ZERO:
                continue
            if position.category == "option":
                option_totals[position.symbol] = (
                    option_totals.get(position.symbol, ZERO) + position.quantity
                )
            elif position.side == "Sell":
                hedge_short += position.quantity
        self._state = AccountingExchangeState(
            option_quantities={
                symbol: Quantity(quantity)
                for symbol, quantity in option_totals.items()
            },
            hedge_short_quantity=hedge_short,
            total_fees=Money(sum((execution.fee for execution in owned), ZERO)),
            funding_pnl=funding_pnl,
            order_ids=frozenset(execution.order_id for execution in owned),
            execution_ids=frozenset(execution.execution_id for execution in owned),
        )

    async def capture_accounting_state(self) -> AccountingExchangeState:
        return self._state


STRATEGY_INSTANCE = "DEMO"
OPTION_HEDGE_CUTOFF = timedelta(hours=24)


class _AlertLogger(NotificationPort):
    def __init__(self, logger: SecretSafeJsonLogger, cycle_id: str) -> None:
        self._logger = logger
        self._cycle_id = cycle_id

    async def send(self, notification: object) -> None:
        code = str(getattr(notification, "code", type(notification).__name__))
        message = str(getattr(notification, "message", "runtime alert"))
        self._logger.write(
            StructuredLogEvent(
                timestamp=datetime.now(timezone.utc),
                service="demo-strategy",
                cycle_id=self._cycle_id,
                level_id=None,
                client_order_id=None,
                exchange_order_id=None,
                execution_id=None,
                correlation_id=self._cycle_id,
                event=f"alert:{code}",
                message=message,
            )
        )


class _SimulatedPrivateStream:
    def __init__(self, clock: Callable[[], datetime]) -> None:
        self._clock = clock
        self.new_entries_blocked = True
        self._reconciled = asyncio.Event()

    async def stream_events(self) -> AsyncIterator[PrivateStreamEvent]:
        yield PrivateConnectionEvent(
            state=PrivateConnectionState.AUTHENTICATED,
            observed_at=self._clock(),
            connection_generation=1,
        )
        await asyncio.Event().wait()

    def mark_reconciled(self, connection_generation: int) -> None:
        if connection_generation != 1:
            raise ValueError("simulated private generation is stale")
        self.new_entries_blocked = False
        self._reconciled.set()


class _RuntimeEntryGate:
    def __init__(
        self,
        kill_switch: KillSwitchController,
        private_stream: PrivateEventPort,
    ) -> None:
        self._kill_switch = kill_switch
        self._private_stream = private_stream

    @property
    def entries_allowed(self) -> bool:
        return (
            self._kill_switch.entries_allowed
            and not self._private_stream.new_entries_blocked
        )


async def run_demo_strategy(command: DemoStrategyCommand) -> dict[str, object]:
    preflight = await run_demo_preflight(command)
    deployment = _demo_deployment_profile()
    runtime_config = RuntimeConfig.from_env()
    math_engine = StrategyMathEngine(ExpirationOptionValuation())
    profile = load_bybit_demo_profile()
    clock = ServerClock(max_absolute_offset_ms=deployment.maximum_clock_drift_ms)
    private = BybitPrivateRestClient(profile=profile, clock=clock)
    public_rest = BybitPublicRestClient()
    market_data = BybitPublicMarketData(rest=public_rest)
    private_stream = BybitPrivateWebSocketClient(
        signer=BybitV5Signer(profile.credentials)
    )
    await private.synchronize_clock()
    execution_store = SqliteExecutionStore(deployment.database_path)
    journal_store = SqliteJournalStore(_journal_path(deployment.database_path))
    await execution_store.initialize()
    await journal_store.initialize()
    def now() -> datetime:
        return datetime.fromtimestamp(clock.timestamp_ms() / 1000, timezone.utc)
    kill_switch = KillSwitchController(
        store=FileKillSwitchStore(_kill_switch_path(deployment.database_path)),
        clock=now,
    )
    kill_state = await kill_switch.initialize()
    if kill_state.mode is not KillSwitchMode.RUNNING:
        raise RuntimeError("demo strategy kill switch is not RUNNING")

    option_instruments = await public_rest.list_instruments(
        "option",
        base_coin="ETH",
    )
    if command.cycle_mode.value == "OPEN_NEW":
        try:
            journal, option, cycle_number = await _open_new_cycle(
                command=command,
                runtime_config=runtime_config,
                private=private,
                public=public_rest,
                store=execution_store,
                journal_store=journal_store,
                option_instruments=option_instruments,
                clock=now,
            )
        except BaseException:
            await _close_failed_option_entry(
                command=command,
                private=private,
                market_data=market_data,
                store=execution_store,
                option_instruments=option_instruments,
                clock=now,
            )
            raise
    else:
        if command.cycle_id is None:
            raise AssertionError("validated RESTORE_ONLY command requires cycle ID")
        journal = await DemoRuntimeJournal.restore(
            store=journal_store,
            cycle_id=command.cycle_id,
            clock=now,
        )
        restored_option = await execution_store.load_option_spread_snapshot(
            command.cycle_id
        )
        if restored_option is None or restored_option.state is not OptionPositionState.OPEN:
            raise RuntimeError("RESTORE_ONLY requires one exact OPEN option spread")
        option = restored_option
        cycle_number = ClientOrderId.parse(option.long_order_link_id).cycle

    instrument = await public_rest.get_instrument("ETHUSDT")
    matched = await _reconcile_runtime(journal, execution_store, private, now)
    if not matched:
        raise RuntimeError("post-option startup reconciliation did not match")

    accounting = await _initialize_accounting_runtime(
        execution_store=execution_store,
        accounting_path=_accounting_path(deployment.database_path),
        option=option,
        cycle_number=cycle_number,
        strategy_instance=STRATEGY_INSTANCE,
        option_quotes=await market_data.get_option_chain("ETH"),
        clock=now,
    )
    if not await _reconcile_accounting_runtime(
        accounting=accounting,
        private=private,
        cycle_number=cycle_number,
        strategy_instance=STRATEGY_INSTANCE,
        clock=now,
    ):
        raise RuntimeError("startup accounting reconciliation did not match")

    secrets = (
        profile.credentials.api_key.get_secret_value(),
        profile.credentials.api_secret.get_secret_value(),
    )
    logger = SecretSafeJsonLogger(sys.stderr, secrets=secrets)
    logger.write(
        StructuredLogEvent(
            timestamp=now(),
            service="demo-strategy",
            cycle_id=journal.state.cycle_id,
            level_id=None,
            client_order_id=None,
            exchange_order_id=None,
            execution_id=None,
            correlation_id=journal.state.cycle_id,
            event="runtime_started",
            message="integrated Bybit demo runtime started",
        )
    )

    close_operations = DemoStrategyCloseOperations(
        trading=private,
        account=private,
        store=execution_store,
        quotes=market_data,
        option_instruments=option_instruments,
        cycle_id=option.cycle_id,
        short_symbol=option.short_symbol,
        long_symbol=option.long_symbol,
        order_link_id_factory=lambda role, attempt: _order_id(
            cycle_number,
            0,
            role,
            attempt,
        ),
        clock=now,
    )
    operations = MutableOperationalState(
        maximum_market_data_age_ms=deployment.maximum_market_data_age_ms,
        clock=now,
    )
    operations.update_runtime(journal.state)
    operations.update_accounting(accounting.state)
    operations.mark_running(True)
    operations.mark_reconciliation(True, "MATCHED")
    try:
        health_server = create_health_server(
            HealthApi(operations.snapshot),
            host=command.health_host,
            port=command.health_port,
        )
    except BaseException:
        await StrategyCloseService(
            controller=kill_switch,
            trading=private,
            operations=close_operations,
        ).close(
            reason="health server failed to start",
            requested_by="demo-runtime",
        )
        raise
    health_thread = Thread(target=health_server.serve_forever, daemon=True)
    health_thread.start()

    runtime_error: BaseException | None = None
    try:
        await _run_supervised_session(
            command=command,
            journal=journal,
            option=option,
            cycle_number=cycle_number,
            private=private,
            private_stream=private_stream,
            market_data=market_data,
            instrument=instrument,
            store=execution_store,
            deployment=deployment,
            kill_switch=kill_switch,
            operations=operations,
            logger=logger,
            clock=now,
            clock_refresh=private.synchronize_clock,
            costs=runtime_config.strategy.costs,
            math_engine=math_engine,
            accounting=accounting,
            accounting_strategy_instance=STRATEGY_INSTANCE,
        )
    except BaseException as exc:
        runtime_error = exc
    finally:
        operations.mark_running(False)
        health_server.shutdown()
        health_server.server_close()
        health_thread.join(timeout=5)

    try:
        await private.synchronize_clock()
    except BaseException as clock_error:
        if runtime_error is None:
            runtime_error = clock_error

    close_verified = False
    try:
        if command.shutdown_policy.value == "CLOSE_ALL":
            close_result = await asyncio.shield(
                StrategyCloseService(
                    controller=kill_switch,
                    trading=private,
                    operations=close_operations,
                ).close(
                    reason=(
                        "bounded runtime completed"
                        if runtime_error is None
                        else "runtime task failed"
                    ),
                    requested_by="demo-runtime",
                )
            )
            close_verified = close_result.verified_closed
        else:
            await kill_switch.activate(
                KillSwitchMode.STRATEGY_CLOSE,
                reason="bounded runtime left matched option protection",
                requested_by="demo-runtime",
            )
            close_verified = await asyncio.shield(
                close_operations.leave_option_protected()
            )
    except BaseException as close_error:
        if runtime_error is None:
            runtime_error = close_error
    if not close_verified:
        raise RuntimeError("bounded demo shutdown could not be proven safe") from runtime_error

    await _recover_confirmed_executions(
        accounting=accounting,
        execution_store=execution_store,
        cycle_id=option.cycle_id,
        cycle_number=cycle_number,
        strategy_instance=STRATEGY_INSTANCE,
        clock=now,
    )
    if not await _reconcile_accounting_runtime(
        accounting=accounting,
        private=private,
        cycle_number=cycle_number,
        strategy_instance=STRATEGY_INSTANCE,
        clock=now,
    ):
        raise RuntimeError("shutdown accounting reconciliation did not match")

    final_reader = BybitPrivateStateReader(
        trading=private,
        account=private,
        clock=now,
    )
    final_exchange = await final_reader.capture()
    _assert_shutdown_accounting(
        accounting.state,
        options_must_be_flat=command.shutdown_policy.value == "CLOSE_ALL",
    )
    await accounting.record_shutdown_snapshot(
        cycle_id=option.cycle_id,
        timestamp=now(),
    )
    operations.update_accounting(accounting.state)
    payload: dict[str, object] = {
        "accepted": runtime_error is None,
        "action": "run",
        "cycle_id": journal.state.cycle_id,
        "shutdown_policy": command.shutdown_policy.value,
        "close_verified": close_verified,
        "final_linear_position_count": sum(
            position.category == "linear" and position.quantity > ZERO
            for position in final_exchange.positions
        ),
        "final_option_position_count": sum(
            position.category == "option" and position.quantity > ZERO
            for position in final_exchange.positions
        ),
        "preflight_evidence_sha256": preflight.get("evidence_sha256"),
        "accounting": accounting.state.to_dict(),
    }
    evidence = _write_runtime_evidence(payload)
    payload["evidence_path"] = str(evidence)
    payload["evidence_sha256"] = hashlib.sha256(evidence.read_bytes()).hexdigest()
    if runtime_error is not None:
        raise RuntimeError("integrated demo runtime failed and was closed") from runtime_error
    return payload


async def run_simulated_strategy_command(
    command: DemoStrategyCommand,
    *,
    exchange: Any,
    option: OptionSpreadExecutionSnapshot,
    state_directory: Path,
    price_driver: Callable[
        [LiveStrategyCoordinator, DemoRuntimeJournal],
        Coroutine[Any, Any, None],
    ],
    level_count: int = 1,
    stop: StopConfig | None = None,
    preserve_state_on_timeout: bool = False,
    execution_store_override: SqliteExecutionStore | None = None,
) -> dict[str, object]:
    """Run the production composition against an injected simulated adapter."""

    if command.action != "run":
        raise ValueError("simulated strategy acceptance requires the run command")
    if level_count <= 0:
        raise ValueError("simulated level count must be positive")
    math_engine = StrategyMathEngine(ExpirationOptionValuation())
    deployment = _demo_deployment_profile()
    execution_store = execution_store_override or SqliteExecutionStore(
        state_directory / "execution.sqlite3"
    )
    journal_store = SqliteJournalStore(state_directory / "journal.sqlite3")
    await execution_store.initialize()
    await journal_store.initialize()
    kill_switch = KillSwitchController(
        store=FileKillSwitchStore(state_directory / "kill-switch.json"),
        clock=lambda: exchange.current_time_utc,
    )
    kill_state = await kill_switch.initialize()
    if kill_state.mode is not KillSwitchMode.RUNNING:
        raise RuntimeError("simulated strategy kill switch is not RUNNING")
    if command.cycle_mode.value == "RESTORE_ONLY":
        if command.cycle_id != option.cycle_id:
            raise ValueError("simulated restore cycle does not match option fixture")
        journal = await DemoRuntimeJournal.restore(
            store=journal_store,
            cycle_id=option.cycle_id,
            clock=lambda: exchange.current_time_utc,
            event_id_factory=_deterministic_event_id_factory(100_000),
        )
    else:
        spread = CreditSpread(
            spot=Decimal("3000"),
            short_put_strike=_strike(option.short_symbol),
            long_put_strike=_strike(option.long_symbol),
            option_quantity=option.matched_quantity,
            premium_credit=option.actual_net_credit,
        )
        levels = build_virtual_levels(
            spread,
            level_count,
            stop,
            math_engine=math_engine,
        )
        journal = await DemoRuntimeJournal.create(
            store=journal_store,
            state=DemoRuntimeState(
                cycle_id=option.cycle_id,
                short_option_symbol=option.short_symbol,
                long_option_symbol=option.long_symbol,
                option_quantity=option.matched_quantity,
                levels=tuple(
                    DemoLevelRuntimeState.from_level(level) for level in levels
                ),
            ),
            clock=lambda: exchange.current_time_utc,
            event_id_factory=_deterministic_event_id_factory(),
        )
    await journal.append(
        JournalEventType.RECONCILIATION_COMPLETED,
        payload={"status": "MATCHED"},
        event_id="sim-reconciled",
    )
    accounting = await _initialize_accounting_runtime(
        execution_store=execution_store,
        accounting_path=state_directory / "accounting.sqlite3",
        option=option,
        cycle_number=1,
        strategy_instance="SIM",
        option_quotes=await exchange.get_option_chain("ETH"),
        clock=lambda: exchange.current_time_utc,
    )
    operations = MutableOperationalState(
        maximum_market_data_age_ms=deployment.maximum_market_data_age_ms,
        clock=lambda: exchange.current_time_utc,
    )
    operations.update_runtime(journal.state)
    operations.update_accounting(accounting.state)
    operations.mark_running(True)
    operations.mark_reconciliation(True, "MATCHED")
    health_server = create_health_server(
        HealthApi(operations.snapshot),
        host=command.health_host,
        port=command.health_port,
    )
    health_thread = Thread(target=health_server.serve_forever, daemon=True)
    health_thread.start()
    private_stream = _SimulatedPrivateStream(
        lambda: exchange.current_time_utc
    )

    async def yield_only(_: float) -> None:
        await asyncio.sleep(0)

    def deterministic_order_id(
        level: int,
        role: ClientOrderRole,
        attempt: int,
    ) -> str:
        role_offset = list(ClientOrderRole).index(role) + 1
        return str(
            ClientOrderId(
                strategy_instance="SIM",
                cycle=1,
                level=level,
                role=role,
                attempt=attempt,
                nonce=f"{level * 100 + attempt * 10 + role_offset:04X}",
            )
        )

    runtime_error: BaseException | None = None
    try:
        await _run_supervised_session(
            command=command,
            journal=journal,
            option=option,
            cycle_number=1,
            private=exchange,
            private_stream=private_stream,
            market_data=exchange,
            instrument=exchange.instrument,
            store=execution_store,
            deployment=deployment,
            kill_switch=kill_switch,
            operations=operations,
            logger=SecretSafeJsonLogger(io.StringIO()),
            clock=lambda: exchange.current_time_utc,
            sleeper=yield_only,
            exit_poll_interval_seconds=0,
            costs=StrategyCostConfig(
                entry_fee_rate=exchange.config.taker_fee_rate,
                tp_fee_rate=exchange.config.maker_fee_rate,
                stop_fee_rate=exchange.config.taker_fee_rate,
                expected_entry_slippage_bps=exchange.config.entry_slippage_bps,
                expected_stop_slippage_bps=exchange.config.stop_slippage_bps,
                spread_cost_entry_bps=exchange.config.perp_spread_bps,
                spread_cost_tp_bps=exchange.config.perp_spread_bps,
                spread_cost_stop_bps=exchange.config.perp_spread_bps,
            ),
            math_engine=math_engine,
            extra_task_factory=lambda coordinator: price_driver(
                coordinator,
                journal,
            ),
            order_link_id_factory=deterministic_order_id,
            accounting=accounting,
            accounting_strategy_instance="SIM",
            accounting_reconciliation_enabled=False,
        )
    except BaseException as exc:
        runtime_error = exc
    finally:
        operations.mark_running(False)
        health_server.shutdown()
        health_server.server_close()
        health_thread.join(timeout=5)
    close_verified = False
    if not preserve_state_on_timeout:
        simulated_close = DemoStrategyCloseOperations(
            trading=exchange,
            account=exchange,
            store=execution_store,
            quotes=exchange,
            option_instruments=(
                _simulated_option_instrument(
                    option.short_symbol,
                    option.expiry_time_utc,
                ),
                _simulated_option_instrument(
                    option.long_symbol,
                    option.expiry_time_utc,
                ),
            ),
            cycle_id=option.cycle_id,
            short_symbol=option.short_symbol,
            long_symbol=option.long_symbol,
            order_link_id_factory=lambda role, attempt: str(
                ClientOrderId(
                    strategy_instance="SIM",
                    cycle=1,
                    level=0,
                    role=role,
                    attempt=attempt,
                    nonce=f"{attempt + 500:04X}",
                )
            ),
            clock=lambda: exchange.current_time_utc,
            sleeper=yield_only,
            poll_interval_seconds=0,
        )
        try:
            close_verified = (
                await asyncio.shield(
                    StrategyCloseService(
                        controller=kill_switch,
                        trading=exchange,
                        operations=simulated_close,
                    ).close(
                        reason=(
                            "simulated bounded runtime completed"
                            if runtime_error is None
                            else "simulated runtime task failed"
                        ),
                        requested_by="simulated-runtime",
                    )
                )
            ).verified_closed
        except BaseException as close_error:
            raise RuntimeError(
                "simulated runtime could not prove safe closure"
            ) from close_error
    positions = await exchange.get_positions("linear", "ETHUSDT")
    orders = await exchange.get_open_orders("linear", "ETHUSDT")
    restored = await DemoRuntimeJournal.restore(
        store=journal_store,
        cycle_id=option.cycle_id,
        clock=lambda: exchange.current_time_utc,
    )
    if not preserve_state_on_timeout and (positions or orders):
        raise RuntimeError("simulated composition did not finish flat")
    await _recover_confirmed_executions(
        accounting=accounting,
        execution_store=execution_store,
        cycle_id=option.cycle_id,
        cycle_number=1,
        strategy_instance="SIM",
        clock=lambda: exchange.current_time_utc,
    )
    if not preserve_state_on_timeout:
        _assert_shutdown_accounting(accounting.state, options_must_be_flat=True)
    await accounting.record_shutdown_snapshot(
        cycle_id=option.cycle_id,
        timestamp=exchange.current_time_utc,
    )
    operations.update_accounting(accounting.state)
    result = {
        "accepted": True,
        "cycle_id": option.cycle_id,
        "event_digest": exchange.event_log_digest,
        "event_log": [event.to_json() for event in exchange.event_log],
        "last_event_sequence": restored.last_event_sequence,
        "final_level_states": [
            level.state.value for level in restored.state.levels
        ],
        "final_level_attempts": [
            level.attempts for level in restored.state.levels
        ],
        "final_recovery_debt": str(
            sum(
                (level.confirmed_debt for level in restored.state.levels),
                ZERO,
            )
        ),
        "final_linear_position_count": len(positions),
        "final_linear_order_count": len(orders),
        "close_verified": close_verified,
        "accounting": accounting.state.to_dict(),
    }
    if runtime_error is not None:
        raise RuntimeError("simulated runtime failed and was closed") from runtime_error
    return result


async def _open_new_cycle(
    *,
    command: DemoStrategyCommand,
    runtime_config: RuntimeConfig,
    private: BybitPrivateRestClient,
    public: BybitPublicRestClient,
    store: SqliteExecutionStore,
    journal_store: SqliteJournalStore,
    option_instruments: tuple[InstrumentSpec, ...],
    clock: Callable[[], datetime],
) -> tuple[DemoRuntimeJournal, OptionSpreadExecutionSnapshot, int]:
    if (
        command.short_symbol is None
        or command.long_symbol is None
        or command.option_quantity is None
        or command.minimum_net_credit is None
        or command.maximum_entry_deviation_bps is None
    ):
        raise AssertionError("validated OPEN_NEW command is incomplete")
    quotes = await public.get_option_chain("ETH")
    quote_by_symbol = {quote.symbol: quote for quote in quotes}
    instrument_by_symbol = {
        instrument.symbol: instrument for instrument in option_instruments
    }
    short_quote = quote_by_symbol.get(command.short_symbol)
    long_quote = quote_by_symbol.get(command.long_symbol)
    short_instrument = instrument_by_symbol.get(command.short_symbol)
    long_instrument = instrument_by_symbol.get(command.long_symbol)
    if any(
        value is None
        for value in (short_quote, long_quote, short_instrument, long_instrument)
    ):
        raise RuntimeError("explicit option selection is no longer available")
    assert short_quote is not None and long_quote is not None
    assert short_instrument is not None and long_instrument is not None
    if short_quote.bid_price is None or long_quote.ask_price is None:
        raise RuntimeError("explicit option selection is not executable")
    quoted_long_price = quantize_limit_price(
        long_quote.ask_price,
        long_instrument.price_filter.tick_size,
        side="Buy",
        policy=PriceQuantizationPolicy.AGGRESSIVE,
    )
    quoted_short_price = quantize_limit_price(
        short_quote.bid_price,
        short_instrument.price_filter.tick_size,
        side="Sell",
        policy=PriceQuantizationPolicy.AGGRESSIVE,
    )
    long_price, short_price, maximum_credit_deviation = (
        _bounded_option_entry_limits(
            quoted_long_price=quoted_long_price,
            quoted_short_price=quoted_short_price,
            long_tick_size=long_instrument.price_filter.tick_size,
            short_tick_size=short_instrument.price_filter.tick_size,
            quantity=command.option_quantity,
            maximum_deviation_bps=command.maximum_entry_deviation_bps,
        )
    )
    expected_credit = (
        quoted_short_price - quoted_long_price
    ) * command.option_quantity
    if expected_credit < command.minimum_net_credit:
        raise RuntimeError("current executable net credit is below the required minimum")
    cycle_number = _next_cycle_number(await store.load_all_order_intents())
    cycle_id = f"DEMO-C{cycle_number:04d}"
    spread = CreditSpread(
        spot=short_quote.underlying_price,
        short_put_strike=_strike(command.short_symbol),
        long_put_strike=_strike(command.long_symbol),
        option_quantity=command.option_quantity,
        premium_credit=expected_credit,
    )
    levels = build_virtual_levels(
        spread,
        runtime_config.strategy.level_count,
        runtime_config.strategy.stop,
    )
    journal = await DemoRuntimeJournal.create(
        store=journal_store,
        state=DemoRuntimeState(
            cycle_id=cycle_id,
            short_option_symbol=command.short_symbol,
            long_option_symbol=command.long_symbol,
            option_quantity=command.option_quantity,
            levels=tuple(DemoLevelRuntimeState.from_level(level) for level in levels),
            daily_realized_pnl=await _daily_realized_pnl(store, clock()),
        ),
        clock=clock,
    )
    service = OptionSpreadEntryService(
        trading=private,
        store=store,
        clock=clock,
        fill_attempts=60,
        fill_interval_seconds=0.25,
    )
    option = await service.open_spread(
        OptionSpreadEntryPlan(
            cycle_id=cycle_id,
            long_symbol=command.long_symbol,
            short_symbol=command.short_symbol,
            expiry_time_utc=_required_delivery(long_instrument),
            quantity=command.option_quantity,
            long_limit_price=long_price,
            short_limit_price=short_price,
            expected_net_credit=expected_credit,
            long_order_link_id=_order_id(
                cycle_number,
                0,
                ClientOrderRole.OPTION_LONG,
                1,
            ),
            short_order_link_id=_order_id(
                cycle_number,
                0,
                ClientOrderRole.OPTION_SHORT,
                1,
            ),
        ),
        OptionEntryPolicy(
            max_leg_wait_seconds=Decimal("15"),
            allow_partial_spread=False,
            minimum_matched_quantity=command.option_quantity,
            maximum_credit_deviation=maximum_credit_deviation,
            minimum_net_credit=command.minimum_net_credit,
            unmatched_long_policy=UnmatchedLongPolicy.RETAIN,
        ),
    )
    await journal.append(
        JournalEventType.OPTION_SPREAD_OPENED,
        payload={
            "actual_net_credit": str(option.actual_net_credit),
            "quantity": str(option.matched_quantity),
        },
    )
    return journal, option, cycle_number


def _bounded_option_entry_limits(
    *,
    quoted_long_price: Decimal,
    quoted_short_price: Decimal,
    long_tick_size: Decimal,
    short_tick_size: Decimal,
    quantity: Decimal,
    maximum_deviation_bps: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    if maximum_deviation_bps <= ZERO or maximum_deviation_bps >= BPS:
        raise ValueError("option entry deviation must be between 0 and 10000 bps")
    long_limit = quantize_limit_price(
        quoted_long_price * (BPS + maximum_deviation_bps) / BPS,
        long_tick_size,
        side="Buy",
        policy=PriceQuantizationPolicy.AGGRESSIVE,
    )
    short_limit = quantize_limit_price(
        quoted_short_price * (BPS - maximum_deviation_bps) / BPS,
        short_tick_size,
        side="Sell",
        policy=PriceQuantizationPolicy.AGGRESSIVE,
    )
    maximum_credit_deviation = (
        (long_limit - quoted_long_price)
        + (quoted_short_price - short_limit)
    ) * quantity
    return long_limit, short_limit, maximum_credit_deviation


async def _close_failed_option_entry(
    *,
    command: DemoStrategyCommand,
    private: BybitPrivateRestClient,
    market_data: BybitPublicMarketData,
    store: SqliteExecutionStore,
    option_instruments: tuple[InstrumentSpec, ...],
    clock: Callable[[], datetime],
) -> None:
    snapshots = await store.load_all_option_spread_snapshots()
    candidates = tuple(
        snapshot
        for snapshot in snapshots
        if snapshot.short_symbol == command.short_symbol
        and snapshot.long_symbol == command.long_symbol
    )
    if not candidates:
        return
    snapshot = max(candidates, key=lambda value: value.updated_at)
    cycle_number = ClientOrderId.parse(snapshot.long_order_link_id).cycle
    operations = DemoStrategyCloseOperations(
        trading=private,
        account=private,
        store=store,
        quotes=market_data,
        option_instruments=option_instruments,
        cycle_id=snapshot.cycle_id,
        short_symbol=snapshot.short_symbol,
        long_symbol=snapshot.long_symbol,
        order_link_id_factory=lambda role, attempt: _order_id(
            cycle_number,
            0,
            role,
            attempt,
        ),
        clock=clock,
    )
    await operations.close_option_spread()
    if not await operations.verify_strategy_closed():
        raise RuntimeError("failed option entry could not be proven closed")


async def _run_supervised_session(
    *,
    command: DemoStrategyCommand,
    journal: DemoRuntimeJournal,
    option: OptionSpreadExecutionSnapshot,
    cycle_number: int,
    private: BybitPrivateRestClient,
    private_stream: PrivateEventPort,
    market_data: BybitPublicMarketData,
    instrument: InstrumentSpec,
    store: SqliteExecutionStore,
    deployment: Any,
    kill_switch: KillSwitchController,
    operations: MutableOperationalState,
    logger: SecretSafeJsonLogger,
    clock: Callable[[], datetime],
    clock_refresh: Callable[[], Awaitable[object]] | None = None,
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    exit_poll_interval_seconds: float = 0.25,
    extra_task_factory: (
        Callable[[LiveStrategyCoordinator], Coroutine[Any, Any, None]] | None
    ) = None,
    order_link_id_factory: (
        Callable[[int, ClientOrderRole, int], str] | None
    ) = None,
    costs: StrategyCostConfig | None = None,
    math_engine: StrategyMathEngine | None = None,
    accounting: AccountingRuntime,
    accounting_strategy_instance: str,
    accounting_reconciliation_enabled: bool = True,
) -> None:
    tasks: list[asyncio.Task[None]] = []

    def spawn(coroutine: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = group.create_task(coroutine)
        tasks.append(task)
        return task

    async def sleep_and_cancel() -> None:
        await asyncio.sleep(command.run_seconds)
        for task in tuple(tasks):
            if task is not asyncio.current_task():
                task.cancel()

    risk_engine = RiskEngine()
    sizing_engine = math_engine or StrategyMathEngine(ExpirationOptionValuation())
    recovery_planner = SameLevelRecoveryPlanner(
        risk_engine,
        costs,
        sizing_engine,
    )
    entry_gate = _RuntimeEntryGate(kill_switch, private_stream)
    recovery_service = SameLevelRecoveryService(
        entry_service=OneLevelEntryService(
            trading=private,
            store=store,
            clock=clock,
        ),
        store=store,
        planner=recovery_planner,
        clock=clock,
        entry_gate=entry_gate,
    )

    def lifecycle() -> OneLevelLifecycleService:
        entry = OneLevelEntryService(trading=private, store=store, clock=clock)
        exits = ProtectiveExitService(
            trading=private,
            account=private,
            store=store,
            clock=clock,
            sleeper=sleeper,
            visibility_attempts=10,
            visibility_interval_seconds=0.25,
        )
        return OneLevelLifecycleService(
            trading=private,
            account=private,
            store=store,
            entry_service=entry,
            exit_service=exits,
            instrument=instrument,
            clock=clock,
            sleeper=sleeper,
            fill_attempts=60,
            fill_interval_seconds=0.25,
        )

    async def refresh_accounting() -> CombinedLedgerState:
        await _recover_confirmed_executions(
            accounting=accounting,
            execution_store=store,
            cycle_id=option.cycle_id,
            cycle_number=cycle_number,
            strategy_instance=accounting_strategy_instance,
            clock=clock,
        )
        operations.update_accounting(accounting.state)
        return accounting.state

    async with asyncio.TaskGroup() as group:
        coordinator = LiveStrategyCoordinator(
            journal=journal,
            account=private,
            store=store,
            instrument=instrument,
            risk_engine=risk_engine,
            risk_limits=deployment.risk_limits,
            risk_state_builder=RuntimeRiskStateBuilder(
                maximum_market_data_age_ms=deployment.maximum_market_data_age_ms
            ),
            recovery_service=recovery_service,
            recovery_planner=recovery_planner,
            lifecycle_factory=lifecycle,
            order_link_id_factory=(
                order_link_id_factory
                or (
                    lambda level, role, attempt: _order_id(
                        cycle_number,
                        level,
                        role,
                        attempt,
                    )
                )
            ),
            task_spawner=spawn,
            clock=clock,
            costs=costs,
            math_engine=sizing_engine,
            entry_gate=entry_gate,
            sleeper=sleeper,
            exit_poll_interval_seconds=exit_poll_interval_seconds,
            accounting_refresh=refresh_accounting,
        )
        await coordinator.restore_active_levels()
        spawn(
            _private_loop(
                journal,
                store,
                private,
                private_stream,
                operations,
                clock,
                accounting,
                cycle_number,
                accounting_strategy_instance,
            )
        )
        await _wait_for_private_reconciliation(private_stream)
        spawn(
            _public_loop(
                coordinator,
                market_data,
                operations,
                journal,
                clock,
            )
        )
        spawn(
            _reconciliation_loop(
                journal,
                store,
                private,
                operations,
                clock,
                deployment.risk_limits.maximum_reconciliation_failures,
                accounting=accounting,
                cycle_id=option.cycle_id,
                cycle_number=cycle_number,
                strategy_instance=accounting_strategy_instance,
                accounting_reconciliation_enabled=(
                    accounting_reconciliation_enabled
                ),
            )
        )
        spawn(
            _option_health_loop(
                journal,
                option,
                market_data,
                operations,
                clock,
                deployment.maximum_market_data_age_ms,
                accounting,
            )
        )
        spawn(
            _alert_loop(
                operations,
                deployment.risk_limits.maximum_recovery_debt,
                option.expiry_time_utc,
                logger,
                journal.state.cycle_id,
                clock,
            )
        )
        if clock_refresh is not None:
            spawn(_clock_refresh_loop(clock_refresh))
        if extra_task_factory is not None:
            spawn(extra_task_factory(coordinator))
        spawn(sleep_and_cancel())


async def _clock_refresh_loop(
    refresh: Callable[[], Awaitable[object]],
    *,
    interval_seconds: float = 30,
    refresh_attempts: int = 3,
    retry_interval_seconds: float = 0.25,
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    if interval_seconds <= 0:
        raise ValueError("clock refresh interval must be positive")
    if refresh_attempts <= 0:
        raise ValueError("clock refresh attempts must be positive")
    if retry_interval_seconds <= 0:
        raise ValueError("clock refresh retry interval must be positive")
    while True:
        await sleeper(interval_seconds)
        for attempt in range(refresh_attempts):
            try:
                await refresh()
                break
            except Exception:
                if attempt + 1 >= refresh_attempts:
                    raise
                await sleeper(retry_interval_seconds)


async def _wait_for_private_reconciliation(
    stream: PrivateEventPort,
    *,
    timeout_seconds: float = 30,
) -> None:
    if timeout_seconds <= 0:
        raise ValueError("private reconciliation timeout must be positive")
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while stream.new_entries_blocked:
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("private stream reconciliation did not complete")
        await asyncio.sleep(0.01)


async def _public_loop(
    coordinator: LiveStrategyCoordinator,
    market_data: BybitPublicMarketData,
    operations: MutableOperationalState,
    journal: DemoRuntimeJournal,
    clock: Callable[[], datetime],
) -> None:
    trigger_router = TriggerPriceRouter(TriggerPriceSource.LAST_TRADE)
    del clock
    try:
        async for trade in market_data.stream_trades("ETHUSDT"):
            operations.mark_public(True, trade.timestamp_utc)
            event = trigger_router.from_trade(trade)
            if event is None:
                continue
            result = await coordinator.on_trigger(event)
            operations.mark_risk(
                tuple(reason for _, reasons in result.blocked for reason in reasons)
            )
            operations.update_runtime(journal.state)
    finally:
        operations.mark_public(False)


async def _private_loop(
    journal: DemoRuntimeJournal,
    store: SqliteExecutionStore,
    private: BybitPrivateRestClient,
    stream: PrivateEventPort,
    operations: MutableOperationalState,
    clock: Callable[[], datetime],
    accounting: AccountingRuntime,
    cycle_number: int,
    strategy_instance: str,
) -> None:
    async for event in stream.stream_events():
        if isinstance(event, PrivateConnectionEvent):
            authenticated = event.state is PrivateConnectionState.AUTHENTICATED
            operations.mark_private(authenticated)
            if authenticated:
                matched = await _reconcile_runtime(journal, store, private, clock)
                operations.update_runtime(journal.state)
                operations.mark_reconciliation(matched, "MATCHED" if matched else "MISMATCH")
                if not matched:
                    raise RuntimeError("private reconnect reconciliation failed")
                stream.mark_reconciled(event.connection_generation)
            else:
                await journal.append(
                    JournalEventType.TRADING_SUSPENDED,
                    payload={"reason": event.reason or "private stream disconnected"},
                )
                operations.update_runtime(journal.state)
        elif isinstance(event, ExecutionUpdateBatch):
            classifier = await _execution_classifier(
                store=store,
                cycle_id=journal.state.cycle_id,
                cycle_number=cycle_number,
                strategy_instance=strategy_instance,
            )
            state = await accounting.apply_private_update_batch(event, classifier)
            operations.update_accounting(state)
            operations.update_runtime(journal.state)


async def _reconciliation_loop(
    journal: DemoRuntimeJournal,
    store: SqliteExecutionStore,
    private: BybitPrivateRestClient,
    operations: MutableOperationalState,
    clock: Callable[[], datetime],
    maximum_failures: int,
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    *,
    accounting: AccountingRuntime | None = None,
    cycle_id: str | None = None,
    cycle_number: int | None = None,
    strategy_instance: str | None = None,
    accounting_reconciliation_enabled: bool = False,
) -> None:
    if maximum_failures <= 0:
        raise ValueError("maximum reconciliation failures must be positive")
    while True:
        await sleeper(5)
        if accounting is not None:
            if cycle_id is None or cycle_number is None or strategy_instance is None:
                raise ValueError("accounting reconciliation identifiers are required")
            await _recover_confirmed_executions(
                accounting=accounting,
                execution_store=store,
                cycle_id=cycle_id,
                cycle_number=cycle_number,
                strategy_instance=strategy_instance,
                clock=clock,
            )
            operations.update_accounting(accounting.state)
        accounting_matched = True
        if accounting is not None and accounting_reconciliation_enabled:
            if cycle_number is None or strategy_instance is None:
                raise ValueError("accounting reconciliation identifiers are required")
            accounting_matched = await _reconcile_accounting_runtime(
                accounting=accounting,
                private=private,
                cycle_number=cycle_number,
                strategy_instance=strategy_instance,
                clock=clock,
            )
        matched = (
            await _reconcile_runtime(journal, store, private, clock)
            and accounting_matched
        )
        operations.update_runtime(journal.state)
        operations.mark_reconciliation(matched, "MATCHED" if matched else "MISMATCH")
        if (
            not matched
            and journal.state.consecutive_reconciliation_failures
            >= maximum_failures
        ):
            raise RuntimeError("periodic reconciliation failed")


async def _option_health_loop(
    journal: DemoRuntimeJournal,
    option: OptionSpreadExecutionSnapshot,
    market_data: BybitPublicMarketData,
    operations: MutableOperationalState,
    clock: Callable[[], datetime],
    maximum_age_ms: int,
    accounting: AccountingRuntime,
) -> None:
    while True:
        quotes = await market_data.get_option_chain("ETH")
        selected = {
            quote.symbol: quote
            for quote in quotes
            if quote.symbol in {option.short_symbol, option.long_symbol}
        }
        now = clock()
        fresh = len(selected) == 2 and all(
            timedelta(0) <= now - quote.timestamp_utc <= timedelta(milliseconds=maximum_age_ms)
            for quote in selected.values()
        )
        expiry_safe = now < option.expiry_time_utc - OPTION_HEDGE_CUTOFF
        if fresh:
            await _record_option_quotes(
                accounting,
                tuple(selected.values()),
                option.cycle_id,
                maximum_age_ms,
            )
            operations.update_accounting(accounting.state)
        if not fresh or not expiry_safe:
            await journal.append(
                JournalEventType.TRADING_SUSPENDED,
                payload={
                    "reason": (
                        "option quotes are stale"
                        if not fresh
                        else "option hedge cutoff reached"
                    )
                },
            )
            operations.update_runtime(journal.state)
        await asyncio.sleep(1)


async def _alert_loop(
    operations: MutableOperationalState,
    maximum_recovery_debt: Decimal,
    expiry: datetime,
    logger: SecretSafeJsonLogger,
    cycle_id: str,
    clock: Callable[[], datetime],
) -> None:
    dispatcher = AlertDispatcher(_AlertLogger(logger, cycle_id))
    policy = AlertPolicy(
        maximum_market_data_age_ms=1000,
        maximum_option_quote_age_ms=1000,
        maximum_pending_order_age_ms=10000,
        large_slippage=Decimal("10"),
        maximum_recovery_debt=maximum_recovery_debt,
        debt_warning_ratio=Decimal("0.8"),
        expiry_warning_hours=48,
    )
    while True:
        snapshot = operations.snapshot()
        await dispatcher.dispatch(
            AlertObservation(
                unprotected_quantity=snapshot.unprotected_quantity,
                unknown_position=False,
                risk_violation=snapshot.risk_lock_active,
                database_available=snapshot.database_available,
                authentication_succeeded=snapshot.private_connected,
                dangerous_reconciliation=(
                    snapshot.reconciliation_state == "MISMATCH"
                ),
                kill_switch_triggered=False,
                market_data_age_ms=snapshot.market_data_age_ms,
                option_quote_age_ms=snapshot.market_data_age_ms,
                pending_order_age_ms=0,
                stop_slippage=ZERO,
                recovery_debt=snapshot.recovery_debt,
                hours_to_expiry=max(
                    0,
                    int((expiry - clock()).total_seconds() // 3600),
                ),
            ),
            policy,
        )
        await asyncio.sleep(1)


def _accounting_path(execution_path: Path) -> Path:
    return execution_path.with_name(f"{execution_path.stem}-accounting.sqlite3")


def _assert_shutdown_accounting(
    state: CombinedLedgerState,
    *,
    options_must_be_flat: bool,
) -> None:
    if state.hedge.open_quantity != ZERO:
        raise RuntimeError("shutdown ledger still contains an open hedge lot")
    if options_must_be_flat and (
        state.option.long.open_quantity != ZERO
        or state.option.short.open_quantity != ZERO
    ):
        raise RuntimeError("shutdown ledger still contains open option fills")
    residuals = (
        state.mark_identity_residual.value,
        state.liquidation_identity_residual.value,
        state.cash_equity_mark_residual.value,
        state.cash_equity_liquidation_residual.value,
        state.debt_identity_residual.value,
    )
    if any(residual != ZERO for residual in residuals):
        raise RuntimeError("shutdown ledger identity residual is nonzero")


async def _initialize_accounting_runtime(
    *,
    execution_store: SqliteExecutionStore,
    accounting_path: Path,
    option: OptionSpreadExecutionSnapshot,
    cycle_number: int,
    strategy_instance: str,
    option_quotes: tuple[OptionMarketQuote, ...],
    clock: Callable[[], datetime],
) -> AccountingRuntime:
    store = SqliteAccountingStore(accounting_path)
    await store.initialize()
    runtime = AccountingRuntime(
        store=store,
        reconstructor=CombinedLedgerReconstructor(),
    )
    await runtime.initialize()
    selected_quotes = tuple(
        quote
        for quote in option_quotes
        if quote.symbol in {option.long_symbol, option.short_symbol}
    )
    await _record_option_quotes(runtime, selected_quotes, option.cycle_id, 1_000)
    await _recover_confirmed_executions(
        accounting=runtime,
        execution_store=execution_store,
        cycle_id=option.cycle_id,
        cycle_number=cycle_number,
        strategy_instance=strategy_instance,
        clock=clock,
    )
    return runtime


async def _execution_classifier(
    *,
    store: SqliteExecutionStore,
    cycle_id: str,
    cycle_number: int,
    strategy_instance: str,
) -> PrivateExecutionClassifier:
    reference_prices = {
        request.order_link_id: Price(request.trigger_price)
        for request in await store.load_all_order_intents()
        if request.trigger_price is not None
    }
    return PrivateExecutionClassifier(
        cycle_id=cycle_id,
        cycle_number=cycle_number,
        strategy_instance=strategy_instance,
        reference_prices=reference_prices,
    )


async def _recover_confirmed_executions(
    *,
    accounting: AccountingRuntime,
    execution_store: SqliteExecutionStore,
    cycle_id: str,
    cycle_number: int,
    strategy_instance: str,
    clock: Callable[[], datetime],
) -> None:
    executions: list[ExecutionUpdate] = []
    for execution in await execution_store.load_all_executions():
        try:
            client_id = ClientOrderId.parse(execution.order_link_id)
        except ValueError:
            continue
        if (
            client_id.strategy_instance == strategy_instance
            and client_id.cycle == cycle_number
        ):
            executions.append(execution)
    if not executions:
        return
    payload = "\n".join(
        "|".join(
            (
                execution.execution_id,
                execution.order_id,
                execution.order_link_id,
                execution.symbol,
                execution.side,
                str(execution.price),
                str(execution.quantity),
                str(execution.fee),
                execution.executed_at.isoformat(),
            )
        )
        for execution in executions
    )
    batch = ExecutionUpdateBatch(
        executions=tuple(executions),
        received_at=clock(),
        raw_payload_hash=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    )
    classifier = await _execution_classifier(
        store=execution_store,
        cycle_id=cycle_id,
        cycle_number=cycle_number,
        strategy_instance=strategy_instance,
    )
    await accounting.apply_rest_update_batch(batch, classifier)


async def _record_option_quotes(
    accounting: AccountingRuntime,
    quotes: tuple[OptionMarketQuote, ...],
    cycle_id: str,
    maximum_age_ms: int,
) -> None:
    if maximum_age_ms <= 0:
        raise ValueError("maximum option quote age must be positive")
    for quote in quotes:
        if quote.bid_price is None or quote.ask_price is None:
            continue
        identity = "|".join(
            (
                cycle_id,
                quote.symbol,
                quote.timestamp_utc.isoformat(),
                str(quote.bid_price),
                str(quote.ask_price),
                str(quote.mark_price),
            )
        )
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        await accounting.apply_event(
            OptionQuoteRecorded(
                event_id=f"option-quote:{digest}",
                event_version=1,
                cycle_id=cycle_id,
                timestamp=quote.timestamp_utc,
                source=EventSource.SYSTEM,
                correlation_id=digest,
                symbol=quote.symbol,
                bid=Price(quote.bid_price),
                ask=Price(quote.ask_price),
                mark=Price(quote.mark_price),
                valid_until=quote.timestamp_utc
                + timedelta(milliseconds=maximum_age_ms),
            )
        )


async def _reconcile_runtime(
    journal: DemoRuntimeJournal,
    store: SqliteExecutionStore,
    private: BybitPrivateRestClient,
    clock: Callable[[], datetime],
) -> bool:
    exchange = await BybitPrivateStateReader(
        trading=private,
        account=private,
        clock=clock,
    ).capture()
    known = frozenset(
        request.order_link_id for request in await store.load_all_order_intents()
    )
    result = evaluate_private_snapshot(
        exchange,
        known_order_link_ids=known,
        expected_positions=await _expected_durable_positions(store),
    )
    if result.trading_allowed:
        await journal.append(
            JournalEventType.RECONCILIATION_COMPLETED,
            payload={"status": "MATCHED"},
        )
    else:
        await journal.append(
            JournalEventType.TRADING_SUSPENDED,
            payload={
                "reason": "; ".join(
                    difference.detail for difference in result.differences
                )
            },
        )
    return result.trading_allowed


async def _reconcile_accounting_runtime(
    *,
    accounting: AccountingRuntime,
    private: BybitPrivateRestClient,
    cycle_number: int,
    strategy_instance: str,
    clock: Callable[[], datetime],
) -> bool:
    snapshot = await BybitPrivateStateReader(
        trading=private,
        account=private,
        clock=clock,
    ).capture()
    result = await accounting.reconcile(
        state_reader=_CapturedAccountingReader(
            snapshot,
            cycle_number=cycle_number,
            strategy_instance=strategy_instance,
            funding_pnl=accounting.state.funding_pnl,
        ),
        clock=clock,
    )
    return result.report.trading_allowed


def _next_cycle_number(requests: tuple[Any, ...]) -> int:
    cycles: list[int] = []
    for request in requests:
        try:
            parsed = ClientOrderId.parse(request.order_link_id)
        except ValueError:
            continue
        if parsed.strategy_instance == STRATEGY_INSTANCE:
            cycles.append(parsed.cycle)
    cycle = max(cycles, default=0) + 1
    if cycle > 9_999:
        raise RuntimeError("demo strategy exhausted cycle IDs")
    return cycle


def _belongs_to_cycle(
    order_link_id: str,
    *,
    cycle_number: int,
    strategy_instance: str,
) -> bool:
    try:
        parsed = ClientOrderId.parse(order_link_id)
    except ValueError:
        return False
    return (
        parsed.cycle == cycle_number
        and parsed.strategy_instance == strategy_instance
    )


def _order_id(
    cycle: int,
    level: int,
    role: ClientOrderRole,
    attempt: int,
) -> str:
    return str(ClientOrderId.new(STRATEGY_INSTANCE, cycle, level, role, attempt))


def _strike(symbol: str) -> Decimal:
    parts = symbol.split("-")
    if len(parts) != 5:
        raise ValueError(f"unexpected option symbol: {symbol}")
    return Decimal(parts[2])


def _required_delivery(instrument: InstrumentSpec) -> datetime:
    if instrument.delivery_time_utc is None:
        raise ValueError("option instrument has no delivery time")
    return instrument.delivery_time_utc


async def _daily_realized_pnl(
    store: _ProtectionSnapshotReader,
    observed_at_utc: datetime,
) -> Decimal:
    day = observed_at_utc.astimezone(timezone.utc).date()
    return sum(
        (
            snapshot.realized_pnl
            for snapshot in await store.load_all_protection_snapshots()
            if snapshot.updated_at.astimezone(timezone.utc).date() == day
        ),
        ZERO,
    )


def _simulated_option_instrument(
    symbol: str,
    expiry: datetime,
) -> InstrumentSpec:
    from eth_credit_hedge.domain.instruments import LotSizeFilter, PriceFilter

    return InstrumentSpec(
        symbol=symbol,
        category="option",
        status="Trading",
        base_coin="ETH",
        quote_coin="USDT",
        settle_coin="USDT",
        price_filter=PriceFilter(
            tick_size=Decimal("0.01"),
            min_price=Decimal("0.01"),
            max_price=Decimal("100000"),
        ),
        lot_size_filter=LotSizeFilter(
            qty_step=Decimal("0.001"),
            min_order_qty=Decimal("0.001"),
            max_order_qty=Decimal("100"),
            max_market_order_qty=Decimal("100"),
            min_notional=Decimal("0.01"),
        ),
        contract_multiplier=Decimal("1"),
        delivery_time_utc=expiry,
    )


def _write_runtime_evidence(payload: dict[str, object]) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = Path("artifacts") / f"integrated-demo-runtime-{stamp}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    return path


def _deterministic_event_id_factory(start: int = 0) -> Callable[[], str]:
    sequence = start

    def next_event_id() -> str:
        nonlocal sequence
        sequence += 1
        return f"sim-event-{sequence:06d}"

    return next_event_id


__all__ = ["run_demo_strategy", "run_simulated_strategy_command"]
