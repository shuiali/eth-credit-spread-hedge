"""Contract and reconciliation failures that must block accounting mutation."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from eth_credit_hedge.domain.accounting.errors import AccountingContractError
from eth_credit_hedge.domain.accounting.events import (
    AccountingSnapshotCreated,
    EventSource,
    FeeOwner,
    FeeRecorded,
    FundingAllocation,
    FundingRecorded,
    HedgeExecutionRecorded,
    MigratedFromLegacySnapshot,
    MigrationKind,
    OptionExecutionRecorded,
    OptionQuoteRecorded,
    PositionReconciled,
    RecoveryDebtChanged,
    ReferencePriceRecorded,
    ReferenceType,
    event_from_dict,
    event_to_dict,
)
from eth_credit_hedge.domain.accounting.fills import InstrumentKind
from eth_credit_hedge.domain.accounting.funding import allocate_funding
from eth_credit_hedge.domain.accounting.reconciliation import (
    AccountingDifferenceKind,
    AccountingExchangeState,
    evaluate_accounting_reconciliation,
)
from eth_credit_hedge.domain.accounting.reconstruction import CombinedLedgerReconstructor
from eth_credit_hedge.domain.strategy_math.units import Money, Price, Quantity


D = Decimal
NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)
FIXTURE = (
    Path(__file__).parents[2]
    / "fixtures"
    / "accounting"
    / "m2_6_full_lifecycle.jsonl"
)


def _events() -> tuple[object, ...]:
    return tuple(
        event_from_dict(json.loads(line))
        for line in FIXTURE.read_text(encoding="utf-8").splitlines()
        if line
    )


def _metadata() -> dict[str, object]:
    return {
        "event_id": "event",
        "event_version": 1,
        "cycle_id": "cycle",
        "timestamp": NOW,
        "source": EventSource.SYSTEM,
        "correlation_id": "correlation",
    }


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"event_id": ""}, "event ID"),
        ({"cycle_id": ""}, "cycle ID"),
        ({"correlation_id": ""}, "correlation ID"),
        ({"event_version": 0}, "event version"),
        ({"level_id": 0}, "level ID"),
        ({"execution_id": ""}, "execution ID"),
        ({"order_id": ""}, "order ID"),
        ({"order_link_id": ""}, "order link ID"),
        ({"symbol": ""}, "symbol"),
        ({"source": "SYSTEM"}, "source"),
        ({"timestamp": NOW.replace(tzinfo=None)}, "timezone-aware"),
    ],
)
def test_event_metadata_rejects_ambiguous_identity(
    changes: dict[str, object], message: str
) -> None:
    quote = _events()[0]
    assert isinstance(quote, OptionQuoteRecorded)
    with pytest.raises((AccountingContractError, ValueError), match=message):
        replace(quote, **changes)  # type: ignore[arg-type]


def test_execution_event_contracts_reject_wrong_owner_and_exit_semantics() -> None:
    option = _events()[2]
    hedge_entry = _events()[6]
    hedge_exit = _events()[9]
    assert isinstance(option, OptionExecutionRecorded)
    assert isinstance(hedge_entry, HedgeExecutionRecorded)
    assert isinstance(hedge_exit, HedgeExecutionRecorded)

    invalid_options = (
        {"execution": replace(option.execution, instrument_kind=InstrumentKind.PERPETUAL)},
        {"execution_id": "different"},
        {"symbol": "different"},
        {"leg": "LONG"},
    )
    for changes in invalid_options:
        with pytest.raises(AccountingContractError):
            replace(option, **changes)  # type: ignore[arg-type]

    invalid_hedges = (
        {"execution": replace(hedge_entry.execution, instrument_kind=InstrumentKind.OPTION)},
        {"level_id": None},
        {"execution_id": "different"},
        {"symbol": "different"},
        {"lot_id": ""},
        {"attempt": 0},
        {"role": "BASELINE"},
        {"exit_reason": "STOP"},
        {"exit_reason": hedge_exit.exit_reason},
        {"reference_type": "TRIGGER", "reference_price": Price(D("1"))},
        {"reference_price": "1", "reference_type": ReferenceType.TRIGGER},
        {"reference_type": ReferenceType.TRIGGER},
    )
    for changes in invalid_hedges:
        with pytest.raises(AccountingContractError):
            replace(hedge_entry, **changes)  # type: ignore[arg-type]
    with pytest.raises(AccountingContractError, match="exit requires"):
        replace(hedge_exit, exit_reason=None)


def test_fee_funding_quote_and_reconciliation_contracts_fail_closed() -> None:
    fee = FeeRecorded(
        **_metadata(),  # type: ignore[arg-type]
        fee_id="fee",
        owner=FeeOwner.OPTION,
        amount=Money(D("1")),
        currency="USDC",
    )
    for changes in (
        {"fee_id": ""},
        {"owner": "OPTION"},
        {"amount": "1"},
        {"amount": Money(D("-1"))},
        {"currency": ""},
    ):
        with pytest.raises(AccountingContractError):
            replace(fee, **changes)  # type: ignore[arg-type]

    funding = FundingRecorded(
        **_metadata(),  # type: ignore[arg-type]
        funding_id="funding",
        position_quantity=Quantity(D("1")),
        rate=D("0.01"),
        amount=Money(D("1")),
        allocations=(FundingAllocation("lot", Money(D("1"))),),
    )
    for changes in (
        {"funding_id": ""},
        {"position_quantity": "1"},
        {"rate": D("NaN")},
        {"amount": "1"},
        {"allocations": (funding.allocations[0], funding.allocations[0])},
        {"allocations": (FundingAllocation("lot", Money(D("2"))),)},
    ):
        with pytest.raises(AccountingContractError):
            replace(funding, **changes)  # type: ignore[arg-type]
    with pytest.raises(AccountingContractError):
        FundingAllocation("", Money(D("1")))
    with pytest.raises(AccountingContractError):
        FundingAllocation("lot", "1")  # type: ignore[arg-type]
    with pytest.raises(AccountingContractError, match="must be Money"):
        allocate_funding("1", {"lot": D("1")})  # type: ignore[arg-type]
    with pytest.raises(AccountingContractError, match="at least one"):
        allocate_funding(Money(D("1")), {})
    with pytest.raises(AccountingContractError, match="positive Decimals"):
        allocate_funding(Money(D("1")), {"": D("1")})

    quote = _events()[0]
    assert isinstance(quote, OptionQuoteRecorded)
    for changes in (
        {"bid": "1"},
        {"bid": Price(D("20")), "ask": Price(D("19"))},
        {"valid_until": quote.timestamp - timedelta(seconds=1)},
    ):
        with pytest.raises(AccountingContractError):
            replace(quote, **changes)  # type: ignore[arg-type]

    reconciled = PositionReconciled(
        **_metadata(),  # type: ignore[arg-type]
        internal_quantity=Quantity(D("1")),
        external_quantity=Quantity(D("1")),
        matched=True,
        detail="matched",
    )
    for changes in (
        {"internal_quantity": "1"},
        {"external_quantity": "1"},
        {"matched": 1},
        {"detail": ""},
    ):
        with pytest.raises(AccountingContractError):
            replace(reconciled, **changes)  # type: ignore[arg-type]


def test_reference_snapshot_debt_and_migration_contracts_are_explicit() -> None:
    reference = ReferencePriceRecorded(
        **_metadata(),  # type: ignore[arg-type]
        reference_type=ReferenceType.MARK,
        price=Price(D("100")),
    )
    with pytest.raises(AccountingContractError):
        replace(reference, reference_type="MARK")  # type: ignore[arg-type]
    with pytest.raises(AccountingContractError):
        replace(reference, price="100")  # type: ignore[arg-type]

    snapshot = AccountingSnapshotCreated(
        **_metadata(),  # type: ignore[arg-type]
        sequence=0,
        ledger_digest="digest",
    )
    with pytest.raises(AccountingContractError):
        replace(snapshot, sequence=-1)
    with pytest.raises(AccountingContractError):
        replace(snapshot, ledger_digest="")

    debt = RecoveryDebtChanged(
        **_metadata(),  # type: ignore[arg-type]
        increment=Money(D("1")),
        reason="stop",
    )
    for changes in (
        {"increment": Money(D("-1"))},
        {"actual_recovery_allocation": Money(D("-1"))},
        {"actual_recovery_allocation": Money(D("1"))},
        {"reason": ""},
    ):
        with pytest.raises(AccountingContractError):
            replace(debt, **changes)  # type: ignore[arg-type]

    migration = MigratedFromLegacySnapshot(
        **_metadata(),  # type: ignore[arg-type]
        legacy_snapshot_type="protection",
        legacy_snapshot_key="key",
        legacy_payload_digest="digest",
    )
    for changes in (
        {"legacy_snapshot_type": ""},
        {"legacy_snapshot_key": ""},
        {"legacy_payload_digest": ""},
        {"migration_kind": "MIGRATED_FROM_LEGACY_SNAPSHOT"},
    ):
        with pytest.raises(AccountingContractError):
            replace(migration, **changes)  # type: ignore[arg-type]
    assert migration.migration_kind is MigrationKind.MIGRATED_FROM_LEGACY_SNAPSHOT


def test_event_parser_rejects_noncanonical_shapes_and_binary_numbers() -> None:
    option = _events()[2]
    assert isinstance(option, OptionExecutionRecorded)
    valid = event_to_dict(option)
    mutations = []
    for field, value in (
        ("event_type", "Unknown"),
        ("event_id", 1),
        ("event_version", "1"),
        ("level_id", "1"),
        ("symbol", 1),
        ("timestamp", "not-a-time"),
    ):
        payload = dict(valid)
        payload[field] = value
        mutations.append(payload)
    payload = dict(valid)
    payload["execution"] = "not-an-object"
    mutations.append(payload)
    payload = json.loads(json.dumps(valid))
    payload["execution"]["price"] = 10
    mutations.append(payload)
    payload = json.loads(json.dumps(valid))
    payload["execution"]["price"] = "NaN"
    mutations.append(payload)
    payload = json.loads(json.dumps(valid))
    payload["execution"]["timestamp"] = "bad"
    mutations.append(payload)
    for payload in mutations:
        with pytest.raises((AccountingContractError, ValueError)):
            event_from_dict(payload)

    position_payload = event_to_dict(
        PositionReconciled(
            **_metadata(),  # type: ignore[arg-type]
            internal_quantity=Quantity(D("1")),
            external_quantity=Quantity(D("1")),
            matched=True,
            detail="matched",
        )
    )
    position_payload["matched"] = "true"
    with pytest.raises(AccountingContractError, match="match flag"):
        event_from_dict(position_payload)


def test_reconciliation_reports_each_unavailable_and_conflicting_external_fact() -> None:
    state = CombinedLedgerReconstructor().reconstruct(())
    unavailable = AccountingExchangeState(
        option_quantities=None,
        hedge_short_quantity=None,
        total_fees=None,
        funding_pnl=None,
        order_ids=None,
        execution_ids=None,
    )
    report = evaluate_accounting_reconciliation(
        state,
        unavailable,
        replay_digest_matches=False,
        legacy_migration_pending=True,
    )
    kinds = {difference.kind for difference in report.differences}
    assert AccountingDifferenceKind.REPLAY_DIGEST_MISMATCH in kinds
    assert AccountingDifferenceKind.LEGACY_MIGRATION_REQUIRES_RECONCILIATION in kinds
    assert AccountingDifferenceKind.UNKNOWN_EXTERNAL_STATE in kinds

    conflicts = AccountingExchangeState(
        option_quantities={"unknown": Quantity(D("1"))},
        hedge_short_quantity=D("1"),
        total_fees=Money(D("1")),
        funding_pnl=Money(D("1")),
        order_ids=frozenset({"unknown"}),
        execution_ids=frozenset({"unknown"}),
    )
    report = evaluate_accounting_reconciliation(
        state,
        conflicts,
        replay_digest_matches=True,
        legacy_migration_pending=False,
    )
    assert {difference.kind for difference in report.differences} == {
        AccountingDifferenceKind.OPTION_POSITION_MISMATCH,
        AccountingDifferenceKind.HEDGE_POSITION_MISMATCH,
        AccountingDifferenceKind.FEE_MISMATCH,
        AccountingDifferenceKind.FUNDING_MISMATCH,
        AccountingDifferenceKind.ORDER_MISMATCH,
        AccountingDifferenceKind.EXECUTION_MISMATCH,
    }
    unknown = evaluate_accounting_reconciliation(
        state,
        None,
        replay_digest_matches=True,
        legacy_migration_pending=False,
    )
    assert not unknown.trading_allowed


def test_reconciliation_value_objects_reject_invalid_shapes() -> None:
    with pytest.raises(ValueError):
        AccountingExchangeState({}, D("-1"), Money(D("0")), Money(D("0")))
    with pytest.raises(ValueError):
        AccountingExchangeState({"x": "1"}, D("0"), Money(D("0")), Money(D("0")))  # type: ignore[dict-item]
    with pytest.raises(ValueError):
        AccountingExchangeState({}, D("0"), "0", Money(D("0")))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        AccountingExchangeState({}, D("0"), Money(D("0")), Money(D("0")), {"x"})  # type: ignore[arg-type]
