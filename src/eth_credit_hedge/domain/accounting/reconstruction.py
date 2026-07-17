"""Deterministic reconstruction of the authoritative combined accounting state."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import TypeAlias

from eth_credit_hedge.domain.accounting.errors import (
    AccountingContractError,
    DuplicateAccountingIdentifierError,
)
from eth_credit_hedge.domain.accounting.events import (
    AccountingSnapshotCreated,
    AccountingEvent,
    FeeOwner,
    FeeRecorded,
    FundingRecorded,
    HedgeExecutionRecorded,
    HedgeRole,
    OptionExecutionRecorded,
    OptionQuoteRecorded,
    PositionReconciled,
    RecoveryDebtChanged,
    RecoveryDebtIncremented,
    RecoveryAllocationRecorded,
    canonical_event_json,
)
from eth_credit_hedge.domain.accounting.recovery_projection import (
    AttemptRecoveryProjection,
    AttemptRecoveryStatus,
    HedgeAttemptKey,
    LevelRecoveryProjection,
)
from eth_credit_hedge.domain.accounting.hedge_ledger import HedgeLedger, HedgeLedgerSnapshot
from eth_credit_hedge.domain.accounting.option_ledger import OptionLedger, OptionLedgerSnapshot
from eth_credit_hedge.domain.strategy_math.units import Money, Price


ZERO = Decimal("0")
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
OptionQuoteSet: TypeAlias = Sequence[OptionQuoteRecorded]


@dataclass(frozen=True, slots=True)
class CombinedLedgerState:
    """One replayed state, with gross P&L, costs, and all identity residuals separate."""

    as_of: datetime
    option: OptionLedgerSnapshot
    hedge: HedgeLedgerSnapshot
    option_realized_pnl: Money
    option_open_mark_pnl: Money
    option_open_liquidation_pnl: Money
    hedge_realized_pnl: Money
    hedge_open_mark_pnl: Money
    hedge_open_liquidation_pnl: Money
    option_fees: Money
    hedge_fees: Money
    funding_pnl: Money
    slippage_attribution: Money
    net_combined_mark_pnl: Money
    net_combined_liquidation_pnl: Money
    initial_cash: Money
    execution_cash_flow: Money
    ending_cash: Money
    mark_open_position_value: Money
    liquidation_open_position_value: Money
    mark_equity_change: Money
    liquidation_equity_change: Money
    mark_identity_residual: Money
    liquidation_identity_residual: Money
    cash_equity_mark_residual: Money
    cash_equity_liquidation_residual: Money
    debt_increments: Money
    actual_recovery_allocations: Money
    confirmed_recovery_debt: Money
    debt_identity_residual: Money
    recovery_level_projections: tuple[LevelRecoveryProjection, ...]
    ledger_digest: str

    def debt_for_attempt(self, key: HedgeAttemptKey) -> Money:
        for level in self.recovery_level_projections:
            for attempt in level.attempt_projections:
                if attempt.key == key:
                    return attempt.remaining_debt
        return Money(ZERO)

    def debt_for_level(self, cycle_id: str, level_id: int) -> Money:
        projection = self.projection_for_level(cycle_id, level_id)
        return Money(ZERO) if projection is None else projection.total_remaining_debt

    def projection_for_level(
        self, cycle_id: str, level_id: int
    ) -> LevelRecoveryProjection | None:
        return next(
            (
                projection
                for projection in self.recovery_level_projections
                if projection.cycle_id == cycle_id and projection.level_id == level_id
            ),
            None,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "as_of": self.as_of.isoformat(),
            "option_long_open_quantity": str(self.option.long.open_quantity),
            "option_short_open_quantity": str(self.option.short.open_quantity),
            "option_matched_open_quantity": str(self.option.matched_quantity),
            "hedge_open_quantity": str(self.hedge.open_quantity),
            "hedge_open_lot_ids": [
                lot.lot_id for lot in self.hedge.lots if lot.open_quantity > ZERO
            ],
            "option_realized_pnl": str(self.option_realized_pnl.value),
            "option_open_mark_pnl": str(self.option_open_mark_pnl.value),
            "option_open_liquidation_pnl": str(self.option_open_liquidation_pnl.value),
            "hedge_realized_pnl": str(self.hedge_realized_pnl.value),
            "hedge_open_mark_pnl": str(self.hedge_open_mark_pnl.value),
            "hedge_open_liquidation_pnl": str(self.hedge_open_liquidation_pnl.value),
            "option_fees": str(self.option_fees.value),
            "hedge_fees": str(self.hedge_fees.value),
            "funding_pnl": str(self.funding_pnl.value),
            "slippage_attribution": str(self.slippage_attribution.value),
            "net_combined_mark_pnl": str(self.net_combined_mark_pnl.value),
            "net_combined_liquidation_pnl": str(self.net_combined_liquidation_pnl.value),
            "initial_cash": str(self.initial_cash.value),
            "execution_cash_flow": str(self.execution_cash_flow.value),
            "ending_cash": str(self.ending_cash.value),
            "mark_open_position_value": str(self.mark_open_position_value.value),
            "liquidation_open_position_value": str(self.liquidation_open_position_value.value),
            "mark_equity_change": str(self.mark_equity_change.value),
            "liquidation_equity_change": str(self.liquidation_equity_change.value),
            "mark_identity_residual": str(self.mark_identity_residual.value),
            "liquidation_identity_residual": str(self.liquidation_identity_residual.value),
            "cash_equity_mark_residual": str(self.cash_equity_mark_residual.value),
            "cash_equity_liquidation_residual": str(self.cash_equity_liquidation_residual.value),
            "debt_increments": str(self.debt_increments.value),
            "actual_recovery_allocations": str(self.actual_recovery_allocations.value),
            "confirmed_recovery_debt": str(self.confirmed_recovery_debt.value),
            "debt_identity_residual": str(self.debt_identity_residual.value),
            "recovery_level_projections": [
                {"cycle_id": level.cycle_id, "level_id": level.level_id,
                 "remaining_debt": str(level.total_remaining_debt.value)}
                for level in self.recovery_level_projections
            ],
            "ledger_digest": self.ledger_digest,
        }


class CombinedLedgerReconstructor:
    """Replay confirmed accounting facts into the one combined authority."""

    def __init__(
        self,
        *,
        option_contract_multiplier: Decimal = Decimal("1"),
        initial_cash: Money = Money(ZERO),
    ) -> None:
        if not isinstance(option_contract_multiplier, Decimal) or option_contract_multiplier <= ZERO:
            raise AccountingContractError("option contract multiplier must be positive Decimal")
        if not isinstance(initial_cash, Money):
            raise AccountingContractError("initial cash must be Money")
        self._option_contract_multiplier = option_contract_multiplier
        self._initial_cash = initial_cash

    def reconstruct(
        self,
        events: Sequence[AccountingEvent],
        option_quotes: OptionQuoteSet | None = None,
        hedge_mark: Price | None = None,
        hedge_liquidation: Price | None = None,
    ) -> CombinedLedgerState:
        if hedge_mark is not None and not isinstance(hedge_mark, Price):
            raise AccountingContractError("hedge mark must be Price")
        if hedge_liquidation is not None and not isinstance(hedge_liquidation, Price):
            raise AccountingContractError("hedge liquidation price must be Price")
        ordered = self._unique_sorted(events)
        option_ledger = OptionLedger(contract_multiplier=self._option_contract_multiplier)
        hedge_ledger = HedgeLedger()
        extra_option_fees = ZERO
        extra_hedge_fees = ZERO
        execution_cash_flow = ZERO
        debt_increments = ZERO
        recovery_allocations = ZERO
        recovery_attempts: dict[
            HedgeAttemptKey, tuple[Decimal, Decimal, list[str]]
        ] = {}
        recorded_recovery_profit = ZERO
        timestamps = [event.timestamp for event in ordered]

        for event in ordered:
            if isinstance(event, OptionExecutionRecorded):
                option_ledger.apply_execution(event)
                execution_cash_flow += self._cash_flow(event) * self._option_contract_multiplier
            elif isinstance(event, HedgeExecutionRecorded):
                hedge_ledger.apply_execution(event)
                execution_cash_flow += self._cash_flow(event)
            elif isinstance(event, FundingRecorded):
                hedge_ledger.apply_funding(event)
            elif isinstance(event, FeeRecorded):
                if event.owner is FeeOwner.OPTION:
                    extra_option_fees += event.amount.value
                else:
                    extra_hedge_fees += event.amount.value
            elif isinstance(event, OptionQuoteRecorded):
                option_ledger.apply_quote(event)
            elif isinstance(event, RecoveryDebtChanged):
                debt_increments += event.increment.value
                recovery_allocations += event.actual_recovery_allocation.value
            elif isinstance(event, RecoveryDebtIncremented):
                increment, allocation, event_ids = recovery_attempts.get(
                    event.target, (ZERO, ZERO, [])
                )
                recovery_attempts[event.target] = (
                    increment + event.amount.value,
                    allocation,
                    [*event_ids, event.event_id],
                )
                debt_increments += event.amount.value
            elif isinstance(event, RecoveryAllocationRecorded):
                increment, allocation, event_ids = recovery_attempts.get(
                    event.target, (ZERO, ZERO, [])
                )
                recovery_attempts[event.target] = (
                    increment,
                    allocation + event.allocated_amount.value,
                    [*event_ids, event.event_id],
                )
                recovery_allocations += event.allocated_amount.value
                recorded_recovery_profit += (
                    event.gross_realized_recovery_profit.value
                    - event.fees.value
                    + event.funding.value
                )

        quotes = tuple(option_quotes or ())
        if not all(isinstance(quote, OptionQuoteRecorded) for quote in quotes):
            raise AccountingContractError("option quotes must be OptionQuoteRecorded events")
        for quote in quotes:
            option_ledger.apply_quote(quote)
            timestamps.append(quote.timestamp)
        as_of = max(timestamps, default=EPOCH)
        hedge_liquidation = hedge_liquidation or hedge_mark
        option = option_ledger.snapshot(as_of=as_of)
        hedge = hedge_ledger.snapshot(
            mark_price=hedge_mark,
            liquidation_price=hedge_liquidation,
        )

        option_fees = option.option_fees.value + extra_option_fees
        hedge_fees = hedge.hedge_fees.value + extra_hedge_fees
        funding = hedge.funding_pnl.value
        mark_net = (
            option.option_realized_pnl.value
            + option.option_open_mark_pnl.value
            + hedge.hedge_realized_pnl.value
            + hedge.hedge_open_mark_pnl.value
            + funding
            - option_fees
            - hedge_fees
        )
        liquidation_net = (
            option.option_realized_pnl.value
            + option.option_open_liquidation_pnl.value
            + hedge.hedge_realized_pnl.value
            + hedge.hedge_open_liquidation_pnl.value
            + funding
            - option_fees
            - hedge_fees
        )
        ending_cash = (
            self._initial_cash.value
            + execution_cash_flow
            - option_fees
            - hedge_fees
            + funding
        )
        mark_open_value = option.mark_open_value.value + hedge.mark_open_value.value
        liquidation_open_value = (
            option.liquidation_open_value.value + hedge.liquidation_open_value.value
        )
        mark_equity_change = ending_cash + mark_open_value - self._initial_cash.value
        liquidation_equity_change = (
            ending_cash + liquidation_open_value - self._initial_cash.value
        )
        total_debt_increments = hedge.confirmed_recovery_debt.value + debt_increments
        confirmed_debt = total_debt_increments - recovery_allocations
        actual_recovery_profit = sum(
            (
                max(lot.net_realized_pnl.value, ZERO)
                for lot in hedge.lots
                if lot.role is HedgeRole.RECOVERY
            ),
            ZERO,
        )
        actual_recovery_profit = max(actual_recovery_profit, recorded_recovery_profit)
        if recovery_allocations > actual_recovery_profit:
            raise AccountingContractError(
                "actual recovery allocations exceed realized recovery profit"
            )
        if confirmed_debt < ZERO:
            raise AccountingContractError("actual recovery allocations exceed confirmed debt")
        attempts = tuple(
            AttemptRecoveryProjection(
                key=key,
                debt_increments=Money(values[0]),
                recovery_allocations=Money(values[1]),
                remaining_debt=Money(values[0] - values[1]),
                status=(AttemptRecoveryStatus.SETTLED if values[0] == values[1]
                        else AttemptRecoveryStatus.OUTSTANDING),
                source_event_ids=tuple(values[2]),
            )
            for key, values in sorted(recovery_attempts.items(), key=lambda item: (item[0].cycle_id, item[0].level_id, item[0].attempt))
        )
        if any(item.remaining_debt.value < ZERO for item in attempts):
            raise AccountingContractError("recovery allocation exceeds target attempt debt")
        levels: dict[tuple[str, int], list[AttemptRecoveryProjection]] = {}
        for attempt in attempts:
            levels.setdefault((attempt.key.cycle_id, attempt.key.level_id), []).append(attempt)
        projections = tuple(
            LevelRecoveryProjection(
                cycle_id=key[0], level_id=key[1], attempt_projections=tuple(items),
                total_remaining_debt=Money(sum((item.remaining_debt.value for item in items), ZERO)),
            ) for key, items in sorted(levels.items())
        )
        if recovery_attempts and sum((level.total_remaining_debt.value for level in projections), ZERO) != confirmed_debt:
            raise AccountingContractError("global recovery debt differs from attempt projections")
        state_values = {
            "as_of": as_of.isoformat(),
            "event_digests": [canonical_event_json(event) for event in ordered],
            "option_quote_ids": [quote.event_id for quote in quotes],
            "hedge_mark": None if hedge_mark is None else str(hedge_mark.value),
            "hedge_liquidation": None
            if hedge_liquidation is None
            else str(hedge_liquidation.value),
            "initial_cash": str(self._initial_cash.value),
        }
        digest = hashlib.sha256(
            json.dumps(state_values, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).hexdigest()
        return CombinedLedgerState(
            as_of=as_of,
            option=option,
            hedge=hedge,
            option_realized_pnl=option.option_realized_pnl,
            option_open_mark_pnl=option.option_open_mark_pnl,
            option_open_liquidation_pnl=option.option_open_liquidation_pnl,
            hedge_realized_pnl=hedge.hedge_realized_pnl,
            hedge_open_mark_pnl=hedge.hedge_open_mark_pnl,
            hedge_open_liquidation_pnl=hedge.hedge_open_liquidation_pnl,
            option_fees=Money(option_fees),
            hedge_fees=Money(hedge_fees),
            funding_pnl=Money(funding),
            slippage_attribution=hedge.slippage_attribution,
            net_combined_mark_pnl=Money(mark_net),
            net_combined_liquidation_pnl=Money(liquidation_net),
            initial_cash=self._initial_cash,
            execution_cash_flow=Money(execution_cash_flow),
            ending_cash=Money(ending_cash),
            mark_open_position_value=Money(mark_open_value),
            liquidation_open_position_value=Money(liquidation_open_value),
            mark_equity_change=Money(mark_equity_change),
            liquidation_equity_change=Money(liquidation_equity_change),
            mark_identity_residual=Money(
                mark_net
                - (
                    option.option_realized_pnl.value
                    + option.option_open_mark_pnl.value
                    + hedge.hedge_realized_pnl.value
                    + hedge.hedge_open_mark_pnl.value
                    + funding
                    - option_fees
                    - hedge_fees
                )
            ),
            liquidation_identity_residual=Money(
                liquidation_net
                - (
                    option.option_realized_pnl.value
                    + option.option_open_liquidation_pnl.value
                    + hedge.hedge_realized_pnl.value
                    + hedge.hedge_open_liquidation_pnl.value
                    + funding
                    - option_fees
                    - hedge_fees
                )
            ),
            cash_equity_mark_residual=Money(mark_net - mark_equity_change),
            cash_equity_liquidation_residual=Money(
                liquidation_net - liquidation_equity_change
            ),
            debt_increments=Money(total_debt_increments),
            actual_recovery_allocations=Money(recovery_allocations),
            confirmed_recovery_debt=Money(confirmed_debt),
            debt_identity_residual=Money(
                confirmed_debt - total_debt_increments + recovery_allocations
            ),
            recovery_level_projections=projections,
            ledger_digest=digest,
        )

    def reconstruct_snapshots(
        self,
        events: Sequence[AccountingEvent],
        option_quotes: OptionQuoteSet | None = None,
        hedge_mark: Price | None = None,
        hedge_liquidation: Price | None = None,
    ) -> tuple[CombinedLedgerState, ...]:
        """Return deterministic post-event snapshots for the specified trigger events."""
        ordered = self._unique_sorted(events)
        trigger_types = (
            OptionExecutionRecorded,
            HedgeExecutionRecorded,
            FundingRecorded,
            OptionQuoteRecorded,
            PositionReconciled,
            RecoveryDebtChanged,
            AccountingSnapshotCreated,
        )
        return tuple(
            self.reconstruct(
                ordered[: index + 1], option_quotes, hedge_mark, hedge_liquidation
            )
            for index, event in enumerate(ordered)
            if isinstance(event, trigger_types)
        )

    @staticmethod
    def _cash_flow(event: OptionExecutionRecorded | HedgeExecutionRecorded) -> Decimal:
        return event.execution.cash_flow.value

    @staticmethod
    def _unique_sorted(events: Sequence[AccountingEvent]) -> tuple[AccountingEvent, ...]:
        by_event_id: dict[str, str] = {}
        by_execution_id: dict[str, tuple[object, ...]] = {}
        by_funding_id: dict[str, tuple[object, ...]] = {}
        by_fee_id: dict[str, tuple[object, ...]] = {}
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
            if isinstance(event, OptionExecutionRecorded):
                identifier = event.execution.execution_id
                content: tuple[object, ...] = (event.execution, event.leg)
                duplicate = CombinedLedgerReconstructor._record_identifier(
                    by_execution_id, identifier, content, "execution"
                )
            elif isinstance(event, HedgeExecutionRecorded):
                identifier = event.execution.execution_id
                content = (
                    event.execution,
                    event.lot_id,
                    event.attempt,
                    event.role,
                    event.exit_reason,
                    event.reference_type,
                    event.reference_price,
                )
                duplicate = CombinedLedgerReconstructor._record_identifier(
                    by_execution_id, identifier, content, "execution"
                )
            elif isinstance(event, FundingRecorded):
                content = (
                    event.symbol,
                    event.position_quantity,
                    event.rate,
                    event.amount,
                    event.allocations,
                    event.timestamp,
                )
                duplicate = CombinedLedgerReconstructor._record_identifier(
                    by_funding_id, event.funding_id, content, "funding"
                )
            elif isinstance(event, FeeRecorded):
                content = (event.owner, event.amount, event.currency, event.timestamp)
                duplicate = CombinedLedgerReconstructor._record_identifier(
                    by_fee_id, event.fee_id, content, "fee"
                )
            else:
                duplicate = False
            by_event_id[event.event_id] = canonical
            if not duplicate:
                unique.append(event)
        return tuple(sorted(unique, key=lambda event: (event.timestamp, event.event_id)))

    @staticmethod
    def _record_identifier(
        seen: dict[str, tuple[object, ...]],
        identifier: str,
        content: tuple[object, ...],
        label: str,
    ) -> bool:
        prior = seen.get(identifier)
        if prior is not None:
            if prior != content:
                raise DuplicateAccountingIdentifierError(
                    f"conflicting {label} ID: {identifier}"
                )
            return True
        seen[identifier] = content
        return False
