"""Independent finite-limit risk decisions for live proposals."""

from dataclasses import replace
from decimal import Decimal

import pytest

from eth_credit_hedge.domain.risk import (
    RiskEngine,
    RiskLimits,
    RiskState,
    TradeProposal,
)


def limits() -> RiskLimits:
    return RiskLimits(
        maximum_perp_quantity=Decimal("1"),
        maximum_perp_notional=Decimal("5000"),
        maximum_margin_usage=Decimal("0.5"),
        minimum_liquidation_distance=Decimal("0.1"),
        maximum_recovery_debt=Decimal("100"),
        maximum_projected_stop_loss=Decimal("10"),
        maximum_realized_cycle_loss=Decimal("200"),
        maximum_daily_realized_loss=Decimal("500"),
        maximum_entries_per_level=2,
        maximum_active_levels=1,
        maximum_order_requests_per_minute=10,
        maximum_reconciliation_failures=3,
    )


def proposal() -> TradeProposal:
    return TradeProposal(
        symbol="ETHUSDT",
        side="Sell",
        quantity=Decimal("0.01"),
        price=Decimal("3000"),
        notional=Decimal("30"),
        projected_stop_loss=Decimal("0.045"),
        opens_new_level=True,
    )


def state() -> RiskState:
    return RiskState(
        current_perp_quantity=Decimal("0"),
        current_perp_notional=Decimal("0"),
        post_trade_margin_usage=Decimal("0.1"),
        post_trade_liquidation_distance=Decimal("0.5"),
        confirmed_recovery_debt=Decimal("0"),
        realized_cycle_loss=Decimal("0"),
        daily_realized_loss=Decimal("0"),
        entries_for_level=0,
        active_levels=0,
        order_requests_last_minute=0,
        consecutive_reconciliation_failures=0,
        market_data_fresh=True,
        reconciliation_succeeded=True,
    )


def test_approved_risk_never_silently_changes_quantity() -> None:
    decision = RiskEngine().evaluate(proposal(), state(), limits())

    assert decision.approved
    assert decision.approved_quantity == Decimal("0.01")
    assert decision.reasons == ()
    assert not decision.requires_operator_ack


@pytest.mark.parametrize(
    ("changed_state", "changed_proposal", "reason"),
    [
        (
            replace(state(), current_perp_quantity=Decimal("0.995")),
            proposal(),
            "maximum perpetual quantity exceeded",
        ),
        (
            replace(state(), current_perp_notional=Decimal("4990")),
            proposal(),
            "maximum perpetual notional exceeded",
        ),
        (
            replace(state(), post_trade_margin_usage=Decimal("0.51")),
            proposal(),
            "maximum margin usage exceeded",
        ),
        (
            replace(state(), post_trade_liquidation_distance=Decimal("0.09")),
            proposal(),
            "minimum liquidation distance breached",
        ),
        (
            replace(state(), confirmed_recovery_debt=Decimal("101")),
            proposal(),
            "maximum recovery debt exceeded",
        ),
        (
            state(),
            replace(proposal(), projected_stop_loss=Decimal("11")),
            "maximum projected stop loss exceeded",
        ),
        (
            replace(state(), realized_cycle_loss=Decimal("201")),
            proposal(),
            "maximum realized cycle loss exceeded",
        ),
        (
            replace(state(), daily_realized_loss=Decimal("501")),
            proposal(),
            "maximum daily realized loss exceeded",
        ),
        (
            replace(state(), entries_for_level=2),
            proposal(),
            "maximum entries per level reached",
        ),
        (
            replace(state(), active_levels=1),
            proposal(),
            "maximum active levels reached",
        ),
        (
            replace(state(), order_requests_last_minute=10),
            proposal(),
            "maximum order request rate reached",
        ),
        (
            replace(state(), consecutive_reconciliation_failures=3),
            proposal(),
            "maximum reconciliation failures reached",
        ),
        (
            replace(state(), market_data_fresh=False),
            proposal(),
            "market data is stale",
        ),
        (
            replace(state(), reconciliation_succeeded=False),
            proposal(),
            "reconciliation has not succeeded",
        ),
    ],
)
def test_each_mandatory_limit_can_veto(
    changed_state: RiskState,
    changed_proposal: TradeProposal,
    reason: str,
) -> None:
    decision = RiskEngine().evaluate(changed_proposal, changed_state, limits())

    assert not decision.approved
    assert decision.approved_quantity is None
    assert reason in decision.reasons


def test_limits_must_be_finite_and_bounded() -> None:
    with pytest.raises(ValueError, match="finite"):
        replace(limits(), maximum_perp_notional=Decimal("Infinity"))
    with pytest.raises(ValueError, match="positive"):
        replace(limits(), maximum_entries_per_level=0)
