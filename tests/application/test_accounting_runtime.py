"""M2.6 runtime integration tests use hand-calculated raw-fill expectations."""

from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from eth_credit_hedge.application.accounting_runtime import AccountingRuntime
from eth_credit_hedge.application.accounting_runtime import AccountingValuation
from eth_credit_hedge.application.operational_state import MutableOperationalState
from eth_credit_hedge.domain.accounting.errors import DuplicateAccountingIdentifierError
from eth_credit_hedge.domain.accounting.events import (
    AccountingEvent,
    AccountingSnapshotCreated,
    EventSource,
    FundingRecorded,
    HedgeExecutionRecorded,
    RecoveryDebtChanged,
    OptionQuoteRecorded,
    OptionExecutionRecorded,
    event_from_dict,
)
from eth_credit_hedge.domain.accounting.reconstruction import CombinedLedgerReconstructor
from eth_credit_hedge.domain.strategy_math.units import Money, Price
from eth_credit_hedge.infrastructure.persistence.sqlite_accounting_store import (
    SqliteAccountingStore,
)
from eth_credit_hedge.interfaces.ledger_simulated_lifecycle import run_simulated_lifecycle
from eth_credit_hedge.visualization.accounting_dashboard import (
    LedgerDashboard,
    build_ledger_dashboard_payload,
)
from eth_credit_hedge.visualization import payload as legacy_dashboard_payload


D = Decimal
ROOT = Path(__file__).parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "accounting" / "m2_6_full_lifecycle.jsonl"


def _events() -> tuple[AccountingEvent, ...]:
    return tuple(
        event_from_dict(json.loads(line))
        for line in FIXTURE.read_text(encoding="utf-8").splitlines()
        if line
    )


def test_full_offline_lifecycle_has_manual_cashflow_and_cost_expectations(tmp_path: Path) -> None:
    result = run_simulated_lifecycle(tmp_path)
    ledger = result["combined_ledger"]
    assert isinstance(ledger, dict)
    # Hand calculation: option 15 - 10 = 5; hedge -10.4 + 10.6 = .2;
    # fees are 4 + 4; funding is -.5 + .75 = .25.
    assert ledger["option_realized_pnl"] == "5.0"
    assert ledger["hedge_realized_pnl"] == "0.2"
    assert ledger["option_fees"] == "4.0"
    assert ledger["hedge_fees"] == "4.0"
    assert ledger["funding_pnl"] == "0.25"
    assert ledger["slippage_attribution"] == "2.4"
    assert ledger["confirmed_recovery_debt"] == "3.55"
    assert ledger["net_combined_mark_pnl"] == "-2.55"
    assert ledger["net_combined_liquidation_pnl"] == "-2.55"
    assert D(str(ledger["mark_identity_residual"])) == D("0")
    assert D(str(ledger["liquidation_identity_residual"])) == D("0")
    assert result["reconciliation"] is True
    assert result["conflicting_funding_identifier_rejected"] is True
    assert result["funding_evidence"] == [
        {
            "funding_id": "m2-6-base-funding",
            "event_amount": "-0.5",
            "cumulative_funding_pnl": "-0.5",
            "duplicate_ignored": True,
        },
        {
            "event_id": "m2-6-base-stop-b",
            "confirmed_stop_debt": "12.9",
            "allocated_funding_pnl": "-0.5",
        },
        {
            "funding_id": "m2-6-recovery-funding",
            "event_amount": "0.75",
            "cumulative_funding_pnl": "0.25",
            "duplicate_ignored": True,
        },
    ]
    for name in (
        "accounting_events.jsonl",
        "raw_executions.jsonl",
        "funding_events.jsonl",
        "quotes.jsonl",
        "combined_ledger.json",
        "reconciliation.json",
        "ledger_recompute_report.json",
        "dashboard.json",
        "funding_evidence.json",
    ):
        assert (tmp_path / name).exists()
    recompute = json.loads((tmp_path / "ledger_recompute_report.json").read_text())
    assert recompute["snapshot_digest_matches"] is True
    assert recompute["state"]["ledger_digest"] == ledger["ledger_digest"]


def test_private_batches_are_immediate_idempotent_and_reject_conflicts(tmp_path: Path) -> None:
    async def exercise() -> None:
        store = SqliteAccountingStore(tmp_path / "accounting.sqlite3")
        await store.initialize()
        runtime = AccountingRuntime(store=store, reconstructor=CombinedLedgerReconstructor())
        await runtime.initialize()
        events = _events()
        for quote in events[:2]:
            await runtime.record_system_event(quote)
        fill = events[2]
        assert isinstance(fill, OptionExecutionRecorded)
        first = await runtime.apply_private_execution_batch((fill,))
        assert first.option.long.open_quantity == D("0.4")
        duplicate = await runtime.apply_private_execution_batch((fill,))
        assert duplicate.ledger_digest == first.ledger_digest
        assert len(runtime.events) == 3
        with pytest.raises(DuplicateAccountingIdentifierError, match="conflicting event ID"):
            await runtime.apply_private_execution_batch(
                (replace(fill, execution=replace(fill.execution, fee=Money(D("0.5")))),)
            )

    asyncio.run(exercise())


def test_operator_restart_replays_the_same_events_without_reallocation(
    tmp_path: Path,
) -> None:
    first = run_simulated_lifecycle(tmp_path)
    restarted = run_simulated_lifecycle(tmp_path)
    assert restarted["combined_ledger"] == first["combined_ledger"]
    assert restarted["funding_evidence"] == first["funding_evidence"]
    assert restarted["event_count"] == first["event_count"]


def test_ledger_dashboard_adapts_authoritative_fields_without_new_arithmetic() -> None:
    state = CombinedLedgerReconstructor().reconstruct(_events())
    rendered = LedgerDashboard(build_ledger_dashboard_payload(state)).render()
    assert rendered["net_combined_mark_pnl"] == "-2.55"
    assert rendered["net_combined_liquidation_pnl"] == "-2.55"
    assert rendered["confirmed_recovery_debt"] == "3.55"
    assert rendered["ledger_digest"] == state.ledger_digest


def test_quote_changes_are_calculated_only_by_the_ledger() -> None:
    events = _events()
    opening = events[:6]
    first_state = CombinedLedgerReconstructor().reconstruct(opening)
    first_dashboard = build_ledger_dashboard_payload(first_state)
    changed = tuple(
        replace(
            event,
            mark=Price(D("21")),
            bid=Price(D("19")),
            ask=Price(D("22")),
        )
        if isinstance(event, OptionQuoteRecorded) and "3000-P" in str(event.symbol)
        else replace(event, mark=Price(D("44")), ask=Price(D("45")))
        if isinstance(event, OptionQuoteRecorded)
        else event
        for event in opening
    )
    changed_state = CombinedLedgerReconstructor().reconstruct(changed)
    changed_dashboard = legacy_dashboard_payload.build_ledger_dashboard_payload(
        changed_state
    )

    assert first_dashboard.option_open_mark_pnl == first_state.option_open_mark_pnl.value
    assert changed_dashboard.option_open_mark_pnl == changed_state.option_open_mark_pnl.value
    assert first_dashboard.option_open_mark_pnl == D("3.0")
    assert changed_dashboard.option_open_mark_pnl == D("7.0")
    assert changed_dashboard.option_open_liquidation_pnl == D("4.0")
    source = inspect.getsource(legacy_dashboard_payload)
    assert ".mark_pnl(" not in source
    assert ".liquidation_pnl(" not in source
    assert "realized + open_position" not in source


def test_runtime_caps_actual_recovery_settlement_and_snapshots_shutdown_once(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        store = SqliteAccountingStore(tmp_path / "accounting.sqlite3")
        await store.initialize()
        runtime = AccountingRuntime(
            store=store,
            reconstructor=CombinedLedgerReconstructor(),
        )
        await runtime.initialize()
        assert await runtime.apply_private_execution_batch(()) == runtime.state

        events = _events()
        hedge_events = events[6:16]
        for event in hedge_events:
            if isinstance(event, FundingRecorded):
                await runtime.apply_funding(event)
                continue
            assert isinstance(event, HedgeExecutionRecorded)
            if event.event_id in {"m2-6-recovery-tp-a", "m2-6-recovery-tp-b"}:
                event = replace(
                    event,
                    execution=replace(event.execution, price=Price(D("80"))),
                )
            await runtime.apply_private_execution_batch(
                (event,),
                valuation=AccountingValuation(
                    hedge_mark=Price(D("100")),
                    hedge_liquidation=Price(D("101")),
                ),
            )

        settlements = tuple(
            event
            for event in runtime.events
            if isinstance(event, RecoveryDebtChanged)
        )
        assert len(settlements) == 1
        assert settlements[0].actual_recovery_allocation == Money(D("12.9"))
        assert runtime.state.confirmed_recovery_debt == Money(D("0.0"))

        first = await runtime.record_shutdown_snapshot(
            cycle_id="m2-6-cycle",
            timestamp=events[-1].timestamp,
        )
        second = await runtime.record_shutdown_snapshot(
            cycle_id="m2-6-cycle",
            timestamp=events[-1].timestamp,
        )
        assert second.ledger_digest == first.ledger_digest
        assert len(
            tuple(
                event
                for event in runtime.events
                if isinstance(event, AccountingSnapshotCreated)
            )
        ) == 1

        private_event = hedge_events[0]
        assert isinstance(private_event, HedgeExecutionRecorded)
        with pytest.raises(ValueError, match="PRIVATE_STREAM"):
            await runtime.apply_private_execution_batch(
                (replace(private_event, source=EventSource.SYSTEM),)
            )
        with pytest.raises(ValueError, match="REST_RECOVERY"):
            await runtime.apply_rest_recovery((private_event,))

    asyncio.run(exercise())


def test_health_projection_reads_quantity_debt_and_pnl_from_ledger() -> None:
    state = CombinedLedgerReconstructor().reconstruct(_events())
    operations = MutableOperationalState(
        maximum_market_data_age_ms=1_000,
        clock=lambda: state.as_of,
    )
    operations.update_accounting(state)
    health = operations.snapshot()
    assert health.open_hedge_quantity == state.hedge.open_quantity
    assert health.recovery_debt == state.confirmed_recovery_debt.value
    assert health.daily_pnl == state.net_combined_mark_pnl.value
