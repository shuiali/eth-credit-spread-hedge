"""Persistence-first M10 same-level recovery submission tests."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from eth_credit_hedge.application.one_level_entry import OneLevelEntryService
from eth_credit_hedge.application.same_level_recovery import SameLevelRecoveryService
from eth_credit_hedge.core.virtual_levels import HedgeLevel
from eth_credit_hedge.domain.execution import (
    ExchangeOrder,
    OrderRequestAck,
    OrderRequestKind,
    PlaceOrderRequest,
)
from eth_credit_hedge.domain.instruments import (
    InstrumentSpec,
    LotSizeFilter,
    PriceFilter,
)
from eth_credit_hedge.domain.live_recovery import (
    LockedLevelAction,
    SameLevelRecoveryPlanner,
)
from eth_credit_hedge.domain.risk import RiskEngine, RiskLimits, RiskState
from eth_credit_hedge.infrastructure.persistence.sqlite_execution_store import (
    SqliteExecutionStore,
)


NOW = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
RECOVERY_ID = "ECH-01-C0001-L01-ENTRY-A02-D00D"


def level() -> HedgeLevel:
    return HedgeLevel(
        level_id=1,
        entry_price=Decimal("3100"),
        tp_price=Decimal("3000"),
        stop_price=Decimal("3104.65"),
        option_budget=Decimal("1"),
    )


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


def limits() -> RiskLimits:
    return RiskLimits(
        maximum_perp_quantity=Decimal("1"),
        maximum_perp_notional=Decimal("5000"),
        maximum_margin_usage=Decimal("0.5"),
        minimum_liquidation_distance=Decimal("0.1"),
        maximum_recovery_debt=Decimal("10"),
        maximum_projected_stop_loss=Decimal("10"),
        maximum_realized_cycle_loss=Decimal("20"),
        maximum_daily_realized_loss=Decimal("50"),
        maximum_entries_per_level=3,
        maximum_active_levels=3,
        maximum_order_requests_per_minute=10,
        maximum_reconciliation_failures=3,
    )


def risk_state() -> RiskState:
    return RiskState(
        current_perp_quantity=Decimal("0"),
        current_perp_notional=Decimal("0"),
        post_trade_margin_usage=Decimal("0.1"),
        post_trade_liquidation_distance=Decimal("0.5"),
        confirmed_recovery_debt=Decimal("0.0338"),
        realized_cycle_loss=Decimal("0.0338"),
        daily_realized_loss=Decimal("0.0338"),
        entries_for_level=1,
        active_levels=0,
        order_requests_last_minute=1,
        consecutive_reconciliation_failures=0,
        market_data_fresh=True,
        reconciliation_succeeded=True,
    )


class RecoveryAwareExchange:
    def __init__(self, store: SqliteExecutionStore) -> None:
        self.store = store
        self.requests: list[PlaceOrderRequest] = []

    async def place_order(self, request: PlaceOrderRequest) -> OrderRequestAck:
        assert await self.store.load_order_intent(request.order_link_id) == request
        debt = await self.store.load_recovery_debt_snapshot(1)
        assert debt is not None
        assert debt.debt.allocated_debt == Decimal("0.0338")
        self.requests.append(request)
        return OrderRequestAck(
            request_kind=OrderRequestKind.PLACE,
            order_id="recovery-order",
            order_link_id=request.order_link_id,
            acknowledged_at=NOW,
        )

    async def get_order_by_link_id(
        self,
        category: str,
        symbol: str,
        order_link_id: str,
    ) -> ExchangeOrder | None:
        return None


def make_service(
    tmp_path: Path,
    *,
    entry_gate=None,
) -> tuple[SameLevelRecoveryService, RecoveryAwareExchange, SqliteExecutionStore]:
    store = SqliteExecutionStore(tmp_path / "execution.sqlite3")
    asyncio.run(store.initialize())
    exchange = RecoveryAwareExchange(store)
    entries = OneLevelEntryService(
        trading=exchange,
        store=store,
        clock=lambda: NOW,
    )
    service = SameLevelRecoveryService(
        entry_service=entries,
        store=store,
        planner=SameLevelRecoveryPlanner(RiskEngine()),
        clock=lambda: NOW,
        entry_gate=entry_gate,
    )
    return service, exchange, store


def test_recovery_debt_is_allocated_durably_before_order_submission(
    tmp_path: Path,
) -> None:
    service, exchange, store = make_service(tmp_path)
    asyncio.run(
        service.record_confirmed_stop_debt(
            level_id=1,
            actual_stop_debt=Decimal("0.0338"),
            projected_debt=Decimal("5"),
        )
    )

    submission = asyncio.run(
        service.submit_recovery(
            level=level(),
            instrument=instrument(),
            risk_state=risk_state(),
            limits=limits(),
            order_link_id=RECOVERY_ID,
        )
    )

    assert submission.plan.approved
    assert submission.entry_snapshot is not None
    assert exchange.requests[0].quantity == Decimal("0.011")
    debt_snapshot = asyncio.run(store.load_recovery_debt_snapshot(1))
    assert debt_snapshot is not None
    assert debt_snapshot.debt.allocated_debt == Decimal("0.0338")

    settled = asyncio.run(
        service.settle_take_profit(
            level_id=1,
            realized_take_profit=Decimal("1.02"),
            zone_budget=Decimal("1"),
        )
    )
    assert settled.debt.confirmed_debt == Decimal("0.0138")
    assert settled.debt.remaining_debt == Decimal("0.0138")


def test_rejected_recovery_locks_with_option_close_and_sends_no_order(
    tmp_path: Path,
) -> None:
    service, exchange, store = make_service(tmp_path)
    original = asyncio.run(
        service.record_confirmed_stop_debt(
            level_id=1,
            actual_stop_debt=Decimal("0.0338"),
            projected_debt=Decimal("5"),
        )
    )

    submission = asyncio.run(
        service.submit_recovery(
            level=level(),
            instrument=instrument(),
            risk_state=risk_state(),
            limits=replace(limits(), maximum_perp_quantity=Decimal("0.010")),
            order_link_id=RECOVERY_ID,
        )
    )

    assert not submission.plan.approved
    assert submission.plan.locked_action is LockedLevelAction.CLOSE_OPTION_STRATEGY
    assert submission.entry_snapshot is None
    assert exchange.requests == []
    assert asyncio.run(store.load_recovery_debt_snapshot(1)) == original


def test_soft_pause_blocks_recovery_without_allocating_debt(tmp_path: Path) -> None:
    class PausedGate:
        entries_allowed = False

    service, exchange, store = make_service(tmp_path, entry_gate=PausedGate())
    original = asyncio.run(
        service.record_confirmed_stop_debt(
            level_id=1,
            actual_stop_debt=Decimal("0.0338"),
            projected_debt=Decimal("5"),
        )
    )

    submission = asyncio.run(
        service.submit_recovery(
            level=level(),
            instrument=instrument(),
            risk_state=risk_state(),
            limits=limits(),
            order_link_id=RECOVERY_ID,
        )
    )

    assert not submission.plan.approved
    assert submission.plan.reasons[-1] == "kill switch blocks new entries"
    assert submission.plan.locked_action is None
    assert exchange.requests == []
    assert asyncio.run(store.load_recovery_debt_snapshot(1)) == original


def test_recovery_stop_releases_allocation_and_adds_actual_debt(
    tmp_path: Path,
) -> None:
    service, _, store = make_service(tmp_path)
    asyncio.run(
        service.record_confirmed_stop_debt(
            level_id=1,
            actual_stop_debt=Decimal("0.0338"),
            projected_debt=Decimal("5"),
        )
    )
    asyncio.run(
        service.submit_recovery(
            level=level(),
            instrument=instrument(),
            risk_state=risk_state(),
            limits=limits(),
            order_link_id=RECOVERY_ID,
        )
    )

    stopped = asyncio.run(
        service.record_recovery_stop_debt(
            level_id=1,
            actual_stop_debt=Decimal("0.012"),
            projected_debt=Decimal("0.02"),
        )
    )

    assert stopped.debt.projected_debt == Decimal("0.02")
    assert stopped.debt.confirmed_debt == Decimal("0.0458")
    assert stopped.debt.allocated_debt == Decimal("0")
    assert stopped.debt.remaining_debt == Decimal("0.0458")
