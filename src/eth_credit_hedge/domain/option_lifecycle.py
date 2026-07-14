"""Option entry completion policy and expiry lifecycle cutoffs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum


ZERO = Decimal("0")


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


class UnmatchedLongPolicy(str, Enum):
    CLOSE = "CLOSE"
    RETAIN = "RETAIN"


@dataclass(frozen=True, slots=True)
class OptionEntryPolicy:
    max_leg_wait_seconds: Decimal
    allow_partial_spread: bool
    minimum_matched_quantity: Decimal
    maximum_credit_deviation: Decimal
    unmatched_long_policy: UnmatchedLongPolicy = UnmatchedLongPolicy.CLOSE

    def __post_init__(self) -> None:
        max_wait = _decimal(self.max_leg_wait_seconds, "max leg wait seconds")
        minimum_quantity = _decimal(
            self.minimum_matched_quantity,
            "minimum matched quantity",
        )
        maximum_deviation = _decimal(
            self.maximum_credit_deviation,
            "maximum credit deviation",
        )
        if max_wait <= ZERO:
            raise ValueError("max leg wait seconds must be positive")
        if minimum_quantity <= ZERO:
            raise ValueError("minimum matched quantity must be positive")
        if maximum_deviation < ZERO:
            raise ValueError("maximum credit deviation cannot be negative")
        if not isinstance(self.allow_partial_spread, bool):
            raise ValueError("allow partial spread must be boolean")

        object.__setattr__(self, "max_leg_wait_seconds", max_wait)
        object.__setattr__(self, "minimum_matched_quantity", minimum_quantity)
        object.__setattr__(self, "maximum_credit_deviation", maximum_deviation)
        object.__setattr__(
            self,
            "unmatched_long_policy",
            UnmatchedLongPolicy(self.unmatched_long_policy),
        )

    def accepts_completion(
        self,
        *,
        requested_quantity: Decimal,
        matched_quantity: Decimal,
        credit_deviation: Decimal,
    ) -> bool:
        requested = _decimal(requested_quantity, "requested quantity")
        matched = _decimal(matched_quantity, "matched quantity")
        deviation = _decimal(credit_deviation, "credit deviation")
        if requested <= ZERO:
            raise ValueError("requested quantity must be positive")
        if matched < ZERO or matched > requested:
            raise ValueError("matched quantity must be between zero and requested")
        if deviation < ZERO:
            raise ValueError("credit deviation cannot be negative")
        if matched < self.minimum_matched_quantity:
            return False
        if deviation > self.maximum_credit_deviation:
            return False
        return self.allow_partial_spread or matched == requested


class OptionLifecycleEvent(str, Enum):
    OPTION_EXPIRY_APPROACHING = "OPTION_EXPIRY_APPROACHING"
    OPTION_TRADING_CUTOFF = "OPTION_TRADING_CUTOFF"
    OPTION_DELIVERY = "OPTION_DELIVERY"
    OPTION_SETTLED = "OPTION_SETTLED"


@dataclass(frozen=True, slots=True)
class OptionLifecyclePolicy:
    last_new_hedge_time_utc: datetime
    last_option_adjustment_time_utc: datetime
    forced_close_time_utc: datetime
    expiry_time_utc: datetime

    def __post_init__(self) -> None:
        for field_name in (
            "last_new_hedge_time_utc",
            "last_option_adjustment_time_utc",
            "forced_close_time_utc",
            "expiry_time_utc",
        ):
            object.__setattr__(
                self,
                field_name,
                _utc(getattr(self, field_name), field_name.replace("_", " ")),
            )
        if not (
            self.last_new_hedge_time_utc
            <= self.last_option_adjustment_time_utc
            <= self.forced_close_time_utc
            <= self.expiry_time_utc
        ):
            raise ValueError("option lifecycle times must be ordered")

    def allows_new_hedge(self, as_of_utc: datetime) -> bool:
        return _utc(as_of_utc, "as of time") < self.last_new_hedge_time_utc

    def allows_option_adjustment(self, as_of_utc: datetime) -> bool:
        return _utc(as_of_utc, "as of time") < self.last_option_adjustment_time_utc

    def requires_forced_close(self, as_of_utc: datetime) -> bool:
        as_of = _utc(as_of_utc, "as of time")
        return self.forced_close_time_utc <= as_of < self.expiry_time_utc

    def events_due(
        self,
        as_of_utc: datetime,
        *,
        settled: bool = False,
    ) -> tuple[OptionLifecycleEvent, ...]:
        as_of = _utc(as_of_utc, "as of time")
        if settled and as_of < self.expiry_time_utc:
            raise ValueError("option cannot be settled before expiry")
        events: list[OptionLifecycleEvent] = []
        if as_of >= self.last_new_hedge_time_utc:
            events.append(OptionLifecycleEvent.OPTION_EXPIRY_APPROACHING)
        if as_of >= self.last_option_adjustment_time_utc:
            events.append(OptionLifecycleEvent.OPTION_TRADING_CUTOFF)
        if as_of >= self.expiry_time_utc:
            events.append(OptionLifecycleEvent.OPTION_DELIVERY)
        if settled:
            events.append(OptionLifecycleEvent.OPTION_SETTLED)
        return tuple(events)
