"""Explicitly gated Bybit demo burn-in runner; never binds mainnet mutations."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from eth_credit_hedge.application.emergency_flatten import EmergencyFlattenService
from eth_credit_hedge.application.execution_hash import execution_payload_hash
from eth_credit_hedge.application.one_level_entry import OneLevelEntryService
from eth_credit_hedge.application.one_level_lifecycle import (
    OneLevelLifecycleService,
)
from eth_credit_hedge.application.option_spread_entry import (
    OptionSpreadEntryPlan,
    OptionSpreadEntryService,
)
from eth_credit_hedge.application.protective_exits import ProtectiveExitService
from eth_credit_hedge.application.read_only_reconciliation import (
    BybitPrivateStateReader,
    ExpectedPosition,
    PrivateAccountSnapshot,
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
from eth_credit_hedge.domain.client_order_ids import (
    ClientOrderId,
    ClientOrderRole,
)
from eth_credit_hedge.domain.execution import (
    AmendOrderRequest,
    LiveExecutionState,
    PlaceOrderRequest,
    UncertainOrderOutcomeError,
)
from eth_credit_hedge.domain.instrument_rules import (
    PriceQuantizationPolicy,
    quantize_limit_price,
)
from eth_credit_hedge.domain.instruments import InstrumentSpec, OptionMarketQuote
from eth_credit_hedge.domain.live_option_execution import (
    OptionSpreadExecutionSnapshot,
)
from eth_credit_hedge.domain.option_lifecycle import (
    OptionEntryPolicy,
    UnmatchedLongPolicy,
)
from eth_credit_hedge.domain.option_position import OptionPositionState
from eth_credit_hedge.domain.reconciliation import ReconciliationReport
from eth_credit_hedge.domain.risk import (
    RiskEngine,
    RiskState,
    TradeProposal,
)
from eth_credit_hedge.infrastructure.bybit.clock import ServerClock
from eth_credit_hedge.infrastructure.bybit.error_mapping import (
    BybitUnknownOrderError,
)
from eth_credit_hedge.infrastructure.bybit.private_rest import (
    BybitPrivateRestClient,
)
from eth_credit_hedge.infrastructure.bybit.public_rest import (
    BybitPublicRestClient,
)
from eth_credit_hedge.infrastructure.persistence.sqlite_execution_store import (
    SqliteExecutionStore,
)


MUTATION_GATE_ENV = "RUN_BYBIT_DEMO_MUTATIONS"
D3_MUTATION_TOKEN = "D3_MANUAL_ONE_LEVEL"
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

    quotes, instruments = await asyncio.gather(
        public.get_option_chain("ETH"),
        public.list_instruments("option", base_coin="ETH"),
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
    policy = OptionEntryPolicy(
        max_leg_wait_seconds=Decimal("15"),
        allow_partial_spread=False,
        minimum_matched_quantity=quantity,
        maximum_credit_deviation=max(Decimal("1"), expected_credit / 2),
        unmatched_long_policy=UnmatchedLongPolicy.RETAIN,
    )
    await _synchronize_clock(private)
    service = OptionSpreadEntryService(
        trading=private,
        store=store,
        clock=lambda: _clock_time(clock),
        fill_attempts=50,
        fill_interval_seconds=0.25,
    )
    return await service.open_spread(plan, policy)


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
                order_link_id=tp_id,
                price=marketable_tp,
            )
        )
        await restarted_store.record_acknowledgement(acknowledgement)
    except (BybitUnknownOrderError, UncertainOrderOutcomeError):
        pass
    closed = await restarted_lifecycle.await_exit(entry_id)
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
        raise RuntimeError("ETHUSDT position remains after D3 exit")
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
        protected_restart_status=restart_report.status.value,
        final_state=closed.state.value,
        realized_pnl=closed.realized_pnl,
        confirmed_recovery_debt=closed.confirmed_recovery_debt,
        final_reconciliation_status=final_report.status.value,
    )


def _lifecycle_services(
    *,
    store: SqliteExecutionStore,
    private: BybitPrivateRestClient,
    instrument: InstrumentSpec,
    clock: ServerClock,
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
        fill_attempts=80,
        fill_interval_seconds=0.25,
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
    parser.add_argument("stage", choices=("d3-manual",))
    args = parser.parse_args()
    if args.stage == "d3-manual":
        print(asyncio.run(run_d3_manual()).to_json())
        return 0
    raise AssertionError("argparse returned an unknown demo stage")


if __name__ == "__main__":
    raise SystemExit(main())
