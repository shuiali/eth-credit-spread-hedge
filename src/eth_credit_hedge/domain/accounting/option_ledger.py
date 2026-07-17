"""FIFO option-spread accounting reconstructed from confirmed option fills."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum

from eth_credit_hedge.domain.accounting.errors import (
    AccountingContractError,
    DuplicateAccountingIdentifierError,
)
from eth_credit_hedge.domain.accounting.events import OptionExecutionRecorded, OptionLeg, OptionQuoteRecorded
from eth_credit_hedge.domain.accounting.fills import Side
from eth_credit_hedge.domain.strategy_math.units import Money


ZERO = Decimal("0")


class OptionLedgerState(str, Enum):
    EMPTY = "EMPTY"
    LONG_ONLY = "LONG_ONLY"
    PARTIAL_SPREAD = "PARTIAL_SPREAD"
    OPEN_MATCHED = "OPEN_MATCHED"
    PARTIALLY_CLOSED = "PARTIALLY_CLOSED"
    CLOSED = "CLOSED"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class OpenOptionLot:
    execution_id: str
    quantity: Decimal
    remaining_quantity: Decimal
    price: Decimal
    fee: Money
    timestamp: datetime


@dataclass(frozen=True, slots=True)
class OptionLegSnapshot:
    symbol: str | None
    side: OptionLeg
    entry_quantity: Decimal
    exit_quantity: Decimal
    open_quantity: Decimal
    average_entry_price: Decimal
    average_exit_price: Decimal
    entry_cash_flow: Money
    exit_cash_flow: Money
    entry_fees: Money
    exit_fees: Money
    realized_pnl: Money
    remaining_cost_basis: Money


@dataclass(slots=True)
class OptionLegLedger:
    side: OptionLeg
    contract_multiplier: Decimal
    symbol: str | None = None
    entry_lots: list[OpenOptionLot] = field(default_factory=list)
    entry_quantity: Decimal = ZERO
    exit_quantity: Decimal = ZERO
    entry_notional: Decimal = ZERO
    exit_notional: Decimal = ZERO
    entry_fees: Decimal = ZERO
    exit_fees: Decimal = ZERO
    realized_pnl: Decimal = ZERO

    @property
    def open_quantity(self) -> Decimal:
        return self.entry_quantity - self.exit_quantity

    @property
    def average_entry_price(self) -> Decimal:
        return self.entry_notional / self.entry_quantity if self.entry_quantity else ZERO

    @property
    def average_exit_price(self) -> Decimal:
        return self.exit_notional / self.exit_quantity if self.exit_quantity else ZERO

    @property
    def remaining_cost_basis(self) -> Decimal:
        return sum(
            (
                lot.remaining_quantity * lot.price * self.contract_multiplier
                for lot in self.entry_lots
            ),
            ZERO,
        )

    def apply(self, event: OptionExecutionRecorded) -> None:
        execution = event.execution
        if self.symbol is None:
            self.symbol = execution.symbol
        if execution.symbol != self.symbol:
            raise AccountingContractError("option leg symbol changed")
        opens = (self.side is OptionLeg.LONG and execution.side is Side.BUY) or (
            self.side is OptionLeg.SHORT and execution.side is Side.SELL
        )
        notional = execution.price.value * execution.quantity.value * self.contract_multiplier
        if opens:
            self.entry_lots.append(
                OpenOptionLot(
                    execution_id=execution.execution_id,
                    quantity=execution.quantity.value,
                    remaining_quantity=execution.quantity.value,
                    price=execution.price.value,
                    fee=execution.fee,
                    timestamp=execution.timestamp,
                )
            )
            self.entry_quantity += execution.quantity.value
            self.entry_notional += notional
            self.entry_fees += execution.fee.value
            return
        self._close(execution.quantity.value, execution.price.value)
        self.exit_quantity += execution.quantity.value
        self.exit_notional += notional
        self.exit_fees += execution.fee.value

    def _close(self, quantity: Decimal, price: Decimal) -> None:
        if quantity > self.open_quantity:
            raise AccountingContractError(f"{self.side.value.lower()} option exit exceeds open quantity")
        remaining = quantity
        updated: list[OpenOptionLot] = []
        for lot in self.entry_lots:
            allocated = min(lot.remaining_quantity, remaining)
            if allocated:
                direction = Decimal("1") if self.side is OptionLeg.LONG else Decimal("-1")
                self.realized_pnl += (
                    (price - lot.price) * allocated * self.contract_multiplier * direction
                )
                remaining -= allocated
            updated.append(
                OpenOptionLot(
                    execution_id=lot.execution_id,
                    quantity=lot.quantity,
                    remaining_quantity=lot.remaining_quantity - allocated,
                    price=lot.price,
                    fee=lot.fee,
                    timestamp=lot.timestamp,
                )
            )
        if remaining:
            raise AssertionError("FIFO option close was not fully allocated")
        self.entry_lots = updated

    def open_pnl(self, price: Decimal) -> Decimal:
        direction = Decimal("1") if self.side is OptionLeg.LONG else Decimal("-1")
        return sum(
            (
                (price - lot.price) * lot.remaining_quantity * self.contract_multiplier * direction
                for lot in self.entry_lots
            ),
            ZERO,
        )

    def signed_open_value(self, price: Decimal) -> Decimal:
        sign = Decimal("1") if self.side is OptionLeg.LONG else Decimal("-1")
        return price * self.open_quantity * self.contract_multiplier * sign

    def snapshot(self) -> OptionLegSnapshot:
        entry_sign = Decimal("-1") if self.side is OptionLeg.LONG else Decimal("1")
        exit_sign = -entry_sign
        return OptionLegSnapshot(
            symbol=self.symbol,
            side=self.side,
            entry_quantity=self.entry_quantity,
            exit_quantity=self.exit_quantity,
            open_quantity=self.open_quantity,
            average_entry_price=self.average_entry_price,
            average_exit_price=self.average_exit_price,
            entry_cash_flow=Money(entry_sign * self.entry_notional),
            exit_cash_flow=Money(exit_sign * self.exit_notional),
            entry_fees=Money(self.entry_fees),
            exit_fees=Money(self.exit_fees),
            realized_pnl=Money(self.realized_pnl),
            remaining_cost_basis=Money(self.remaining_cost_basis),
        )


@dataclass(frozen=True, slots=True)
class OptionLedgerSnapshot:
    state: OptionLedgerState
    long: OptionLegSnapshot
    short: OptionLegSnapshot
    matched_quantity: Decimal
    option_realized_pnl: Money
    option_open_mark_pnl: Money
    option_open_liquidation_pnl: Money
    option_fees: Money
    option_entry_fees: Money
    actual_net_credit: Money
    mark_open_value: Money
    liquidation_open_value: Money


class OptionLedger:
    """One option-spread ledger with FIFO price P&L and separate fee costs."""

    def __init__(self, *, contract_multiplier: Decimal = Decimal("1")) -> None:
        if not isinstance(contract_multiplier, Decimal) or contract_multiplier <= ZERO:
            raise AccountingContractError("option contract multiplier must be positive Decimal")
        self.long = OptionLegLedger(OptionLeg.LONG, contract_multiplier)
        self.short = OptionLegLedger(OptionLeg.SHORT, contract_multiplier)
        self.quotes: dict[str, OptionQuoteRecorded] = {}
        self._executions: dict[str, tuple[object, ...]] = {}
        self._ever_matched = False

    def apply_execution(self, event: OptionExecutionRecorded) -> None:
        content = (event.execution, event.leg)
        execution_id = event.execution.execution_id
        previous = self._executions.get(execution_id)
        if previous is not None:
            if previous != content:
                raise DuplicateAccountingIdentifierError(
                    f"conflicting option execution ID: {execution_id}"
                )
            return
        other = self.short if event.leg is OptionLeg.LONG else self.long
        if other.symbol == event.execution.symbol:
            raise AccountingContractError("long and short option legs must use distinct symbols")
        (self.long if event.leg is OptionLeg.LONG else self.short).apply(event)
        self._executions[execution_id] = content
        if self.long.open_quantity and self.short.open_quantity:
            self._ever_matched = True

    def apply_quote(self, event: OptionQuoteRecorded) -> None:
        if event.symbol is None:
            raise AccountingContractError("option quote requires symbol")
        self.quotes[event.symbol] = event

    @property
    def state(self) -> OptionLedgerState:
        long_open = self.long.open_quantity
        short_open = self.short.open_quantity
        if short_open > long_open:
            return OptionLedgerState.ERROR
        if long_open == ZERO and short_open == ZERO:
            return OptionLedgerState.CLOSED if self._ever_matched else OptionLedgerState.EMPTY
        if short_open == ZERO:
            return OptionLedgerState.LONG_ONLY if not self._ever_matched else OptionLedgerState.PARTIALLY_CLOSED
        if long_open > short_open:
            return (
                OptionLedgerState.PARTIAL_SPREAD
                if self.long.exit_quantity == ZERO and self.short.exit_quantity == ZERO
                else OptionLedgerState.PARTIALLY_CLOSED
            )
        return OptionLedgerState.OPEN_MATCHED if self.long.exit_quantity == ZERO else OptionLedgerState.PARTIALLY_CLOSED

    def snapshot(self, *, as_of: datetime | None = None) -> OptionLedgerSnapshot:
        mark_pnl = ZERO
        liquidation_pnl = ZERO
        mark_value = ZERO
        liquidation_value = ZERO
        for leg in (self.long, self.short):
            if leg.open_quantity == ZERO:
                continue
            quote = self._current_quote(leg, as_of)
            liquidation_price = quote.bid.value if leg.side is OptionLeg.LONG else quote.ask.value
            mark_pnl += leg.open_pnl(quote.mark.value)
            liquidation_pnl += leg.open_pnl(liquidation_price)
            mark_value += leg.signed_open_value(quote.mark.value)
            liquidation_value += leg.signed_open_value(liquidation_price)
        long = self.long.snapshot()
        short = self.short.snapshot()
        entry_fees = long.entry_fees.value + short.entry_fees.value
        return OptionLedgerSnapshot(
            state=self.state,
            long=long,
            short=short,
            matched_quantity=min(self.long.open_quantity, self.short.open_quantity),
            option_realized_pnl=Money(self.long.realized_pnl + self.short.realized_pnl),
            option_open_mark_pnl=Money(mark_pnl),
            option_open_liquidation_pnl=Money(liquidation_pnl),
            option_fees=Money(
                long.entry_fees.value + long.exit_fees.value + short.entry_fees.value + short.exit_fees.value
            ),
            option_entry_fees=Money(entry_fees),
            actual_net_credit=Money(short.entry_cash_flow.value + long.entry_cash_flow.value - entry_fees),
            mark_open_value=Money(mark_value),
            liquidation_open_value=Money(liquidation_value),
        )

    def _current_quote(
        self,
        leg: OptionLegLedger,
        as_of: datetime | None,
    ) -> OptionQuoteRecorded:
        if leg.symbol is None or leg.symbol not in self.quotes:
            raise AccountingContractError(f"missing option quote for {leg.symbol}")
        quote = self.quotes[leg.symbol]
        if as_of is not None and not (quote.timestamp <= as_of <= quote.valid_until):
            raise AccountingContractError(f"stale or future option quote for {leg.symbol}")
        return quote
