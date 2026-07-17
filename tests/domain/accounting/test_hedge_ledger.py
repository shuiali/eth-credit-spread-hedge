"""Independent M2.3 perpetual, funding, slippage, and debt arithmetic tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from eth_credit_hedge.domain.accounting.errors import (
    AccountingContractError,
    DuplicateAccountingIdentifierError,
)
from eth_credit_hedge.domain.accounting.events import (
    EventSource,
    ExitReason,
    FundingRecorded,
    HedgeExecutionRecorded,
    HedgeRole,
    ReferenceType,
)
from eth_credit_hedge.domain.accounting.fills import ConfirmedExecution, InstrumentKind, Side
from eth_credit_hedge.domain.accounting.funding import allocate_funding
from eth_credit_hedge.domain.accounting.hedge_ledger import HedgeLedger
from eth_credit_hedge.domain.strategy_math.units import Money, Price, Quantity


NOW = datetime(2026, 7, 17, 10, tzinfo=timezone.utc)
D = Decimal


def hedge_event(
    identity: str,
    *,
    lot_id: str,
    attempt: int,
    role: HedgeRole,
    side: Side,
    price: str,
    quantity: str,
    fee: str,
    minute: int,
    exit_reason: ExitReason | None = None,
    reference: str | None = None,
) -> HedgeExecutionRecorded:
    execution = ConfirmedExecution(
        execution_id=identity,
        symbol="ETHUSDT",
        instrument_kind=InstrumentKind.PERPETUAL,
        side=side,
        price=Price(D(price)),
        quantity=Quantity(D(quantity)),
        fee=Money(D(fee)),
        fee_currency="USDT",
        timestamp=NOW + timedelta(minutes=minute),
        order_id=f"order-{identity}",
        order_link_id=f"link-{identity}",
    )
    return HedgeExecutionRecorded(
        event_id=f"event-{identity}",
        event_version=1,
        cycle_id="cycle-1",
        level_id=1,
        timestamp=execution.timestamp,
        source=EventSource.SYSTEM,
        correlation_id="hedge-ledger-test",
        execution_id=execution.execution_id,
        order_id=execution.order_id,
        order_link_id=execution.order_link_id,
        symbol=execution.symbol,
        execution=execution,
        lot_id=lot_id,
        attempt=attempt,
        role=role,
        exit_reason=exit_reason,
        reference_type=ReferenceType.MARK if reference else None,
        reference_price=Price(D(reference)) if reference else None,
    )


def funding(identity: str, *, amount: str, quantity: str, minute: int) -> FundingRecorded:
    return FundingRecorded(
        event_id=f"event-{identity}",
        event_version=1,
        cycle_id="cycle-1",
        timestamp=NOW + timedelta(minutes=minute),
        source=EventSource.SYSTEM,
        correlation_id="hedge-ledger-test",
        symbol="ETHUSDT",
        funding_id=identity,
        position_quantity=Quantity(D(quantity)),
        rate=D("0.0001"),
        amount=Money(D(amount)),
    )


def test_partial_tp_then_stop_uses_net_confirmed_result_for_debt() -> None:
    ledger = HedgeLedger()
    entry_one = hedge_event(
        "entry-1", lot_id="baseline", attempt=1, role=HedgeRole.BASELINE,
        side=Side.SELL, price="100", quantity="1", fee="1", minute=0, reference="105",
    )
    entry_two = hedge_event(
        "entry-2", lot_id="baseline", attempt=1, role=HedgeRole.BASELINE,
        side=Side.SELL, price="110", quantity="2", fee="2", minute=1,
    )
    partial_tp = hedge_event(
        "tp", lot_id="baseline", attempt=1, role=HedgeRole.BASELINE,
        side=Side.BUY, price="90", quantity="1", fee="0.5", minute=3,
        exit_reason=ExitReason.TAKE_PROFIT,
    )
    final_stop = hedge_event(
        "stop", lot_id="baseline", attempt=1, role=HedgeRole.BASELINE,
        side=Side.BUY, price="120", quantity="2", fee="1", minute=4,
        exit_reason=ExitReason.STOP, reference="115",
    )
    ledger.apply_execution(entry_one)
    ledger.apply_execution(entry_two)
    allocations = ledger.apply_funding(funding("paid", amount="-6", quantity="3", minute=2))
    ledger.apply_execution(partial_tp)

    partial = ledger.snapshot(mark_price=Price(D("100")))
    assert partial.lots[0].open_quantity == D("2")
    assert partial.confirmed_recovery_debt == Money(D("0"))
    assert allocations[0].amount == Money(D("-6"))

    ledger.apply_execution(final_stop)
    closed = ledger.snapshot()
    lot = closed.lots[0]
    # Gross = (100 - 90) + 2 * (110 - 120) = -10. Net = gross - 3 entry
    # fees - 1.5 exit fees - 6 funding = -20.5. The stop debt is actual net loss.
    assert lot.gross_realized_pnl == Money(D("-10"))
    assert lot.net_realized_pnl == Money(D("-20.5"))
    assert lot.debt_increment == Money(D("20.5"))
    assert closed.confirmed_recovery_debt == Money(D("20.5"))
    assert closed.hedge_fees == Money(D("4.5"))
    assert closed.funding_pnl == Money(D("-6"))
    # Adverse sell slippage is 105 - 100; adverse buy slippage is 120 - 115 * 2.
    assert closed.slippage_attribution == Money(D("15"))


def test_funding_received_and_open_mark_are_separate_from_slippage() -> None:
    ledger = HedgeLedger()
    entry = hedge_event(
        "recovery-entry", lot_id="recovery", attempt=2, role=HedgeRole.RECOVERY,
        side=Side.SELL, price="200", quantity="2", fee="2", minute=0,
    )
    ledger.apply_execution(entry)
    ledger.apply_funding(funding("received", amount="4", quantity="2", minute=1))

    snapshot = ledger.snapshot(mark_price=Price(D("190")), liquidation_price=Price(D("191")))
    assert snapshot.hedge_open_mark_pnl == Money(D("20"))
    assert snapshot.hedge_open_liquidation_pnl == Money(D("18"))
    assert snapshot.funding_pnl == Money(D("4"))
    assert snapshot.hedge_fees == Money(D("2"))
    assert snapshot.slippage_attribution == Money(D("0"))
    ledger.reconcile_exchange_short(Quantity(D("2")))
    with pytest.raises(AccountingContractError, match="internal hedge quantity"):
        ledger.reconcile_exchange_short(Quantity(D("1")))


def test_funding_allocation_duplicates_and_out_of_order_replay_are_deterministic() -> None:
    allocations = allocate_funding(
        Money(D("1")),
        {"b": D("3"), "a": D("1")},
    )
    assert allocations[0].lot_id == "a"
    assert allocations[0].amount == Money(D("0.25"))
    assert allocations[1].amount == Money(D("0.75"))

    persisted = HedgeLedger()
    persisted.apply_execution(
        hedge_event(
            "a-entry", lot_id="a", attempt=1, role=HedgeRole.BASELINE,
            side=Side.SELL, price="100", quantity="1", fee="0", minute=0,
        )
    )
    persisted.apply_execution(
        hedge_event(
            "b-entry", lot_id="b", attempt=1, role=HedgeRole.BASELINE,
            side=Side.SELL, price="100", quantity="3", fee="0", minute=0,
        )
    )
    persisted_funding = replace(
        funding("persisted", amount="-4", quantity="4", minute=1),
        allocations=allocate_funding(Money(D("-4")), {"a": D("1"), "b": D("3")}),
    )
    persisted.apply_funding(persisted_funding)
    by_lot = {lot.lot_id: lot for lot in persisted.snapshot().lots}
    assert by_lot["a"].allocated_funding == Money(D("-1"))
    assert by_lot["b"].allocated_funding == Money(D("-3"))

    events = (
        hedge_event(
            "entry", lot_id="lot", attempt=1, role=HedgeRole.BASELINE,
            side=Side.SELL, price="100", quantity="1", fee="0", minute=0,
        ),
        funding("fund", amount="-1", quantity="1", minute=1),
        hedge_event(
            "tp", lot_id="lot", attempt=1, role=HedgeRole.BASELINE,
            side=Side.BUY, price="90", quantity="1", fee="0", minute=2,
            exit_reason=ExitReason.TAKE_PROFIT,
        ),
    )
    ordered = HedgeLedger.replay(events)
    replayed = HedgeLedger.replay(tuple(reversed(events)))
    assert replayed.snapshot() == ordered.snapshot()

    duplicate = HedgeLedger()
    duplicate.apply_execution(events[0])
    duplicate.apply_execution(events[0])
    with pytest.raises(DuplicateAccountingIdentifierError, match="execution ID"):
        duplicate.apply_execution(replace(events[0], role=HedgeRole.RECOVERY))
    duplicate.apply_funding(funding("duplicate-funding", amount="-1", quantity="1", minute=1))
    duplicate.apply_funding(funding("duplicate-funding", amount="-1", quantity="1", minute=1))
    with pytest.raises(DuplicateAccountingIdentifierError, match="funding ID"):
        duplicate.apply_funding(funding("duplicate-funding", amount="-2", quantity="1", minute=1))
