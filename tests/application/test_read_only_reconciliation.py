"""Read-only private snapshot and explanation tests."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

from eth_credit_hedge.application.read_only_reconciliation import (
    BybitPrivateStateReader,
    ExpectedPosition,
    evaluate_private_snapshot,
)
from eth_credit_hedge.domain.execution import (
    ExchangeOrder,
    ExchangePosition,
    ExecutionUpdate,
    WalletBalance,
    WalletState,
)


NOW = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
KNOWN_LINK = "ECH-01-C0001-L01-ENTRY-A01-9F3C"


def exchange_order(order_link_id: str = KNOWN_LINK) -> ExchangeOrder:
    return ExchangeOrder(
        category="linear",
        order_id="order-1",
        order_link_id=order_link_id,
        symbol="ETHUSDT",
        status="New",
        side="Sell",
        order_type="Limit",
        price=Decimal("3000"),
        quantity=Decimal("0.01"),
        cumulative_filled_quantity=Decimal("0"),
        average_price=None,
        reduce_only=False,
        created_at=NOW,
        updated_at=NOW,
    )


def exchange_position(quantity: Decimal = Decimal("0.01")) -> ExchangePosition:
    return ExchangePosition(
        category="linear",
        symbol="ETHUSDT",
        side="Sell" if quantity else None,
        quantity=quantity,
        average_price=Decimal("3000") if quantity else None,
        mark_price=Decimal("2999"),
        unrealized_pnl=Decimal("0.01"),
        updated_at=NOW,
    )


def wallet_state() -> WalletState:
    return WalletState(
        account_type="UNIFIED",
        total_equity=Decimal("1000"),
        total_wallet_balance=Decimal("1000"),
        total_available_balance=Decimal("900"),
        balances=(
            WalletBalance(
                coin="USDT",
                equity=Decimal("1000"),
                wallet_balance=Decimal("1000"),
                available_balance=None,
                unrealized_pnl=Decimal("0"),
            ),
        ),
        updated_at=NOW,
    )


class FakePrivateAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []
        self.open_orders = (exchange_order(),)
        self.positions = (exchange_position(),)

    async def get_open_orders(
        self,
        category: str,
        symbol: str | None = None,
    ) -> tuple[ExchangeOrder, ...]:
        self.calls.append(("open_orders", category, symbol))
        return self.open_orders if category == "linear" else ()

    async def get_order_history(
        self,
        category: str,
        symbol: str | None = None,
    ) -> tuple[ExchangeOrder, ...]:
        self.calls.append(("order_history", category, symbol))
        return ()

    async def get_execution_history(
        self,
        category: str,
        symbol: str | None = None,
        order_link_id: str | None = None,
    ) -> tuple[ExecutionUpdate, ...]:
        del order_link_id
        self.calls.append(("executions", category, symbol))
        return ()

    async def get_positions(
        self,
        category: str,
        symbol: str | None = None,
    ) -> tuple[ExchangePosition, ...]:
        self.calls.append(("positions", category, symbol))
        return self.positions if category == "linear" else ()

    async def get_wallet_state(self) -> WalletState:
        self.calls.append(("wallet", "UNIFIED", None))
        return wallet_state()


def test_reader_captures_all_private_state_needed_before_reconciliation() -> None:
    adapter = FakePrivateAdapter()
    reader = BybitPrivateStateReader(
        trading=adapter,
        account=adapter,
        clock=lambda: NOW,
    )

    snapshot = asyncio.run(reader.capture())

    assert snapshot.captured_at_utc == NOW
    assert snapshot.open_orders == (exchange_order(),)
    assert snapshot.positions == (exchange_position(),)
    assert snapshot.wallet.account_type == "UNIFIED"
    assert set(adapter.calls) == {
        ("open_orders", "linear", "ETHUSDT"),
        ("open_orders", "option", None),
        ("order_history", "linear", "ETHUSDT"),
        ("order_history", "option", None),
        ("executions", "linear", "ETHUSDT"),
        ("positions", "linear", "ETHUSDT"),
        ("positions", "option", None),
        ("wallet", "UNIFIED", None),
    }


def test_snapshot_is_tradable_only_when_orders_and_positions_are_explained() -> None:
    snapshot = asyncio.run(
        BybitPrivateStateReader(
            trading=FakePrivateAdapter(),
            account=FakePrivateAdapter(),
            clock=lambda: NOW,
        ).capture()
    )
    expected = ExpectedPosition(
        category="linear",
        symbol="ETHUSDT",
        side="Sell",
        quantity=Decimal("0.01"),
    )

    matched = evaluate_private_snapshot(
        snapshot,
        known_order_link_ids=frozenset({KNOWN_LINK}),
        expected_positions=(expected,),
    )
    unknown_order = evaluate_private_snapshot(
        snapshot,
        known_order_link_ids=frozenset(),
        expected_positions=(expected,),
    )
    wrong_position = evaluate_private_snapshot(
        snapshot,
        known_order_link_ids=frozenset({KNOWN_LINK}),
        expected_positions=(),
    )

    assert matched.trading_allowed
    assert matched.differences == ()
    assert not unknown_order.trading_allowed
    assert unknown_order.differences[0].kind == "UNKNOWN_OPEN_ORDER"
    assert not wrong_position.trading_allowed
    assert wrong_position.differences[0].kind == "UNKNOWN_POSITION"


def test_missing_expected_position_blocks_reconciliation() -> None:
    adapter = FakePrivateAdapter()
    adapter.open_orders = ()
    adapter.positions = ()
    snapshot = asyncio.run(
        BybitPrivateStateReader(
            trading=adapter,
            account=adapter,
            clock=lambda: NOW,
        ).capture()
    )

    result = evaluate_private_snapshot(
        snapshot,
        known_order_link_ids=frozenset(),
        expected_positions=(
            ExpectedPosition(
                category="linear",
                symbol="ETHUSDT",
                side="Sell",
                quantity=Decimal("0.01"),
            ),
        ),
    )

    assert not result.trading_allowed
    assert result.differences[0].kind == "MISSING_POSITION"
