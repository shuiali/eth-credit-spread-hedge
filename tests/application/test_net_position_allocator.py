"""Internal hedge lots reconcile to Bybit's aggregate one-way position."""

from __future__ import annotations

from decimal import Decimal

import pytest

from eth_credit_hedge.application.demo_runtime_state import LiveHedgeRole
from eth_credit_hedge.application.net_position_allocator import (
    ExitReservationRole,
    HedgeLot,
    LotExecution,
    NetPositionAllocator,
)


def lot(name: str, level_id: int) -> HedgeLot:
    return HedgeLot(
        lot_id=name,
        cycle_id="cycle-1",
        level_id=level_id,
        attempt=1,
        entry_order_link_id=f"ENTRY-{level_id}",
        role=LiveHedgeRole.BASELINE,
        entry_executions=(
            LotExecution(f"FILL-{level_id}", Decimal("0.1"), Decimal("1800")),
        ),
    )


def test_multiple_lots_reconcile_to_one_aggregate_short() -> None:
    allocator = NetPositionAllocator((lot("L1", 1), lot("L2", 2)))
    assert allocator.reconcile_exchange_short(Decimal("0.2"))
    assert not allocator.reconcile_exchange_short(Decimal("0.1"))


def test_sibling_tp_and_stop_share_one_close_capacity_per_lot() -> None:
    allocator = NetPositionAllocator((lot("L1", 1), lot("L2", 2)))
    for lot_id in ("L1", "L2"):
        allocator.reserve_exit(
            lot_id,
            role=ExitReservationRole.TAKE_PROFIT,
            order_link_id=f"TP-{lot_id}",
            quantity=Decimal("0.1"),
        )
        allocator.reserve_exit(
            lot_id,
            role=ExitReservationRole.STOP,
            order_link_id=f"STOP-{lot_id}",
            quantity=Decimal("0.1"),
        )
    assert allocator.total_reserved_close_capacity == Decimal("0.2")


def test_partial_owned_exit_is_allocated_exactly_once() -> None:
    allocator = NetPositionAllocator((lot("L1", 1),))
    allocator.reserve_exit(
        "L1",
        role=ExitReservationRole.TAKE_PROFIT,
        order_link_id="TP-L1",
        quantity=Decimal("0.1"),
    )
    execution = LotExecution("EXIT-1", Decimal("0.04"), Decimal("1750"))
    assert allocator.allocate_owned_exit("TP-L1", execution) == "L1"
    assert allocator.allocate_owned_exit("TP-L1", execution) is None
    assert allocator.total_open_quantity == Decimal("0.06")


def test_aggregate_exit_uses_deterministic_order_link_priority() -> None:
    allocator = NetPositionAllocator((lot("L2", 2), lot("L1", 1)))
    allocations = allocator.allocate_aggregate_exit(
        "EMERGENCY-1",
        Decimal("0.15"),
        Decimal("1810"),
    )
    assert allocations == (("L1", Decimal("0.1")), ("L2", Decimal("0.05")))
    assert allocator.total_open_quantity == Decimal("0.05")


def test_exit_cannot_be_silently_allocated_to_unknown_order() -> None:
    allocator = NetPositionAllocator((lot("L1", 1),))
    with pytest.raises(ValueError, match="exactly one lot"):
        allocator.allocate_owned_exit(
            "UNKNOWN",
            LotExecution("EXIT-X", Decimal("0.1"), Decimal("1750")),
        )
