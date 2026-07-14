"""Stable deterministic event serialization tests."""

from decimal import Decimal

from eth_credit_hedge.core.ledger import LedgerEvent, LedgerEventType
from eth_credit_hedge.core.virtual_levels import LevelState


def test_ledger_event_serialization_is_versioned_and_decimal_exact() -> None:
    event = LedgerEvent(
        sequence=12,
        tick_index=42,
        event_type=LedgerEventType.STOP,
        level_id=1,
        price=Decimal("3004.5"),
        quantity=Decimal("1.225"),
        realized_pnl=Decimal("-5.5125"),
        level_state=LevelState.READY,
        attempt=2,
        projected_stop_loss=Decimal("5.5125"),
        recovery_allocations={1: Decimal("5.5125")},
    )

    assert event.to_dict() == {
        "event_version": 1,
        "sequence": 12,
        "tick_index": 42,
        "event_type": "STOP",
        "level_id": 1,
        "price": "3004.5",
        "quantity": "1.225",
        "realized_pnl": "-5.5125",
        "level_state": "READY",
        "attempt": 2,
        "projected_stop_loss": "5.5125",
        "zone_profit_component": "0",
        "recovery_profit_component": "0",
        "recovery_allocations": {"1": "5.5125"},
    }
    assert event.to_json() == (
        '{"event_version":1,"sequence":12,"tick_index":42,'
        '"event_type":"STOP","level_id":1,"price":"3004.5",'
        '"quantity":"1.225","realized_pnl":"-5.5125",'
        '"level_state":"READY","attempt":2,'
        '"projected_stop_loss":"5.5125",'
        '"zone_profit_component":"0",'
        '"recovery_profit_component":"0",'
        '"recovery_allocations":{"1":"5.5125"}}'
    )
