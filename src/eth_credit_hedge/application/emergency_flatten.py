"""Persistence-first emergency close for the one-level ETH short."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from eth_credit_hedge.domain.execution import (
    OrderRequestAck,
    OrderRequestKind,
    PlaceOrderRequest,
    UncertainOrderOutcomeError,
)
from eth_credit_hedge.ports.account import AccountPort
from eth_credit_hedge.ports.persistence import ExecutionPersistencePort
from eth_credit_hedge.ports.trading import TradingPort


@dataclass(frozen=True, slots=True)
class FlattenResult:
    request: PlaceOrderRequest
    acknowledgement: OrderRequestAck
    position_confirmed_flat: bool


class EmergencyFlattenService:
    """Close one proven ETH short; an acknowledgement never proves it flat."""

    def __init__(
        self,
        *,
        trading: TradingPort,
        account: AccountPort,
        store: ExecutionPersistencePort,
        clock: Callable[[], datetime],
    ) -> None:
        self._trading = trading
        self._account = account
        self._store = store
        self._clock = clock

    async def flatten_short(self, order_link_id: str) -> FlattenResult:
        positions = await self._account.get_positions("linear", "ETHUSDT")
        nonzero = tuple(position for position in positions if position.quantity > 0)
        if (
            len(nonzero) != 1
            or nonzero[0].category != "linear"
            or nonzero[0].symbol != "ETHUSDT"
            or nonzero[0].side != "Sell"
        ):
            raise RuntimeError("flatten requires exactly one ETHUSDT short position")

        request = PlaceOrderRequest(
            category="linear",
            symbol="ETHUSDT",
            side="Buy",
            order_type="Market",
            quantity=nonzero[0].quantity,
            order_link_id=order_link_id,
            reduce_only=True,
            position_idx=0,
        )
        await self._store.persist_order_intent(request, self._clock())
        try:
            acknowledgement = await self._trading.place_order(request)
        except UncertainOrderOutcomeError:
            discovered = await self._trading.get_order_by_link_id(
                request.category,
                request.symbol,
                request.order_link_id,
            )
            if discovered is None:
                raise
            acknowledgement = OrderRequestAck(
                request_kind=OrderRequestKind.PLACE,
                order_id=discovered.order_id,
                order_link_id=discovered.order_link_id,
                acknowledged_at=self._clock(),
            )
        await self._store.record_acknowledgement(acknowledgement)
        return FlattenResult(
            request=request,
            acknowledgement=acknowledgement,
            position_confirmed_flat=False,
        )

    async def confirm_flattened(self) -> bool:
        positions = await self._account.get_positions("linear", "ETHUSDT")
        return not any(position.quantity > 0 for position in positions)
