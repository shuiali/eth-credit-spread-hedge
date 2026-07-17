"""Plan 3.1 guards for authoritative runtime ownership."""

from __future__ import annotations

import ast
import json
from dataclasses import fields
from pathlib import Path

from eth_credit_hedge.application.authoritative_runtime import (
    AuthoritativeStrategyRuntime,
    assemble_authoritative_runtime,
)
from eth_credit_hedge.ports import runtime as runtime_ports


ROOT = Path(__file__).parents[2]
MANIFEST_PATH = ROOT / "artifacts" / "runtime_composition.json"


def _manifest() -> dict[str, object]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _constructor_modules(constructor: str) -> set[str]:
    found: set[str] = set()
    for path in (ROOT / "src").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == constructor
            for node in ast.walk(tree)
        ):
            found.add(path.relative_to(ROOT).as_posix())
    return found


def test_runtime_manifest_declares_every_required_owner_and_port() -> None:
    manifest = _manifest()
    authorities = manifest["authorities"]
    assert isinstance(authorities, dict)
    assert set(authorities) == {
        "configuration",
        "clock",
        "public_market_data",
        "private_executions",
        "option_entry_and_close",
        "level_generation",
        "crossings",
        "hedge_entry_and_exits",
        "protection",
        "internal_lots",
        "accounting",
        "recovery_debt",
        "risk",
        "persistence",
        "reconciliation",
        "health",
        "kill_switch",
        "shutdown",
        "metrics_and_logging",
    }
    assert manifest["ports"] == [
        "MarketDataPort",
        "PrivateExecutionPort",
        "TradingMutationPort",
        "ExchangeQueryPort",
        "FundingPort",
        "ClockPort",
    ]


def test_runtime_facade_has_one_injected_slot_per_required_authority() -> None:
    assert [field.name for field in fields(AuthoritativeStrategyRuntime)] == [
        "configuration",
        "clock",
        "market_data",
        "private_executions",
        "trading",
        "exchange_queries",
        "funding",
        "strategy_math",
        "coordinator",
        "accounting",
        "allocator",
        "reconciliation",
        "risk",
        "health",
        "shutdown",
    ]


def test_runtime_facade_assembles_the_injected_authorities_once() -> None:
    dependency = object()
    runtime = assemble_authoritative_runtime(
        configuration=dependency,
        clock=dependency,
        market_data=dependency,
        private_executions=dependency,
        trading=dependency,
        exchange_queries=dependency,
        funding=dependency,
        strategy_math=dependency,
        coordinator=dependency,
        accounting=dependency,
        allocator=dependency,
        reconciliation=dependency,
        risk=dependency,
        health=dependency,
        shutdown=dependency,
    )
    assert all(getattr(runtime, field.name) is dependency for field in fields(runtime))


def test_runtime_port_protocols_are_explicit() -> None:
    expected_methods = {
        "MarketDataPort": {"get_instrument", "get_option_chain", "stream_trades"},
        "PrivateExecutionPort": {"stream_execution_batches", "mark_reconciled"},
        "TradingMutationPort": {"place_order", "amend_order", "cancel_order"},
        "ExchangeQueryPort": {"get_positions", "get_execution_history"},
        "FundingPort": {"get_confirmed_funding"},
        "ClockPort": {"now_utc"},
    }
    for name, methods in expected_methods.items():
        port = getattr(runtime_ports, name)
        assert methods <= set(vars(port))


def test_authority_construction_cannot_escape_the_checked_in_inventory() -> None:
    manifest = _manifest()
    approved = manifest["approved_constructor_modules"]
    assert isinstance(approved, dict)
    for constructor in (
        "StrategyMathEngine",
        "AccountingRuntime",
        "NetPositionAllocator",
        "StartupReconciliationService",
    ):
        allowed = approved[constructor]
        assert isinstance(allowed, list)
        assert _constructor_modules(constructor) == set(allowed)


def test_facade_assembly_does_not_construct_hidden_authorities() -> None:
    path = (
        ROOT
        / "src"
        / "eth_credit_hedge"
        / "application"
        / "authoritative_runtime.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    calls = [
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    assert calls.count("AuthoritativeStrategyRuntime") == 1
    assert not {
        "StrategyMathEngine",
        "AccountingRuntime",
        "NetPositionAllocator",
        "StartupReconciliationService",
    } & set(calls)
