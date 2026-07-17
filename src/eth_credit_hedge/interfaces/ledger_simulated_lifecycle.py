"""Run the full Milestone 2 ledger lifecycle offline from confirmed raw fills."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Sequence

from eth_credit_hedge.application.accounting_runtime import AccountingRuntime
from eth_credit_hedge.application.private_execution_accounting import (
    PrivateExecutionClassifier,
)
from eth_credit_hedge.domain.accounting.errors import DuplicateAccountingIdentifierError
from eth_credit_hedge.domain.accounting.events import (
    AccountingEvent,
    FundingAllocation,
    FundingRecorded,
    HedgeExecutionRecorded,
    OptionExecutionRecorded,
    RecoveryDebtChanged,
    event_from_dict,
    event_to_dict,
)
from eth_credit_hedge.domain.client_order_ids import ClientOrderId, ClientOrderRole
from eth_credit_hedge.domain.execution import ExecutionUpdate, ExecutionUpdateBatch
from eth_credit_hedge.domain.accounting.reconciliation import AccountingExchangeState
from eth_credit_hedge.domain.accounting.reconstruction import CombinedLedgerReconstructor
from eth_credit_hedge.domain.strategy_math.units import Money, Price
from eth_credit_hedge.infrastructure.persistence.sqlite_accounting_store import (
    SqliteAccountingStore,
)
from eth_credit_hedge.visualization.accounting_dashboard import (
    LedgerDashboard,
    build_ledger_dashboard_payload,
)


ROOT = Path(__file__).parents[3]
FIXTURE = ROOT / "tests" / "fixtures" / "accounting" / "m2_6_full_lifecycle.jsonl"
ZERO = Decimal("0")


class _FlatAccountingReader:
    def __init__(self, events: tuple[AccountingEvent, ...], total_fees: Money, funding: Money) -> None:
        executions = tuple(
            event
            for event in events
            if isinstance(event, (OptionExecutionRecorded, HedgeExecutionRecorded))
        )
        self._state = AccountingExchangeState(
            option_quantities={},
            hedge_short_quantity=ZERO,
            total_fees=total_fees,
            funding_pnl=funding,
            order_ids=frozenset(event.execution.order_id for event in executions),
            execution_ids=frozenset(event.execution.execution_id for event in executions),
        )

    async def capture_accounting_state(self) -> AccountingExchangeState:
        return self._state


def run_simulated_lifecycle(output: Path) -> dict[str, object]:
    """Produce deterministic operator artifacts without credentials or exchange access."""
    return asyncio.run(_run_simulated_lifecycle(output))


async def _run_simulated_lifecycle(output: Path) -> dict[str, object]:
    output.mkdir(parents=True, exist_ok=True)
    events = _read_events(FIXTURE)
    store = SqliteAccountingStore(output / "accounting.sqlite3")
    await store.initialize()
    runtime = AccountingRuntime(store=store, reconstructor=CombinedLedgerReconstructor())
    await runtime.initialize()
    raw_by_event_id, reference_prices = _raw_executions(events)
    classifier = PrivateExecutionClassifier(
        cycle_id="m2-6-cycle",
        cycle_number=1,
        strategy_instance="M2",
        reference_prices=reference_prices,
    )
    funding_duplicates: dict[str, bool] = {}
    conflicting_funding_rejected = False
    for event in events:
        if isinstance(event, (OptionExecutionRecorded, HedgeExecutionRecorded)):
            execution = raw_by_event_id[event.event_id]
            batch = _execution_batch(execution)
            await runtime.apply_private_update_batch(batch, classifier)
            await runtime.apply_private_update_batch(batch, classifier)
        elif isinstance(event, FundingRecorded):
            existing = next(
                (
                    recorded
                    for recorded in runtime.events
                    if isinstance(recorded, FundingRecorded)
                    and recorded.funding_id == event.funding_id
                ),
                None,
            )
            if existing is None:
                open_lots = tuple(
                    lot
                    for lot in runtime.state.hedge.lots
                    if lot.open_quantity > ZERO
                )
                if len(open_lots) != 1:
                    raise AssertionError(
                        "funding fixture requires one exact open hedge lot"
                    )
                funding_event = replace(
                    event,
                    allocations=(
                        FundingAllocation(
                            lot_id=open_lots[0].lot_id,
                            amount=event.amount,
                        ),
                    ),
                )
            else:
                funding_event = existing
            await runtime.apply_funding(funding_event)
            before_duplicate = len(runtime.events)
            await runtime.apply_funding(funding_event)
            funding_duplicates[event.funding_id] = (
                len(runtime.events) == before_duplicate
            )
            if event.funding_id == "m2-6-recovery-funding":
                conflict_amount = Money(event.amount.value + Decimal("0.01"))
                try:
                    await runtime.apply_funding(
                        replace(
                            funding_event,
                            amount=conflict_amount,
                            allocations=(
                                FundingAllocation(
                                    lot_id=funding_event.allocations[0].lot_id,
                                    amount=conflict_amount,
                                ),
                            ),
                        )
                    )
                except DuplicateAccountingIdentifierError:
                    conflicting_funding_rejected = True
        elif isinstance(event, RecoveryDebtChanged) and any(
            recorded.event_id.startswith("recovery-settlement:")
            for recorded in runtime.events
        ):
            continue
        else:
            await runtime.record_system_event(event)
    await runtime.record_shutdown_snapshot(
        cycle_id="m2-6-cycle",
        timestamp=max(event.timestamp for event in runtime.events),
    )
    state = runtime.state
    funding_evidence = _funding_evidence(runtime.events, funding_duplicates)
    reconciliation = await runtime.reconcile(
        state_reader=_FlatAccountingReader(
            runtime.events,
            Money(state.option_fees.value + state.hedge_fees.value),
            state.funding_pnl,
        ),
        clock=lambda: datetime.now(timezone.utc),
    )
    replay = await store.replay(CombinedLedgerReconstructor())
    dashboard = LedgerDashboard(build_ledger_dashboard_payload(state)).render()
    _write_jsonl(output / "accounting_events.jsonl", runtime.events)
    _write_raw_executions(
        output / "raw_executions.jsonl",
        tuple(raw_by_event_id.values()),
    )
    _write_jsonl(
        output / "funding_events.jsonl",
        tuple(event for event in runtime.events if isinstance(event, FundingRecorded)),
    )
    _write_jsonl(
        output / "quotes.jsonl",
        tuple(event for event in runtime.events if type(event).__name__ == "OptionQuoteRecorded"),
    )
    _write_json(output / "combined_ledger.json", state.to_dict())
    _write_json(
        output / "reconciliation.json",
        {
            "trading_allowed": reconciliation.report.trading_allowed,
            "differences": [
                {"kind": difference.kind.value, "detail": difference.detail}
                for difference in reconciliation.report.differences
            ],
        },
    )
    _write_json(
        output / "ledger_recompute_report.json",
        {
            "event_count": replay.event_count,
            "full_replay_digest": replay.full_replay_digest,
            "snapshot_tail_digest": replay.snapshot_tail_digest,
            "snapshot_digest_matches": replay.snapshot_digest_matches,
            "state": replay.state.to_dict(),
        },
    )
    _write_json(output / "dashboard.json", dashboard)
    _write_json(
        output / "funding_evidence.json",
        {
            "events": funding_evidence,
            "conflicting_funding_identifier_rejected": conflicting_funding_rejected,
            "final_funding_pnl": str(state.funding_pnl.value),
            "final_combined_mark_pnl": str(state.net_combined_mark_pnl.value),
            "final_identity_residual": str(state.mark_identity_residual.value),
        },
    )
    return {
        "offline_only": True,
        "event_count": len(runtime.events),
        "combined_ledger": state.to_dict(),
        "reconciliation": reconciliation.report.trading_allowed,
        "funding_evidence": funding_evidence,
        "conflicting_funding_identifier_rejected": conflicting_funding_rejected,
        "output": str(output),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(run_simulated_lifecycle(args.output), sort_keys=True))
    return 0


def _read_events(path: Path) -> tuple[AccountingEvent, ...]:
    return tuple(
        event_from_dict(json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )


def _write_jsonl(path: Path, events: tuple[AccountingEvent, ...]) -> None:
    path.write_text(
        "\n".join(json.dumps(event_to_dict(event), sort_keys=True) for event in events) + "\n",
        encoding="utf-8",
    )


def _raw_executions(
    events: tuple[AccountingEvent, ...],
) -> tuple[dict[str, ExecutionUpdate], dict[str, Price]]:
    raw: dict[str, ExecutionUpdate] = {}
    references: dict[str, Price] = {}
    sequence = 0
    for event in events:
        if not isinstance(event, (OptionExecutionRecorded, HedgeExecutionRecorded)):
            continue
        sequence += 1
        execution = event.execution
        if isinstance(event, OptionExecutionRecorded):
            role = (
                ClientOrderRole.OPTION_LONG
                if event.leg.value == "LONG"
                else ClientOrderRole.OPTION_SHORT
            )
            level = 0
            attempt = 1
        else:
            level = event.level_id or 0
            attempt = event.attempt
            role = (
                ClientOrderRole.HEDGE_ENTRY
                if event.exit_reason is None
                else ClientOrderRole.HEDGE_TP
                if event.exit_reason.value == "TAKE_PROFIT"
                else ClientOrderRole.HEDGE_STOP
                if event.exit_reason.value == "STOP"
                else ClientOrderRole.EMERGENCY_CLOSE
            )
        order_link_id = str(
            ClientOrderId("M2", 1, level, role, attempt, f"{sequence:04X}")
        )
        update = ExecutionUpdate(
            execution_id=execution.execution_id,
            order_id=execution.order_id,
            order_link_id=order_link_id,
            symbol=execution.symbol,
            side="Buy" if execution.side.value == "BUY" else "Sell",
            price=execution.price.value,
            quantity=execution.quantity.value,
            fee=execution.fee.value,
            is_maker=None,
            executed_at=execution.timestamp,
        )
        raw[event.event_id] = update
        if isinstance(event, HedgeExecutionRecorded) and event.reference_price is not None:
            references[order_link_id] = event.reference_price
    return raw, references


def _execution_batch(execution: ExecutionUpdate) -> ExecutionUpdateBatch:
    payload = "|".join(
        (
            execution.execution_id,
            execution.order_id,
            execution.order_link_id,
            execution.symbol,
            execution.side,
            str(execution.price),
            str(execution.quantity),
            str(execution.fee),
            execution.executed_at.isoformat(),
        )
    )
    return ExecutionUpdateBatch(
        executions=(execution,),
        received_at=execution.executed_at,
        raw_payload_hash=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    )


def _write_raw_executions(
    path: Path,
    executions: tuple[ExecutionUpdate, ...],
) -> None:
    path.write_text(
        "\n".join(
            json.dumps(
                {
                    "execution_id": execution.execution_id,
                    "order_id": execution.order_id,
                    "order_link_id": execution.order_link_id,
                    "symbol": execution.symbol,
                    "side": execution.side,
                    "price": str(execution.price),
                    "quantity": str(execution.quantity),
                    "fee": str(execution.fee),
                    "executed_at": execution.executed_at.isoformat(),
                },
                sort_keys=True,
            )
            for execution in executions
        )
        + "\n",
        encoding="utf-8",
    )


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _funding_evidence(
    events: tuple[AccountingEvent, ...],
    duplicate_results: dict[str, bool],
) -> list[dict[str, object]]:
    reconstructor = CombinedLedgerReconstructor()
    evidence: list[dict[str, object]] = []
    for index, event in enumerate(events):
        state = reconstructor.reconstruct(events[: index + 1])
        if isinstance(event, FundingRecorded):
            evidence.append(
                {
                    "funding_id": event.funding_id,
                    "event_amount": str(event.amount.value),
                    "cumulative_funding_pnl": str(state.funding_pnl.value),
                    "duplicate_ignored": duplicate_results[event.funding_id],
                }
            )
        elif (
            isinstance(event, HedgeExecutionRecorded)
            and event.execution.execution_id == "m2-6-base-stop-b"
        ):
            baseline = next(
                lot for lot in state.hedge.lots if lot.role.value == "BASELINE"
            )
            evidence.append(
                {
                    "event_id": event.execution.execution_id,
                    "confirmed_stop_debt": str(
                        state.confirmed_recovery_debt.value
                    ),
                    "allocated_funding_pnl": str(
                        baseline.allocated_funding.value
                    ),
                }
            )
    return evidence


if __name__ == "__main__":
    raise SystemExit(main())
