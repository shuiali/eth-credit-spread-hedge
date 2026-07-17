"""Immutable M2.1 accounting events and deterministic canonical serialization."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, TypeAlias, cast

from eth_credit_hedge.domain.accounting.errors import (
    AccountingContractError,
    DuplicateAccountingIdentifierError,
)
from eth_credit_hedge.domain.accounting.fills import (
    ConfirmedExecution,
    InstrumentKind,
    Side,
    required_text,
    utc_timestamp,
)
from eth_credit_hedge.domain.strategy_math.units import Money, Price, Quantity
from eth_credit_hedge.domain.accounting.recovery_projection import HedgeAttemptKey


class EventSource(str, Enum):
    PRIVATE_STREAM = "PRIVATE_STREAM"
    REST_RECOVERY = "REST_RECOVERY"
    MIGRATION = "MIGRATION"
    SYSTEM = "SYSTEM"
    LEGACY_DEBT_MIGRATION = "LEGACY_DEBT_MIGRATION"


class OptionLeg(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class HedgeRole(str, Enum):
    BASELINE = "BASELINE"
    RECOVERY = "RECOVERY"


class ExitReason(str, Enum):
    TAKE_PROFIT = "TAKE_PROFIT"
    STOP = "STOP"
    EMERGENCY = "EMERGENCY"


class FeeOwner(str, Enum):
    OPTION = "OPTION"
    HEDGE = "HEDGE"


class ReferenceType(str, Enum):
    TRIGGER = "TRIGGER"
    DECISION = "DECISION"
    BEST_BID_ASK = "BEST_BID_ASK"
    MARK = "MARK"
    EXPECTED_FILL = "EXPECTED_FILL"


class MigrationKind(str, Enum):
    MIGRATED_FROM_LEGACY_SNAPSHOT = "MIGRATED_FROM_LEGACY_SNAPSHOT"


@dataclass(frozen=True, slots=True)
class FundingAllocation:
    lot_id: str
    amount: Money

    def __post_init__(self) -> None:
        required_text(self.lot_id, "funding allocation lot ID")
        if not isinstance(self.amount, Money):
            raise AccountingContractError("funding allocation amount is invalid")


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
    exit_reason: ExitReason | None = None
    reference_type: ReferenceType | None = None
    reference_price: Price | None = None

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
        if self.exit_reason is not None and not isinstance(self.exit_reason, ExitReason):
            raise AccountingContractError("hedge exit reason is invalid")
        if self.execution.side is Side.SELL and self.exit_reason is not None:
            raise AccountingContractError("hedge entry cannot have an exit reason")
        if self.execution.side is Side.BUY and self.exit_reason is None:
            raise AccountingContractError("hedge exit requires an exit reason")
        if self.reference_type is not None and not isinstance(self.reference_type, ReferenceType):
            raise AccountingContractError("hedge reference type is invalid")
        if self.reference_price is not None and not isinstance(self.reference_price, Price):
            raise AccountingContractError("hedge reference price is invalid")
        if (self.reference_type is None) != (self.reference_price is None):
            raise AccountingContractError("hedge reference type and price must be paired")


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
    allocations: tuple[FundingAllocation, ...] = ()

    def __post_init__(self) -> None:
        EventMetadata.__post_init__(self)
        required_text(self.funding_id, "funding ID")
        if not isinstance(self.position_quantity, Quantity):
            raise AccountingContractError("funding position quantity is invalid")
        if not isinstance(self.rate, Decimal) or not self.rate.is_finite():
            raise AccountingContractError("funding rate must be a finite Decimal")
        if not isinstance(self.amount, Money):
            raise AccountingContractError("funding amount is invalid")
        allocations = tuple(self.allocations)
        if len({allocation.lot_id for allocation in allocations}) != len(allocations):
            raise AccountingContractError("funding allocation lot IDs must be unique")
        if allocations and sum((allocation.amount.value for allocation in allocations), Decimal("0")) != self.amount.value:
            raise AccountingContractError("funding allocations must equal funding amount")
        object.__setattr__(self, "allocations", allocations)


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
    actual_recovery_allocation: Money = Money(Decimal("0"))

    def __post_init__(self) -> None:
        EventMetadata.__post_init__(self)
        if not isinstance(self.increment, Money) or self.increment.value < 0:
            raise AccountingContractError("debt increment must be nonnegative")
        if (
            not isinstance(self.actual_recovery_allocation, Money)
            or self.actual_recovery_allocation.value < 0
        ):
            raise AccountingContractError("actual recovery allocation must be nonnegative")
        if self.increment.value and self.actual_recovery_allocation.value:
            raise AccountingContractError(
                "debt change cannot increment and settle debt simultaneously"
            )
        required_text(self.reason, "debt reason")


@dataclass(frozen=True, slots=True, kw_only=True)
class RecoveryDebtIncremented(EventMetadata):
    target: HedgeAttemptKey
    source_hedge_lot_id: str
    source_stop_execution_ids: tuple[str, ...]
    amount: Money

    def __post_init__(self) -> None:
        EventMetadata.__post_init__(self)
        if self.target.cycle_id != self.cycle_id or self.target.level_id != self.level_id:
            raise AccountingContractError("recovery debt target must match event identity")
        required_text(self.source_hedge_lot_id, "source hedge lot ID")
        if not isinstance(self.amount, Money) or self.amount.value < 0:
            raise AccountingContractError("recovery debt increment is invalid")
        ids = tuple(self.source_stop_execution_ids)
        if not ids or len(ids) != len(set(ids)):
            raise AccountingContractError("source stop execution IDs are invalid")
        for execution_id in ids:
            required_text(execution_id, "source stop execution ID")
        object.__setattr__(self, "source_stop_execution_ids", ids)


@dataclass(frozen=True, slots=True, kw_only=True)
class RecoveryAllocationRecorded(EventMetadata):
    target: HedgeAttemptKey
    recovery_hedge_lot_id: str
    gross_realized_recovery_profit: Money
    fees: Money
    funding: Money
    allocated_amount: Money

    def __post_init__(self) -> None:
        EventMetadata.__post_init__(self)
        if self.target.cycle_id != self.cycle_id or self.target.level_id != self.level_id:
            raise AccountingContractError("recovery allocation target must match event identity")
        required_text(self.recovery_hedge_lot_id, "recovery hedge lot ID")
        if not all(isinstance(value, Money) for value in (
            self.gross_realized_recovery_profit, self.fees, self.funding, self.allocated_amount,
        )):
            raise AccountingContractError("recovery allocation amounts are invalid")
        if self.allocated_amount.value < 0:
            raise AccountingContractError("recovery allocation cannot be negative")
        if self.allocated_amount.value > (
            self.gross_realized_recovery_profit.value - self.fees.value + self.funding.value
        ):
            raise AccountingContractError("recovery allocation exceeds actual net recovery profit")


@dataclass(frozen=True, slots=True, kw_only=True)
class MigratedFromLegacySnapshot(EventMetadata):
    """An explicit migration marker; it is never a synthetic raw execution."""

    legacy_snapshot_type: str
    legacy_snapshot_key: str
    legacy_payload_digest: str
    migration_kind: MigrationKind = MigrationKind.MIGRATED_FROM_LEGACY_SNAPSHOT

    def __post_init__(self) -> None:
        EventMetadata.__post_init__(self)
        required_text(self.legacy_snapshot_type, "legacy snapshot type")
        required_text(self.legacy_snapshot_key, "legacy snapshot key")
        required_text(self.legacy_payload_digest, "legacy payload digest")
        if self.migration_kind is not MigrationKind.MIGRATED_FROM_LEGACY_SNAPSHOT:
            raise AccountingContractError("legacy migration kind is invalid")


AccountingEvent: TypeAlias = (
    OptionExecutionRecorded | HedgeExecutionRecorded | FeeRecorded | FundingRecorded
    | ReferencePriceRecorded | OptionQuoteRecorded | PositionReconciled
    | AccountingSnapshotCreated | RecoveryDebtChanged | RecoveryDebtIncremented
    | RecoveryAllocationRecorded | MigratedFromLegacySnapshot
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


def event_from_dict(payload: dict[str, object]) -> AccountingEvent:
    """Reconstruct one canonical JSONL accounting event without float coercion."""
    event_type = _text(payload, "event_type")
    metadata = cast(Any, _metadata(payload))
    if event_type == "OptionExecutionRecorded":
        return OptionExecutionRecorded(
            **metadata,
            execution=_execution(_object(payload, "execution")),
            leg=OptionLeg(_text(payload, "leg")),
        )
    if event_type == "HedgeExecutionRecorded":
        return HedgeExecutionRecorded(
            **metadata,
            execution=_execution(_object(payload, "execution")),
            lot_id=_text(payload, "lot_id"),
            attempt=_integer(payload, "attempt"),
            role=HedgeRole(_text(payload, "role")),
            exit_reason=_exit_reason_or_none(payload),
            reference_type=_reference_type_or_none(payload),
            reference_price=_price_or_none(payload, "reference_price"),
        )
    if event_type == "FeeRecorded":
        return FeeRecorded(
            **metadata,
            fee_id=_text(payload, "fee_id"),
            owner=FeeOwner(_text(payload, "owner")),
            amount=Money(_decimal(payload, "amount")),
            currency=_text(payload, "currency"),
        )
    if event_type == "FundingRecorded":
        allocations = tuple(
            FundingAllocation(
                lot_id=_text(allocation, "lot_id"),
                amount=Money(_decimal(allocation, "amount")),
            )
            for allocation in _objects(payload, "allocations")
        )
        return FundingRecorded(
            **metadata,
            funding_id=_text(payload, "funding_id"),
            position_quantity=Quantity(_decimal(payload, "position_quantity")),
            rate=_decimal(payload, "rate"),
            amount=Money(_decimal(payload, "amount")),
            allocations=allocations,
        )
    if event_type == "ReferencePriceRecorded":
        return ReferencePriceRecorded(
            **metadata,
            reference_type=ReferenceType(_text(payload, "reference_type")),
            price=Price(_decimal(payload, "price")),
        )
    if event_type == "OptionQuoteRecorded":
        return OptionQuoteRecorded(
            **metadata,
            bid=Price(_decimal(payload, "bid")),
            ask=Price(_decimal(payload, "ask")),
            mark=Price(_decimal(payload, "mark")),
            valid_until=_datetime(payload, "valid_until"),
        )
    if event_type == "PositionReconciled":
        matched = payload.get("matched")
        if type(matched) is not bool:
            raise AccountingContractError("reconciliation match flag is invalid")
        return PositionReconciled(
            **metadata,
            internal_quantity=Quantity(_decimal(payload, "internal_quantity")),
            external_quantity=Quantity(_decimal(payload, "external_quantity")),
            matched=matched,
            detail=_text(payload, "detail"),
        )
    if event_type == "AccountingSnapshotCreated":
        return AccountingSnapshotCreated(
            **metadata,
            sequence=_integer(payload, "sequence"),
            ledger_digest=_text(payload, "ledger_digest"),
        )
    if event_type == "RecoveryDebtChanged":
        return RecoveryDebtChanged(
            **metadata,
            increment=Money(_decimal(payload, "increment")),
            actual_recovery_allocation=Money(
                _decimal_or_default(payload, "actual_recovery_allocation", Decimal("0"))
            ),
            reason=_text(payload, "reason"),
        )
    if event_type == "RecoveryDebtIncremented":
        return RecoveryDebtIncremented(
            **metadata,
            target=HedgeAttemptKey(
                cycle_id=_text(_object(payload, "target"), "cycle_id"),
                level_id=_integer(_object(payload, "target"), "level_id"),
                attempt=_integer(_object(payload, "target"), "attempt"),
            ),
            source_hedge_lot_id=_text(payload, "source_hedge_lot_id"),
            source_stop_execution_ids=_texts(payload, "source_stop_execution_ids"),
            amount=Money(_decimal(payload, "amount")),
        )
    if event_type == "RecoveryAllocationRecorded":
        return RecoveryAllocationRecorded(
            **metadata,
            target=HedgeAttemptKey(
                cycle_id=_text(_object(payload, "target"), "cycle_id"),
                level_id=_integer(_object(payload, "target"), "level_id"),
                attempt=_integer(_object(payload, "target"), "attempt"),
            ),
            recovery_hedge_lot_id=_text(payload, "recovery_hedge_lot_id"),
            gross_realized_recovery_profit=Money(_decimal(payload, "gross_realized_recovery_profit")),
            fees=Money(_decimal(payload, "fees")),
            funding=Money(_decimal(payload, "funding")),
            allocated_amount=Money(_decimal(payload, "allocated_amount")),
        )
    if event_type == "MigratedFromLegacySnapshot":
        return MigratedFromLegacySnapshot(
            **metadata,
            legacy_snapshot_type=_text(payload, "legacy_snapshot_type"),
            legacy_snapshot_key=_text(payload, "legacy_snapshot_key"),
            legacy_payload_digest=_text(payload, "legacy_payload_digest"),
            migration_kind=MigrationKind(
                _text_or_default(
                    payload,
                    "migration_kind",
                    MigrationKind.MIGRATED_FROM_LEGACY_SNAPSHOT.value,
                )
            ),
        )
    raise AccountingContractError(f"unsupported accounting event type: {event_type}")


def _texts(payload: dict[str, object], field_name: str) -> tuple[str, ...]:
    raw = payload.get(field_name)
    if not isinstance(raw, list):
        raise AccountingContractError(f"{field_name} must be a list")
    return tuple(_text({"value": value}, "value") for value in raw)


def _metadata(payload: dict[str, object]) -> dict[str, object]:
    return {
        "event_id": _text(payload, "event_id"),
        "event_version": _integer(payload, "event_version"),
        "cycle_id": _text(payload, "cycle_id"),
        "timestamp": _datetime(payload, "timestamp"),
        "source": EventSource(_text(payload, "source")),
        "correlation_id": _text(payload, "correlation_id"),
        "level_id": _optional_integer(payload, "level_id"),
        "execution_id": _optional_text(payload, "execution_id"),
        "order_id": _optional_text(payload, "order_id"),
        "order_link_id": _optional_text(payload, "order_link_id"),
        "symbol": _optional_text(payload, "symbol"),
    }


def _execution(payload: dict[str, object]) -> ConfirmedExecution:
    return ConfirmedExecution(
        execution_id=_text(payload, "execution_id"),
        symbol=_text(payload, "symbol"),
        instrument_kind=InstrumentKind(_text(payload, "instrument_kind")),
        side=Side(_text(payload, "side")),
        price=Price(_decimal(payload, "price")),
        quantity=Quantity(_decimal(payload, "quantity")),
        fee=Money(_decimal(payload, "fee")),
        fee_currency=_text(payload, "fee_currency"),
        timestamp=_datetime(payload, "timestamp"),
        order_id=_text(payload, "order_id"),
        order_link_id=_optional_text(payload, "order_link_id"),
    )


def _object(payload: dict[str, object], field_name: str) -> dict[str, object]:
    value = payload.get(field_name)
    if not isinstance(value, dict):
        raise AccountingContractError(f"{field_name} must be an object")
    return value


def _objects(payload: dict[str, object], field_name: str) -> tuple[dict[str, object], ...]:
    value = payload.get(field_name, [])
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise AccountingContractError(f"{field_name} must be an array of objects")
    return tuple(value)


def _text(payload: dict[str, object], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str):
        raise AccountingContractError(f"{field_name} must be text")
    return value


def _text_or_default(
    payload: dict[str, object], field_name: str, default: str
) -> str:
    return default if field_name not in payload else _text(payload, field_name)


def _optional_text(payload: dict[str, object], field_name: str) -> str | None:
    value = payload.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise AccountingContractError(f"{field_name} must be text or null")
    return value


def _integer(payload: dict[str, object], field_name: str) -> int:
    value = payload.get(field_name)
    if type(value) is not int:
        raise AccountingContractError(f"{field_name} must be an integer")
    return value


def _optional_integer(payload: dict[str, object], field_name: str) -> int | None:
    value = payload.get(field_name)
    if value is None:
        return None
    if type(value) is not int:
        raise AccountingContractError(f"{field_name} must be an integer or null")
    return value


def _decimal(payload: dict[str, object], field_name: str) -> Decimal:
    value = payload.get(field_name)
    if not isinstance(value, str):
        raise AccountingContractError(f"{field_name} must be an exact decimal string")
    try:
        decimal = Decimal(value)
    except Exception as error:
        raise AccountingContractError(f"{field_name} must be an exact decimal string") from error
    if not decimal.is_finite():
        raise AccountingContractError(f"{field_name} must be finite")
    return decimal


def _decimal_or_default(
    payload: dict[str, object], field_name: str, default: Decimal
) -> Decimal:
    return default if field_name not in payload else _decimal(payload, field_name)


def _datetime(payload: dict[str, object], field_name: str) -> datetime:
    value = _text(payload, field_name)
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        raise AccountingContractError(f"{field_name} must be an ISO timestamp") from error


def _enum_text_or_none(payload: dict[str, object], field_name: str) -> str | None:
    value = payload.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise AccountingContractError(f"{field_name} must be text or null")
    return value


def _exit_reason_or_none(payload: dict[str, object]) -> ExitReason | None:
    value = _enum_text_or_none(payload, "exit_reason")
    return None if value is None else ExitReason(value)


def _reference_type_or_none(payload: dict[str, object]) -> ReferenceType | None:
    value = _enum_text_or_none(payload, "reference_type")
    return None if value is None else ReferenceType(value)


def _price_or_none(payload: dict[str, object], field_name: str) -> Price | None:
    return None if payload.get(field_name) is None else Price(_decimal(payload, field_name))


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
