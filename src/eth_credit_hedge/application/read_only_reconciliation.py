"""Read-only private-state capture and exact explanation checks."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Literal

from eth_credit_hedge.domain.execution import (
    Category,
    ExchangeOrder,
    ExchangePosition,
    ExecutionUpdate,
    OrderSide,
    WalletState,
)
from eth_credit_hedge.ports.account import AccountPort
from eth_credit_hedge.ports.trading import TradingPort


ZERO = Decimal("0")
Clock = Callable[[], datetime]
DifferenceKind = Literal[
    "UNKNOWN_OPEN_ORDER",
    "UNKNOWN_POSITION",
    "MISSING_POSITION",
    "POSITION_QUANTITY_MISMATCH",
]
PositionKey = tuple[Category, str, OrderSide]


def _decimal(value: Decimal, field_name: str) -> Decimal:
    try:
        normalized = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be finite") from exc
    if not normalized.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return normalized


def _utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class ExpectedPosition:
    category: Category
    symbol: str
    side: OrderSide
    quantity: Decimal

    def __post_init__(self) -> None:
        if self.category not in ("linear", "option"):
            raise ValueError("expected position category must be linear or option")
        if not self.symbol.strip():
            raise ValueError("expected position symbol cannot be empty")
        if self.side not in ("Buy", "Sell"):
            raise ValueError("expected position side must be Buy or Sell")
        quantity = _decimal(self.quantity, "expected position quantity")
        if quantity <= ZERO:
            raise ValueError("expected position quantity must be positive")
        object.__setattr__(self, "quantity", quantity)


@dataclass(frozen=True, slots=True)
class PrivateAccountSnapshot:
    open_orders: tuple[ExchangeOrder, ...]
    recent_orders: tuple[ExchangeOrder, ...]
    executions: tuple[ExecutionUpdate, ...]
    positions: tuple[ExchangePosition, ...]
    wallet: WalletState
    captured_at_utc: datetime

    def __post_init__(self) -> None:
        for field_name in (
            "open_orders",
            "recent_orders",
            "executions",
            "positions",
        ):
            object.__setattr__(self, field_name, tuple(getattr(self, field_name)))
        object.__setattr__(
            self,
            "captured_at_utc",
            _utc(self.captured_at_utc, "private snapshot time"),
        )


@dataclass(frozen=True, slots=True)
class PrivateStateDifference:
    kind: DifferenceKind
    detail: str


@dataclass(frozen=True, slots=True)
class ReadOnlyReconciliationResult:
    trading_allowed: bool
    differences: tuple[PrivateStateDifference, ...]


class BybitPrivateStateReader:
    """Capture the demo private surfaces needed for startup reconciliation."""

    def __init__(
        self,
        *,
        trading: TradingPort,
        account: AccountPort,
        clock: Clock,
    ) -> None:
        self.trading = trading
        self.account = account
        self._clock = clock

    async def capture(self) -> PrivateAccountSnapshot:
        linear_open_task = asyncio.create_task(
            self.trading.get_open_orders("linear", "ETHUSDT")
        )
        option_open_task = asyncio.create_task(
            self.trading.get_open_orders("option")
        )
        linear_history_task = asyncio.create_task(
            self.trading.get_order_history("linear", "ETHUSDT")
        )
        option_history_task = asyncio.create_task(
            self.trading.get_order_history("option")
        )
        execution_task = asyncio.create_task(
            self.trading.get_execution_history("linear", "ETHUSDT")
        )
        linear_position_task = asyncio.create_task(
            self.account.get_positions("linear", "ETHUSDT")
        )
        option_position_task = asyncio.create_task(
            self.account.get_positions("option")
        )
        wallet_task = asyncio.create_task(self.account.get_wallet_state())
        tasks = (
            linear_open_task,
            option_open_task,
            linear_history_task,
            option_history_task,
            execution_task,
            linear_position_task,
            option_position_task,
            wallet_task,
        )
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

        return PrivateAccountSnapshot(
            open_orders=(
                *linear_open_task.result(),
                *option_open_task.result(),
            ),
            recent_orders=(
                *linear_history_task.result(),
                *option_history_task.result(),
            ),
            executions=execution_task.result(),
            positions=(
                *linear_position_task.result(),
                *option_position_task.result(),
            ),
            wallet=wallet_task.result(),
            captured_at_utc=self._clock(),
        )


def evaluate_private_snapshot(
    snapshot: PrivateAccountSnapshot,
    *,
    known_order_link_ids: frozenset[str],
    expected_positions: tuple[ExpectedPosition, ...],
) -> ReadOnlyReconciliationResult:
    """Require every open order and nonzero position to be explained exactly."""
    differences: list[PrivateStateDifference] = []
    for order in snapshot.open_orders:
        if not order.order_link_id or order.order_link_id not in known_order_link_ids:
            differences.append(
                PrivateStateDifference(
                    kind="UNKNOWN_OPEN_ORDER",
                    detail=(
                        f"unexplained {order.category} order {order.order_id} "
                        f"on {order.symbol}"
                    ),
                )
            )

    actual = _actual_position_quantities(snapshot.positions)
    expected = _expected_position_quantities(expected_positions)
    for key in sorted(actual.keys() | expected.keys()):
        actual_quantity = actual.get(key)
        expected_quantity = expected.get(key)
        category, symbol, side = key
        if expected_quantity is None:
            differences.append(
                PrivateStateDifference(
                    kind="UNKNOWN_POSITION",
                    detail=(
                        f"unexplained {category} {side} position on {symbol}: "
                        f"{actual_quantity}"
                    ),
                )
            )
        elif actual_quantity is None:
            differences.append(
                PrivateStateDifference(
                    kind="MISSING_POSITION",
                    detail=(
                        f"expected {category} {side} position on {symbol}: "
                        f"{expected_quantity}"
                    ),
                )
            )
        elif actual_quantity != expected_quantity:
            differences.append(
                PrivateStateDifference(
                    kind="POSITION_QUANTITY_MISMATCH",
                    detail=(
                        f"{category} {side} position on {symbol}: local "
                        f"{expected_quantity}, exchange {actual_quantity}"
                    ),
                )
            )
    return ReadOnlyReconciliationResult(
        trading_allowed=not differences,
        differences=tuple(differences),
    )


def _actual_position_quantities(
    positions: Iterable[ExchangePosition],
) -> dict[PositionKey, Decimal]:
    quantities: dict[PositionKey, Decimal] = {}
    for position in positions:
        if position.quantity == ZERO:
            continue
        if position.side is None:
            raise ValueError("nonzero exchange position must have a side")
        key = (position.category, position.symbol, position.side)
        quantities[key] = quantities.get(key, ZERO) + position.quantity
    return quantities


def _expected_position_quantities(
    positions: Iterable[ExpectedPosition],
) -> dict[PositionKey, Decimal]:
    quantities: dict[PositionKey, Decimal] = {}
    for position in positions:
        key = (position.category, position.symbol, position.side)
        quantities[key] = quantities.get(key, ZERO) + position.quantity
    return quantities
