from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from eth_credit_hedge.domain.accounting.combined import (
    CombinedLedgerSnapshot,
    HedgePositionState,
    OptionPositionState,
)
from eth_credit_hedge.domain.accounting.errors import (
    AccountingContractError,
    DuplicateAccountingIdentifierError,
)
from eth_credit_hedge.domain.accounting.events import (
    EventSource,
    FeeOwner,
    FeeRecorded,
    OptionExecutionRecorded,
    OptionLeg,
    canonical_event_json,
    ensure_unique_events,
    event_digest,
    event_to_dict,
)
from eth_credit_hedge.domain.accounting.fills import (
    ConfirmedExecution,
    InstrumentKind,
    Side,
)
from eth_credit_hedge.domain.strategy_math.units import Money, Price, Quantity


NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)


def execution(identity: str = "execution-1", *, side: Side = Side.SELL) -> ConfirmedExecution:
    return ConfirmedExecution(
        execution_id=identity,
        symbol="ETH-31JUL26-3000-P-USDC",
        instrument_kind=InstrumentKind.OPTION,
        side=side,
        price=Price(Decimal("10")),
        quantity=Quantity(Decimal("1")),
        fee=Money(Decimal("0.25")),
        fee_currency="usdc",
        timestamp=NOW,
        order_id="order-1",
        order_link_id="link-1",
    )


def option_event(identity: str = "event-1") -> OptionExecutionRecorded:
    fill = execution(identity.replace("event", "execution"))
    return OptionExecutionRecorded(
        event_id=identity,
        event_version=1,
        cycle_id="cycle-1",
        timestamp=NOW,
        source=EventSource.PRIVATE_STREAM,
        correlation_id="corr-1",
        execution_id=fill.execution_id,
        order_id=fill.order_id,
        order_link_id=fill.order_link_id,
        symbol=fill.symbol,
        execution=fill,
        leg=OptionLeg.SHORT,
    )


def test_confirmed_execution_is_immutable_exact_and_has_explicit_cash_sign() -> None:
    sell = execution()
    buy = execution(side=Side.BUY)
    assert sell.fee_currency == "USDC"
    assert sell.cash_flow == Money(Decimal("10"))
    assert buy.cash_flow == Money(Decimal("-10"))
    with pytest.raises(AccountingContractError, match="UTC"):
        replace(sell, timestamp=NOW.astimezone(timezone(timedelta(hours=1))))
    with pytest.raises(AccountingContractError, match="fee"):
        replace(sell, fee=Money(Decimal("-1")))
    with pytest.raises(AccountingContractError, match="unsupported"):
        replace(sell, fee_currency="BTC")
    with pytest.raises(Exception, match="positive"):
        replace(sell, price=Price(Decimal("0")))
    with pytest.raises(Exception, match="positive"):
        replace(sell, quantity=Quantity(Decimal("0")))


def test_events_are_immutable_and_canonical_serialization_is_deterministic() -> None:
    event = option_event()
    assert canonical_event_json(event) == canonical_event_json(event)
    assert event_digest(event) == event_digest(event)
    assert event_to_dict(event)["timestamp"] == NOW.isoformat()
    assert event_to_dict(event)["execution"]["price"] == "10"  # type: ignore[index]
    with pytest.raises(AttributeError):
        event.event_id = "changed"  # type: ignore[misc]


def test_duplicate_event_and_execution_ids_are_idempotent_or_explicitly_conflicting() -> None:
    event = option_event()
    assert ensure_unique_events((event, event)) == (event,)
    with pytest.raises(DuplicateAccountingIdentifierError, match="event ID"):
        ensure_unique_events((event, replace(event, correlation_id="different")))
    same_execution = replace(event, event_id="other-event")
    assert ensure_unique_events((event, same_execution)) == (event,)
    with pytest.raises(DuplicateAccountingIdentifierError, match="execution ID"):
        ensure_unique_events(
            (event, replace(same_execution, execution=replace(event.execution, fee=Money(Decimal("1")))))
        )


def test_fee_owner_and_snapshot_contract_are_explicit_and_immutable() -> None:
    fee = FeeRecorded(
        event_id="fee-event",
        event_version=1,
        cycle_id="cycle-1",
        timestamp=NOW,
        source=EventSource.SYSTEM,
        correlation_id="corr",
        fee_id="fee-1",
        owner=FeeOwner.OPTION,
        amount=Money(Decimal("1")),
        currency="USDC",
    )
    assert fee.owner is FeeOwner.OPTION
    money = Money(Decimal("0"))
    snapshot = CombinedLedgerSnapshot(
        as_of=NOW,
        option_realized_pnl=money,
        option_open_mark_pnl=money,
        option_open_liquidation_pnl=money,
        hedge_realized_pnl=money,
        hedge_open_mark_pnl=money,
        hedge_open_liquidation_pnl=money,
        option_fees=money,
        hedge_fees=money,
        funding_pnl=money,
        slippage_attribution=money,
        net_combined_mark_pnl=money,
        net_combined_liquidation_pnl=money,
        confirmed_recovery_debt=money,
        option_position_state=OptionPositionState.UNKNOWN,
        hedge_position_state=HedgePositionState.UNKNOWN,
    )
    assert snapshot.confirmed_recovery_debt == money
    with pytest.raises(AttributeError):
        snapshot.option_fees = Money(Decimal("1"))  # type: ignore[misc]
