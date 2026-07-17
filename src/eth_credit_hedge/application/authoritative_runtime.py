"""The declared dependency graph for the one authoritative strategy runtime.

Plan 3.1 defines this graph without changing the still-legacy lifecycle.  Later
Milestone 3 plans wire every field into the production composition root.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from eth_credit_hedge.application.accounting_runtime import AccountingRuntime
from eth_credit_hedge.application.live_strategy_coordinator import (
    LiveStrategyCoordinator,
)
from eth_credit_hedge.application.net_position_allocator import (
    NetPositionAllocator,
)
from eth_credit_hedge.application.startup_reconciliation import (
    StartupReconciliationService,
)
from eth_credit_hedge.ports.runtime import (
    ClockPort,
    ExchangeQueryPort,
    FundingPort,
    MarketDataPort,
    PrivateExecutionPort,
    TradingMutationPort,
)

if TYPE_CHECKING:
    from eth_credit_hedge.application.kill_switch import StrategyCloseService
    from eth_credit_hedge.application.operational_state import MutableOperationalState
    from eth_credit_hedge.application.runtime_risk_state import RuntimeRiskStateBuilder
    from eth_credit_hedge.config.schema import RuntimeConfig
    from eth_credit_hedge.domain.strategy_math import StrategyMathEngine


@dataclass(frozen=True, slots=True)
class AuthoritativeStrategyRuntime:
    """One injected owner graph shared by simulated and future-demo adapters."""

    configuration: RuntimeConfig
    clock: ClockPort
    market_data: MarketDataPort
    private_executions: PrivateExecutionPort
    trading: TradingMutationPort
    exchange_queries: ExchangeQueryPort
    funding: FundingPort
    strategy_math: StrategyMathEngine
    coordinator: LiveStrategyCoordinator
    accounting: AccountingRuntime
    allocator: NetPositionAllocator
    reconciliation: StartupReconciliationService
    risk: RuntimeRiskStateBuilder
    health: MutableOperationalState
    shutdown: StrategyCloseService


def assemble_authoritative_runtime(
    *,
    configuration: RuntimeConfig,
    clock: ClockPort,
    market_data: MarketDataPort,
    private_executions: PrivateExecutionPort,
    trading: TradingMutationPort,
    exchange_queries: ExchangeQueryPort,
    funding: FundingPort,
    strategy_math: StrategyMathEngine,
    coordinator: LiveStrategyCoordinator,
    accounting: AccountingRuntime,
    allocator: NetPositionAllocator,
    reconciliation: StartupReconciliationService,
    risk: RuntimeRiskStateBuilder,
    health: MutableOperationalState,
    shutdown: StrategyCloseService,
) -> AuthoritativeStrategyRuntime:
    """Assemble exactly the authorities supplied by an approved composition root."""

    return AuthoritativeStrategyRuntime(
        configuration=configuration,
        clock=clock,
        market_data=market_data,
        private_executions=private_executions,
        trading=trading,
        exchange_queries=exchange_queries,
        funding=funding,
        strategy_math=strategy_math,
        coordinator=coordinator,
        accounting=accounting,
        allocator=allocator,
        reconciliation=reconciliation,
        risk=risk,
        health=health,
        shutdown=shutdown,
    )


__all__ = ["AuthoritativeStrategyRuntime", "assemble_authoritative_runtime"]
