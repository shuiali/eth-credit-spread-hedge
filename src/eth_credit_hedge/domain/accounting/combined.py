"""Immutable M2.1 combined-ledger snapshot contract without reconstruction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from eth_credit_hedge.domain.accounting.fills import utc_timestamp
from eth_credit_hedge.domain.strategy_math.units import Money


class OptionPositionState(str, Enum):
    UNKNOWN = "UNKNOWN"


class HedgePositionState(str, Enum):
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class CombinedLedgerSnapshot:
    as_of: datetime
    option_realized_pnl: Money
    option_open_mark_pnl: Money
    option_open_liquidation_pnl: Money
    hedge_realized_pnl: Money
    hedge_open_mark_pnl: Money
    hedge_open_liquidation_pnl: Money
    option_fees: Money
    hedge_fees: Money
    funding_pnl: Money
    slippage_attribution: Money
    net_combined_mark_pnl: Money
    net_combined_liquidation_pnl: Money
    confirmed_recovery_debt: Money
    option_position_state: OptionPositionState
    hedge_position_state: HedgePositionState

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", utc_timestamp(self.as_of, "snapshot timestamp"))
        if not all(isinstance(getattr(self, field), Money) for field in self.__dataclass_fields__ if field != "as_of" and not field.endswith("state")):
            raise TypeError("combined snapshot accounting fields must be Money")
