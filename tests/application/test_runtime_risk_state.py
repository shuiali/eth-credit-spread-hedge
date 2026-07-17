"""Runtime risk inputs come from durable and exchange-authoritative state."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from eth_credit_hedge.application.demo_runtime_state import (
    DemoLevelRuntimeState,
    DemoRuntimeState,
    LiveHedgeRole,
)
from eth_credit_hedge.application.runtime_risk_state import RuntimeRiskStateBuilder
from eth_credit_hedge.application.demo_strategy_runtime import (
    _bounded_option_entry_limits,
    _clock_refresh_loop,
)
from eth_credit_hedge.domain.client_order_ids import ClientOrderId, ClientOrderRole
from eth_credit_hedge.domain.accounting.reconstruction import CombinedLedgerReconstructor
from eth_credit_hedge.domain.execution import (
    ExchangePosition,
    LiveExecutionState,
    WalletState,
)
from eth_credit_hedge.domain.protected_execution import ProtectionSnapshot
from eth_credit_hedge.domain.strategy_math.units import Money


NOW = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)


def level(level_id: int, **changes: object) -> DemoLevelRuntimeState:
    values: dict[str, object] = {
        "level_id": level_id,
        "entry_price": Decimal(3100 - level_id * 100),
        "take_profit_price": Decimal(3050 - level_id * 100),
        "stop_price": Decimal(3103 - level_id * 100),
        "option_budget": Decimal("5"),
    }
    values.update(changes)
    return DemoLevelRuntimeState(**values)  # type: ignore[arg-type]


def wallet(*, equity: str = "1000", available: str | None = "900") -> WalletState:
    return WalletState(
        account_type="UNIFIED",
        total_equity=Decimal(equity),
        total_wallet_balance=Decimal(equity),
        total_available_balance=(
            None if available is None else Decimal(available)
        ),
        balances=(),
        updated_at=NOW,
    )


def short_position(*, liquidation: str | None = "3300") -> ExchangePosition:
    return ExchangePosition(
        category="linear",
        symbol="ETHUSDT",
        side="Sell",
        quantity=Decimal("0.02"),
        average_price=Decimal("3010"),
        mark_price=Decimal("3000"),
        liquidation_price=(
            None if liquidation is None else Decimal(liquidation)
        ),
        unrealized_pnl=Decimal("0.2"),
        updated_at=NOW,
    )


def accounting(*, debt: str = "0", mark_pnl: str = "0"):
    return replace(
        CombinedLedgerReconstructor().reconstruct(()),
        confirmed_recovery_debt=Money(Decimal(debt)),
        net_combined_mark_pnl=Money(Decimal(mark_pnl)),
    )


def test_builder_uses_all_live_and_durable_risk_inputs() -> None:
    runtime = DemoRuntimeState(
        cycle_id="cycle-risk",
        short_option_symbol="SHORT",
        long_option_symbol="LONG",
        option_quantity=Decimal("0.1"),
        levels=(
            level(
                1,
                attempts=3,
                active_entry_order_link_id="ENTRY",
                active_role=LiveHedgeRole.BASELINE,
                active_stop_order_link_id="STOP",
                active_take_profit_order_link_id="TP",
                active_quantity=Decimal("0.02"),
                average_entry_price=Decimal("3010"),
                confirmed_debt=Decimal("1.25"),
                realized_pnl=Decimal("-5"),
            ),
            level(2, confirmed_debt=Decimal("0.75"), realized_pnl=Decimal("2")),
        ),
        reconciliation_complete=True,
        consecutive_reconciliation_failures=2,
        daily_realized_pnl=Decimal("-4"),
        order_request_times_utc=(NOW - timedelta(seconds=30), NOW - timedelta(minutes=2)),
    )

    state = RuntimeRiskStateBuilder(maximum_market_data_age_ms=1_000).build(
        runtime=runtime,
        positions=(short_position(),),
        wallet=wallet(),
        level_id=1,
        proposed_notional=Decimal("50"),
        last_market_event_at_utc=NOW - timedelta(milliseconds=500),
        now_utc=NOW,
        accounting=accounting(debt="2", mark_pnl="-4"),
    )

    assert state.current_perp_quantity == Decimal("0.02")
    assert state.current_perp_notional == Decimal("60")
    assert state.post_trade_margin_usage == Decimal("0.11")
    assert state.post_trade_liquidation_distance == Decimal("0.1")
    assert state.confirmed_recovery_debt == Decimal("2")
    assert state.realized_cycle_loss == Decimal("4")
    assert state.daily_realized_loss == Decimal("4")
    assert state.entries_for_level == 3
    assert state.active_levels == 1
    assert state.order_requests_last_minute == 1
    assert state.consecutive_reconciliation_failures == 2
    assert state.market_data_fresh
    assert state.reconciliation_succeeded


def test_builder_fails_closed_for_stale_market_and_unusable_account_data() -> None:
    runtime = DemoRuntimeState(
        cycle_id="cycle-risk",
        short_option_symbol="SHORT",
        long_option_symbol="LONG",
        option_quantity=Decimal("0.1"),
        levels=(level(1),),
    )

    state = RuntimeRiskStateBuilder(maximum_market_data_age_ms=1_000).build(
        runtime=runtime,
        positions=(short_position(liquidation=None),),
        wallet=wallet(equity="0", available=None),
        level_id=1,
        proposed_notional=Decimal("1"),
        last_market_event_at_utc=NOW - timedelta(seconds=2),
        now_utc=NOW,
        accounting=accounting(),
    )

    assert state.post_trade_margin_usage == Decimal("1")
    assert state.post_trade_liquidation_distance == Decimal("0")
    assert not state.market_data_fresh
    assert not state.reconciliation_succeeded


def test_clock_refresh_loop_repeats_at_the_requested_interval() -> None:
    refresh_count = 0
    delays: list[float] = []

    async def sleeper(delay: float) -> None:
        delays.append(delay)

    async def refresh() -> object:
        nonlocal refresh_count
        refresh_count += 1
        if refresh_count == 2:
            raise RuntimeError("stop refresh loop")
        return object()

    try:
        asyncio.run(
            _clock_refresh_loop(
                refresh,
                interval_seconds=30,
                refresh_attempts=1,
                sleeper=sleeper,
            )
        )
    except RuntimeError as exc:
        assert str(exc) == "stop refresh loop"
    else:
        raise AssertionError("refresh loop did not propagate the refresh failure")

    assert refresh_count == 2
    assert delays == [30, 30]


def test_clock_refresh_loop_retries_transient_bad_samples() -> None:
    outcomes: list[BaseException | object] = [
        RuntimeError("transient uncertainty"),
        object(),
        RuntimeError("persistent uncertainty 1"),
        RuntimeError("persistent uncertainty 2"),
    ]
    delays: list[float] = []

    async def sleeper(delay: float) -> None:
        delays.append(delay)

    async def refresh() -> object:
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    try:
        asyncio.run(
            _clock_refresh_loop(
                refresh,
                interval_seconds=30,
                refresh_attempts=2,
                retry_interval_seconds=0.25,
                sleeper=sleeper,
            )
        )
    except RuntimeError as exc:
        assert str(exc) == "persistent uncertainty 2"
    else:
        raise AssertionError("persistent clock uncertainty did not fail closed")

    assert outcomes == []
    assert delays == [30, 0.25, 30, 0.25]


def test_option_entry_limits_use_the_configured_per_leg_deviation() -> None:
    long_limit, short_limit, maximum_credit_deviation = (
        _bounded_option_entry_limits(
            quoted_long_price=Decimal("8.7"),
            quoted_short_price=Decimal("12"),
            long_tick_size=Decimal("0.1"),
            short_tick_size=Decimal("0.1"),
            quantity=Decimal("0.1"),
            maximum_deviation_bps=Decimal("100"),
        )
    )

    assert long_limit == Decimal("8.8")
    assert short_limit == Decimal("11.8")
    assert maximum_credit_deviation == Decimal("0.03")


def closed_snapshot(level_id: int, updated_at: datetime) -> ProtectionSnapshot:
    def order_id(role: ClientOrderRole) -> str:
        return str(
            ClientOrderId(
                strategy_instance="SIM",
                cycle=1,
                level=level_id,
                role=role,
                attempt=1,
                nonce=f"{level_id:04X}",
            )
        )

    return ProtectionSnapshot(
        entry_order_link_id=order_id(ClientOrderRole.HEDGE_ENTRY),
        state=LiveExecutionState.CLOSED_TP,
        entry_quantity=Decimal("0.1"),
        open_quantity=Decimal("0"),
        average_entry_price=Decimal("3000"),
        entry_fees=Decimal("0.1"),
        stop_order_link_id=order_id(ClientOrderRole.HEDGE_STOP),
        stop_order_id="stop-order",
        stop_trigger_price=Decimal("3005"),
        tp_order_link_id=order_id(ClientOrderRole.HEDGE_TP),
        tp_order_id="tp-order",
        tp_price=Decimal("2990"),
        tp_filled_quantity=Decimal("0.1"),
        stop_filled_quantity=Decimal("0"),
        exit_notional=Decimal("299"),
        exit_fees=Decimal("0.1"),
        confirmed_recovery_debt=Decimal("0"),
        pending_terminal_state=None,
        version=1,
        updated_at=updated_at,
    )
