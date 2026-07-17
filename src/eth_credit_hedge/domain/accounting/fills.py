"""Confirmed, Decimal-backed execution facts for the combined ledger."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from eth_credit_hedge.domain.accounting.errors import AccountingContractError
from eth_credit_hedge.domain.strategy_math.units import Money, Price, Quantity


class InstrumentKind(str, Enum):
    OPTION = "OPTION"
    PERPETUAL = "PERPETUAL"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


SUPPORTED_FEE_CURRENCIES = frozenset({"USD", "USDT", "USDC"})


def required_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AccountingContractError(f"{field_name} must be nonempty text")
    return value


def utc_timestamp(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise AccountingContractError(f"{field_name} must be timezone-aware UTC")
    if value.utcoffset() != timezone.utc.utcoffset(value):
        raise AccountingContractError(f"{field_name} must be UTC")
    return value


@dataclass(frozen=True, slots=True)
class ConfirmedExecution:
    execution_id: str
    symbol: str
    instrument_kind: InstrumentKind
    side: Side
    price: Price
    quantity: Quantity
    fee: Money
    fee_currency: str
    timestamp: datetime
    order_id: str
    order_link_id: str | None

    def __post_init__(self) -> None:
        required_text(self.execution_id, "execution ID")
        required_text(self.symbol, "symbol")
        required_text(self.order_id, "order ID")
        if self.order_link_id is not None:
            required_text(self.order_link_id, "order link ID")
        if not isinstance(self.instrument_kind, InstrumentKind):
            raise AccountingContractError("instrument kind is invalid")
        if not isinstance(self.side, Side):
            raise AccountingContractError("side is invalid")
        if not isinstance(self.price, Price) or not isinstance(self.quantity, Quantity):
            raise AccountingContractError("price and quantity require exact units")
        if not isinstance(self.fee, Money) or self.fee.value < 0:
            raise AccountingContractError("fee must be nonnegative Money")
        currency = required_text(self.fee_currency, "fee currency").upper()
        if currency not in SUPPORTED_FEE_CURRENCIES:
            raise AccountingContractError(f"unsupported fee currency: {currency}")
        object.__setattr__(self, "fee_currency", currency)
        object.__setattr__(self, "timestamp", utc_timestamp(self.timestamp, "timestamp"))

    @property
    def cash_flow(self) -> Money:
        notional = self.price.value * self.quantity.value
        return Money(notional if self.side is Side.SELL else -notional)
