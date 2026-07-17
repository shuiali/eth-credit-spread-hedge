"""Build live risk inputs only from durable and exchange-authoritative data."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from eth_credit_hedge.application.demo_runtime_state import DemoRuntimeState
from eth_credit_hedge.domain.accounting.reconstruction import CombinedLedgerState
from eth_credit_hedge.domain.execution import ExchangePosition, WalletState
from eth_credit_hedge.domain.risk import RiskState


ZERO = Decimal("0")
ONE = Decimal("1")


class RuntimeRiskStateBuilder:
    def __init__(self, *, maximum_market_data_age_ms: int) -> None:
        if maximum_market_data_age_ms <= 0:
            raise ValueError("maximum market-data age must be positive")
        self._maximum_market_data_age = timedelta(
            milliseconds=maximum_market_data_age_ms
        )

    def build(
        self,
        *,
        runtime: DemoRuntimeState,
        positions: tuple[ExchangePosition, ...],
        wallet: WalletState,
        level_id: int,
        proposed_notional: Decimal,
        last_market_event_at_utc: datetime | None,
        now_utc: datetime,
        accounting: CombinedLedgerState,
    ) -> RiskState:
        now = _utc(now_utc)
        proposed = Decimal(proposed_notional)
        if not proposed.is_finite() or proposed < ZERO:
            raise ValueError("proposed notional cannot be negative")
        linear = tuple(
            position
            for position in positions
            if position.category == "linear"
            and position.symbol == "ETHUSDT"
            and position.quantity > ZERO
        )
        current_quantity = sum(
            (position.quantity for position in linear),
            ZERO,
        )
        current_notional = sum(
            (position.quantity * _position_mark(position) for position in linear),
            ZERO,
        )
        margin_usage = _margin_usage(wallet)
        if wallet.total_equity > ZERO:
            margin_usage = max(
                margin_usage,
                (current_notional + proposed) / wallet.total_equity,
            )
        else:
            margin_usage = ONE
        liquidation_distance = _liquidation_distance(linear)
        market_fresh = False
        if last_market_event_at_utc is not None:
            observed = _utc(last_market_event_at_utc)
            age = now - observed
            market_fresh = timedelta(0) <= age <= self._maximum_market_data_age
        cutoff = now - timedelta(minutes=1)
        ledger_pnl = accounting.net_combined_mark_pnl.value
        return RiskState(
            current_perp_quantity=current_quantity,
            current_perp_notional=current_notional,
            post_trade_margin_usage=margin_usage,
            post_trade_liquidation_distance=liquidation_distance,
            confirmed_recovery_debt=accounting.confirmed_recovery_debt.value,
            realized_cycle_loss=max(-ledger_pnl, ZERO),
            daily_realized_loss=max(-ledger_pnl, ZERO),
            entries_for_level=runtime.level(level_id).attempts,
            active_levels=sum(
                level.active_entry_order_link_id is not None
                for level in runtime.levels
            ),
            order_requests_last_minute=sum(
                value >= cutoff for value in runtime.order_request_times_utc
            ),
            consecutive_reconciliation_failures=(
                runtime.consecutive_reconciliation_failures
            ),
            market_data_fresh=market_fresh,
            reconciliation_succeeded=runtime.reconciliation_complete,
        )


def _margin_usage(wallet: WalletState) -> Decimal:
    available = wallet.total_available_balance
    if wallet.total_equity <= ZERO or available is None:
        return ONE
    return max(
        ZERO,
        min(ONE, (wallet.total_equity - available) / wallet.total_equity),
    )


def _liquidation_distance(
    positions: tuple[ExchangePosition, ...],
) -> Decimal:
    if not positions:
        return ONE
    distances: list[Decimal] = []
    for position in positions:
        liquidation = position.liquidation_price
        mark = _position_mark(position)
        if liquidation is None or mark <= ZERO:
            return ZERO
        if position.side == "Sell":
            distance = (liquidation - mark) / mark
        else:
            distance = (mark - liquidation) / mark
        distances.append(max(distance, ZERO))
    return min(distances)


def _position_mark(position: ExchangePosition) -> Decimal:
    value = position.mark_price or position.average_price
    if value is None:
        raise ValueError("non-flat position requires a price for risk")
    return value


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("risk timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


__all__ = ["RuntimeRiskStateBuilder"]
