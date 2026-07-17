"""Immutable M2.1 accounting events and deterministic canonical serialization."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import TypeAlias

from eth_credit_hedge.domain.accounting.errors import (
    AccountingContractError,
    DuplicateAccountingIdentifierError,
)
from eth_credit_hedge.domain.accounting.fills import (
    ConfirmedExecution,
    InstrumentKind,
    required_text,
    utc_timestamp,
)
from eth_credit_hedge.domain.strategy_math.units import Money, Price, Quantity


class EventSource(str, Enum):
    PRIVATE_STREAM = "PRIVATE_STREAM"
    REST_RECOVERY = "REST_RECOVERY"
    MIGRATION = "MIGRATION"
    SYSTEM = "SYSTEM"


class OptionLeg(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class HedgeRole(str, Enum):
    BASELINE = "BASELINE"
    RECOVERY = "RECOVERY"


class FeeOwner(str, Enum):
    OPTION = "OPTION"
    HEDGE = "HEDGE"


class ReferenceType(str, Enum):
    TRIGGER = "TRIGGER"
    DECISION = "DECISION"
    BEST_BID_ASK = "BEST_BID_ASK"
    MARK = "MARK"
    EXPECTED_FILL = "EXPECTED_FILL"


@dataclass(frozen=True, slots=True, kw_only=True)
class EventMetadata:
    event_id: str
    event_version: int
    cycle_id: str
    timestamp: datetime
    source: EventSource
    correlation_id: str
    level_id: int | None = None
    execution_id: str | None = None
    order_id: str | None = None
    order_link_id: str | None = None
    symbol: str | None = None

    def __post_init__(self) -> None:
        required_text(self.event_id, "event ID")
        required_text(self.cycle_id, "cycle ID")
        required_text(self.correlation_id, "correlation ID")
        if not isinstance(self.event_version, int) or self.event_version <= 0:
            raise AccountingContractError("event version must be positive")
        if self.level_id is not None and (
            not isinstance(self.level_id, int) or self.level_id <= 0
        ):
            raise AccountingContractError("level ID must be positive when present")
        for value, name in (
            (self.execution_id, "execution ID"),
            (self.order_id, "order ID"),
            (self.order_link_id, "order link ID"),
            (self.symbol, "symbol"),
        ):
            if value is not None:
                required_text(value, name)
        if not isinstance(self.source, EventSource):
            raise AccountingContractError("event source is invalid")
        object.__setattr__(self, "timestamp", utc_timestamp(self.timestamp, "timestamp"))


@dataclass(frozen=True, slots=True, kw_only=True)
class OptionExecutionRecorded(EventMetadata):
    execution: ConfirmedExecution
    leg: OptionLeg

    def __post_init__(self) -> None:
        EventMetadata.__post_init__(self)
        if self.execution.instrument_kind is not InstrumentKind.OPTION:
            raise AccountingContractError("option event requires an option execution")
        if self.execution_id != self.execution.execution_id or self.symbol != self.execution.symbol:
            raise AccountingContractError("option event metadata must match execution")
        if not isinstance(self.leg, OptionLeg):
            raise AccountingContractError("option leg is invalid")


@dataclass(frozen=True, slots=True, kw_only=True)
class HedgeExecutionRecorded(EventMetadata):
    execution: ConfirmedExecution
    lot_id: str
    attempt: int
    role: HedgeRole

    def __post_init__(self) -> None:
        EventMetadata.__post_init__(self)
        if self.execution.instrument_kind is not InstrumentKind.PERPETUAL:
            raise AccountingContractError("hedge event requires a perpetual execution")
        if self.level_id is None or self.execution_id != self.execution.execution_id:
            raise AccountingContractError("hedge event requires matching level and execution")
        if self.symbol != self.execution.symbol:
            raise AccountingContractError("hedge event metadata must match execution")
        required_text(self.lot_id, "lot ID")
        if not isinstance(self.attempt, int) or self.attempt <= 0:
            raise AccountingContractError("attempt must be positive")
        if not isinstance(self.role, HedgeRole):
            raise AccountingContractError("hedge role is invalid")


@dataclass(frozen=True, slots=True, kw_only=True)
class FeeRecorded(EventMetadata):
    fee_id: str
    owner: FeeOwner
    amount: Money
    currency: str

    def __post_init__(self) -> None:
        EventMetadata.__post_init__(self)
        required_text(self.fee_id, "fee ID")
        if not isinstance(self.owner, FeeOwner) or not isinstance(self.amount, Money):
            raise AccountingContractError("fee owner and amount are invalid")
        if self.amount.value < 0:
            raise AccountingContractError("fee amount cannot be negative")
        required_text(self.currency, "fee currency")


@dataclass(frozen=True, slots=True, kw_only=True)
class FundingRecorded(EventMetadata):
    funding_id: str
    position_quantity: Quantity
    rate: Decimal
    amount: Money

    def __post_init__(self) -> None:
        EventMetadata.__post_init__(self)
        required_text(self.funding_id, "funding ID")
        if not isinstance(self.position_quantity, Quantity):
            raise AccountingContractError("funding position quantity is invalid")
        if not isinstance(self.rate, Decimal) or not self.rate.is_finite():
            raise AccountingContractError("funding rate must be a finite Decimal")
        if not isinstance(self.amount, Money):
            raise AccountingContractError("funding amount is invalid")


@dataclass(frozen=True, slots=True, kw_only=True)
class ReferencePriceRecorded(EventMetadata):
    reference_type: ReferenceType
    price: Price

    def __post_init__(self) -> None:
        EventMetadata.__post_init__(self)
        if not isinstance(self.reference_type, ReferenceType) or not isinstance(self.price, Price):
            raise AccountingContractError("reference price is invalid")


@dataclass(frozen=True, slots=True, kw_only=True)
class OptionQuoteRecorded(EventMetadata):
    bid: Price
    ask: Price
    mark: Price
    valid_until: datetime

    def __post_init__(self) -> None:
        EventMetadata.__post_init__(self)
        if not all(isinstance(value, Price) for value in (self.bid, self.ask, self.mark)):
            raise AccountingContractError("option quote prices are invalid")
        if self.bid.value > self.ask.value:
            raise AccountingContractError("option quote bid cannot exceed ask")
        valid_until = utc_timestamp(self.valid_until, "quote valid-until")
        if valid_until < self.timestamp:
            raise AccountingContractError("quote valid-until precedes timestamp")
        object.__setattr__(self, "valid_until", valid_until)


@dataclass(frozen=True, slots=True, kw_only=True)
class PositionReconciled(EventMetadata):
    internal_quantity: Quantity
    external_quantity: Quantity
    matched: bool
    detail: str

    def __post_init__(self) -> None:
        EventMetadata.__post_init__(self)
        if not isinstance(self.internal_quantity, Quantity) or not isinstance(self.external_quantity, Quantity):
            raise AccountingContractError("reconciliation quantities are invalid")
        if type(self.matched) is not bool:
            raise AccountingContractError("reconciliation match flag is invalid")
        required_text(self.detail, "reconciliation detail")


@dataclass(frozen=True, slots=True, kw_only=True)
class AccountingSnapshotCreated(EventMetadata):
    sequence: int
    ledger_digest: str

    def __post_init__(self) -> None:
        EventMetadata.__post_init__(self)
        if not isinstance(self.sequence, int) or self.sequence < 0:
            raise AccountingContractError("snapshot sequence is invalid")
        required_text(self.ledger_digest, "ledger digest")


@dataclass(frozen=True, slots=True, kw_only=True)
class RecoveryDebtChanged(EventMetadata):
    increment: Money
    reason: str

    def __post_init__(self) -> None:
        EventMetadata.__post_init__(self)
        if not isinstance(self.increment, Money) or self.increment.value < 0:
            raise AccountingContractError("debt increment must be nonnegative")
        required_text(self.reason, "debt reason")


AccountingEvent: TypeAlias = (
    OptionExecutionRecorded | HedgeExecutionRecorded | FeeRecorded | FundingRecorded
    | ReferencePriceRecorded | OptionQuoteRecorded | PositionReconciled
    | AccountingSnapshotCreated | RecoveryDebtChanged
)


def _value(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (Money, Price, Quantity)):
        return str(value.value)
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value):
        return {field.name: _value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, tuple):
        return [_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _value(item) for key, item in value.items()}
    if isinstance(value, float):
        raise AccountingContractError("binary float is not permitted in accounting serialization")
    return value


def event_to_dict(event: AccountingEvent) -> dict[str, object]:
    payload = _value(event)
    if not isinstance(payload, dict):
        raise AssertionError("accounting event must serialize as an object")
    return {"event_type": type(event).__name__, **payload}


def canonical_event_json(event: AccountingEvent) -> str:
    return json.dumps(event_to_dict(event), separators=(",", ":"), sort_keys=True)


def event_digest(event: AccountingEvent) -> str:
    return hashlib.sha256(canonical_event_json(event).encode("utf-8")).hexdigest()


def ensure_unique_events(events: tuple[AccountingEvent, ...]) -> tuple[AccountingEvent, ...]:
    """Return canonical duplicates once and reject conflicting identifiers."""
    by_event_id: dict[str, str] = {}
    by_execution_id: dict[str, str] = {}
    unique: list[AccountingEvent] = []
    for event in events:
        canonical = canonical_event_json(event)
        prior_event = by_event_id.get(event.event_id)
        if prior_event is not None:
            if prior_event != canonical:
                raise DuplicateAccountingIdentifierError(
                    f"conflicting event ID: {event.event_id}"
                )
            continue
        if isinstance(event, (OptionExecutionRecorded, HedgeExecutionRecorded)):
            execution_id = event.execution.execution_id
            execution_content = canonical_event_json(event).replace(event.event_id, "")
            prior_execution = by_execution_id.get(execution_id)
            if prior_execution is not None:
                if prior_execution != execution_content:
                    raise DuplicateAccountingIdentifierError(
                        f"conflicting execution ID: {execution_id}"
                    )
                continue
            by_execution_id[execution_id] = execution_content
        by_event_id[event.event_id] = canonical
        unique.append(event)
    return tuple(unique)
