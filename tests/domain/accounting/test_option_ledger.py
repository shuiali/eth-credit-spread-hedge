"""Hand-calculated M2.2 raw-fill option accounting tests."""

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
    OptionExecutionRecorded,
    OptionLeg,
    OptionQuoteRecorded,
)
from eth_credit_hedge.domain.accounting.fills import ConfirmedExecution, InstrumentKind, Side
from eth_credit_hedge.domain.accounting.option_ledger import OptionLedger, OptionLedgerState
from eth_credit_hedge.domain.strategy_math.units import Money, Price, Quantity


NOW = datetime(2026, 7, 17, 10, tzinfo=timezone.utc)
D = Decimal


def execution_event(
    identity: str,
    *,
    leg: OptionLeg,
    side: Side,
    price: str,
    quantity: str = "1",
    fee: str = "0",
    minute: int = 0,
) -> OptionExecutionRecorded:
    symbol = "ETH-LONG-PUT" if leg is OptionLeg.LONG else "ETH-SHORT-PUT"
    execution = ConfirmedExecution(
        execution_id=identity,
        symbol=symbol,
        instrument_kind=InstrumentKind.OPTION,
        side=side,
        price=Price(D(price)),
        quantity=Quantity(D(quantity)),
        fee=Money(D(fee)),
        fee_currency="USDC",
        timestamp=NOW + timedelta(minutes=minute),
        order_id=f"order-{identity}",
        order_link_id=f"link-{identity}",
    )
    return OptionExecutionRecorded(
        event_id=f"event-{identity}",
        event_version=1,
        cycle_id="cycle-1",
        timestamp=execution.timestamp,
        source=EventSource.SYSTEM,
        correlation_id="raw-fill-test",
        execution_id=execution.execution_id,
        order_id=execution.order_id,
        order_link_id=execution.order_link_id,
        symbol=execution.symbol,
        execution=execution,
        leg=leg,
    )


def quote(symbol: str, *, bid: str, ask: str, mark: str) -> OptionQuoteRecorded:
    return OptionQuoteRecorded(
        event_id=f"quote-{symbol}",
        event_version=1,
        cycle_id="cycle-1",
        timestamp=NOW + timedelta(minutes=6),
        source=EventSource.SYSTEM,
        correlation_id="raw-fill-test",
        symbol=symbol,
        bid=Price(D(bid)),
        ask=Price(D(ask)),
        mark=Price(D(mark)),
        valid_until=NOW + timedelta(minutes=20),
    )


def test_fifo_weighted_fills_partial_close_and_valuations_are_fill_derived() -> None:
    ledger = OptionLedger()
    for event in (
        execution_event("long-1", leg=OptionLeg.LONG, side=Side.BUY, price="20", fee="0.2"),
        execution_event("long-2", leg=OptionLeg.LONG, side=Side.BUY, price="22", quantity="2", fee="0.4", minute=1),
        execution_event("short-1", leg=OptionLeg.SHORT, side=Side.SELL, price="50", quantity="2", fee="0.5", minute=2),
        execution_event("short-2", leg=OptionLeg.SHORT, side=Side.SELL, price="48", fee="0.3", minute=3),
        execution_event("short-close", leg=OptionLeg.SHORT, side=Side.BUY, price="45", fee="0.1", minute=4),
        execution_event("long-close", leg=OptionLeg.LONG, side=Side.SELL, price="19", fee="0.1", minute=5),
    ):
        ledger.apply_execution(event)
    ledger.apply_quote(quote("ETH-LONG-PUT", bid="17", ask="19", mark="18"))
    ledger.apply_quote(quote("ETH-SHORT-PUT", bid="43", ask="45", mark="44"))

    snapshot = ledger.snapshot(as_of=NOW + timedelta(minutes=7))

    # Entry long cost = 20 + (22 * 2) = 64; short proceeds = (50 * 2) + 48 = 148.
    # Entry fees = 1.4, so actual net credit = 82.6. FIFO closed price P&L is
    # (50 - 45) + (19 - 20) = 4; all fees remain separate positive costs.
    assert snapshot.actual_net_credit == Money(D("82.6"))
    assert snapshot.option_entry_fees == Money(D("1.4"))
    assert snapshot.option_fees == Money(D("1.6"))
    assert snapshot.option_realized_pnl == Money(D("4"))
    assert snapshot.long.average_entry_price == D("64") / D("3")
    assert snapshot.short.average_entry_price == D("148") / D("3")
    assert snapshot.long.remaining_cost_basis == Money(D("44"))
    assert snapshot.short.remaining_cost_basis == Money(D("98"))
    assert snapshot.state is OptionLedgerState.PARTIALLY_CLOSED
    assert snapshot.matched_quantity == D("2")
    # Mark: long -8 plus short +10. Liquidation: sell long at bid for -10 and
    # buy short at ask for +8.
    assert snapshot.option_open_mark_pnl == Money(D("2"))
    assert snapshot.option_open_liquidation_pnl == Money(D("-2"))


def test_long_only_partial_matched_and_unmatched_short_states_are_explicit() -> None:
    ledger = OptionLedger()
    ledger.apply_execution(
        execution_event("long", leg=OptionLeg.LONG, side=Side.BUY, price="20", quantity="2")
    )
    assert ledger.state is OptionLedgerState.LONG_ONLY
    ledger.apply_execution(
        execution_event("short-1", leg=OptionLeg.SHORT, side=Side.SELL, price="50")
    )
    assert ledger.state is OptionLedgerState.PARTIAL_SPREAD
    ledger.apply_execution(
        execution_event("short-2", leg=OptionLeg.SHORT, side=Side.SELL, price="48", minute=1)
    )
    assert ledger.state is OptionLedgerState.OPEN_MATCHED

    naked = OptionLedger()
    naked.apply_execution(
        execution_event("naked", leg=OptionLeg.SHORT, side=Side.SELL, price="50")
    )
    assert naked.state is OptionLedgerState.ERROR


def test_duplicates_are_idempotent_conflicts_fail_and_stale_quotes_are_rejected() -> None:
    event = execution_event("long", leg=OptionLeg.LONG, side=Side.BUY, price="20")
    ledger = OptionLedger()
    ledger.apply_execution(event)
    ledger.apply_execution(event)
    assert ledger.long.open_quantity == D("1")
    with pytest.raises(DuplicateAccountingIdentifierError, match="execution ID"):
        ledger.apply_execution(replace(event, leg=OptionLeg.SHORT))

    ledger.apply_execution(
        execution_event("short", leg=OptionLeg.SHORT, side=Side.SELL, price="50", minute=1)
    )
    ledger.apply_quote(quote("ETH-LONG-PUT", bid="17", ask="19", mark="18"))
    ledger.apply_quote(quote("ETH-SHORT-PUT", bid="43", ask="45", mark="44"))
    with pytest.raises(AccountingContractError, match="stale"):
        ledger.snapshot(as_of=NOW + timedelta(hours=1))
