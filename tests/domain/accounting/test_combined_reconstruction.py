"""Independent M2.4 reconstruction arithmetic using fixed canonical JSONL facts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from eth_credit_hedge.domain.accounting.errors import (
    AccountingContractError,
    DuplicateAccountingIdentifierError,
)
from eth_credit_hedge.domain.accounting.events import (
    EventSource,
    OptionExecutionRecorded,
    OptionLeg,
    event_from_dict,
)
from eth_credit_hedge.domain.accounting.fills import ConfirmedExecution, InstrumentKind, Side
from eth_credit_hedge.domain.accounting.reconstruction import CombinedLedgerReconstructor
from eth_credit_hedge.domain.strategy_math.units import Money, Price, Quantity


D = Decimal
ROOT = Path(__file__).parents[3]
FIXTURES = ROOT / "tests" / "fixtures" / "accounting"
NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)


def _events(filename: str) -> tuple[object, ...]:
    return tuple(
        event_from_dict(json.loads(line))
        for line in (FIXTURES / filename).read_text(encoding="utf-8").splitlines()
        if line
    )


def test_fixed_stream_preserves_combined_mark_liquidation_cash_and_debt_identities() -> None:
    events = _events("m2_4_combined_events.jsonl")
    quotes = _events("m2_4_option_quotes.jsonl")
    state = CombinedLedgerReconstructor().reconstruct(events, quotes)  # type: ignore[arg-type]

    # Hand-calculated: option mark is -2 + 5, hedge price P&L is -10 + 10,
    # fees are 2 + 4, funding is -1 + 1, and stopped-attempt debt is 13 - 9.
    assert state.option_open_mark_pnl == Money(D("3"))
    assert state.option_open_liquidation_pnl == Money(D("1"))
    assert state.hedge_realized_pnl == Money(D("0"))
    assert state.option_fees == Money(D("2"))
    assert state.hedge_fees == Money(D("4"))
    assert state.funding_pnl == Money(D("0"))
    assert state.slippage_attribution == Money(D("2"))
    assert state.net_combined_mark_pnl == Money(D("-3"))
    assert state.net_combined_liquidation_pnl == Money(D("-5"))
    assert state.ending_cash == Money(D("24"))
    assert state.mark_open_position_value == Money(D("-27"))
    assert state.liquidation_open_position_value == Money(D("-29"))
    assert state.mark_equity_change == Money(D("-3"))
    assert state.liquidation_equity_change == Money(D("-5"))
    assert state.debt_increments == Money(D("13"))
    assert state.actual_recovery_allocations == Money(D("9"))
    assert state.confirmed_recovery_debt == Money(D("4"))
    assert state.mark_identity_residual == Money(D("0"))
    assert state.liquidation_identity_residual == Money(D("0"))
    assert state.cash_equity_mark_residual == Money(D("0"))
    assert state.cash_equity_liquidation_residual == Money(D("0"))
    assert state.debt_identity_residual == Money(D("0"))


def test_replay_is_deterministic_for_causal_reordering_restart_and_duplicates() -> None:
    events = _events("m2_4_combined_events.jsonl")
    quotes = _events("m2_4_option_quotes.jsonl")
    reconstructor = CombinedLedgerReconstructor()
    baseline = reconstructor.reconstruct(events, quotes)  # type: ignore[arg-type]
    reordered = reconstructor.reconstruct(tuple(reversed(events)), quotes)  # type: ignore[arg-type]
    duplicate = replace(events[0], event_id="replayed-option-long")
    restarted = reconstructor.reconstruct(
        tuple(events[:4]) + tuple(events[4:]) + (duplicate,), quotes  # type: ignore[arg-type]
    )

    assert reordered.to_dict() == baseline.to_dict()
    assert restarted.to_dict() == baseline.to_dict()
    with pytest.raises(DuplicateAccountingIdentifierError, match="execution ID"):
        reconstructor.reconstruct(
            (
                events[0],
                replace(
                    duplicate,
                    execution=replace(duplicate.execution, fee=Money(D("2"))),
                ),
            )
        )


def test_projected_recovery_cannot_settle_confirmed_debt() -> None:
    events = _events("m2_4_combined_events.jsonl")
    projected_settlement = replace(
        events[-1], actual_recovery_allocation=Money(D("10"))
    )
    with pytest.raises(AccountingContractError, match="realized recovery profit"):
        CombinedLedgerReconstructor().reconstruct(
            tuple(events[:-1]) + (projected_settlement,),
            _events("m2_4_option_quotes.jsonl"),  # type: ignore[arg-type]
        )


def test_partial_option_closes_keep_gross_price_pnl_and_fees_separate() -> None:
    quotes = _events("m2_4_option_quotes.jsonl")
    events = (
        _option_event("01-long-entry", "long-fill", "ETH-31JUL26-3000-P-USDC", Side.BUY, "20", "2", "2", OptionLeg.LONG),
        _option_event("02-short-entry", "short-fill", "ETH-31JUL26-3200-P-USDC", Side.SELL, "50", "2", "2", OptionLeg.SHORT),
        _option_event("03-long-close", "long-close-fill", "ETH-31JUL26-3000-P-USDC", Side.SELL, "22", "1", "1", OptionLeg.LONG),
        _option_event("04-short-close", "short-close-fill", "ETH-31JUL26-3200-P-USDC", Side.BUY, "45", "1", "1", OptionLeg.SHORT),
    )
    state = CombinedLedgerReconstructor().reconstruct(events, quotes)  # type: ignore[arg-type]

    assert state.option_realized_pnl == Money(D("7"))
    assert state.option_open_mark_pnl == Money(D("3"))
    assert state.option_fees == Money(D("6"))
    assert state.net_combined_mark_pnl == Money(D("4"))
    assert state.net_combined_liquidation_pnl == Money(D("2"))
    assert state.cash_equity_mark_residual == Money(D("0"))


def test_reconstruct_snapshots_and_offline_cli_report_the_same_zero_residuals() -> None:
    events = _events("m2_4_combined_events.jsonl")
    quotes = _events("m2_4_option_quotes.jsonl")
    snapshots = CombinedLedgerReconstructor().reconstruct_snapshots(
        events, quotes  # type: ignore[arg-type]
    )
    assert len(snapshots) == 9
    assert snapshots[-1].confirmed_recovery_debt == Money(D("4"))

    environment = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "eth_credit_hedge.interfaces.ledger_recompute",
            "--events",
            str(FIXTURES / "m2_4_combined_events.jsonl"),
            "--quotes",
            str(FIXTURES / "m2_4_option_quotes.jsonl"),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    output = json.loads(completed.stdout)
    assert output["ledger_digest"]
    assert output["cash_equity_mark_residual"] == "0"
    assert output["debt_identity_residual"] == "0"


def _option_event(
    event_id: str,
    execution_id: str,
    symbol: str,
    side: Side,
    price: str,
    quantity: str,
    fee: str,
    leg: OptionLeg,
) -> OptionExecutionRecorded:
    execution = ConfirmedExecution(
        execution_id=execution_id,
        symbol=symbol,
        instrument_kind=InstrumentKind.OPTION,
        side=side,
        price=Price(D(price)),
        quantity=Quantity(D(quantity)),
        fee=Money(D(fee)),
        fee_currency="USDC",
        timestamp=NOW,
        order_id=f"{execution_id}-order",
        order_link_id=f"{execution_id}-link",
    )
    return OptionExecutionRecorded(
        event_id=event_id,
        event_version=1,
        cycle_id="cycle-1",
        timestamp=NOW,
        source=EventSource.PRIVATE_STREAM,
        correlation_id=event_id,
        execution_id=execution_id,
        order_id=execution.order_id,
        order_link_id=execution.order_link_id,
        symbol=symbol,
        execution=execution,
        leg=leg,
    )
