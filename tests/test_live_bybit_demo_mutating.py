"""Opt-in, explicitly mutating Bybit demo deployment gates."""

from __future__ import annotations

import asyncio
import os

import pytest

from eth_credit_hedge.interfaces.demo_runner import (
    D3_MUTATION_TOKEN,
    MUTATION_GATE_ENV,
    run_d3_manual,
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
