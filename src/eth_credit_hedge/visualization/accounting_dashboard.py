"""Ledger-only dashboard payload and renderer for combined accounting."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from eth_credit_hedge.domain.accounting.reconstruction import CombinedLedgerState


@dataclass(frozen=True, slots=True)
class LedgerDashboardPayload:
    """Precomputed accounting values; this contract intentionally has no formulas."""

    as_of: str
    option_realized_pnl: Decimal
    option_open_mark_pnl: Decimal
    option_open_liquidation_pnl: Decimal
    hedge_realized_pnl: Decimal
    hedge_open_mark_pnl: Decimal
    hedge_open_liquidation_pnl: Decimal
    option_fees: Decimal
    hedge_fees: Decimal
    funding_pnl: Decimal
    slippage_attribution: Decimal
    confirmed_recovery_debt: Decimal
    net_combined_mark_pnl: Decimal
    net_combined_liquidation_pnl: Decimal
    mark_identity_residual: Decimal
    liquidation_identity_residual: Decimal
    ledger_digest: str

    def to_dict(self) -> dict[str, str]:
        return {
            "as_of": self.as_of,
            "option_realized_pnl": str(self.option_realized_pnl),
            "option_open_mark_pnl": str(self.option_open_mark_pnl),
            "option_open_liquidation_pnl": str(self.option_open_liquidation_pnl),
            "hedge_realized_pnl": str(self.hedge_realized_pnl),
            "hedge_open_mark_pnl": str(self.hedge_open_mark_pnl),
            "hedge_open_liquidation_pnl": str(self.hedge_open_liquidation_pnl),
            "option_fees": str(self.option_fees),
            "hedge_fees": str(self.hedge_fees),
            "funding_pnl": str(self.funding_pnl),
            "slippage_attribution": str(self.slippage_attribution),
            "confirmed_recovery_debt": str(self.confirmed_recovery_debt),
            "net_combined_mark_pnl": str(self.net_combined_mark_pnl),
            "net_combined_liquidation_pnl": str(self.net_combined_liquidation_pnl),
            "mark_identity_residual": str(self.mark_identity_residual),
            "liquidation_identity_residual": str(self.liquidation_identity_residual),
            "ledger_digest": self.ledger_digest,
        }


def build_ledger_dashboard_payload(state: CombinedLedgerState) -> LedgerDashboardPayload:
    """Adapt authoritative state without recalculating any accounting value."""
    return LedgerDashboardPayload(
        as_of=state.as_of.isoformat(),
        option_realized_pnl=state.option_realized_pnl.value,
        option_open_mark_pnl=state.option_open_mark_pnl.value,
        option_open_liquidation_pnl=state.option_open_liquidation_pnl.value,
        hedge_realized_pnl=state.hedge_realized_pnl.value,
        hedge_open_mark_pnl=state.hedge_open_mark_pnl.value,
        hedge_open_liquidation_pnl=state.hedge_open_liquidation_pnl.value,
        option_fees=state.option_fees.value,
        hedge_fees=state.hedge_fees.value,
        funding_pnl=state.funding_pnl.value,
        slippage_attribution=state.slippage_attribution.value,
        confirmed_recovery_debt=state.confirmed_recovery_debt.value,
        net_combined_mark_pnl=state.net_combined_mark_pnl.value,
        net_combined_liquidation_pnl=state.net_combined_liquidation_pnl.value,
        mark_identity_residual=state.mark_identity_residual.value,
        liquidation_identity_residual=state.liquidation_identity_residual.value,
        ledger_digest=state.ledger_digest,
    )


class LedgerDashboard:
    """Renderer boundary: return the supplied ledger payload unchanged."""

    def __init__(self, payload: LedgerDashboardPayload) -> None:
        self._payload = payload

    def render(self) -> dict[str, str]:
        return self._payload.to_dict()

