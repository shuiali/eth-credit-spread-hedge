"""Fill-derived perpetual-short lots, funding, debt, and slippage attribution."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from eth_credit_hedge.domain.accounting.errors import (
    AccountingContractError,
    DuplicateAccountingIdentifierError,
)
from eth_credit_hedge.domain.accounting.events import (
    ExitReason,
    FundingAllocation,
    FundingRecorded,
    HedgeExecutionRecorded,
    HedgeRole,
)
from eth_credit_hedge.domain.accounting.fills import Side
from eth_credit_hedge.domain.accounting.funding import allocate_funding
from eth_credit_hedge.domain.accounting.slippage import adverse_slippage
from eth_credit_hedge.domain.strategy_math.units import Money, Price, Quantity


ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class HedgeEntryLot:
    execution_id: str
    quantity: Decimal
    remaining_quantity: Decimal
    price: Decimal
    fee: Money


@dataclass(frozen=True, slots=True)
class HedgeLotSnapshot:
    lot_id: str
    cycle_id: str
    level_id: int
    attempt: int
    role: HedgeRole
    symbol: str
    entry_quantity: Decimal
    exit_quantity: Decimal
    open_quantity: Decimal
    average_entry_price: Decimal
    average_exit_price: Decimal
    entry_fees: Money
    exit_fees: Money
    allocated_funding: Money
    gross_realized_pnl: Money
    net_realized_pnl: Money
    slippage_attribution: Money
    debt_increment: Money


@dataclass(frozen=True, slots=True)
class HedgeLedgerSnapshot:
    lots: tuple[HedgeLotSnapshot, ...]
    open_quantity: Decimal
    hedge_realized_pnl: Money
    hedge_open_mark_pnl: Money
    hedge_open_liquidation_pnl: Money
    hedge_fees: Money
    funding_pnl: Money
    slippage_attribution: Money
    confirmed_recovery_debt: Money
    mark_open_value: Money
    liquidation_open_value: Money


@dataclass(slots=True)
class HedgeLotLedger:
    lot_id: str
    cycle_id: str
    level_id: int
    attempt: int
    role: HedgeRole
    symbol: str
    entries: list[HedgeEntryLot] = field(default_factory=list)
    entry_quantity: Decimal = ZERO
    exit_quantity: Decimal = ZERO
    entry_notional: Decimal = ZERO
    exit_notional: Decimal = ZERO
    entry_fees: Decimal = ZERO
    exit_fees: Decimal = ZERO
    funding_pnl: Decimal = ZERO
    gross_realized_pnl: Decimal = ZERO
    slippage_attribution: Decimal = ZERO
    stop_occurred: bool = False
    debt_increment: Decimal = ZERO
    finalized: bool = False

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
    def net_realized_pnl(self) -> Decimal:
        if self.exit_quantity == ZERO:
            return ZERO
        allocated_entry_fees = self.entry_fees * self.exit_quantity / self.entry_quantity
        return self.gross_realized_pnl - allocated_entry_fees - self.exit_fees + self.funding_pnl

    def apply(self, event: HedgeExecutionRecorded) -> None:
        execution = event.execution
        if (
            event.cycle_id != self.cycle_id
            or event.level_id != self.level_id
            or event.attempt != self.attempt
            or event.role is not self.role
            or execution.symbol != self.symbol
        ):
            raise AccountingContractError("hedge execution metadata does not match its lot")
        if execution.side is Side.SELL:
            self.entries.append(
                HedgeEntryLot(
                    execution_id=execution.execution_id,
                    quantity=execution.quantity.value,
                    remaining_quantity=execution.quantity.value,
                    price=execution.price.value,
                    fee=execution.fee,
                )
            )
            self.entry_quantity += execution.quantity.value
            self.entry_notional += execution.price.value * execution.quantity.value
            self.entry_fees += execution.fee.value
        else:
            self._close(execution.quantity.value, execution.price.value)
            self.exit_quantity += execution.quantity.value
            self.exit_notional += execution.price.value * execution.quantity.value
            self.exit_fees += execution.fee.value
            self.stop_occurred = self.stop_occurred or event.exit_reason is ExitReason.STOP
        if event.reference_price is not None:
            self.slippage_attribution += adverse_slippage(
                side=execution.side,
                actual_price=execution.price,
                reference_price=event.reference_price,
                quantity=execution.quantity,
            ).value

    def _close(self, quantity: Decimal, price: Decimal) -> None:
        if quantity > self.open_quantity:
            raise AccountingContractError("hedge exit exceeds lot open quantity")
        remaining = quantity
        updated: list[HedgeEntryLot] = []
        for entry in self.entries:
            allocated = min(entry.remaining_quantity, remaining)
            if allocated:
                self.gross_realized_pnl += (entry.price - price) * allocated
                remaining -= allocated
            updated.append(
                HedgeEntryLot(
                    execution_id=entry.execution_id,
                    quantity=entry.quantity,
                    remaining_quantity=entry.remaining_quantity - allocated,
                    price=entry.price,
                    fee=entry.fee,
                )
            )
        if remaining:
            raise AssertionError("FIFO hedge close was not fully allocated")
        self.entries = updated

    def open_pnl(self, price: Price) -> Money:
        return Money(sum(((entry.price - price.value) * entry.remaining_quantity for entry in self.entries), ZERO))

    def open_liability(self, price: Price) -> Money:
        return Money(-price.value * self.open_quantity)

    def snapshot(self) -> HedgeLotSnapshot:
        return HedgeLotSnapshot(
            lot_id=self.lot_id,
            cycle_id=self.cycle_id,
            level_id=self.level_id,
            attempt=self.attempt,
            role=self.role,
            symbol=self.symbol,
            entry_quantity=self.entry_quantity,
            exit_quantity=self.exit_quantity,
            open_quantity=self.open_quantity,
            average_entry_price=self.average_entry_price,
            average_exit_price=self.average_exit_price,
            entry_fees=Money(self.entry_fees),
            exit_fees=Money(self.exit_fees),
            allocated_funding=Money(self.funding_pnl),
            gross_realized_pnl=Money(self.gross_realized_pnl),
            net_realized_pnl=Money(self.net_realized_pnl),
            slippage_attribution=Money(self.slippage_attribution),
            debt_increment=Money(self.debt_increment),
        )


class HedgeLedger:
    """The M2 accounting authority for one-way perpetual-short hedge lots."""

    def __init__(self) -> None:
        self.lots: dict[str, HedgeLotLedger] = {}
        self._executions: dict[str, tuple[object, ...]] = {}
        self._funding: dict[str, tuple[tuple[object, ...], tuple[FundingAllocation, ...]]] = {}
        self.confirmed_recovery_debt = ZERO

    @classmethod
    def replay(
        cls,
        events: Sequence[HedgeExecutionRecorded | FundingRecorded],
    ) -> HedgeLedger:
        ledger = cls()
        for event in sorted(events, key=lambda item: (item.timestamp, item.event_id)):
            if isinstance(event, HedgeExecutionRecorded):
                ledger.apply_execution(event)
            else:
                ledger.apply_funding(event)
        return ledger

    def apply_execution(self, event: HedgeExecutionRecorded) -> None:
        execution_id = event.execution.execution_id
        content = (
            event.execution,
            event.lot_id,
            event.attempt,
            event.role,
            event.exit_reason,
            event.reference_type,
            event.reference_price,
        )
        previous = self._executions.get(execution_id)
        if previous is not None:
            if previous != content:
                raise DuplicateAccountingIdentifierError(f"conflicting execution ID: {execution_id}")
            return
        lot = self.lots.get(event.lot_id)
        if lot is None:
            if event.execution.side is not Side.SELL:
                raise AccountingContractError("hedge lot must be opened before it can close")
            if event.level_id is None:
                raise AccountingContractError("hedge lot requires a level ID")
            lot = HedgeLotLedger(
                lot_id=event.lot_id,
                cycle_id=event.cycle_id,
                level_id=event.level_id,
                attempt=event.attempt,
                role=event.role,
                symbol=event.execution.symbol,
            )
            self.lots[event.lot_id] = lot
        was_open = lot.open_quantity
        lot.apply(event)
        self._executions[execution_id] = content
        if was_open > ZERO and lot.open_quantity == ZERO:
            self._finalize(lot)

    def apply_funding(self, event: FundingRecorded) -> tuple[FundingAllocation, ...]:
        content = (
            event.symbol,
            event.position_quantity,
            event.rate,
            event.amount,
            event.allocations,
            event.timestamp,
        )
        prior = self._funding.get(event.funding_id)
        if prior is not None:
            if prior[0] != content:
                raise DuplicateAccountingIdentifierError(f"conflicting funding ID: {event.funding_id}")
            return prior[1]
        if event.position_quantity.value != self.open_quantity:
            raise AccountingContractError("funding position quantity does not match open hedge lots")
        allocations = event.allocations or allocate_funding(
            event.amount,
            {lot_id: lot.open_quantity for lot_id, lot in self.lots.items() if lot.open_quantity > ZERO},
        )
        for allocation in allocations:
            lot = self.lots.get(allocation.lot_id)
            if lot is None or lot.open_quantity <= ZERO:
                raise AccountingContractError("funding allocation requires an open hedge lot")
            lot.funding_pnl += allocation.amount.value
        self._funding[event.funding_id] = (content, allocations)
        return allocations

    def reconcile_exchange_short(self, exchange_short_quantity: Quantity) -> None:
        if not isinstance(exchange_short_quantity, Quantity):
            raise AccountingContractError("exchange short quantity must be Quantity")
        if self.open_quantity != exchange_short_quantity.value:
            raise AccountingContractError(
                f"internal hedge quantity {self.open_quantity} != exchange short {exchange_short_quantity.value}"
            )

    @property
    def open_quantity(self) -> Decimal:
        return sum((lot.open_quantity for lot in self.lots.values()), ZERO)

    def snapshot(
        self,
        *,
        mark_price: Price | None = None,
        liquidation_price: Price | None = None,
    ) -> HedgeLedgerSnapshot:
        lots = tuple(lot.snapshot() for _, lot in sorted(self.lots.items()))
        mark = sum((lot.open_pnl(mark_price).value for lot in self.lots.values()), ZERO) if mark_price else ZERO
        liquidation = sum((lot.open_pnl(liquidation_price).value for lot in self.lots.values()), ZERO) if liquidation_price else ZERO
        mark_value = sum((lot.open_liability(mark_price).value for lot in self.lots.values()), ZERO) if mark_price else ZERO
        liquidation_value = sum((lot.open_liability(liquidation_price).value for lot in self.lots.values()), ZERO) if liquidation_price else ZERO
        return HedgeLedgerSnapshot(
            lots=lots,
            open_quantity=self.open_quantity,
            hedge_realized_pnl=Money(sum((lot.gross_realized_pnl for lot in self.lots.values()), ZERO)),
            hedge_open_mark_pnl=Money(mark),
            hedge_open_liquidation_pnl=Money(liquidation),
            hedge_fees=Money(sum((lot.entry_fees + lot.exit_fees for lot in self.lots.values()), ZERO)),
            funding_pnl=Money(sum((lot.funding_pnl for lot in self.lots.values()), ZERO)),
            slippage_attribution=Money(sum((lot.slippage_attribution for lot in self.lots.values()), ZERO)),
            confirmed_recovery_debt=Money(self.confirmed_recovery_debt),
            mark_open_value=Money(mark_value),
            liquidation_open_value=Money(liquidation_value),
        )

    def _finalize(self, lot: HedgeLotLedger) -> None:
        if lot.finalized:
            return
        if lot.stop_occurred:
            lot.debt_increment = max(-lot.net_realized_pnl, ZERO)
            self.confirmed_recovery_debt += lot.debt_increment
        lot.finalized = True
