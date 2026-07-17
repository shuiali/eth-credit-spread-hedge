"""One comparison authority for reconstructed accounting and external facts."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Mapping

from eth_credit_hedge.domain.accounting.events import (
    AccountingEvent,
    HedgeExecutionRecorded,
    OptionExecutionRecorded,
)
from eth_credit_hedge.domain.accounting.reconstruction import CombinedLedgerState
from eth_credit_hedge.domain.strategy_math.units import Money, Quantity


ZERO = Decimal("0")


class AccountingDifferenceKind(str, Enum):
    UNKNOWN_EXTERNAL_STATE = "UNKNOWN_EXTERNAL_STATE"
    OPTION_POSITION_MISMATCH = "OPTION_POSITION_MISMATCH"
    HEDGE_POSITION_MISMATCH = "HEDGE_POSITION_MISMATCH"
    FEE_MISMATCH = "FEE_MISMATCH"
    FUNDING_MISMATCH = "FUNDING_MISMATCH"
    ORDER_MISMATCH = "ORDER_MISMATCH"
    EXECUTION_MISMATCH = "EXECUTION_MISMATCH"
    REPLAY_DIGEST_MISMATCH = "REPLAY_DIGEST_MISMATCH"
    LEGACY_MIGRATION_REQUIRES_RECONCILIATION = (
        "LEGACY_MIGRATION_REQUIRES_RECONCILIATION"
    )


@dataclass(frozen=True, slots=True)
class AccountingDifference:
    kind: AccountingDifferenceKind
    detail: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", AccountingDifferenceKind(self.kind))
        if not self.detail.strip():
            raise ValueError("accounting reconciliation detail cannot be empty")


@dataclass(frozen=True, slots=True)
class AccountingExchangeState:
    """Read-only external facts; unavailable facts must be represented as ``None``."""

    option_quantities: Mapping[str, Quantity] | None
    hedge_short_quantity: Decimal | None
    total_fees: Money | None
    funding_pnl: Money | None
    order_ids: frozenset[str] | None = None
    execution_ids: frozenset[str] | None = None

    def __post_init__(self) -> None:
        if self.option_quantities is not None:
            quantities = dict(self.option_quantities)
            if not all(isinstance(value, Quantity) for value in quantities.values()):
                raise ValueError("external option quantities must be Quantity")
            object.__setattr__(self, "option_quantities", quantities)
        if self.hedge_short_quantity is not None:
            try:
                hedge_quantity = Decimal(str(self.hedge_short_quantity))
            except (InvalidOperation, ValueError) as error:
                raise ValueError("external hedge quantity must be exact Decimal") from error
            if not hedge_quantity.is_finite() or hedge_quantity < ZERO:
                raise ValueError("external hedge quantity must be nonnegative")
            object.__setattr__(self, "hedge_short_quantity", hedge_quantity)
        for value, name in (
            (self.total_fees, "external fee total"),
            (self.funding_pnl, "external funding P&L"),
        ):
            if value is not None and not isinstance(value, Money):
                raise ValueError(f"{name} has invalid units")
        for identifiers, name in (
            (self.order_ids, "external order IDs"),
            (self.execution_ids, "external execution IDs"),
        ):
            if identifiers is not None and (
                not isinstance(identifiers, frozenset)
                or not all(
                    isinstance(identifier, str) and identifier
                    for identifier in identifiers
                )
            ):
                raise ValueError(f"{name} must be a set of nonempty text")


@dataclass(frozen=True, slots=True)
class AccountingReconciliationReport:
    differences: tuple[AccountingDifference, ...]
    trading_allowed: bool

    def __post_init__(self) -> None:
        differences = tuple(self.differences)
        if type(self.trading_allowed) is not bool:
            raise ValueError("trading allowed must be boolean")
        if self.trading_allowed != (not differences):
            raise ValueError("trading is allowed only for an exact accounting match")
        object.__setattr__(self, "differences", differences)


def evaluate_accounting_reconciliation(
    state: CombinedLedgerState,
    external: AccountingExchangeState | None,
    *,
    replay_digest_matches: bool,
    legacy_migration_pending: bool,
    events: tuple[AccountingEvent, ...] = (),
) -> AccountingReconciliationReport:
    """Require every externally observable ledger fact to match exactly."""
    differences: list[AccountingDifference] = []
    if not replay_digest_matches:
        differences.append(
            AccountingDifference(
                AccountingDifferenceKind.REPLAY_DIGEST_MISMATCH,
                "full replay and snapshot-plus-tail replay produced different digests",
            )
        )
    if legacy_migration_pending:
        differences.append(
            AccountingDifference(
                AccountingDifferenceKind.LEGACY_MIGRATION_REQUIRES_RECONCILIATION,
                "legacy migration cannot activate trading before external reconciliation",
            )
        )
    if external is None:
        differences.append(
            AccountingDifference(
                AccountingDifferenceKind.UNKNOWN_EXTERNAL_STATE,
                "external accounting state was unavailable",
            )
        )
        return AccountingReconciliationReport(tuple(differences), False)
    executions = tuple(
        event
        for event in events
        if isinstance(event, (OptionExecutionRecorded, HedgeExecutionRecorded))
    )
    expected_execution_ids = frozenset(event.execution.execution_id for event in executions)
    expected_order_ids = frozenset(event.execution.order_id for event in executions)
    if external.execution_ids is None:
        differences.append(
            AccountingDifference(
                AccountingDifferenceKind.UNKNOWN_EXTERNAL_STATE,
                "external confirmed executions were unavailable",
            )
        )
    elif external.execution_ids != expected_execution_ids:
        differences.append(
            AccountingDifference(
                AccountingDifferenceKind.EXECUTION_MISMATCH,
                "external executions do not match persisted confirmed accounting executions",
            )
        )
    if external.order_ids is None:
        differences.append(
            AccountingDifference(
                AccountingDifferenceKind.UNKNOWN_EXTERNAL_STATE,
                "external orders were unavailable",
            )
        )
    elif external.order_ids != expected_order_ids:
        differences.append(
            AccountingDifference(
                AccountingDifferenceKind.ORDER_MISMATCH,
                "external order IDs do not match persisted confirmed accounting orders",
            )
        )
    if external.option_quantities is None:
        differences.append(
            AccountingDifference(
                AccountingDifferenceKind.UNKNOWN_EXTERNAL_STATE,
                "external option positions were unavailable",
            )
        )
    else:
        expected = {
            snapshot.symbol: Quantity(snapshot.open_quantity)
            for snapshot in (state.option.long, state.option.short)
            if snapshot.symbol is not None and snapshot.open_quantity > ZERO
        }
        if dict(external.option_quantities) != expected:
            differences.append(
                AccountingDifference(
                    AccountingDifferenceKind.OPTION_POSITION_MISMATCH,
                    "external option quantities do not match reconstructed lots",
                )
            )
    if external.hedge_short_quantity is None:
        differences.append(
            AccountingDifference(
                AccountingDifferenceKind.UNKNOWN_EXTERNAL_STATE,
                "external hedge position was unavailable",
            )
        )
    elif external.hedge_short_quantity != state.hedge.open_quantity:
        differences.append(
            AccountingDifference(
                AccountingDifferenceKind.HEDGE_POSITION_MISMATCH,
                "external hedge quantity does not match reconstructed hedge lots",
            )
        )
    expected_fees = Money(state.option_fees.value + state.hedge_fees.value)
    if external.total_fees is None:
        differences.append(
            AccountingDifference(
                AccountingDifferenceKind.UNKNOWN_EXTERNAL_STATE,
                "external fee total was unavailable",
            )
        )
    elif external.total_fees != expected_fees:
        differences.append(
            AccountingDifference(
                AccountingDifferenceKind.FEE_MISMATCH,
                "external fees do not match the reconstructed ledger",
            )
        )
    if external.funding_pnl is None:
        differences.append(
            AccountingDifference(
                AccountingDifferenceKind.UNKNOWN_EXTERNAL_STATE,
                "external funding total was unavailable",
            )
        )
    elif external.funding_pnl != state.funding_pnl:
        differences.append(
            AccountingDifference(
                AccountingDifferenceKind.FUNDING_MISMATCH,
                "external funding does not match the reconstructed ledger",
            )
        )
    return AccountingReconciliationReport(tuple(differences), not differences)
