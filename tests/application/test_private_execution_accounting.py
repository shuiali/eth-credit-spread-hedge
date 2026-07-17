"""Independent private-fill classification and fail-closed runtime tests."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from eth_credit_hedge.application.accounting_runtime import AccountingRuntime
from eth_credit_hedge.application.private_execution_accounting import (
    PrivateExecutionClassificationError,
    PrivateExecutionClassifier,
)
from eth_credit_hedge.domain.accounting.events import (
    ExitReason,
    HedgeExecutionRecorded,
    OptionExecutionRecorded,
    OptionLeg,
    PositionReconciled,
)
from eth_credit_hedge.domain.accounting.errors import DuplicateAccountingIdentifierError
from eth_credit_hedge.domain.accounting.reconciliation import (
    AccountingDifferenceKind,
    AccountingExchangeState,
    evaluate_accounting_reconciliation,
)
from eth_credit_hedge.domain.accounting.reconstruction import CombinedLedgerReconstructor
from eth_credit_hedge.domain.client_order_ids import ClientOrderId, ClientOrderRole
from eth_credit_hedge.domain.execution import ExecutionUpdate, ExecutionUpdateBatch
from eth_credit_hedge.domain.strategy_math.units import Money
from eth_credit_hedge.infrastructure.persistence.sqlite_accounting_store import (
    SqliteAccountingStore,
)


D = Decimal
NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)


def _link(role: ClientOrderRole, *, level: int = 1, attempt: int = 1) -> str:
    return str(ClientOrderId("SIM", 1, level, role, attempt, "ABCD"))


def _execution(
    identity: str,
    role: ClientOrderRole,
    side: str,
    *,
    symbol: str = "ETHUSDT",
    level: int = 1,
    attempt: int = 1,
    quantity: str = "1",
) -> ExecutionUpdate:
    return ExecutionUpdate(
        execution_id=identity,
        order_id=f"order-{identity}",
        order_link_id=_link(role, level=level, attempt=attempt),
        symbol=symbol,
        side=side,  # type: ignore[arg-type]
        price=D("100"),
        quantity=D(quantity),
        fee=D("0.25"),
        is_maker=False,
        executed_at=NOW + timedelta(seconds=len(identity)),
    )


def _batch(*executions: ExecutionUpdate) -> ExecutionUpdateBatch:
    return ExecutionUpdateBatch(tuple(executions), NOW, "a" * 64)


def _classifier() -> PrivateExecutionClassifier:
    return PrivateExecutionClassifier(
        cycle_id="SIM-C0001",
        cycle_number=1,
        strategy_instance="SIM",
    )


def test_all_private_execution_roles_are_classified_without_guessing() -> None:
    empty = CombinedLedgerReconstructor().reconstruct(())
    events = _classifier().classify_batch(
        _batch(
            _execution("long-entry", ClientOrderRole.OPTION_LONG, "Buy", symbol="ETH-P-USDC"),
            _execution("short-entry", ClientOrderRole.OPTION_SHORT, "Sell", symbol="ETH-P2-USDC"),
            _execution("long-close", ClientOrderRole.OPTION_LONG, "Sell", symbol="ETH-P-USDC"),
            _execution("short-close", ClientOrderRole.OPTION_SHORT, "Buy", symbol="ETH-P2-USDC"),
            _execution("hedge-entry", ClientOrderRole.HEDGE_ENTRY, "Sell"),
            _execution("hedge-tp", ClientOrderRole.HEDGE_TP, "Buy"),
            _execution(
                "recovery-entry",
                ClientOrderRole.HEDGE_ENTRY,
                "Sell",
                attempt=2,
            ),
            _execution("hedge-stop", ClientOrderRole.HEDGE_STOP, "Buy", attempt=2),
        ),
        empty,
    )
    option_events = tuple(event for event in events if isinstance(event, OptionExecutionRecorded))
    hedge_events = tuple(event for event in events if isinstance(event, HedgeExecutionRecorded))
    assert [event.leg for event in option_events] == [
        OptionLeg.LONG,
        OptionLeg.SHORT,
        OptionLeg.LONG,
        OptionLeg.SHORT,
    ]
    assert [event.exit_reason for event in hedge_events] == [
        None,
        ExitReason.TAKE_PROFIT,
        None,
        ExitReason.STOP,
    ]
    assert all(event.execution.fee == Money(D("0.25")) for event in events)


def test_manual_close_requires_one_exact_open_lot() -> None:
    classifier = _classifier()
    empty = CombinedLedgerReconstructor().reconstruct(())
    entry = classifier.classify_batch(
        _batch(_execution("entry", ClientOrderRole.HEDGE_ENTRY, "Sell")),
        empty,
    )[0]
    state = CombinedLedgerReconstructor().reconstruct((entry,))
    manual = classifier.classify_batch(
        _batch(_execution("manual", ClientOrderRole.EMERGENCY_CLOSE, "Buy")),
        state,
    )[0]
    assert isinstance(manual, HedgeExecutionRecorded)
    assert manual.exit_reason is ExitReason.EMERGENCY
    assert manual.lot_id == state.hedge.lots[0].lot_id

    with pytest.raises(PrivateExecutionClassificationError, match="exactly one"):
        classifier.classify_batch(
            _batch(
                _execution(
                    "ambiguous",
                    ClientOrderRole.EMERGENCY_CLOSE,
                    "Buy",
                    level=0,
                )
            ),
            empty,
        )


def test_unknown_private_execution_persists_fault_and_blocks_reconciliation(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        store = SqliteAccountingStore(tmp_path / "accounting.sqlite3")
        await store.initialize()
        runtime = AccountingRuntime(store=store, reconstructor=CombinedLedgerReconstructor())
        await runtime.initialize()
        unknown = _execution("unknown", ClientOrderRole.HEDGE_ENTRY, "Sell")
        unknown = ExecutionUpdate(
            execution_id=unknown.execution_id,
            order_id=unknown.order_id,
            order_link_id="external-order",
            symbol=unknown.symbol,
            side=unknown.side,
            price=unknown.price,
            quantity=unknown.quantity,
            fee=unknown.fee,
            is_maker=unknown.is_maker,
            executed_at=unknown.executed_at,
        )
        with pytest.raises(PrivateExecutionClassificationError):
            await runtime.apply_private_update_batch(_batch(unknown), _classifier())
        assert len(runtime.events) == 1
        fault = runtime.events[0]
        assert isinstance(fault, PositionReconciled)
        assert not fault.matched
        report = evaluate_accounting_reconciliation(
            runtime.state,
            AccountingExchangeState(
                option_quantities={},
                hedge_short_quantity=D("0"),
                total_fees=Money(D("0")),
                funding_pnl=Money(D("0")),
                order_ids=frozenset(),
                execution_ids=frozenset(),
            ),
            replay_digest_matches=True,
            legacy_migration_pending=False,
            events=runtime.events,
        )
        assert AccountingDifferenceKind.PRIVATE_EXECUTION_CLASSIFICATION_FAILURE in {
            difference.kind for difference in report.differences
        }

    asyncio.run(exercise())


def test_private_classifier_rejects_wrong_cycle_sides_lots_and_fee_currency() -> None:
    empty = CombinedLedgerReconstructor().reconstruct(())
    with pytest.raises(ValueError, match="cycle ID"):
        PrivateExecutionClassifier(
            cycle_id=" ", cycle_number=1, strategy_instance="SIM"
        )

    wrong_cycle = _execution("wrong-cycle", ClientOrderRole.HEDGE_ENTRY, "Sell")
    wrong_cycle = ExecutionUpdate(
        execution_id=wrong_cycle.execution_id,
        order_id=wrong_cycle.order_id,
        order_link_id=str(
            ClientOrderId("SIM", 2, 1, ClientOrderRole.HEDGE_ENTRY, 1, "ABCD")
        ),
        symbol=wrong_cycle.symbol,
        side=wrong_cycle.side,
        price=wrong_cycle.price,
        quantity=wrong_cycle.quantity,
        fee=wrong_cycle.fee,
        is_maker=wrong_cycle.is_maker,
        executed_at=wrong_cycle.executed_at,
    )
    invalid = (
        wrong_cycle,
        _execution("entry-buy", ClientOrderRole.HEDGE_ENTRY, "Buy"),
        _execution("exit-sell", ClientOrderRole.HEDGE_TP, "Sell"),
        _execution("exit-no-lot", ClientOrderRole.HEDGE_TP, "Buy"),
        _execution("manual-sell", ClientOrderRole.EMERGENCY_CLOSE, "Sell"),
        _execution(
            "bad-currency",
            ClientOrderRole.OPTION_LONG,
            "Buy",
            symbol="ETHBTC",
        ),
    )
    for execution in invalid:
        with pytest.raises((PrivateExecutionClassificationError, ValueError)):
            _classifier().classify_batch(_batch(execution), empty)

    entry = _classifier().classify_batch(
        _batch(
            _execution(
                "small-entry",
                ClientOrderRole.HEDGE_ENTRY,
                "Sell",
                quantity="0.5",
            )
        ),
        empty,
    )[0]
    state = CombinedLedgerReconstructor().reconstruct((entry,))
    with pytest.raises(PrivateExecutionClassificationError, match="exceeds"):
        _classifier().classify_batch(
            _batch(_execution("large-manual", ClientOrderRole.EMERGENCY_CLOSE, "Buy")),
            state,
        )
    with pytest.raises(ValueError, match="unsupported fee currency"):
        _classifier().classify_batch(
            _batch(
                _execution(
                    "unsupported-currency",
                    ClientOrderRole.OPTION_LONG,
                    "Buy",
                    symbol="ETH-P-BTC",
                )
            ),
            empty,
        )


def test_private_batch_tracks_multiple_partial_entry_fills_before_exit() -> None:
    events = _classifier().classify_batch(
        _batch(
            _execution(
                "partial-entry-a",
                ClientOrderRole.HEDGE_ENTRY,
                "Sell",
                quantity="0.4",
            ),
            _execution(
                "partial-entry-b",
                ClientOrderRole.HEDGE_ENTRY,
                "Sell",
                quantity="0.6",
            ),
            _execution("full-tp", ClientOrderRole.HEDGE_TP, "Buy"),
        ),
        CombinedLedgerReconstructor().reconstruct(()),
    )
    assert len(events) == 3


def test_private_raw_duplicate_is_ignored_and_conflicting_execution_is_rejected(
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
        execution = _execution("raw-duplicate", ClientOrderRole.HEDGE_ENTRY, "Sell")
        batch = _batch(execution)
        first = await runtime.apply_private_update_batch(batch, _classifier())
        duplicate = await runtime.apply_private_update_batch(batch, _classifier())
        assert duplicate.ledger_digest == first.ledger_digest
        conflict = ExecutionUpdate(
            execution_id=execution.execution_id,
            order_id=execution.order_id,
            order_link_id=execution.order_link_id,
            symbol=execution.symbol,
            side=execution.side,
            price=execution.price + D("1"),
            quantity=execution.quantity,
            fee=execution.fee,
            is_maker=execution.is_maker,
            executed_at=execution.executed_at,
        )
        with pytest.raises(
            DuplicateAccountingIdentifierError,
            match="conflicting execution ID",
        ):
            await runtime.apply_private_update_batch(_batch(conflict), _classifier())

    asyncio.run(exercise())
