"""Fill-derived option positions and distinct valuation views."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Literal

from eth_credit_hedge.domain.instruments import (
    OptionContract,
    OptionFill,
    OptionMarketQuote,
)


LegSide = Literal["Long", "Short"]
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


class OptionPositionState(str, Enum):
    PLANNED = "PLANNED"
    LONG_PROTECTION_PENDING = "LONG_PROTECTION_PENDING"
    LONG_PROTECTION_FILLED = "LONG_PROTECTION_FILLED"
    SHORT_PREMIUM_PENDING = "SHORT_PREMIUM_PENDING"
    OPEN = "OPEN"
    PARTIALLY_OPEN = "PARTIALLY_OPEN"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class OptionQuoteValidationPolicy:
    max_quote_age_seconds: Decimal
    max_leg_timestamp_difference_seconds: Decimal
    max_index_price_difference_ratio: Decimal

    def __post_init__(self) -> None:
        for field_name in (
            "max_quote_age_seconds",
            "max_leg_timestamp_difference_seconds",
            "max_index_price_difference_ratio",
        ):
            value = _decimal(getattr(self, field_name), field_name.replace("_", " "))
            if value < ZERO:
                raise ValueError(f"{field_name.replace('_', ' ')} cannot be negative")
            object.__setattr__(self, field_name, value)


DEFAULT_QUOTE_VALIDATION_POLICY = OptionQuoteValidationPolicy(
    max_quote_age_seconds=Decimal("5"),
    max_leg_timestamp_difference_seconds=Decimal("2"),
    max_index_price_difference_ratio=Decimal("0.001"),
)


def _aggregate_fills(
    contract: OptionContract,
    side: LegSide,
    fills: tuple[OptionFill, ...],
) -> tuple[Decimal, Decimal, Decimal]:
    expected_side = "Buy" if side == "Long" else "Sell"
    execution_ids: set[str] = set()
    quantity = ZERO
    notional = ZERO
    fees = ZERO

    for fill in fills:
        if fill.execution_id in execution_ids:
            raise ValueError(f"duplicate execution ID: {fill.execution_id}")
        execution_ids.add(fill.execution_id)
        if fill.symbol != contract.symbol:
            raise ValueError("fill symbol must match the leg contract")
        if fill.side != expected_side:
            raise ValueError(f"fill side must be {expected_side} for a {side} leg")
        quantity += fill.quantity
        notional += fill.price * fill.quantity
        fees += fill.fee

    average_price = notional / quantity if quantity > ZERO else ZERO
    return quantity, average_price, fees


@dataclass(slots=True)
class OptionLegPosition:
    contract: OptionContract
    side: LegSide
    requested_quantity: Decimal
    filled_quantity: Decimal
    average_entry_price: Decimal
    fees_paid: Decimal
    fills: tuple[OptionFill, ...] = ()

    def __post_init__(self) -> None:
        if self.side not in ("Long", "Short"):
            raise ValueError("leg side must be Long or Short")
        for field_name in (
            "requested_quantity",
            "filled_quantity",
            "average_entry_price",
            "fees_paid",
        ):
            setattr(
                self,
                field_name,
                _decimal(getattr(self, field_name), field_name.replace("_", " ")),
            )
        self.fills = tuple(self.fills)

        if self.requested_quantity <= ZERO:
            raise ValueError("requested quantity must be positive")
        if not ZERO <= self.filled_quantity <= self.requested_quantity:
            raise ValueError("filled quantity must be between zero and requested")
        if self.fees_paid < ZERO:
            raise ValueError("fees paid cannot be negative")
        if self.filled_quantity == ZERO and self.average_entry_price != ZERO:
            raise ValueError("unfilled leg must have zero average entry price")
        if self.filled_quantity > ZERO and self.average_entry_price <= ZERO:
            raise ValueError("filled leg must have a positive average entry price")

        if self.fills:
            quantity, average_price, fees = _aggregate_fills(
                self.contract,
                self.side,
                self.fills,
            )
            if quantity != self.filled_quantity:
                raise ValueError("filled quantity must equal confirmed fill quantity")
            if average_price != self.average_entry_price:
                raise ValueError("average entry price must equal fill-weighted average")
            if fees != self.fees_paid:
                raise ValueError("fees paid must equal confirmed fill fees")

    @classmethod
    def from_fills(
        cls,
        *,
        contract: OptionContract,
        side: LegSide,
        requested_quantity: Decimal,
        fills: tuple[OptionFill, ...],
    ) -> OptionLegPosition:
        normalized_fills = tuple(fills)
        quantity, average_price, fees = _aggregate_fills(
            contract,
            side,
            normalized_fills,
        )
        return cls(
            contract=contract,
            side=side,
            requested_quantity=requested_quantity,
            filled_quantity=quantity,
            average_entry_price=average_price,
            fees_paid=fees,
            fills=normalized_fills,
        )

    @property
    def remaining_quantity(self) -> Decimal:
        return self.requested_quantity - self.filled_quantity


@dataclass(frozen=True, slots=True)
class OptionPositionSnapshot:
    short_symbol: str
    long_symbol: str
    matched_quantity: Decimal
    short_average_entry_price: Decimal
    long_average_entry_price: Decimal
    actual_net_credit: Decimal
    total_fees: Decimal
    opened_time_utc: datetime | None
    expiry_time_utc: datetime
    state: OptionPositionState

    def to_dict(self) -> dict[str, str | None]:
        return {
            "short_symbol": self.short_symbol,
            "long_symbol": self.long_symbol,
            "matched_quantity": str(self.matched_quantity),
            "short_average_entry_price": str(self.short_average_entry_price),
            "long_average_entry_price": str(self.long_average_entry_price),
            "actual_net_credit": str(self.actual_net_credit),
            "total_fees": str(self.total_fees),
            "opened_time_utc": (
                self.opened_time_utc.isoformat()
                if self.opened_time_utc is not None
                else None
            ),
            "expiry_time_utc": self.expiry_time_utc.isoformat(),
            "state": self.state.value,
        }


@dataclass(slots=True)
class PutCreditSpreadPosition:
    short_put: OptionLegPosition
    long_put: OptionLegPosition
    state: OptionPositionState
    opened_time_utc: datetime | None = None

    def __post_init__(self) -> None:
        self.state = OptionPositionState(self.state)
        self._validate_contracts()
        self._validate_protection()
        self._set_opened_time()
        self._validate_state()

    @property
    def matched_quantity(self) -> Decimal:
        return min(self.short_put.filled_quantity, self.long_put.filled_quantity)

    @property
    def has_naked_short(self) -> bool:
        return self.short_put.filled_quantity > self.long_put.filled_quantity

    @property
    def short_proceeds(self) -> Decimal:
        return (
            self.short_put.average_entry_price
            * self.short_put.filled_quantity
            * self.short_put.contract.contract_multiplier
        )

    @property
    def long_cost(self) -> Decimal:
        return (
            self.long_put.average_entry_price
            * self.long_put.filled_quantity
            * self.long_put.contract.contract_multiplier
        )

    @property
    def total_entry_fees(self) -> Decimal:
        return self.short_put.fees_paid + self.long_put.fees_paid

    @property
    def actual_net_credit(self) -> Decimal:
        return self.short_proceeds - self.long_cost - self.total_entry_fees

    def expiration_pnl(self, underlying_price: Decimal) -> Decimal:
        price = _decimal(underlying_price, "underlying price")
        if price < ZERO:
            raise ValueError("underlying price cannot be negative")
        short_intrinsic = max(self.short_put.contract.strike - price, ZERO)
        long_intrinsic = max(self.long_put.contract.strike - price, ZERO)
        return (
            self.actual_net_credit
            - short_intrinsic
            * self.short_put.filled_quantity
            * self.short_put.contract.contract_multiplier
            + long_intrinsic
            * self.long_put.filled_quantity
            * self.long_put.contract.contract_multiplier
        )

    def mark_pnl(
        self,
        short_quote: OptionMarketQuote,
        long_quote: OptionMarketQuote,
        *,
        as_of_utc: datetime | None = None,
        validation_policy: OptionQuoteValidationPolicy = (
            DEFAULT_QUOTE_VALIDATION_POLICY
        ),
        short_instrument_status: str = "Trading",
        long_instrument_status: str = "Trading",
    ) -> Decimal:
        self._validate_quotes(
            short_quote,
            long_quote,
            as_of_utc=as_of_utc,
            validation_policy=validation_policy,
            short_instrument_status=short_instrument_status,
            long_instrument_status=long_instrument_status,
            require_liquidation_prices=False,
        )
        return self._pnl_at_prices(short_quote.mark_price, long_quote.mark_price)

    def liquidation_pnl(
        self,
        short_quote: OptionMarketQuote,
        long_quote: OptionMarketQuote,
        *,
        as_of_utc: datetime | None = None,
        validation_policy: OptionQuoteValidationPolicy = (
            DEFAULT_QUOTE_VALIDATION_POLICY
        ),
        short_instrument_status: str = "Trading",
        long_instrument_status: str = "Trading",
    ) -> Decimal:
        self._validate_quotes(
            short_quote,
            long_quote,
            as_of_utc=as_of_utc,
            validation_policy=validation_policy,
            short_instrument_status=short_instrument_status,
            long_instrument_status=long_instrument_status,
            require_liquidation_prices=True,
        )
        if short_quote.ask_price is None:
            raise ValueError("short ask is required for liquidation")
        if long_quote.bid_price is None:
            raise ValueError("long bid is required for liquidation")
        return self._pnl_at_prices(short_quote.ask_price, long_quote.bid_price)

    def snapshot(self) -> OptionPositionSnapshot:
        return OptionPositionSnapshot(
            short_symbol=self.short_put.contract.symbol,
            long_symbol=self.long_put.contract.symbol,
            matched_quantity=self.matched_quantity,
            short_average_entry_price=self.short_put.average_entry_price,
            long_average_entry_price=self.long_put.average_entry_price,
            actual_net_credit=self.actual_net_credit,
            total_fees=self.total_entry_fees,
            opened_time_utc=self.opened_time_utc,
            expiry_time_utc=self.short_put.contract.expiry_time_utc,
            state=self.state,
        )

    def _pnl_at_prices(self, short_price: Decimal, long_price: Decimal) -> Decimal:
        return (
            self.actual_net_credit
            - short_price
            * self.short_put.filled_quantity
            * self.short_put.contract.contract_multiplier
            + long_price
            * self.long_put.filled_quantity
            * self.long_put.contract.contract_multiplier
        )

    def _validate_quotes(
        self,
        short_quote: OptionMarketQuote,
        long_quote: OptionMarketQuote,
        *,
        as_of_utc: datetime | None,
        validation_policy: OptionQuoteValidationPolicy,
        short_instrument_status: str,
        long_instrument_status: str,
        require_liquidation_prices: bool,
    ) -> None:
        as_of = (
            datetime.now(timezone.utc)
            if as_of_utc is None
            else _utc(as_of_utc, "valuation time")
        )
        if as_of >= self.short_put.contract.expiry_time_utc:
            raise ValueError("option expiry has passed")
        if short_quote.symbol != self.short_put.contract.symbol:
            raise ValueError("short quote symbol must match the short contract")
        if long_quote.symbol != self.long_put.contract.symbol:
            raise ValueError("long quote symbol must match the long contract")
        for leg_name, status in (
            ("short", short_instrument_status),
            ("long", long_instrument_status),
        ):
            if status != "Trading":
                raise ValueError(f"{leg_name} instrument is not Trading")

        for leg_name, quote in (("short", short_quote), ("long", long_quote)):
            age_seconds = Decimal(str((as_of - quote.timestamp_utc).total_seconds()))
            if age_seconds < ZERO:
                raise ValueError(f"{leg_name} quote timestamp is after valuation time")
            if age_seconds > validation_policy.max_quote_age_seconds:
                raise ValueError(f"{leg_name} quote is stale")

        timestamp_difference = abs(
            Decimal(
                str(
                    (
                        short_quote.timestamp_utc - long_quote.timestamp_utc
                    ).total_seconds()
                )
            )
        )
        if (
            timestamp_difference
            > validation_policy.max_leg_timestamp_difference_seconds
        ):
            raise ValueError("option quote timestamps differ beyond tolerance")

        index_difference_ratio = (
            abs(short_quote.index_price - long_quote.index_price)
            / max(short_quote.index_price, long_quote.index_price)
        )
        if (
            index_difference_ratio
            > validation_policy.max_index_price_difference_ratio
        ):
            raise ValueError("option quote index prices differ beyond tolerance")

        if require_liquidation_prices:
            if short_quote.ask_price is None:
                raise ValueError("short ask is required for liquidation")
            if long_quote.bid_price is None:
                raise ValueError("long bid is required for liquidation")

    def _set_opened_time(self) -> None:
        if self.opened_time_utc is not None:
            self.opened_time_utc = _utc(self.opened_time_utc, "opened time")
            return
        if self.matched_quantity <= ZERO:
            return
        fill_times = tuple(
            fill.timestamp_utc
            for fill in (*self.short_put.fills, *self.long_put.fills)
        )
        if fill_times:
            self.opened_time_utc = max(fill_times)

    def _validate_contracts(self) -> None:
        if self.short_put.side != "Short" or self.long_put.side != "Long":
            raise ValueError("credit spread requires short and long leg sides")
        short = self.short_put.contract
        long = self.long_put.contract
        if short.option_type != "Put" or long.option_type != "Put":
            raise ValueError("credit spread legs must both be puts")
        if (
            short.base_coin,
            short.quote_coin,
            short.settle_coin,
        ) != (
            long.base_coin,
            long.quote_coin,
            long.settle_coin,
        ):
            raise ValueError("credit spread legs must use the same coins")
        if short.expiry_time_utc != long.expiry_time_utc:
            raise ValueError("credit spread legs must have the same expiry")
        if short.contract_multiplier != long.contract_multiplier:
            raise ValueError("credit spread legs must have the same multiplier")
        if short.strike <= long.strike:
            raise ValueError("short put strike must be above long put strike")

    def _validate_protection(self) -> None:
        if self.has_naked_short:
            raise ValueError("short fill would create naked short option exposure")

    def _validate_state(self) -> None:
        if self.state is OptionPositionState.PLANNED and (
            self.short_put.filled_quantity != ZERO
            or self.long_put.filled_quantity != ZERO
        ):
            raise ValueError("PLANNED position cannot contain fills")
        if self.state is OptionPositionState.LONG_PROTECTION_PENDING:
            if self.short_put.filled_quantity != ZERO:
                raise ValueError("short cannot fill while long protection is pending")
        if self.state is OptionPositionState.LONG_PROTECTION_FILLED:
            if self.long_put.filled_quantity <= ZERO:
                raise ValueError("state requires confirmed protective long")
            if self.short_put.filled_quantity != ZERO:
                raise ValueError("short cannot fill before premium submission")
        if self.state is OptionPositionState.SHORT_PREMIUM_PENDING:
            if self.long_put.filled_quantity <= ZERO:
                raise ValueError("state requires confirmed protective long")
        if self.state is OptionPositionState.OPEN:
            if self.matched_quantity <= ZERO:
                raise ValueError("OPEN requires confirmed matched quantity")
            if self.short_put.filled_quantity != self.long_put.filled_quantity:
                raise ValueError("OPEN requires equal confirmed leg quantities")
            if self.actual_net_credit <= ZERO:
                raise ValueError("OPEN requires positive actual net credit")
        if self.state is OptionPositionState.PARTIALLY_OPEN:
            if self.matched_quantity <= ZERO:
                raise ValueError("PARTIALLY_OPEN requires confirmed matched quantity")
            if self.short_put.filled_quantity == self.long_put.filled_quantity:
                raise ValueError("equal filled quantities must use OPEN state")
