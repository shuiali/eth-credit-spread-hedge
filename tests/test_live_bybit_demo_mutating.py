"""Opt-in, explicitly mutating Bybit demo deployment gates."""

from __future__ import annotations

import asyncio
import os
from decimal import Decimal

import pytest

from eth_credit_hedge.interfaces.demo_runner import (
    D3_MUTATION_TOKEN,
    D4_MUTATION_TOKEN,
    D5_MUTATION_TOKEN,
    MUTATION_GATE_ENV,
    run_d3_manual,
    run_d4_automatic,
    run_d5_multiple,
)


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get(MUTATION_GATE_ENV) != D3_MUTATION_TOKEN,
    reason=f"set {MUTATION_GATE_ENV}={D3_MUTATION_TOKEN} for D3 demo orders",
)
def test_d3_manual_one_level_is_protected_restarted_and_closed() -> None:
    result = asyncio.run(run_d3_manual())

    assert result.option_state == "OPEN"
    assert result.option_matched_quantity > 0
    assert result.option_actual_net_credit > 0
    assert result.entry_quantity == result.option_matched_quantity
    assert result.protected_restart_status == "MATCHED"
    assert result.final_state in {"CLOSED_TP", "CLOSED_STOP"}
    assert result.final_reconciliation_status == "MATCHED"


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get(MUTATION_GATE_ENV) != D4_MUTATION_TOKEN,
    reason=f"set {MUTATION_GATE_ENV}={D4_MUTATION_TOKEN} for D4 demo orders",
)
def test_d4_last_trade_crossing_completes_the_protected_lifecycle() -> None:
    result = asyncio.run(run_d4_automatic())

    assert result.trigger_source == "LAST_TRADE"
    assert result.armed_price > result.level_entry_price
    assert result.crossing_price <= result.level_entry_price
    assert result.request_quantity > 0
    assert result.protected_restart_status == "MATCHED"
    assert result.final_state in {"CLOSED_TP", "CLOSED_STOP"}
    assert result.final_reconciliation_status == "MATCHED"


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get(MUTATION_GATE_ENV) != D5_MUTATION_TOKEN,
    reason=f"set {MUTATION_GATE_ENV}={D5_MUTATION_TOKEN} for D5 demo orders",
)
def test_d5_two_baseline_levels_protect_restart_and_exit_independently() -> None:
    result = asyncio.run(run_d5_multiple())

    assert result.trigger_source == "LAST_TRADE"
    assert len(result.level_entry_prices) == 2
    assert len(result.crossing_prices) == 2
    assert result.aggregate_quantity == Decimal("0.2")
    assert len(result.stop_trigger_prices) == 2
    assert result.protected_restart_status == "MATCHED"
    assert result.final_states == ("CLOSED_TP", "CLOSED_TP")
    assert result.final_reconciliation_status == "MATCHED"
