"""Explicitly gated Bybit demo burn-in runner; never binds mainnet mutations."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import AsyncGenerator
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import cast

from eth_credit_hedge.application.emergency_flatten import EmergencyFlattenService
from eth_credit_hedge.application.execution_hash import execution_payload_hash
from eth_credit_hedge.application.one_level_entry import OneLevelEntryService
from eth_credit_hedge.application.one_level_coordinator import OneLevelCoordinator
from eth_credit_hedge.application.multi_level_execution import (
    MultiLevelCoordinator,
)
from eth_credit_hedge.application.one_level_lifecycle import (
    ExitFillNotConfirmedError,
    OneLevelLifecycleService,
    ProtectedOneLevel,
)
from eth_credit_hedge.application.option_spread_entry import (
    OptionSpreadEntryPlan,
    OptionSpreadEntryService,
    OptionSpreadNotOpenedError,
)
from eth_credit_hedge.application.protective_exits import ProtectiveExitService
from eth_credit_hedge.application.read_only_reconciliation import (
    BybitPrivateStateReader,
    ExpectedPosition,
    PrivateAccountSnapshot,
)
from eth_credit_hedge.application.same_level_recovery import (
    SameLevelRecoveryService,
)
from eth_credit_hedge.application.startup_reconciliation import (
    LocalExecutionRecoveryState,
    evaluate_startup_reconciliation,
)
from eth_credit_hedge.config.bybit import load_bybit_demo_profile
from eth_credit_hedge.config.deployment import (
    EnvironmentProfile,
    load_all_environment_profiles,
)
from eth_credit_hedge.config.schema import RuntimeEnvironment
from eth_credit_hedge.core.virtual_levels import HedgeLevel
from eth_credit_hedge.domain.client_order_ids import (
    ClientOrderId,
    ClientOrderRole,
)
from eth_credit_hedge.domain.execution import (
    AmendOrderRequest,
    ExecutionUpdate,
    LiveExecutionState,
    PlaceOrderRequest,
    UncertainOrderOutcomeError,
)
from eth_credit_hedge.domain.instrument_rules import (
    PriceQuantizationPolicy,
    quantize_limit_price,
)
from eth_credit_hedge.domain.instruments import (
    InstrumentSpec,
    OptionContract,
    OptionMarketQuote,
)
from eth_credit_hedge.domain.live_option_execution import (
    OptionSpreadExecutionSnapshot,
)
from eth_credit_hedge.domain.live_execution import (
    EntryExecutionSnapshot,
    transition_entry_snapshot,
)
from eth_credit_hedge.domain.live_recovery import (
    LockedLevelAction,
    SameLevelRecoveryPlanner,
)
from eth_credit_hedge.domain.option_lifecycle import (
    OptionEntryPolicy,
    UnmatchedLongPolicy,
)
from eth_credit_hedge.domain.option_position import OptionPositionState
from eth_credit_hedge.domain.option_position import (
    OptionLegPosition,
    PutCreditSpreadPosition,
)
from eth_credit_hedge.domain.market_data import (
    MarketDataHealthPolicy,
    MarketDataHealthResult,
    MarketDataHealthSnapshot,
    TradeEvent,
    TriggerPriceRouter,
    TriggerPriceSource,
    evaluate_market_data_health,
)
from eth_credit_hedge.domain.protected_execution import (
    ProtectionSnapshot,
    aggregate_protection_position_matches,
    apply_emergency_exit_execution,
)
from eth_credit_hedge.domain.reconciliation import ReconciliationReport
from eth_credit_hedge.domain.risk import (
    RiskEngine,
    RiskState,
    TradeProposal,
)
from eth_credit_hedge.infrastructure.bybit.clock import ClockStaleError, ServerClock
from eth_credit_hedge.infrastructure.bybit.error_mapping import (
    BybitUnknownOrderError,
)
from eth_credit_hedge.infrastructure.bybit.private_rest import (
    BybitPrivateRestClient,
)
from eth_credit_hedge.infrastructure.bybit.public_rest import (
    BybitPublicRestClient,
)
from eth_credit_hedge.infrastructure.bybit.public_market_data import (
    BybitPublicMarketData,
)
from eth_credit_hedge.infrastructure.persistence.sqlite_execution_store import (
    SqliteExecutionStore,
)


MUTATION_GATE_ENV = "RUN_BYBIT_DEMO_MUTATIONS"
D3_MUTATION_TOKEN = "D3_MANUAL_ONE_LEVEL"
D4_MUTATION_TOKEN = "D4_AUTOMATIC_ONE_LEVEL"
D5_MUTATION_TOKEN = "D5_MULTIPLE_BASELINE_LEVELS"
D6_MUTATION_TOKEN = "D6_FULL_NEXT_TP_RECOVERY"
STRATEGY_INSTANCE = "D3"
ZERO = Decimal("0")


class DemoMutationRefusedError(RuntimeError):
    """The explicit demo mutation token or a finite safety check is missing."""


@dataclass(frozen=True, slots=True)
class DemoD3Result:
    option_cycle_id: str
    option_state: str
    option_matched_quantity: Decimal
    option_actual_net_credit: Decimal
    perp_cycle_number: int
    entry_order_link_id: str
    entry_quantity: Decimal
    average_entry_price: Decimal
    stop_order_link_id: str
    stop_trigger_price: Decimal
    take_profit_order_link_id: str
    protected_restart_status: str
    final_state: str
    realized_pnl: Decimal
    confirmed_recovery_debt: Decimal
    final_reconciliation_status: str

    def to_json(self) -> str:
        return json.dumps(
            {
                "option_cycle_id": self.option_cycle_id,
                "option_state": self.option_state,
                "option_matched_quantity": str(self.option_matched_quantity),
                "option_actual_net_credit": str(self.option_actual_net_credit),
                "perp_cycle_number": self.perp_cycle_number,
                "entry_order_link_id": self.entry_order_link_id,
                "entry_quantity": str(self.entry_quantity),
                "average_entry_price": str(self.average_entry_price),
                "stop_order_link_id": self.stop_order_link_id,
                "stop_trigger_price": str(self.stop_trigger_price),
                "take_profit_order_link_id": self.take_profit_order_link_id,
                "protected_restart_status": self.protected_restart_status,
                "final_state": self.final_state,
                "realized_pnl": str(self.realized_pnl),
                "confirmed_recovery_debt": str(
                    self.confirmed_recovery_debt
                ),
                "final_reconciliation_status": (
                    self.final_reconciliation_status
                ),
            },
            separators=(",", ":"),
            sort_keys=True,
        )


@dataclass(frozen=True, slots=True)
class DemoD4Result:
    option_cycle_id: str
    perp_cycle_number: int
    trigger_source: str
    armed_price: Decimal
    level_entry_price: Decimal
    crossing_price: Decimal
    crossing_time_utc: datetime
    connection_generation: int
    request_quantity: Decimal
    average_entry_price: Decimal
    stop_trigger_price: Decimal
    protected_restart_status: str
    final_state: str
    realized_pnl: Decimal
    final_reconciliation_status: str

    def to_json(self) -> str:
        return json.dumps(
            {
                "option_cycle_id": self.option_cycle_id,
                "perp_cycle_number": self.perp_cycle_number,
                "trigger_source": self.trigger_source,
                "armed_price": str(self.armed_price),
                "level_entry_price": str(self.level_entry_price),
                "crossing_price": str(self.crossing_price),
                "crossing_time_utc": self.crossing_time_utc.isoformat(),
                "connection_generation": self.connection_generation,
                "request_quantity": str(self.request_quantity),
                "average_entry_price": str(self.average_entry_price),
                "stop_trigger_price": str(self.stop_trigger_price),
                "protected_restart_status": self.protected_restart_status,
                "final_state": self.final_state,
                "realized_pnl": str(self.realized_pnl),
                "final_reconciliation_status": (
                    self.final_reconciliation_status
                ),
            },
            separators=(",", ":"),
            sort_keys=True,
        )


@dataclass(frozen=True, slots=True)
class DemoD5Result:
    option_cycle_id: str
    perp_cycle_number: int
    trigger_source: str
    level_entry_prices: tuple[Decimal, ...]
    crossing_prices: tuple[Decimal, ...]
    aggregate_quantity: Decimal
    average_entry_prices: tuple[Decimal, ...]
    stop_trigger_prices: tuple[Decimal, ...]
    protected_restart_status: str
    final_states: tuple[str, ...]
    realized_pnls: tuple[Decimal, ...]
    final_reconciliation_status: str

    def to_json(self) -> str:
        return json.dumps(
            {
                "option_cycle_id": self.option_cycle_id,
                "perp_cycle_number": self.perp_cycle_number,
                "trigger_source": self.trigger_source,
                "level_entry_prices": [
                    str(value) for value in self.level_entry_prices
                ],
                "crossing_prices": [
                    str(value) for value in self.crossing_prices
                ],
                "aggregate_quantity": str(self.aggregate_quantity),
                "average_entry_prices": [
                    str(value) for value in self.average_entry_prices
                ],
                "stop_trigger_prices": [
                    str(value) for value in self.stop_trigger_prices
                ],
                "protected_restart_status": self.protected_restart_status,
                "final_states": list(self.final_states),
                "realized_pnls": [str(value) for value in self.realized_pnls],
                "final_reconciliation_status": (
                    self.final_reconciliation_status
                ),
            },
            separators=(",", ":"),
            sort_keys=True,
        )


@dataclass(frozen=True, slots=True)
class DemoD6Result:
    option_cycle_id: str
    perp_cycle_number: int
    level_id: int
    baseline_average_entry_price: Decimal
    baseline_stop_trigger_price: Decimal
    baseline_stop_fill_price: Decimal
    actual_stop_debt: Decimal
    recovery_crossing_price: Decimal
    recovery_crossing_time_utc: datetime
    connection_generation: int
    raw_recovery_quantity: Decimal
    recovery_quantity: Decimal
    expected_take_profit: Decimal
    allocated_debt: Decimal
    rejected_locked_action: str
    recovery_average_entry_price: Decimal
    recovery_take_profit_price: Decimal
    recovery_state: str
    recovery_realized_pnl: Decimal
    net_zone_budget: Decimal
    remaining_debt: Decimal
    protected_restart_status: str
    final_reconciliation_status: str

    def to_json(self) -> str:
        return json.dumps(
            asdict(self),
            default=lambda value: (
                value.isoformat() if isinstance(value, datetime) else str(value)
            ),
            separators=(",", ":"),
            sort_keys=True,
        )


@dataclass(frozen=True, slots=True)
class _ProtectedCycleOutcome:
    opened: ProtectedOneLevel
    final_state: LiveExecutionState
    realized_pnl: Decimal
    confirmed_recovery_debt: Decimal
    restart_status: str
    final_reconciliation_status: str


async def run_d3_manual() -> DemoD3Result:
    if os.environ.get(MUTATION_GATE_ENV) != D3_MUTATION_TOKEN:
        raise DemoMutationRefusedError(
            f"set {MUTATION_GATE_ENV}={D3_MUTATION_TOKEN} explicitly"
        )
    deployment = _demo_deployment_profile()
    if not deployment.external_order_mutations_enabled:
        raise DemoMutationRefusedError("demo profile disables order mutations")
    credentials = load_bybit_demo_profile()
    if (
        credentials.rest_base_url != deployment.rest_base_url
        or credentials.private_websocket_url != deployment.private_websocket_url
    ):
        raise DemoMutationRefusedError("demo credential endpoints do not match")

    clock = ServerClock(
        max_absolute_offset_ms=deployment.maximum_clock_drift_ms,
    )
    private = BybitPrivateRestClient(profile=credentials, clock=clock)
    public = BybitPublicRestClient()
    store = SqliteExecutionStore(deployment.database_path)
    await store.initialize()
    await _synchronize_clock(private)
    await _apply_recorded_emergency_exits(store, clock)
    await _reconcile_late_option_longs(
        store=store,
        private=private,
        clock=clock,
    )
    cycle_number = _next_cycle_number(await store.load_all_order_intents())
    initial_report, initial_exchange = await _reconcile(
        store,
        private,
        clock,
        cycle_number=cycle_number,
    )
    _require_matched(initial_report, "initial demo reconciliation")
    _require_one_way_account(initial_exchange)
    await _ensure_option_margin_mode(private)

    try:
        option = await _open_or_load_option_spread(
            store=store,
            private=private,
            public=public,
            clock=clock,
            cycle_number=cycle_number,
            deployment=deployment,
        )
        await _synchronize_clock(private)
        option_report, _ = await _reconcile(
            store,
            private,
            clock,
            cycle_number=cycle_number,
        )
        _require_matched(option_report, "option spread reconciliation")
        return await _run_protected_perp_cycle(
            store=store,
            private=private,
            public=public,
            clock=clock,
            deployment=deployment,
            option=option,
            cycle_number=cycle_number,
        )
    except BaseException:
        await _make_perp_safe(
            store=store,
            private=private,
            clock=clock,
            cycle_number=cycle_number,
        )
        raise


async def run_d4_automatic() -> DemoD4Result:
    if os.environ.get(MUTATION_GATE_ENV) != D4_MUTATION_TOKEN:
        raise DemoMutationRefusedError(
            f"set {MUTATION_GATE_ENV}={D4_MUTATION_TOKEN} explicitly"
        )
    deployment = _demo_deployment_profile()
    if not deployment.external_order_mutations_enabled:
        raise DemoMutationRefusedError("demo profile disables order mutations")
    credentials = load_bybit_demo_profile()
    if (
        credentials.rest_base_url != deployment.rest_base_url
        or credentials.private_websocket_url
        != deployment.private_websocket_url
    ):
        raise DemoMutationRefusedError("demo credential endpoints do not match")

    clock = ServerClock(
        max_absolute_offset_ms=deployment.maximum_clock_drift_ms,
    )
    private = BybitPrivateRestClient(profile=credentials, clock=clock)
    public = BybitPublicRestClient()
    store = SqliteExecutionStore(deployment.database_path)
    await store.initialize()
    await _synchronize_clock(private)
    await _apply_recorded_emergency_exits(store, clock)
    await _reconcile_late_option_longs(
        store=store,
        private=private,
        clock=clock,
    )
    cycle_number = _next_cycle_number(await store.load_all_order_intents())
    initial_report, initial_exchange = await _reconcile(
        store,
        private,
        clock,
        cycle_number=cycle_number,
    )
    _require_matched(initial_report, "initial D4 reconciliation")
    _require_one_way_account(initial_exchange)
    await _ensure_option_margin_mode(private)

    try:
        option = await _open_or_load_option_spread(
            store=store,
            private=private,
            public=public,
            clock=clock,
            cycle_number=cycle_number,
            deployment=deployment,
        )
        await _synchronize_clock(private)
        option_report, _ = await _reconcile(
            store,
            private,
            clock,
            cycle_number=cycle_number,
        )
        _require_matched(option_report, "D4 option reconciliation")
        return await _run_automatic_perp_cycle(
            store=store,
            private=private,
            public=public,
            clock=clock,
            deployment=deployment,
            option=option,
            cycle_number=cycle_number,
        )
    except BaseException:
        await _make_perp_safe(
            store=store,
            private=private,
            clock=clock,
            cycle_number=cycle_number,
        )
        raise


async def run_d5_multiple() -> DemoD5Result:
    if os.environ.get(MUTATION_GATE_ENV) != D5_MUTATION_TOKEN:
        raise DemoMutationRefusedError(
            f"set {MUTATION_GATE_ENV}={D5_MUTATION_TOKEN} explicitly"
        )
    deployment = _demo_deployment_profile()
    if not deployment.external_order_mutations_enabled:
        raise DemoMutationRefusedError("demo profile disables order mutations")
    if deployment.risk_limits.maximum_perp_quantity != Decimal("0.20"):
        raise DemoMutationRefusedError("D5 requires the sealed 0.20 ETH demo cap")
    credentials = load_bybit_demo_profile()
    if (
        credentials.rest_base_url != deployment.rest_base_url
        or credentials.private_websocket_url
        != deployment.private_websocket_url
    ):
        raise DemoMutationRefusedError("demo credential endpoints do not match")

    clock = ServerClock(
        max_absolute_offset_ms=deployment.maximum_clock_drift_ms,
    )
    private = BybitPrivateRestClient(profile=credentials, clock=clock)
    public = BybitPublicRestClient()
    store = SqliteExecutionStore(deployment.database_path)
    await store.initialize()
    await _synchronize_clock(private)
    await _apply_recorded_emergency_exits(store, clock)
    await _reconcile_late_option_longs(
        store=store,
        private=private,
        clock=clock,
    )
    cycle_number = _next_cycle_number(await store.load_all_order_intents())
    initial_report, initial_exchange = await _reconcile(
        store,
        private,
        clock,
        cycle_number=cycle_number,
    )
    _require_matched(initial_report, "initial D5 reconciliation")
    _require_one_way_account(initial_exchange)
    await _ensure_option_margin_mode(private)

    try:
        option = await _open_or_load_option_spread(
            store=store,
            private=private,
            public=public,
            clock=clock,
            cycle_number=cycle_number,
            deployment=deployment,
        )
        await _synchronize_clock(private)
        option_report, _ = await _reconcile(
            store,
            private,
            clock,
            cycle_number=cycle_number,
        )
        _require_matched(option_report, "D5 option reconciliation")
        return await _run_multiple_perp_cycle(
            store=store,
            private=private,
            public=public,
            clock=clock,
            deployment=deployment,
            option=option,
            cycle_number=cycle_number,
        )
    except BaseException:
        await _make_perp_safe(
            store=store,
            private=private,
            clock=clock,
            cycle_number=cycle_number,
        )
        raise


async def run_d6_recovery() -> DemoD6Result:
    if os.environ.get(MUTATION_GATE_ENV) != D6_MUTATION_TOKEN:
        raise DemoMutationRefusedError(
            f"set {MUTATION_GATE_ENV}={D6_MUTATION_TOKEN} explicitly"
        )
    deployment = _demo_deployment_profile()
    if not deployment.external_order_mutations_enabled:
        raise DemoMutationRefusedError("demo profile disables order mutations")
    if deployment.risk_limits.maximum_perp_quantity != Decimal("0.20"):
        raise DemoMutationRefusedError("D6 requires the sealed 0.20 ETH demo cap")
    credentials = load_bybit_demo_profile()
    if (
        credentials.rest_base_url != deployment.rest_base_url
        or credentials.private_websocket_url
        != deployment.private_websocket_url
    ):
        raise DemoMutationRefusedError("demo credential endpoints do not match")

    clock = ServerClock(
        max_absolute_offset_ms=deployment.maximum_clock_drift_ms,
    )
    private = BybitPrivateRestClient(profile=credentials, clock=clock)
    public = BybitPublicRestClient()
    store = SqliteExecutionStore(deployment.database_path)
    await store.initialize()
    await _synchronize_clock(private)
    await _apply_recorded_emergency_exits(store, clock)
    await _reconcile_late_option_longs(
        store=store,
        private=private,
        clock=clock,
    )
    cycle_number = _next_cycle_number(await store.load_all_order_intents())
    if cycle_number > 99:
        raise DemoMutationRefusedError("D6 exhausted isolated demo level IDs")
    initial_report, initial_exchange = await _reconcile(
        store,
        private,
        clock,
        cycle_number=cycle_number,
    )
    _require_matched(initial_report, "initial D6 reconciliation")
    _require_one_way_account(initial_exchange)
    await _ensure_option_margin_mode(private)

    try:
        option = await _open_or_load_option_spread(
            store=store,
            private=private,
            public=public,
            clock=clock,
            cycle_number=cycle_number,
            deployment=deployment,
        )
        await _synchronize_clock(private)
        option_report, _ = await _reconcile(
            store,
            private,
            clock,
            cycle_number=cycle_number,
        )
        _require_matched(option_report, "D6 option reconciliation")
        return await _run_recovery_perp_cycle(
            store=store,
            private=private,
            public=public,
            clock=clock,
            deployment=deployment,
            option=option,
            cycle_number=cycle_number,
        )
    except BaseException:
        await _make_perp_safe(
            store=store,
            private=private,
            clock=clock,
            cycle_number=cycle_number,
        )
        raise


async def _run_recovery_perp_cycle(
    *,
    store: SqliteExecutionStore,
    private: BybitPrivateRestClient,
    public: BybitPublicRestClient,
    clock: ServerClock,
    deployment: EnvironmentProfile,
    option: OptionSpreadExecutionSnapshot,
    cycle_number: int,
) -> DemoD6Result:
    instrument, book, option_instruments, option_quotes, wallet = (
        await asyncio.gather(
            public.get_instrument("ETHUSDT"),
            public.get_orderbook_snapshot("ETHUSDT", 1),
            public.list_instruments("option", base_coin="ETH"),
            public.get_option_chain("ETH"),
            private.get_wallet_state(),
        )
    )
    if instrument.status != "Trading" or not book.bids or not book.asks:
        raise DemoMutationRefusedError("ETHUSDT market is not executable")
    level_id = cycle_number
    tick = instrument.price_filter.tick_size
    entry_price = quantize_limit_price(
        book.bids[0][0],
        tick,
        side="Sell",
        policy=PriceQuantizationPolicy.PASSIVE,
    )
    tp_distance = max(Decimal("2.75"), tick * Decimal("275"))
    tp_price = entry_price - tp_distance
    if tp_price <= ZERO:
        raise DemoMutationRefusedError("D6 TP price is invalid")
    level = HedgeLevel(
        level_id=level_id,
        entry_price=entry_price,
        tp_price=tp_price,
        stop_price=entry_price * Decimal("1.005"),
        option_budget=option.matched_quantity * tp_distance,
    )
    quote_timestamps = _option_quote_timestamps(option, option_quotes)
    _put_spread_position(option, option_instruments)
    _approve_perp_risk(
        quantity=option.matched_quantity,
        reference_price=entry_price,
        wallet_equity=wallet.total_equity,
        deployment=deployment,
    )

    baseline_entry_id = _d6_order_id(
        cycle_number,
        level_id,
        ClientOrderRole.HEDGE_ENTRY,
        1,
    )
    baseline_stop_id = _d6_order_id(
        cycle_number,
        level_id,
        ClientOrderRole.HEDGE_STOP,
        1,
    )
    baseline_tp_id = _d6_order_id(
        cycle_number,
        level_id,
        ClientOrderRole.HEDGE_TP,
        1,
    )
    entry_service, exit_service, lifecycle = _lifecycle_services(
        store=store,
        private=private,
        instrument=instrument,
        clock=clock,
        fill_attempts=30,
        fill_interval_seconds=1,
    )
    await _synchronize_clock(private)
    baseline_entry = await entry_service.submit_entry(
        PlaceOrderRequest(
            category="linear",
            symbol="ETHUSDT",
            side="Sell",
            order_type="Market",
            quantity=option.matched_quantity,
            order_link_id=baseline_entry_id,
            time_in_force="IOC",
            position_idx=0,
        )
    )
    baseline_entry = await lifecycle.await_entry_fill(baseline_entry)
    baseline_protection = await exit_service.install_stop(
        baseline_entry_id,
        instrument,
        baseline_stop_id,
        stop_rate=Decimal("0.0002"),
    )
    baseline_protection = await exit_service.install_take_profit(
        baseline_protection,
        instrument,
        baseline_tp_id,
        desired_price=level.tp_price,
    )
    _d6_progress(
        "BASELINE_PROTECTED",
        cycle=cycle_number,
        quantity=baseline_entry.filled_quantity,
        stop=baseline_protection.stop_trigger_price,
        tp=baseline_protection.tp_price,
    )
    await _verify_liquidation_distance(
        private,
        deployment=deployment,
        instrument=instrument,
    )
    baseline_closed = await _await_d6_exit(
        lifecycle,
        baseline_entry_id,
        private,
    )
    if baseline_closed.state is not LiveExecutionState.CLOSED_STOP:
        raise DemoMutationRefusedError(
            "D6 baseline reached TP before an actual stop fill"
        )
    if (
        baseline_closed.stop_filled_quantity == ZERO
        or baseline_closed.confirmed_recovery_debt == ZERO
    ):
        raise DemoMutationRefusedError("D6 stop produced no confirmed debt")
    baseline_average = baseline_entry.average_entry_price
    if baseline_average is None:
        raise AssertionError("D6 baseline entry has no actual average price")
    baseline_stop_fill = (
        baseline_closed.exit_notional / baseline_closed.stop_filled_quantity
    )

    recovery_service = SameLevelRecoveryService(
        entry_service=entry_service,
        store=store,
        planner=SameLevelRecoveryPlanner(RiskEngine()),
        clock=lambda: _clock_time(clock),
    )
    projected_debt = max(
        (
            baseline_closed.stop_trigger_price - baseline_average
        )
        * baseline_entry.filled_quantity
        + baseline_entry.entry_fees * Decimal("2"),
        ZERO,
    )
    debt_snapshot = await recovery_service.record_confirmed_stop_debt(
        level_id=level_id,
        actual_stop_debt=baseline_closed.confirmed_recovery_debt,
        projected_debt=projected_debt,
    )
    _d6_progress(
        "BASELINE_STOPPED",
        cycle=cycle_number,
        stop_fill=baseline_stop_fill,
        confirmed_debt=baseline_closed.confirmed_recovery_debt,
    )
    global_debt, realized_loss = await _demo_loss_state(store)
    recovery_risk = replace(
        _demo_risk_state(
            quantity=deployment.risk_limits.maximum_perp_quantity,
            price=level.entry_price,
            wallet_equity=wallet.total_equity,
            debt=global_debt,
            realized_loss=realized_loss,
        ),
        entries_for_level=1,
        order_requests_last_minute=3,
    )
    rejected = await recovery_service.submit_recovery(
        level=level,
        instrument=instrument,
        risk_state=recovery_risk,
        limits=replace(
            deployment.risk_limits,
            maximum_perp_quantity=instrument.lot_size_filter.qty_step,
        ),
        order_link_id=_d6_order_id(
            cycle_number,
            level_id,
            ClientOrderRole.HEDGE_ENTRY,
            99,
        ),
    )
    if (
        rejected.plan.approved
        or rejected.entry_snapshot is not None
        or rejected.plan.locked_action
        is not LockedLevelAction.CLOSE_OPTION_STRATEGY
    ):
        raise AssertionError("D6 finite-limit rejection did not lock the level")
    if rejected.debt_snapshot != debt_snapshot:
        raise AssertionError("rejected D6 recovery changed debt allocation")
    _d6_progress(
        "REJECTION_LOCKED",
        cycle=cycle_number,
        action=rejected.plan.locked_action.value,
    )

    crossing = await _await_d6_recovery_crossing(
        public=public,
        private=private,
        clock=clock,
        deployment=deployment,
        option=option,
        quote_timestamps=quote_timestamps,
        entry_price=level.entry_price,
    )
    _d6_progress(
        "RECOVERY_CROSSED",
        cycle=cycle_number,
        price=crossing.price,
        observed_at=crossing.timestamp_utc,
    )
    recovery_entry_id = _d6_order_id(
        cycle_number,
        level_id,
        ClientOrderRole.HEDGE_ENTRY,
        2,
    )
    recovery_stop_id = _d6_order_id(
        cycle_number,
        level_id,
        ClientOrderRole.HEDGE_STOP,
        2,
    )
    recovery_tp_id = _d6_order_id(
        cycle_number,
        level_id,
        ClientOrderRole.HEDGE_TP,
        2,
    )
    await _synchronize_clock(private)
    submission = await recovery_service.submit_recovery(
        level=level,
        instrument=instrument,
        risk_state=recovery_risk,
        limits=deployment.risk_limits,
        order_link_id=recovery_entry_id,
    )
    if (
        not submission.plan.approved
        or submission.plan.quantity is None
        or submission.entry_snapshot is None
    ):
        raise DemoMutationRefusedError(
            "D6 recovery was blocked: " + "; ".join(submission.plan.reasons)
        )
    recovery_opened = await lifecycle.protect_submitted_entry(
        submission.entry_snapshot,
        stop_order_link_id=recovery_stop_id,
        take_profit_order_link_id=recovery_tp_id,
        stop_rate=Decimal("0.005"),
        take_profit_price=level.tp_price,
    )
    _d6_progress(
        "RECOVERY_PROTECTED",
        cycle=cycle_number,
        quantity=recovery_opened.entry.filled_quantity,
        stop=recovery_opened.protection.stop_trigger_price,
        tp=recovery_opened.protection.tp_price,
    )
    await _verify_liquidation_distance(
        private,
        deployment=deployment,
        instrument=instrument,
    )

    await _synchronize_clock(private)
    restarted_store = SqliteExecutionStore(deployment.database_path)
    await restarted_store.initialize()
    restart_report, _ = await _reconcile(
        restarted_store,
        private,
        clock,
        cycle_number=cycle_number,
    )
    _require_matched(restart_report, "D6 protected recovery restart")
    _, _, restarted_lifecycle = _lifecycle_services(
        store=restarted_store,
        private=private,
        instrument=instrument,
        clock=clock,
        fill_attempts=30,
        fill_interval_seconds=1,
    )
    recovery_closed = await _await_d6_exit(
        restarted_lifecycle,
        recovery_entry_id,
        private,
    )
    if recovery_closed.state is LiveExecutionState.CLOSED_STOP:
        recovery_projected_debt = max(
            (
                recovery_closed.stop_trigger_price
                - recovery_closed.average_entry_price
            )
            * recovery_closed.entry_quantity
            + recovery_closed.entry_fees * Decimal("2"),
            ZERO,
        )
        await SameLevelRecoveryService(
            entry_service=entry_service,
            store=restarted_store,
            planner=SameLevelRecoveryPlanner(RiskEngine()),
            clock=lambda: _clock_time(clock),
        ).record_recovery_stop_debt(
            level_id=level_id,
            actual_stop_debt=recovery_closed.confirmed_recovery_debt,
            projected_debt=recovery_projected_debt,
        )
        raise DemoMutationRefusedError("D6 recovery stopped before its TP")
    if recovery_closed.state is not LiveExecutionState.CLOSED_TP:
        raise DemoMutationRefusedError("D6 recovery did not reach a TP state")
    _d6_progress(
        "RECOVERY_TP_FILLED",
        cycle=cycle_number,
        realized_pnl=recovery_closed.realized_pnl,
    )

    net_zone_budget = max(
        level.option_budget
        - recovery_closed.entry_fees
        - recovery_closed.exit_fees,
        ZERO,
    )
    settled = await SameLevelRecoveryService(
        entry_service=entry_service,
        store=restarted_store,
        planner=SameLevelRecoveryPlanner(RiskEngine()),
        clock=lambda: _clock_time(clock),
    ).settle_take_profit(
        level_id=level_id,
        realized_take_profit=recovery_closed.realized_pnl,
        zone_budget=net_zone_budget,
    )
    if settled.debt.remaining_debt != ZERO:
        raise DemoMutationRefusedError(
            "D6 actual TP left confirmed recovery debt unpaid"
        )

    await _synchronize_clock(private)
    final_store = SqliteExecutionStore(deployment.database_path)
    await final_store.initialize()
    final_report, final_exchange = await _reconcile(
        final_store,
        private,
        clock,
        cycle_number=cycle_number,
    )
    _require_matched(final_report, "final D6 reconciliation")
    if any(
        position.category == "linear" and position.quantity > ZERO
        for position in final_exchange.positions
    ):
        raise RuntimeError("ETHUSDT position remains after D6 recovery")
    recovery_average = recovery_opened.entry.average_entry_price
    if recovery_average is None or recovery_closed.tp_filled_quantity == ZERO:
        raise AssertionError("D6 recovery lacks actual TP execution data")
    recovery_tp_fill = (
        recovery_closed.exit_notional / recovery_closed.tp_filled_quantity
    )
    return DemoD6Result(
        option_cycle_id=option.cycle_id,
        perp_cycle_number=cycle_number,
        level_id=level_id,
        baseline_average_entry_price=baseline_average,
        baseline_stop_trigger_price=baseline_closed.stop_trigger_price,
        baseline_stop_fill_price=baseline_stop_fill,
        actual_stop_debt=baseline_closed.confirmed_recovery_debt,
        recovery_crossing_price=crossing.price,
        recovery_crossing_time_utc=crossing.timestamp_utc,
        connection_generation=crossing.connection_generation,
        raw_recovery_quantity=submission.plan.raw_desired_quantity,
        recovery_quantity=submission.plan.quantity,
        expected_take_profit=submission.plan.expected_take_profit,
        allocated_debt=submission.plan.allocated_debt,
        rejected_locked_action=rejected.plan.locked_action.value,
        recovery_average_entry_price=recovery_average,
        recovery_take_profit_price=recovery_tp_fill,
        recovery_state=recovery_closed.state.value,
        recovery_realized_pnl=recovery_closed.realized_pnl,
        net_zone_budget=net_zone_budget,
        remaining_debt=settled.debt.remaining_debt,
        protected_restart_status=restart_report.status.value,
        final_reconciliation_status=final_report.status.value,
    )


async def _await_d6_recovery_crossing(
    *,
    public: BybitPublicRestClient,
    private: BybitPrivateRestClient,
    clock: ServerClock,
    deployment: EnvironmentProfile,
    option: OptionSpreadExecutionSnapshot,
    quote_timestamps: tuple[datetime, ...],
    entry_price: Decimal,
) -> TradeEvent:
    market_data = BybitPublicMarketData(rest=public)
    trade_stream = cast(
        AsyncGenerator[TradeEvent, None],
        market_data.stream_trades("ETHUSDT"),
    )
    router = TriggerPriceRouter(TriggerPriceSource.LAST_TRADE)
    previous_price: Decimal | None = None
    connection_generation: int | None = None
    armed = False
    loop = asyncio.get_running_loop()
    await _synchronize_clock(private)
    last_clock_sync = loop.time()
    try:
        async with asyncio.timeout(1_800):
            async for trade in trade_stream:
                if loop.time() - last_clock_sync >= 30:
                    await _synchronize_clock(private)
                    last_clock_sync = loop.time()
                if _quotes_need_refresh(quote_timestamps, clock):
                    quotes = await public.get_option_chain("ETH")
                    quote_timestamps = _option_quote_timestamps(option, quotes)
                trigger = router.from_trade(trade)
                if trigger is None:
                    raise AssertionError("LAST_TRADE router rejected a trade")
                health = _demo_market_health(
                    trade,
                    quote_timestamps,
                    deployment,
                    clock,
                )
                if not health.trading_allowed:
                    previous_price = None
                    connection_generation = None
                    armed = False
                    continue
                if connection_generation != trade.connection_generation:
                    connection_generation = trade.connection_generation
                    previous_price = trigger.observed_price
                    armed = trigger.observed_price >= entry_price
                    continue
                if previous_price is None:
                    raise AssertionError("fresh D6 segment has no previous price")
                current_price = trigger.observed_price
                if current_price >= entry_price:
                    armed = True
                crossed = (
                    armed
                    and previous_price > entry_price
                    and current_price <= entry_price
                )
                previous_price = current_price
                if crossed:
                    return trade
    except TimeoutError as exc:
        raise DemoMutationRefusedError(
            "no fresh same-level recovery crossing arrived within 30 minutes"
        ) from exc
    finally:
        await trade_stream.aclose()
    raise AssertionError("D6 trade stream ended without a crossing")


async def _await_d6_exit(
    lifecycle: OneLevelLifecycleService,
    entry_order_link_id: str,
    private: BybitPrivateRestClient,
) -> ProtectionSnapshot:
    last_error: ExitFillNotConfirmedError | None = None
    for _ in range(20):
        await _synchronize_clock(private)
        try:
            return await lifecycle.await_exit(entry_order_link_id)
        except ClockStaleError:
            continue
        except ExitFillNotConfirmedError as exc:
            last_error = exc
    if last_error is None:
        raise AssertionError("D6 exit wait did not run")
    raise last_error


def _d6_order_id(
    cycle_number: int,
    level_id: int,
    role: ClientOrderRole,
    attempt: int,
) -> str:
    return str(
        ClientOrderId.new(
            STRATEGY_INSTANCE,
            cycle_number,
            level_id,
            role,
            attempt,
        )
    )


def _d6_progress(event: str, **fields: object) -> None:
    print(
        json.dumps(
            {"event": event, **fields},
            default=lambda value: (
                value.isoformat() if isinstance(value, datetime) else str(value)
            ),
            separators=(",", ":"),
            sort_keys=True,
        ),
        flush=True,
    )


async def _run_automatic_perp_cycle(
    *,
    store: SqliteExecutionStore,
    private: BybitPrivateRestClient,
    public: BybitPublicRestClient,
    clock: ServerClock,
    deployment: EnvironmentProfile,
    option: OptionSpreadExecutionSnapshot,
    cycle_number: int,
) -> DemoD4Result:
    instrument, option_instruments, option_quotes, wallet = await asyncio.gather(
        public.get_instrument("ETHUSDT"),
        public.list_instruments("option", base_coin="ETH"),
        public.get_option_chain("ETH"),
        private.get_wallet_state(),
    )
    if instrument.status != "Trading":
        raise DemoMutationRefusedError("ETHUSDT is not Trading")
    option_position = _put_spread_position(option, option_instruments)
    quote_timestamps = _option_quote_timestamps(option, option_quotes)
    entry_id = str(
        ClientOrderId.new(
            STRATEGY_INSTANCE,
            cycle_number,
            1,
            ClientOrderRole.HEDGE_ENTRY,
            1,
        )
    )
    stop_id = str(
        ClientOrderId.new(
            STRATEGY_INSTANCE,
            cycle_number,
            1,
            ClientOrderRole.HEDGE_STOP,
            1,
        )
    )
    tp_id = str(
        ClientOrderId.new(
            STRATEGY_INSTANCE,
            cycle_number,
            1,
            ClientOrderRole.HEDGE_TP,
            1,
        )
    )
    entry_service, _, lifecycle = _lifecycle_services(
        store=store,
        private=private,
        instrument=instrument,
        clock=clock,
    )
    market_data = BybitPublicMarketData(rest=public)
    trade_stream = cast(
        AsyncGenerator[TradeEvent, None],
        market_data.stream_trades("ETHUSDT"),
    )
    try:
        first_trade = await asyncio.wait_for(anext(trade_stream), timeout=20)
        level_entry = first_trade.price - instrument.price_filter.tick_size
        if level_entry <= ZERO:
            raise DemoMutationRefusedError("automatic level entry is invalid")
        level = HedgeLevel(
            level_id=1,
            entry_price=level_entry,
            tp_price=level_entry * Decimal("0.995"),
            stop_price=level_entry * Decimal("1.005"),
            option_budget=(
                option.matched_quantity
                * level_entry
                * Decimal("0.005")
            ),
        )
        coordinator = OneLevelCoordinator(
            entry_service=entry_service,
            option_position=option_position,
            level=level,
            instrument=instrument,
            risk_engine=RiskEngine(),
            risk_limits=deployment.risk_limits,
            order_link_id_factory=lambda: entry_id,
        )
        router = TriggerPriceRouter(TriggerPriceSource.LAST_TRADE)
        first_trigger = router.from_trade(first_trade)
        if first_trigger is None:
            raise AssertionError("LAST_TRADE router rejected a trade")
        debt, realized_loss = await _demo_loss_state(store)
        first_health = _demo_market_health(
            first_trade,
            quote_timestamps,
            deployment,
            clock,
        )
        armed = await coordinator.on_trigger(
            first_trigger,
            first_health,
            _demo_risk_state(
                quantity=option.matched_quantity,
                price=first_trade.price,
                wallet_equity=wallet.total_equity,
                debt=debt,
                realized_loss=realized_loss,
            ),
        )
        if armed.triggered or armed.reasons:
            raise DemoMutationRefusedError(
                "first D4 trade did not arm a clean crossing segment"
            )

        crossing_trade: TradeEvent | None = None
        trigger_result = None
        async with asyncio.timeout(60):
            async for trade in trade_stream:
                if _quotes_need_refresh(quote_timestamps, clock):
                    option_quotes = await public.get_option_chain("ETH")
                    quote_timestamps = _option_quote_timestamps(
                        option,
                        option_quotes,
                    )
                trigger = router.from_trade(trade)
                if trigger is None:
                    raise AssertionError("LAST_TRADE router rejected a trade")
                health = _demo_market_health(
                    trade,
                    quote_timestamps,
                    deployment,
                    clock,
                )
                result = await coordinator.on_trigger(
                    trigger,
                    health,
                    _demo_risk_state(
                        quantity=option.matched_quantity,
                        price=trade.price,
                        wallet_equity=wallet.total_equity,
                        debt=debt,
                        realized_loss=realized_loss,
                    ),
                )
                if result.triggered:
                    crossing_trade = trade
                    trigger_result = result
                    break
    except TimeoutError as exc:
        raise DemoMutationRefusedError(
            "no fresh downward LAST_TRADE crossing arrived within 60 seconds"
        ) from exc
    finally:
        await trade_stream.aclose()

    if (
        crossing_trade is None
        or trigger_result is None
        or trigger_result.snapshot is None
        or trigger_result.request is None
    ):
        raise DemoMutationRefusedError("D4 crossing did not submit an entry")
    opened = await lifecycle.protect_submitted_entry(
        trigger_result.snapshot,
        stop_order_link_id=stop_id,
        take_profit_order_link_id=tp_id,
        stop_rate=Decimal("0.005"),
        take_profit_price=level.tp_price,
    )
    await _verify_liquidation_distance(
        private,
        deployment=deployment,
        instrument=instrument,
    )
    outcome = await _finish_protected_cycle(
        private=private,
        public=public,
        clock=clock,
        deployment=deployment,
        instrument=instrument,
        opened=opened,
        entry_order_link_id=entry_id,
        take_profit_order_link_id=tp_id,
        cycle_number=cycle_number,
    )
    average_entry = opened.entry.average_entry_price
    if average_entry is None:
        raise AssertionError("D4 entry has no actual average price")
    return DemoD4Result(
        option_cycle_id=option.cycle_id,
        perp_cycle_number=cycle_number,
        trigger_source=TriggerPriceSource.LAST_TRADE.value,
        armed_price=first_trade.price,
        level_entry_price=level.entry_price,
        crossing_price=crossing_trade.price,
        crossing_time_utc=crossing_trade.timestamp_utc,
        connection_generation=crossing_trade.connection_generation,
        request_quantity=trigger_result.request.quantity,
        average_entry_price=average_entry,
        stop_trigger_price=opened.protection.stop_trigger_price,
        protected_restart_status=outcome.restart_status,
        final_state=outcome.final_state.value,
        realized_pnl=outcome.realized_pnl,
        final_reconciliation_status=outcome.final_reconciliation_status,
    )


async def _run_multiple_perp_cycle(
    *,
    store: SqliteExecutionStore,
    private: BybitPrivateRestClient,
    public: BybitPublicRestClient,
    clock: ServerClock,
    deployment: EnvironmentProfile,
    option: OptionSpreadExecutionSnapshot,
    cycle_number: int,
) -> DemoD5Result:
    instrument, option_instruments, option_quotes, wallet = await asyncio.gather(
        public.get_instrument("ETHUSDT"),
        public.list_instruments("option", base_coin="ETH"),
        public.get_option_chain("ETH"),
        private.get_wallet_state(),
    )
    if instrument.status != "Trading":
        raise DemoMutationRefusedError("ETHUSDT is not Trading")
    option_position = _put_spread_position(option, option_instruments)
    quote_timestamps = _option_quote_timestamps(option, option_quotes)
    entry_service, exit_service, lifecycle = _lifecycle_services(
        store=store,
        private=private,
        instrument=instrument,
        clock=clock,
    )
    market_data = BybitPublicMarketData(rest=public)
    trade_stream = cast(
        AsyncGenerator[TradeEvent, None],
        market_data.stream_trades("ETHUSDT"),
    )
    active_entries: dict[int, EntryExecutionSnapshot] = {}
    protections: dict[int, ProtectionSnapshot] = {}
    crossing_prices: list[Decimal] = []
    try:
        first_trade = await asyncio.wait_for(anext(trade_stream), timeout=20)
        tick = instrument.price_filter.tick_size
        levels = tuple(
            HedgeLevel(
                level_id=level_id,
                entry_price=first_trade.price - tick * level_id,
                tp_price=(first_trade.price - tick * level_id)
                * Decimal("0.995"),
                stop_price=(first_trade.price - tick * level_id)
                * Decimal("1.005"),
                option_budget=(
                    option.matched_quantity
                    * (first_trade.price - tick * level_id)
                    * Decimal("0.005")
                ),
            )
            for level_id in (1, 2)
        )
        if any(level.entry_price <= ZERO for level in levels):
            raise DemoMutationRefusedError("D5 level price is invalid")
        coordinator = MultiLevelCoordinator(
            entry_service=entry_service,
            store=store,
            option_position=option_position,
            levels=levels,
            instrument=instrument,
            risk_engine=RiskEngine(),
            risk_limits=deployment.risk_limits,
            order_link_id_factory=lambda level_id, attempt: str(
                ClientOrderId.new(
                    STRATEGY_INSTANCE,
                    cycle_number,
                    level_id,
                    ClientOrderRole.HEDGE_ENTRY,
                    attempt,
                )
            ),
        )
        router = TriggerPriceRouter(TriggerPriceSource.LAST_TRADE)
        first_trigger = router.from_trade(first_trade)
        if first_trigger is None:
            raise AssertionError("LAST_TRADE router rejected a trade")
        debt, realized_loss = await _demo_loss_state(store)
        armed = await coordinator.on_trigger(
            first_trigger,
            _demo_market_health(
                first_trade,
                quote_timestamps,
                deployment,
                clock,
            ),
            _d5_risk_state(
                price=first_trade.price,
                wallet_equity=wallet.total_equity,
                debt=debt,
                realized_loss=realized_loss,
                active_entries=0,
                current_quantity=ZERO,
                current_notional=ZERO,
            ),
        )
        if armed.entries or armed.blocked or armed.reasons:
            raise DemoMutationRefusedError(
                "first D5 trade did not arm a clean crossing segment"
            )

        async with asyncio.timeout(90):
            async for trade in trade_stream:
                if _quotes_need_refresh(quote_timestamps, clock):
                    await _synchronize_clock(private)
                    option_quotes = await public.get_option_chain("ETH")
                    quote_timestamps = _option_quote_timestamps(
                        option,
                        option_quotes,
                    )
                trigger = router.from_trade(trade)
                if trigger is None:
                    raise AssertionError("LAST_TRADE router rejected a trade")
                current_quantity = sum(
                    (entry.filled_quantity for entry in active_entries.values()),
                    start=ZERO,
                )
                current_notional = sum(
                    (entry.entry_notional for entry in active_entries.values()),
                    start=ZERO,
                )
                result = await coordinator.on_trigger(
                    trigger,
                    _demo_market_health(
                        trade,
                        quote_timestamps,
                        deployment,
                        clock,
                    ),
                    _d5_risk_state(
                        price=trade.price,
                        wallet_equity=wallet.total_equity,
                        debt=debt,
                        realized_loss=realized_loss,
                        active_entries=len(active_entries),
                        current_quantity=current_quantity,
                        current_notional=current_notional,
                    ),
                )
                if result.blocked:
                    reasons = "; ".join(
                        reason
                        for block in result.blocked
                        for reason in block.reasons
                    )
                    raise DemoMutationRefusedError(
                        "D5 baseline entry was blocked: " + reasons
                    )
                if not result.entries:
                    continue
                await _synchronize_clock(private)
                filled_batch: list[tuple[int, EntryExecutionSnapshot]] = []
                for submission in result.entries:
                    filled = await lifecycle.await_entry_fill(
                        submission.snapshot
                    )
                    filled_batch.append((submission.level_id, filled))
                positions = await private.get_positions("linear", "ETHUSDT")
                if not await coordinator.reconcile_aggregate_position(positions):
                    raise DemoMutationRefusedError(
                        "D5 aggregate entry position did not reconcile"
                    )
                for level_id, filled in filled_batch:
                    level = levels[level_id - 1]
                    stop_id = str(
                        ClientOrderId.new(
                            STRATEGY_INSTANCE,
                            cycle_number,
                            level_id,
                            ClientOrderRole.HEDGE_STOP,
                            1,
                        )
                    )
                    tp_id = str(
                        ClientOrderId.new(
                            STRATEGY_INSTANCE,
                            cycle_number,
                            level_id,
                            ClientOrderRole.HEDGE_TP,
                            1,
                        )
                    )
                    protected = await exit_service.install_stop(
                        filled.order_link_id,
                        instrument,
                        stop_id,
                        stop_rate=Decimal("0.005"),
                    )
                    protected = await exit_service.install_take_profit(
                        protected,
                        instrument,
                        tp_id,
                        desired_price=level.tp_price,
                    )
                    active_entries[level_id] = filled
                    protections[level_id] = protected
                    crossing_prices.append(trade.price)
                if len(active_entries) == 2:
                    break
    except TimeoutError as exc:
        raise DemoMutationRefusedError(
            "both D5 LAST_TRADE levels did not cross within 90 seconds"
        ) from exc
    finally:
        await trade_stream.aclose()

    if len(active_entries) != 2 or len(protections) != 2:
        raise DemoMutationRefusedError("D5 did not protect exactly two levels")
    positions = await private.get_positions("linear", "ETHUSDT")
    ordered_protections = tuple(protections[index] for index in (1, 2))
    if not aggregate_protection_position_matches(
        ordered_protections,
        positions,
    ):
        raise DemoMutationRefusedError(
            "D5 protected aggregate position does not reconcile"
        )
    await _verify_liquidation_distance(
        private,
        deployment=deployment,
        instrument=instrument,
    )

    await _synchronize_clock(private)
    restarted_store = SqliteExecutionStore(deployment.database_path)
    await restarted_store.initialize()
    restart_report, _ = await _reconcile(
        restarted_store,
        private,
        clock,
        cycle_number=cycle_number,
    )
    _require_matched(restart_report, "D5 protected restart reconciliation")
    _, _, restarted_lifecycle = _lifecycle_services(
        store=restarted_store,
        private=private,
        instrument=instrument,
        clock=clock,
    )
    closed: list[ProtectionSnapshot] = []
    for level_id in (1, 2):
        fresh_book = await public.get_orderbook_snapshot("ETHUSDT", 1)
        marketable_tp = quantize_limit_price(
            fresh_book.asks[0][0] + tick * 10,
            tick,
            side="Buy",
            policy=PriceQuantizationPolicy.AGGRESSIVE,
        )
        await _synchronize_clock(private)
        protection = protections[level_id]
        if protection.tp_order_link_id is None:
            raise AssertionError("D5 protected level has no TP")
        try:
            acknowledgement = await private.amend_order(
                AmendOrderRequest(
                    category="linear",
                    symbol="ETHUSDT",
                    order_link_id=protection.tp_order_link_id,
                    price=marketable_tp,
                )
            )
            await restarted_store.record_acknowledgement(acknowledgement)
        except (BybitUnknownOrderError, UncertainOrderOutcomeError):
            pass
        closed.append(
            await restarted_lifecycle.await_exit(
                protection.entry_order_link_id
            )
        )

    await _synchronize_clock(private)
    final_store = SqliteExecutionStore(deployment.database_path)
    await final_store.initialize()
    final_report, final_exchange = await _reconcile(
        final_store,
        private,
        clock,
        cycle_number=cycle_number,
    )
    _require_matched(final_report, "final D5 reconciliation")
    if any(
        position.category == "linear" and position.quantity > ZERO
        for position in final_exchange.positions
    ):
        raise RuntimeError("ETHUSDT position remains after D5 exits")

    ordered_entries = tuple(active_entries[index] for index in (1, 2))
    average_prices = tuple(
        entry.average_entry_price for entry in ordered_entries
    )
    if any(price is None for price in average_prices):
        raise AssertionError("D5 entry has no actual average price")
    return DemoD5Result(
        option_cycle_id=option.cycle_id,
        perp_cycle_number=cycle_number,
        trigger_source=TriggerPriceSource.LAST_TRADE.value,
        level_entry_prices=tuple(level.entry_price for level in levels),
        crossing_prices=tuple(crossing_prices),
        aggregate_quantity=sum(
            (entry.filled_quantity for entry in ordered_entries),
            start=ZERO,
        ),
        average_entry_prices=cast(tuple[Decimal, ...], average_prices),
        stop_trigger_prices=tuple(
            protection.stop_trigger_price
            for protection in ordered_protections
        ),
        protected_restart_status=restart_report.status.value,
        final_states=tuple(item.state.value for item in closed),
        realized_pnls=tuple(item.realized_pnl for item in closed),
        final_reconciliation_status=final_report.status.value,
    )


def _put_spread_position(
    snapshot: OptionSpreadExecutionSnapshot,
    instruments: tuple[InstrumentSpec, ...],
) -> PutCreditSpreadPosition:
    by_symbol = {instrument.symbol: instrument for instrument in instruments}
    long_instrument = by_symbol.get(snapshot.long_symbol)
    short_instrument = by_symbol.get(snapshot.short_symbol)
    if long_instrument is None or short_instrument is None:
        raise DemoMutationRefusedError("open option instruments are unavailable")

    def contract(instrument: InstrumentSpec) -> OptionContract:
        return OptionContract(
            symbol=instrument.symbol,
            base_coin=instrument.base_coin,
            quote_coin=instrument.quote_coin,
            settle_coin=instrument.settle_coin,
            option_type="Put",
            strike=_strike(instrument.symbol),
            expiry_time_utc=_required_delivery(instrument),
            contract_multiplier=instrument.contract_multiplier,
        )

    return PutCreditSpreadPosition(
        short_put=OptionLegPosition(
            contract=contract(short_instrument),
            side="Short",
            requested_quantity=snapshot.requested_quantity,
            filled_quantity=snapshot.short_filled_quantity,
            average_entry_price=snapshot.short_average_price,
            fees_paid=snapshot.short_fees,
        ),
        long_put=OptionLegPosition(
            contract=contract(long_instrument),
            side="Long",
            requested_quantity=snapshot.requested_quantity,
            filled_quantity=snapshot.long_filled_quantity,
            average_entry_price=snapshot.long_average_price,
            fees_paid=snapshot.long_fees,
        ),
        state=snapshot.state,
        opened_time_utc=snapshot.opened_time_utc,
    )


def _option_quote_timestamps(
    snapshot: OptionSpreadExecutionSnapshot,
    quotes: tuple[OptionMarketQuote, ...],
) -> tuple[datetime, ...]:
    by_symbol = {quote.symbol: quote for quote in quotes}
    try:
        return (
            by_symbol[snapshot.long_symbol].timestamp_utc,
            by_symbol[snapshot.short_symbol].timestamp_utc,
        )
    except KeyError as exc:
        raise DemoMutationRefusedError(
            "open option quotes are unavailable"
        ) from exc


def _quotes_need_refresh(
    timestamps: tuple[datetime, ...],
    clock: ServerClock,
) -> bool:
    return any(
        (_clock_time(clock) - timestamp).total_seconds() > 5
        for timestamp in timestamps
    )


def _demo_market_health(
    trade: TradeEvent,
    option_quote_timestamps: tuple[datetime, ...],
    deployment: EnvironmentProfile,
    clock: ServerClock,
) -> MarketDataHealthResult:
    maximum_trigger_age = Decimal(deployment.maximum_market_data_age_ms) / 1000
    return evaluate_market_data_health(
        MarketDataHealthSnapshot(
            trigger_timestamp_utc=trade.timestamp_utc,
            instrument_loaded=True,
            websocket_connected=True,
            option_quote_timestamps_utc=option_quote_timestamps,
            order_book_synchronized=False,
            order_book_timestamp_utc=None,
            clock_synchronized=clock.sample is not None,
        ),
        MarketDataHealthPolicy(
            max_trigger_age_seconds=maximum_trigger_age,
            max_option_quote_age_seconds=Decimal("10"),
            max_order_book_age_seconds=maximum_trigger_age,
        ),
        as_of_utc=_clock_time(clock),
        order_book_required=False,
    )


async def _demo_loss_state(
    store: SqliteExecutionStore,
) -> tuple[Decimal, Decimal]:
    protections = await store.load_all_protection_snapshots()
    debt = sum(
        (snapshot.confirmed_recovery_debt for snapshot in protections),
        start=ZERO,
    )
    realized_loss = sum(
        (max(-snapshot.realized_pnl, ZERO) for snapshot in protections),
        start=ZERO,
    )
    return debt, realized_loss


def _demo_risk_state(
    *,
    quantity: Decimal,
    price: Decimal,
    wallet_equity: Decimal,
    debt: Decimal,
    realized_loss: Decimal,
) -> RiskState:
    if wallet_equity <= ZERO:
        raise DemoMutationRefusedError("demo wallet has no positive equity")
    notional = quantity * price
    return RiskState(
        current_perp_quantity=ZERO,
        current_perp_notional=ZERO,
        post_trade_margin_usage=notional / wallet_equity,
        post_trade_liquidation_distance=Decimal("1"),
        confirmed_recovery_debt=debt,
        realized_cycle_loss=realized_loss,
        daily_realized_loss=realized_loss,
        entries_for_level=0,
        active_levels=0,
        order_requests_last_minute=0,
        consecutive_reconciliation_failures=0,
        market_data_fresh=True,
        reconciliation_succeeded=True,
    )


def _d5_risk_state(
    *,
    price: Decimal,
    wallet_equity: Decimal,
    debt: Decimal,
    realized_loss: Decimal,
    active_entries: int,
    current_quantity: Decimal,
    current_notional: Decimal,
) -> RiskState:
    if wallet_equity <= ZERO:
        raise DemoMutationRefusedError("demo wallet has no positive equity")
    return RiskState(
        current_perp_quantity=current_quantity,
        current_perp_notional=current_notional,
        post_trade_margin_usage=(Decimal("0.20") * price) / wallet_equity,
        post_trade_liquidation_distance=Decimal("1"),
        confirmed_recovery_debt=debt,
        realized_cycle_loss=realized_loss,
        daily_realized_loss=realized_loss,
        entries_for_level=0,
        active_levels=active_entries,
        order_requests_last_minute=active_entries * 3,
        consecutive_reconciliation_failures=0,
        market_data_fresh=True,
        reconciliation_succeeded=True,
    )


async def _open_or_load_option_spread(
    *,
    store: SqliteExecutionStore,
    private: BybitPrivateRestClient,
    public: BybitPublicRestClient,
    clock: ServerClock,
    cycle_number: int,
    deployment: EnvironmentProfile,
) -> OptionSpreadExecutionSnapshot:
    snapshots = await store.load_all_option_spread_snapshots()
    opened = tuple(
        snapshot
        for snapshot in snapshots
        if snapshot.state is OptionPositionState.OPEN
    )
    if len(opened) == 1:
        return opened[0]
    protected = tuple(
        snapshot
        for snapshot in snapshots
        if snapshot.state is OptionPositionState.LONG_PROTECTION_FILLED
    )
    if len(protected) > 1:
        raise DemoMutationRefusedError(
            "multiple unmatched protective option longs exist"
        )

    quotes, instruments = await asyncio.gather(
        public.get_option_chain("ETH"),
        public.list_instruments("option", base_coin="ETH"),
    )
    if len(protected) == 1:
        snapshot = protected[0]
        by_quote = {quote.symbol: quote for quote in quotes}
        by_instrument = {
            instrument.symbol: instrument for instrument in instruments
        }
        short_quote = by_quote.get(snapshot.short_symbol)
        short_instrument = by_instrument.get(snapshot.short_symbol)
        if (
            short_quote is None
            or short_instrument is None
            or not _eligible_short(
                short_quote,
                short_instrument,
                _clock_time(clock),
            )
            or short_quote.bid_price is None
        ):
            raise DemoMutationRefusedError(
                "reconciled protective long has no executable short quote"
            )
        original_cycle = ClientOrderId.parse(
            snapshot.long_order_link_id
        ).cycle
        service = _option_entry_service(store, private, clock)
        return await service.complete_from_protective_long(
            snapshot,
            short_limit_price=quantize_limit_price(
                short_quote.bid_price,
                short_instrument.price_filter.tick_size,
                side="Sell",
                policy=PriceQuantizationPolicy.AGGRESSIVE,
            ),
            short_order_link_id=str(
                ClientOrderId.new(
                    STRATEGY_INSTANCE,
                    original_cycle,
                    0,
                    ClientOrderRole.OPTION_SHORT,
                    1,
                )
            ),
            policy=_option_entry_policy(
                snapshot.requested_quantity,
                snapshot.expected_net_credit,
            ),
        )
    if snapshots:
        safely_rejected = all(
            snapshot.state is OptionPositionState.ERROR
            and snapshot.long_order_id is None
            and snapshot.short_order_id is None
            and snapshot.long_filled_quantity == ZERO
            and snapshot.short_filled_quantity == ZERO
            for snapshot in snapshots
        )
        if not safely_rejected:
            raise DemoMutationRefusedError(
                "durable option state exists but is not exactly one OPEN spread"
            )
    long_quote, short_quote, long_instrument, short_instrument = (
        select_demo_option_pair(
            quotes,
            instruments,
            as_of_utc=_clock_time(clock),
            maximum_loss=deployment.risk_limits.maximum_realized_cycle_loss,
        )
    )
    quantity = max(
        long_instrument.lot_size_filter.min_order_qty,
        short_instrument.lot_size_filter.min_order_qty,
    )
    if (
        quantity % long_instrument.lot_size_filter.qty_step != ZERO
        or quantity % short_instrument.lot_size_filter.qty_step != ZERO
    ):
        raise DemoMutationRefusedError("option quantity steps are incompatible")
    if long_quote.ask_price is None or short_quote.bid_price is None:
        raise AssertionError("selected option quotes must be executable")
    long_price = quantize_limit_price(
        long_quote.ask_price,
        long_instrument.price_filter.tick_size,
        side="Buy",
        policy=PriceQuantizationPolicy.AGGRESSIVE,
    )
    short_price = quantize_limit_price(
        short_quote.bid_price,
        short_instrument.price_filter.tick_size,
        side="Sell",
        policy=PriceQuantizationPolicy.AGGRESSIVE,
    )
    expected_credit = (short_price - long_price) * quantity
    plan = OptionSpreadEntryPlan(
        cycle_id=f"D3-C{cycle_number:04d}",
        long_symbol=long_quote.symbol,
        short_symbol=short_quote.symbol,
        expiry_time_utc=_required_delivery(long_instrument),
        quantity=quantity,
        long_limit_price=long_price,
        short_limit_price=short_price,
        expected_net_credit=expected_credit,
        long_order_link_id=str(
            ClientOrderId.new(
                STRATEGY_INSTANCE,
                cycle_number,
                0,
                ClientOrderRole.OPTION_LONG,
                1,
            )
        ),
        short_order_link_id=str(
            ClientOrderId.new(
                STRATEGY_INSTANCE,
                cycle_number,
                0,
                ClientOrderRole.OPTION_SHORT,
                1,
            )
        ),
    )
    policy = _option_entry_policy(quantity, expected_credit)
    await _synchronize_clock(private)
    service = _option_entry_service(store, private, clock)
    return await service.open_spread(plan, policy)


async def _reconcile_late_option_longs(
    *,
    store: SqliteExecutionStore,
    private: BybitPrivateRestClient,
    clock: ServerClock,
) -> None:
    service = _option_entry_service(store, private, clock)
    for snapshot in await store.load_all_option_spread_snapshots():
        if not (
            snapshot.state is OptionPositionState.ERROR
            and snapshot.long_order_id is not None
            and snapshot.short_order_link_id is None
            and snapshot.short_order_id is None
            and snapshot.short_filled_quantity == ZERO
        ):
            continue
        try:
            await service.reconcile_protective_long(
                snapshot,
                _option_entry_policy(
                    snapshot.requested_quantity,
                    snapshot.expected_net_credit,
                ),
            )
        except OptionSpreadNotOpenedError:
            refreshed = await store.load_option_spread_snapshot(snapshot.cycle_id)
            if refreshed is not None and refreshed.long_filled_quantity > ZERO:
                raise


def _option_entry_service(
    store: SqliteExecutionStore,
    private: BybitPrivateRestClient,
    clock: ServerClock,
) -> OptionSpreadEntryService:
    return OptionSpreadEntryService(
        trading=private,
        store=store,
        clock=lambda: _clock_time(clock),
        fill_attempts=50,
        fill_interval_seconds=0.25,
    )


def _option_entry_policy(
    quantity: Decimal,
    expected_net_credit: Decimal,
) -> OptionEntryPolicy:
    return OptionEntryPolicy(
        max_leg_wait_seconds=Decimal("15"),
        allow_partial_spread=False,
        minimum_matched_quantity=quantity,
        maximum_credit_deviation=max(
            Decimal("1"),
            expected_net_credit / 2,
        ),
        unmatched_long_policy=UnmatchedLongPolicy.RETAIN,
    )


async def _run_protected_perp_cycle(
    *,
    store: SqliteExecutionStore,
    private: BybitPrivateRestClient,
    public: BybitPublicRestClient,
    clock: ServerClock,
    deployment: EnvironmentProfile,
    option: OptionSpreadExecutionSnapshot,
    cycle_number: int,
) -> DemoD3Result:
    instrument, book = await asyncio.gather(
        public.get_instrument("ETHUSDT"),
        public.get_orderbook_snapshot("ETHUSDT", 1),
    )
    if instrument.status != "Trading" or not book.bids or not book.asks:
        raise DemoMutationRefusedError("ETHUSDT market is not executable")
    reference_price = book.bids[0][0]
    wallet = await private.get_wallet_state()
    _approve_perp_risk(
        quantity=option.matched_quantity,
        reference_price=reference_price,
        wallet_equity=wallet.total_equity,
        deployment=deployment,
    )
    entry_id = str(
        ClientOrderId.new(
            STRATEGY_INSTANCE,
            cycle_number,
            1,
            ClientOrderRole.HEDGE_ENTRY,
            1,
        )
    )
    stop_id = str(
        ClientOrderId.new(
            STRATEGY_INSTANCE,
            cycle_number,
            1,
            ClientOrderRole.HEDGE_STOP,
            1,
        )
    )
    tp_id = str(
        ClientOrderId.new(
            STRATEGY_INSTANCE,
            cycle_number,
            1,
            ClientOrderRole.HEDGE_TP,
            1,
        )
    )
    entry_service, exit_service, lifecycle = _lifecycle_services(
        store=store,
        private=private,
        instrument=instrument,
        clock=clock,
    )
    del entry_service, exit_service
    await _synchronize_clock(private)
    opened = await lifecycle.open_and_protect(
        PlaceOrderRequest(
            category="linear",
            symbol="ETHUSDT",
            side="Sell",
            order_type="Market",
            quantity=option.matched_quantity,
            order_link_id=entry_id,
            time_in_force="IOC",
            position_idx=0,
        ),
        stop_order_link_id=stop_id,
        take_profit_order_link_id=tp_id,
        stop_rate=Decimal("0.005"),
        take_profit_price=reference_price * Decimal("0.995"),
        reference_price=reference_price,
    )
    await _verify_liquidation_distance(
        private,
        deployment=deployment,
        instrument=instrument,
    )
    outcome = await _finish_protected_cycle(
        private=private,
        public=public,
        clock=clock,
        deployment=deployment,
        instrument=instrument,
        opened=opened,
        entry_order_link_id=entry_id,
        take_profit_order_link_id=tp_id,
        cycle_number=cycle_number,
    )
    average_entry = opened.entry.average_entry_price
    if average_entry is None:
        raise AssertionError("confirmed entry must have an average price")
    return DemoD3Result(
        option_cycle_id=option.cycle_id,
        option_state=option.state.value,
        option_matched_quantity=option.matched_quantity,
        option_actual_net_credit=option.actual_net_credit,
        perp_cycle_number=cycle_number,
        entry_order_link_id=entry_id,
        entry_quantity=opened.entry.filled_quantity,
        average_entry_price=average_entry,
        stop_order_link_id=stop_id,
        stop_trigger_price=opened.protection.stop_trigger_price,
        take_profit_order_link_id=tp_id,
        protected_restart_status=outcome.restart_status,
        final_state=outcome.final_state.value,
        realized_pnl=outcome.realized_pnl,
        confirmed_recovery_debt=outcome.confirmed_recovery_debt,
        final_reconciliation_status=outcome.final_reconciliation_status,
    )


async def _finish_protected_cycle(
    *,
    private: BybitPrivateRestClient,
    public: BybitPublicRestClient,
    clock: ServerClock,
    deployment: EnvironmentProfile,
    instrument: InstrumentSpec,
    opened: ProtectedOneLevel,
    entry_order_link_id: str,
    take_profit_order_link_id: str,
    cycle_number: int,
) -> _ProtectedCycleOutcome:
    await _synchronize_clock(private)
    restarted_store = SqliteExecutionStore(deployment.database_path)
    await restarted_store.initialize()
    restart_report, _ = await _reconcile(
        restarted_store,
        private,
        clock,
        cycle_number=cycle_number,
    )
    _require_matched(restart_report, "protected restart reconciliation")

    _, _, restarted_lifecycle = _lifecycle_services(
        store=restarted_store,
        private=private,
        instrument=instrument,
        clock=clock,
    )
    fresh_book = await public.get_orderbook_snapshot("ETHUSDT", 1)
    marketable_tp = quantize_limit_price(
        fresh_book.asks[0][0] + instrument.price_filter.tick_size * 10,
        instrument.price_filter.tick_size,
        side="Buy",
        policy=PriceQuantizationPolicy.AGGRESSIVE,
    )
    await _synchronize_clock(private)
    try:
        acknowledgement = await private.amend_order(
            AmendOrderRequest(
                category="linear",
                symbol="ETHUSDT",
                order_link_id=take_profit_order_link_id,
                price=marketable_tp,
            )
        )
        await restarted_store.record_acknowledgement(acknowledgement)
    except (BybitUnknownOrderError, UncertainOrderOutcomeError):
        pass
    closed = await restarted_lifecycle.await_exit(entry_order_link_id)
    if closed.state not in (
        LiveExecutionState.CLOSED_TP,
        LiveExecutionState.CLOSED_STOP,
    ):
        raise RuntimeError("one-level position did not reach a closed state")

    await _synchronize_clock(private)
    final_store = SqliteExecutionStore(deployment.database_path)
    await final_store.initialize()
    final_report, final_exchange = await _reconcile(
        final_store,
        private,
        clock,
        cycle_number=cycle_number,
    )
    _require_matched(final_report, "final one-level reconciliation")
    if any(
        position.category == "linear" and position.quantity > ZERO
        for position in final_exchange.positions
    ):
        raise RuntimeError("ETHUSDT position remains after controlled exit")
    return _ProtectedCycleOutcome(
        opened=opened,
        final_state=closed.state,
        realized_pnl=closed.realized_pnl,
        confirmed_recovery_debt=closed.confirmed_recovery_debt,
        restart_status=restart_report.status.value,
        final_reconciliation_status=final_report.status.value,
    )


def _lifecycle_services(
    *,
    store: SqliteExecutionStore,
    private: BybitPrivateRestClient,
    instrument: InstrumentSpec,
    clock: ServerClock,
    fill_attempts: int = 80,
    fill_interval_seconds: float = 0.25,
) -> tuple[OneLevelEntryService, ProtectiveExitService, OneLevelLifecycleService]:
    entry = OneLevelEntryService(
        trading=private,
        store=store,
        clock=lambda: _clock_time(clock),
    )
    exits = ProtectiveExitService(
        trading=private,
        account=private,
        store=store,
        clock=lambda: _clock_time(clock),
        visibility_attempts=8,
        visibility_interval_seconds=0.25,
    )
    lifecycle = OneLevelLifecycleService(
        trading=private,
        account=private,
        store=store,
        entry_service=entry,
        exit_service=exits,
        instrument=instrument,
        clock=lambda: _clock_time(clock),
        fill_attempts=fill_attempts,
        fill_interval_seconds=fill_interval_seconds,
    )
    return entry, exits, lifecycle


async def _reconcile(
    store: SqliteExecutionStore,
    private: BybitPrivateRestClient,
    clock: ServerClock,
    *,
    cycle_number: int,
) -> tuple[ReconciliationReport, PrivateAccountSnapshot]:
    reader = BybitPrivateStateReader(
        trading=private,
        account=private,
        clock=lambda: _clock_time(clock),
    )
    exchange = await reader.capture()
    option_snapshots = await store.load_all_option_spread_snapshots()
    local = LocalExecutionRecoveryState(
        order_intents=await store.load_all_order_intents(),
        entry_snapshots=await store.load_all_entry_snapshots(),
        protection_snapshots=await store.load_all_protection_snapshots(),
        expected_option_positions=_expected_option_positions(option_snapshots),
        executions=await store.load_all_executions(),
    )
    return (
        evaluate_startup_reconciliation(
            local,
            exchange,
            strategy_instance=STRATEGY_INSTANCE,
            cycle_number=cycle_number,
        ),
        exchange,
    )


def _expected_option_positions(
    snapshots: tuple[OptionSpreadExecutionSnapshot, ...],
) -> tuple[ExpectedPosition, ...]:
    quantities: dict[tuple[str, str], Decimal] = {}
    for snapshot in snapshots:
        if snapshot.long_filled_quantity > ZERO:
            key = (snapshot.long_symbol, "Buy")
            quantities[key] = quantities.get(key, ZERO) + snapshot.long_filled_quantity
        if snapshot.short_filled_quantity > ZERO:
            key = (snapshot.short_symbol, "Sell")
            quantities[key] = quantities.get(key, ZERO) + snapshot.short_filled_quantity
    return tuple(
        ExpectedPosition(
            category="option",
            symbol=symbol,
            side=side,  # type: ignore[arg-type]
            quantity=quantity,
        )
        for (symbol, side), quantity in sorted(quantities.items())
    )


def select_demo_option_pair(
    quotes: tuple[OptionMarketQuote, ...],
    instruments: tuple[InstrumentSpec, ...],
    *,
    as_of_utc: datetime,
    maximum_loss: Decimal,
) -> tuple[OptionMarketQuote, OptionMarketQuote, InstrumentSpec, InstrumentSpec]:
    by_symbol = {instrument.symbol: instrument for instrument in instruments}
    candidates: list[
        tuple[
            tuple[object, ...],
            OptionMarketQuote,
            OptionMarketQuote,
            InstrumentSpec,
            InstrumentSpec,
        ]
    ] = []
    for short_quote in quotes:
        short_instrument = by_symbol.get(short_quote.symbol)
        if not _eligible_short(short_quote, short_instrument, as_of_utc):
            continue
        if short_instrument is None or short_quote.bid_price is None:
            continue
        short_strike = _strike(short_quote.symbol)
        for long_quote in quotes:
            long_instrument = by_symbol.get(long_quote.symbol)
            if not _eligible_long(
                long_quote,
                long_instrument,
                short_instrument,
                as_of_utc,
            ):
                continue
            if long_instrument is None or long_quote.ask_price is None:
                continue
            width = short_strike - _strike(long_quote.symbol)
            if width <= ZERO or width > Decimal("200"):
                continue
            quantity = max(
                short_instrument.lot_size_filter.min_order_qty,
                long_instrument.lot_size_filter.min_order_qty,
            )
            credit = (short_quote.bid_price - long_quote.ask_price) * quantity
            if credit <= ZERO or width * quantity - credit > maximum_loss:
                continue
            expiry = _required_delivery(short_instrument)
            delta_distance = abs(
                (short_quote.delta or Decimal("-9")) - Decimal("-0.30")
            )
            rank: tuple[object, ...] = (
                expiry,
                delta_distance,
                abs(width - Decimal("50")),
                -credit,
                short_quote.symbol,
                long_quote.symbol,
            )
            candidates.append(
                (
                    rank,
                    long_quote,
                    short_quote,
                    long_instrument,
                    short_instrument,
                )
            )
    if not candidates:
        raise DemoMutationRefusedError("no finite-risk liquid ETH put spread found")
    _, long_quote, short_quote, long_instrument, short_instrument = min(
        candidates,
        key=lambda candidate: candidate[0],
    )
    return long_quote, short_quote, long_instrument, short_instrument


def _eligible_short(
    quote: OptionMarketQuote,
    instrument: InstrumentSpec | None,
    as_of: datetime,
) -> bool:
    if not _eligible_quote(quote, instrument, as_of):
        return False
    if quote.bid_price is None or quote.bid_size is None or quote.delta is None:
        return False
    if not Decimal("-0.45") <= quote.delta <= Decimal("-0.15"):
        return False
    if instrument is None:
        return False
    return quote.bid_size >= instrument.lot_size_filter.min_order_qty


def _eligible_long(
    quote: OptionMarketQuote,
    instrument: InstrumentSpec | None,
    short_instrument: InstrumentSpec,
    as_of: datetime,
) -> bool:
    if not _eligible_quote(quote, instrument, as_of):
        return False
    if instrument is None or quote.ask_price is None or quote.ask_size is None:
        return False
    return (
        instrument.delivery_time_utc == short_instrument.delivery_time_utc
        and quote.ask_size >= instrument.lot_size_filter.min_order_qty
    )


def _eligible_quote(
    quote: OptionMarketQuote,
    instrument: InstrumentSpec | None,
    as_of: datetime,
) -> bool:
    if (
        instrument is None
        or instrument.status != "Trading"
        or not quote.symbol.endswith("-P-USDT")
        or quote.bid_price is None
        or quote.ask_price is None
        or quote.bid_price <= ZERO
        or quote.ask_price <= ZERO
    ):
        return False
    expiry = instrument.delivery_time_utc
    if expiry is None or not as_of + timedelta(days=14) <= expiry <= (
        as_of + timedelta(days=90)
    ):
        return False
    age = (as_of - quote.timestamp_utc).total_seconds()
    return 0 <= age <= 10


def _approve_perp_risk(
    *,
    quantity: Decimal,
    reference_price: Decimal,
    wallet_equity: Decimal,
    deployment: EnvironmentProfile,
) -> None:
    if wallet_equity <= ZERO:
        raise DemoMutationRefusedError("demo wallet has no positive equity")
    notional = quantity * reference_price
    projected_stop = notional * Decimal("0.0062")
    proposal = TradeProposal(
        symbol="ETHUSDT",
        side="Sell",
        quantity=quantity,
        price=reference_price,
        notional=notional,
        projected_stop_loss=projected_stop,
        opens_new_level=True,
    )
    decision = RiskEngine().evaluate(
        proposal,
        RiskState(
            current_perp_quantity=ZERO,
            current_perp_notional=ZERO,
            post_trade_margin_usage=notional / wallet_equity,
            post_trade_liquidation_distance=Decimal("1"),
            confirmed_recovery_debt=ZERO,
            realized_cycle_loss=ZERO,
            daily_realized_loss=ZERO,
            entries_for_level=0,
            active_levels=0,
            order_requests_last_minute=0,
            consecutive_reconciliation_failures=0,
            market_data_fresh=True,
            reconciliation_succeeded=True,
        ),
        deployment.risk_limits,
    )
    if not decision.approved or decision.approved_quantity != quantity:
        raise DemoMutationRefusedError(
            "perpetual risk gate rejected: " + "; ".join(decision.reasons)
        )


async def _make_perp_safe(
    *,
    store: SqliteExecutionStore,
    private: BybitPrivateRestClient,
    clock: ServerClock,
    cycle_number: int,
) -> None:
    try:
        await _synchronize_clock(private)
    except Exception:
        pass
    try:
        await private.cancel_all("linear", "ETHUSDT")
    except Exception:
        pass
    try:
        positions = await private.get_positions("linear", "ETHUSDT")
        nonzero = tuple(position for position in positions if position.quantity > ZERO)
        if not nonzero:
            return
        flatten = EmergencyFlattenService(
            trading=private,
            account=private,
            store=store,
            clock=lambda: _clock_time(clock),
        )
        close_id = str(
            ClientOrderId.new(
                STRATEGY_INSTANCE,
                cycle_number,
                1,
                ClientOrderRole.EMERGENCY_CLOSE,
                1,
            )
        )
        result = await flatten.flatten_short(close_id)
        recorded_fill = False
        for _ in range(20):
            executions = await private.get_execution_history(
                "linear",
                "ETHUSDT",
                close_id,
            )
            for execution in executions:
                recorded_fill = True
                await flatten.record_fill(
                    execution,
                    received_at=_clock_time(clock),
                    payload_hash=execution_payload_hash(execution),
                )
                await _apply_emergency_exit(store, execution, clock)
            if recorded_fill and await flatten.confirm_flattened():
                break
            await asyncio.sleep(0.25)
        if not await flatten.confirm_flattened():
            raise RuntimeError(
                f"emergency close {result.acknowledgement.order_id} is not flat"
            )
        if not recorded_fill:
            raise RuntimeError(
                f"emergency close {result.acknowledgement.order_id} fill is missing"
            )
    finally:
        try:
            await private.cancel_all("linear", "ETHUSDT")
        except Exception:
            pass


async def _synchronize_clock(private: BybitPrivateRestClient) -> None:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            await private.synchronize_clock()
            return
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                await asyncio.sleep(0.2)
    if last_error is None:
        raise AssertionError("clock synchronization loop did not run")
    raise last_error


async def _apply_recorded_emergency_exits(
    store: SqliteExecutionStore,
    clock: ServerClock,
) -> None:
    for execution in await store.load_all_executions():
        try:
            role = ClientOrderId.parse(execution.order_link_id).role
        except ValueError:
            continue
        if role is ClientOrderRole.EMERGENCY_CLOSE:
            await _apply_emergency_exit(store, execution, clock)


async def _apply_emergency_exit(
    store: SqliteExecutionStore,
    execution: ExecutionUpdate,
    clock: ServerClock,
) -> None:
    close_id = ClientOrderId.parse(execution.order_link_id)
    if close_id.role is not ClientOrderRole.EMERGENCY_CLOSE:
        raise ValueError("execution is not an emergency close")
    matches: list[ProtectionSnapshot] = []
    for snapshot in await store.load_all_protection_snapshots():
        entry_id = ClientOrderId.parse(snapshot.entry_order_link_id)
        if (
            entry_id.strategy_instance == close_id.strategy_instance
            and entry_id.cycle == close_id.cycle
        ):
            matches.append(snapshot)
    entry_snapshots = await store.load_all_entry_snapshots()
    protected_entry_ids = {
        snapshot.entry_order_link_id for snapshot in matches
    }
    unprotected_entries = tuple(
        snapshot
        for snapshot in entry_snapshots
        if snapshot.order_link_id not in protected_entry_ids
        and snapshot.state is not LiveExecutionState.ERROR
        and snapshot.filled_quantity > ZERO
        and ClientOrderId.parse(snapshot.order_link_id).strategy_instance
        == close_id.strategy_instance
        and ClientOrderId.parse(snapshot.order_link_id).cycle == close_id.cycle
    )
    total_open = sum(
        (snapshot.open_quantity for snapshot in matches),
        start=ZERO,
    )
    active_exposure = total_open + sum(
        (snapshot.filled_quantity for snapshot in unprotected_entries),
        start=ZERO,
    )
    if active_exposure == ZERO:
        return
    remaining = min(execution.quantity, active_exposure)
    ordered = sorted(
        matches,
        key=lambda snapshot: ClientOrderId.parse(
            snapshot.entry_order_link_id
        ).level,
    )
    for snapshot in ordered:
        allocation = min(snapshot.open_quantity, remaining)
        if allocation == ZERO:
            continue
        updated = apply_emergency_exit_execution(
            snapshot,
            execution,
            updated_at=_clock_time(clock),
            allocated_quantity=allocation,
        )
        await store.transition_protection_snapshot(snapshot.version, updated)
        remaining -= allocation
    for entry_snapshot in sorted(
        unprotected_entries,
        key=lambda item: ClientOrderId.parse(item.order_link_id).level,
    ):
        if entry_snapshot.filled_quantity > remaining:
            break
        errored = transition_entry_snapshot(
            entry_snapshot,
            LiveExecutionState.ERROR,
            updated_at=_clock_time(clock),
        )
        await store.transition_entry_snapshot(entry_snapshot.version, errored)
        remaining -= entry_snapshot.filled_quantity
    if remaining != ZERO:
        raise RuntimeError("emergency execution was not fully allocated")


async def _ensure_option_margin_mode(
    private: BybitPrivateRestClient,
) -> None:
    current = await private.get_margin_mode()
    if current in ("REGULAR_MARGIN", "PORTFOLIO_MARGIN"):
        return
    try:
        await private.set_margin_mode("REGULAR_MARGIN")
    except UncertainOrderOutcomeError:
        pass
    for _ in range(20):
        if await private.get_margin_mode() == "REGULAR_MARGIN":
            return
        await asyncio.sleep(0.25)
    raise DemoMutationRefusedError(
        "demo account did not enter cross margin mode for option trading"
    )


async def _verify_liquidation_distance(
    private: BybitPrivateRestClient,
    *,
    deployment: EnvironmentProfile,
    instrument: InstrumentSpec,
) -> None:
    positions = tuple(
        position
        for position in await private.get_positions("linear", "ETHUSDT")
        if position.quantity > ZERO
    )
    if len(positions) != 1 or positions[0].side != "Sell":
        raise DemoMutationRefusedError(
            "expected exactly one protected ETHUSDT short position"
        )
    position = positions[0]
    if position.mark_price is None:
        raise DemoMutationRefusedError(
            "Bybit did not provide a mark price"
        )
    liquidation_boundary = position.liquidation_price
    if liquidation_boundary is None:
        liquidation_boundary = instrument.price_filter.max_price
    if liquidation_boundary is None or liquidation_boundary <= position.mark_price:
        raise DemoMutationRefusedError(
            "Bybit did not provide a usable short liquidation boundary"
        )
    distance = (
        liquidation_boundary - position.mark_price
    ) / position.mark_price
    if distance < deployment.risk_limits.minimum_liquidation_distance:
        raise DemoMutationRefusedError(
            "actual liquidation distance is below the sealed demo limit"
        )


def _require_matched(report: ReconciliationReport, stage: str) -> None:
    if not report.trading_allowed:
        kinds = ",".join(difference.kind.value for difference in report.differences)
        raise DemoMutationRefusedError(f"{stage} failed: {kinds}")


def _require_one_way_account(snapshot: PrivateAccountSnapshot) -> None:
    if snapshot.open_orders:
        raise DemoMutationRefusedError("demo account has open orders before D3")
    if any(
        position.category == "linear" and position.quantity > ZERO
        for position in snapshot.positions
    ):
        raise DemoMutationRefusedError("demo account has an active perpetual")
    if any(
        position.category == "linear" and position.position_idx != 0
        for position in snapshot.positions
    ):
        raise DemoMutationRefusedError("D3 requires one-way position mode")


def _next_cycle_number(requests: tuple[PlaceOrderRequest, ...]) -> int:
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
        raise DemoMutationRefusedError("demo strategy exhausted cycle IDs")
    return cycle


def _demo_deployment_profile() -> EnvironmentProfile:
    matches = tuple(
        profile
        for profile in load_all_environment_profiles()
        if profile.environment is RuntimeEnvironment.DEMO
    )
    if len(matches) != 1:
        raise RuntimeError("expected exactly one DEMO deployment profile")
    return matches[0]


def _clock_time(clock: ServerClock) -> datetime:
    return datetime.fromtimestamp(clock.timestamp_ms() / 1000, tz=timezone.utc)


def _required_delivery(instrument: InstrumentSpec) -> datetime:
    if instrument.delivery_time_utc is None:
        raise DemoMutationRefusedError("option instrument has no delivery time")
    return instrument.delivery_time_utc


def _strike(symbol: str) -> Decimal:
    try:
        return Decimal(symbol.split("-")[2])
    except (IndexError, ValueError) as exc:
        raise ValueError(f"invalid option symbol: {symbol}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "stage",
        choices=(
            "d3-manual",
            "d4-automatic",
            "d5-multiple",
            "d6-recovery",
        ),
    )
    args = parser.parse_args()
    if args.stage == "d3-manual":
        print(asyncio.run(run_d3_manual()).to_json())
        return 0
    if args.stage == "d4-automatic":
        print(asyncio.run(run_d4_automatic()).to_json())
        return 0
    if args.stage == "d5-multiple":
        print(asyncio.run(run_d5_multiple()).to_json())
        return 0
    if args.stage == "d6-recovery":
        print(asyncio.run(run_d6_recovery()).to_json())
        return 0
    raise AssertionError("argparse returned an unknown demo stage")


if __name__ == "__main__":
    raise SystemExit(main())
