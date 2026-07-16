"""Non-mutating bootstrap for the integrated Bybit demo strategy."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from eth_credit_hedge.config.bybit import load_bybit_demo_profile
from eth_credit_hedge.config.deployment import (
    EnvironmentProfile,
    StartupState,
    load_all_environment_profiles,
    validate_startup,
)
from eth_credit_hedge.config.schema import RuntimeEnvironment
from eth_credit_hedge.domain.instruments import InstrumentSpec, OptionMarketQuote
from eth_credit_hedge.domain.control import KillSwitchMode
from eth_credit_hedge.domain.option_exit import OptionExitState
from eth_credit_hedge.domain.execution import LiveExecutionState
from eth_credit_hedge.application.demo_runtime_journal import DemoRuntimeJournal
from eth_credit_hedge.application.kill_switch import KillSwitchController
from eth_credit_hedge.application.read_only_reconciliation import (
    BybitPrivateStateReader,
    ExpectedPosition,
    evaluate_private_snapshot,
)
from eth_credit_hedge.infrastructure.bybit.auth import BybitV5Signer
from eth_credit_hedge.infrastructure.bybit.clock import ServerClock
from eth_credit_hedge.infrastructure.bybit.demo_capabilities import (
    BybitDemoCapabilityProbe,
)
from eth_credit_hedge.infrastructure.bybit.private_rest import (
    BybitPrivateRestClient,
)
from eth_credit_hedge.infrastructure.bybit.private_ws import (
    BybitPrivateWebSocketClient,
)
from eth_credit_hedge.infrastructure.bybit.public_rest import (
    BybitPublicRestClient,
)
from eth_credit_hedge.infrastructure.persistence.file_kill_switch_store import (
    FileKillSwitchStore,
)
from eth_credit_hedge.infrastructure.persistence.sqlite_execution_store import (
    SqliteExecutionStore,
)
from eth_credit_hedge.infrastructure.persistence.sqlite_journal_store import (
    SqliteJournalStore,
)

if TYPE_CHECKING:
    from eth_credit_hedge.interfaces.demo_strategy_runner import (
        DemoStrategyCommand,
    )


async def run_demo_preflight(
    command: DemoStrategyCommand,
) -> dict[str, object]:
    deployment = _demo_deployment_profile()
    profile = load_bybit_demo_profile()
    if profile.rest_base_url != deployment.rest_base_url:
        raise RuntimeError("demo credential and deployment REST endpoints differ")
    if profile.private_websocket_url != deployment.private_websocket_url:
        raise RuntimeError(
            "demo credential and deployment private WebSocket endpoints differ"
        )
    clock = ServerClock(
        max_absolute_offset_ms=deployment.maximum_clock_drift_ms,
    )
    private = BybitPrivateRestClient(profile=profile, clock=clock)
    public = BybitPublicRestClient()
    websocket = BybitPrivateWebSocketClient(
        signer=BybitV5Signer(profile.credentials)
    )
    capability = await BybitDemoCapabilityProbe(
        profile=profile,
        clock=clock,
        private_rest=private,
        public_rest=public,
        private_websocket=websocket,
    ).probe()
    if not capability.accepted:
        raise RuntimeError("Bybit demo capability probe did not pass")

    execution_store = SqliteExecutionStore(deployment.database_path)
    journal_store = SqliteJournalStore(_journal_path(deployment.database_path))
    await execution_store.initialize()
    await journal_store.initialize()
    kill_switch = KillSwitchController(
        store=FileKillSwitchStore(_kill_switch_path(deployment.database_path)),
        clock=lambda: datetime.now(timezone.utc),
    )
    kill_state = await kill_switch.initialize()
    if kill_state.mode is not KillSwitchMode.RUNNING:
        raise RuntimeError(
            f"demo kill switch is not RUNNING: {kill_state.mode.value}"
        )

    restored_cycle = None
    if command.cycle_mode.value == "RESTORE_ONLY":
        if command.cycle_id is None:
            raise AssertionError("validated RESTORE_ONLY command requires cycle ID")
        restored_cycle = await DemoRuntimeJournal.restore(
            store=journal_store,
            cycle_id=command.cycle_id,
            clock=lambda: datetime.now(timezone.utc),
        )
        option = await execution_store.load_option_spread_snapshot(
            command.cycle_id
        )
        if option is None:
            raise RuntimeError("restored runtime has no durable option spread")

    reader = BybitPrivateStateReader(
        trading=private,
        account=private,
        clock=lambda: datetime.now(timezone.utc),
    )
    exchange = await reader.capture()
    if any(
        position.category == "linear" and position.position_idx != 0
        for position in exchange.positions
    ):
        raise RuntimeError("integrated demo requires one-way ETHUSDT position mode")
    known_order_ids = frozenset(
        request.order_link_id
        for request in await execution_store.load_all_order_intents()
    )
    expected_positions = await _expected_durable_positions(execution_store)
    if command.cycle_mode.value == "OPEN_NEW" and expected_positions:
        raise RuntimeError(
            "OPEN_NEW refused because durable strategy exposure already exists; "
            "use RESTORE_ONLY with the exact cycle ID"
        )
    reconciliation = evaluate_private_snapshot(
        exchange,
        known_order_link_ids=known_order_ids,
        expected_positions=expected_positions,
    )
    validate_startup(
        deployment,
        StartupState(
            execution_schema_version=await execution_store.schema_version(),
            journal_schema_version=await journal_store.schema_version(),
            kill_switch_available=True,
            clock_drift_ms=int(capability.clock_offset_ms),
            reconciliation_complete=reconciliation.trading_allowed,
            credentials_available=True,
            database_available=True,
        ),
    )

    selection: dict[str, object] | None = None
    if command.cycle_mode.value == "OPEN_NEW":
        if (
            command.short_symbol is None
            or command.long_symbol is None
            or command.option_quantity is None
            or command.minimum_net_credit is None
        ):
            raise AssertionError("validated OPEN_NEW command is incomplete")
        selection = await _validate_selected_pair(
            public,
            short_symbol=command.short_symbol,
            long_symbol=command.long_symbol,
            quantity=command.option_quantity,
            minimum_net_credit=command.minimum_net_credit,
        )

    payload: dict[str, object] = {
        "accepted": True,
        "action": "preflight",
        "cycle_mode": command.cycle_mode.value,
        "capabilities": capability.to_payload(),
        "selection": selection,
        "startup": {
            "execution_schema_version": await execution_store.schema_version(),
            "journal_schema_version": await journal_store.schema_version(),
            "kill_switch_mode": kill_state.mode.value,
            "reconciliation_complete": reconciliation.trading_allowed,
            "reconciliation_differences": [
                {
                    "kind": difference.kind,
                    "detail": difference.detail,
                }
                for difference in reconciliation.differences
            ],
            "restored_cycle_id": (
                None
                if restored_cycle is None
                else restored_cycle.state.cycle_id
            ),
        },
        "external_order_mutations": 0,
    }
    evidence = _write_evidence(payload)
    payload["evidence_path"] = str(evidence)
    payload["evidence_sha256"] = hashlib.sha256(evidence.read_bytes()).hexdigest()
    return payload


async def _validate_selected_pair(
    public: BybitPublicRestClient,
    *,
    short_symbol: str,
    long_symbol: str,
    quantity: Decimal,
    minimum_net_credit: Decimal,
) -> dict[str, object]:
    quotes = await public.get_option_chain("ETH")
    instruments = await public.list_instruments("option", base_coin="ETH")
    quote_by_symbol = {quote.symbol: quote for quote in quotes}
    instrument_by_symbol = {
        instrument.symbol: instrument for instrument in instruments
    }
    short_quote = _required_quote(quote_by_symbol, short_symbol)
    long_quote = _required_quote(quote_by_symbol, long_symbol)
    short_instrument = _required_instrument(instrument_by_symbol, short_symbol)
    long_instrument = _required_instrument(instrument_by_symbol, long_symbol)
    if not short_symbol.endswith("-P-USDT") or not long_symbol.endswith("-P-USDT"):
        raise ValueError("selected option symbols must be ETH USDT puts")
    if _strike(short_symbol) <= _strike(long_symbol):
        raise ValueError("short put strike must exceed long put strike")
    expiry = short_instrument.delivery_time_utc
    if expiry is None or expiry != long_instrument.delivery_time_utc:
        raise ValueError("selected options must have one common expiry")
    now = datetime.now(timezone.utc)
    if not now + timedelta(days=14) <= expiry <= now + timedelta(days=90):
        raise ValueError("selected option expiry is outside the 14-90 day window")
    for instrument in (short_instrument, long_instrument):
        if instrument.category != "option" or instrument.status != "Trading":
            raise ValueError(f"{instrument.symbol} is not a Trading option")
        lot = instrument.lot_size_filter
        if (
            quantity < lot.min_order_qty
            or quantity > lot.max_order_qty
            or quantity % lot.qty_step != Decimal("0")
        ):
            raise ValueError(f"quantity is invalid for {instrument.symbol}")
    if short_quote.bid_price is None or long_quote.ask_price is None:
        raise ValueError("selected options do not have executable bid/ask quotes")
    net_credit = (short_quote.bid_price - long_quote.ask_price) * quantity
    if net_credit < minimum_net_credit:
        raise ValueError("selected option net credit is below the required minimum")
    return {
        "short_symbol": short_symbol,
        "long_symbol": long_symbol,
        "quantity": str(quantity),
        "short_bid": str(short_quote.bid_price),
        "long_ask": str(long_quote.ask_price),
        "expected_net_credit": str(net_credit),
        "minimum_net_credit": str(minimum_net_credit),
        "expiry_utc": expiry.isoformat(),
    }


def _required_quote(
    by_symbol: dict[str, OptionMarketQuote],
    symbol: str,
) -> OptionMarketQuote:
    quote = by_symbol.get(symbol)
    if quote is None:
        raise ValueError(f"selected option quote is unavailable: {symbol}")
    return quote


def _required_instrument(
    by_symbol: dict[str, InstrumentSpec],
    symbol: str,
) -> InstrumentSpec:
    instrument = by_symbol.get(symbol)
    if instrument is None:
        raise ValueError(f"selected option instrument is unavailable: {symbol}")
    return instrument


def _strike(symbol: str) -> Decimal:
    parts = symbol.split("-")
    if len(parts) != 5:
        raise ValueError(f"unexpected option symbol: {symbol}")
    return Decimal(parts[2])


def _demo_deployment_profile() -> EnvironmentProfile:
    matches = tuple(
        profile
        for profile in load_all_environment_profiles()
        if profile.environment is RuntimeEnvironment.DEMO
    )
    if len(matches) != 1:
        raise RuntimeError("expected exactly one DEMO deployment profile")
    return matches[0]


async def _expected_durable_positions(
    store: SqliteExecutionStore,
) -> tuple[ExpectedPosition, ...]:
    expected: list[ExpectedPosition] = []
    protection_snapshots = await store.load_all_protection_snapshots()
    protected_entry_ids = {
        snapshot.entry_order_link_id for snapshot in protection_snapshots
    }
    open_perp = sum(
        (
            snapshot.open_quantity
            for snapshot in protection_snapshots
        ),
        Decimal("0"),
    )
    open_perp += sum(
        (
            snapshot.filled_quantity
            for snapshot in await store.load_all_entry_snapshots()
            if snapshot.order_link_id not in protected_entry_ids
            and snapshot.state is not LiveExecutionState.ERROR
        ),
        Decimal("0"),
    )
    if open_perp > Decimal("0"):
        expected.append(
            ExpectedPosition(
                category="linear",
                symbol="ETHUSDT",
                side="Sell",
                quantity=open_perp,
            )
        )
    for snapshot in await store.load_all_option_spread_snapshots():
        exit_snapshot = await store.load_option_exit_snapshot(snapshot.cycle_id)
        if exit_snapshot is not None and exit_snapshot.state is OptionExitState.CLOSED:
            continue
        if snapshot.short_filled_quantity > Decimal("0"):
            expected.append(
                ExpectedPosition(
                    category="option",
                    symbol=snapshot.short_symbol,
                    side="Sell",
                    quantity=snapshot.short_filled_quantity,
                )
            )
        if snapshot.long_filled_quantity > Decimal("0"):
            expected.append(
                ExpectedPosition(
                    category="option",
                    symbol=snapshot.long_symbol,
                    side="Buy",
                    quantity=snapshot.long_filled_quantity,
                )
            )
    return tuple(expected)


def _journal_path(database_path: Path) -> Path:
    return database_path.with_name(f"{database_path.stem}-journal.sqlite3")


def _kill_switch_path(database_path: Path) -> Path:
    return database_path.with_name(f"{database_path.stem}-kill-switch.json")


def _write_evidence(payload: dict[str, object]) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = Path("artifacts") / f"demo-preflight-{stamp}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    return path
