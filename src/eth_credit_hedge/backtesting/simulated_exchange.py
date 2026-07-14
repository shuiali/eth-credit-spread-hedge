"""Seeded event-driven exchange implementing live trading/account/data ports."""

from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from typing import TypeVar

from eth_credit_hedge.backtesting.fault_injection import FaultInjector
from eth_credit_hedge.domain.client_order_ids import (
    ClientOrderId,
    ClientOrderRole,
)
from eth_credit_hedge.domain.execution import (
    AmendOrderRequest,
    CancelOrderRequest,
    ExchangeOrder,
    ExchangePosition,
    ExecutionUpdate,
    OrderRequestAck,
    OrderRequestKind,
    PlaceOrderRequest,
    UncertainOrderOutcomeError,
    WalletBalance,
    WalletState,
)
from eth_credit_hedge.domain.instruments import InstrumentSpec, OptionMarketQuote
from eth_credit_hedge.domain.journal import canonical_json
from eth_credit_hedge.domain.market_data import (
    OrderBookEvent,
    OrderBookSnapshot,
    TickerEvent,
    TradeEvent,
)


ZERO = Decimal("0")
ONE = Decimal("1")
BPS = Decimal("10000")
_ACTIVE_STATUSES = {"New", "Untriggered", "PartiallyFilled"}
_StreamEvent = TypeVar("_StreamEvent")


def _decimal(value: Decimal, field_name: str) -> Decimal:
    try:
        normalized = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be finite") from exc
    if not normalized.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return normalized


def _probability(value: Decimal, field_name: str) -> Decimal:
    normalized = _decimal(value, field_name)
    if not ZERO <= normalized <= ONE:
        raise ValueError(f"{field_name} must be between zero and one")
    return normalized


@dataclass(frozen=True, slots=True)
class ExecutionModelConfig:
    acknowledgement_delay_ms: int
    visibility_delay_ms: int
    fill_delay_ms: int
    partial_fill_probability: Decimal
    rejection_probability: Decimal
    maker_fee_rate: Decimal
    taker_fee_rate: Decimal
    stop_slippage_bps: Decimal
    entry_slippage_bps: Decimal
    duplicate_execution_probability: Decimal = ZERO
    reorder_probability: Decimal = ZERO
    uncertain_ack_probability: Decimal = ZERO
    partial_fill_fraction: Decimal = Decimal("0.5")
    perp_spread_bps: Decimal = ZERO

    def __post_init__(self) -> None:
        for field_name in (
            "acknowledgement_delay_ms",
            "visibility_delay_ms",
            "fill_delay_ms",
        ):
            value = getattr(self, field_name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{field_name.replace('_', ' ')} cannot be negative")
        for field_name in (
            "partial_fill_probability",
            "rejection_probability",
            "duplicate_execution_probability",
            "reorder_probability",
            "uncertain_ack_probability",
        ):
            object.__setattr__(
                self,
                field_name,
                _probability(getattr(self, field_name), field_name.replace("_", " ")),
            )
        fraction = _probability(self.partial_fill_fraction, "partial fill fraction")
        if fraction in (ZERO, ONE):
            raise ValueError("partial fill fraction must be strictly between zero and one")
        object.__setattr__(self, "partial_fill_fraction", fraction)
        for field_name in (
            "maker_fee_rate",
            "taker_fee_rate",
            "stop_slippage_bps",
            "entry_slippage_bps",
            "perp_spread_bps",
        ):
            value = _decimal(getattr(self, field_name), field_name.replace("_", " "))
            if value < ZERO:
                raise ValueError(f"{field_name.replace('_', ' ')} cannot be negative")
            object.__setattr__(self, field_name, value)


class SimulatedOrderRejectedError(RuntimeError):
    """A seeded known rejection before an order is accepted."""


@dataclass(frozen=True, slots=True)
class SimulatedEvent:
    sequence: int
    event_type: str
    elapsed_ms: int
    payload: dict[str, object]

    def to_json(self) -> str:
        return canonical_json(
            {
                "sequence": self.sequence,
                "event_type": self.event_type,
                "elapsed_ms": self.elapsed_ms,
                "payload": self.payload,
            }
        )


@dataclass(frozen=True, slots=True)
class SimulatedExecutionCost:
    execution_id: str
    order_link_id: str
    trigger_price: Decimal | None
    first_available_price: Decimal
    fill_price: Decimal
    gap_slippage: Decimal
    spread_cost: Decimal
    model_slippage: Decimal
    fee: Decimal


@dataclass(frozen=True, slots=True)
class SimulatedMetrics:
    mode: str
    entry_fees: Decimal
    exit_fees: Decimal
    tp_fees: Decimal
    stop_fees: Decimal
    funding: Decimal
    gross_trading_pnl: Decimal
    net_hedge_pnl: Decimal
    entry_slippage: Decimal
    stop_slippage: Decimal
    spread_cost: Decimal
    rejection_count: int
    partial_fill_count: int
    duplicate_event_count: int
    maximum_stale_duration_ms: int
    public_disconnect_count: int
    maximum_unprotected_ms: int


@dataclass(frozen=True, slots=True)
class ReplayedSimulatedFinancials:
    gross_trading_pnl: Decimal
    entry_fees: Decimal
    tp_fees: Decimal
    stop_fees: Decimal
    funding: Decimal
    net_hedge_pnl: Decimal


def _payload_decimal(payload: dict[str, object], field_name: str) -> Decimal:
    if field_name not in payload:
        raise ValueError(f"simulated event is missing {field_name}")
    try:
        value = Decimal(str(payload[field_name]))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"simulated event {field_name} must be finite") from exc
    if not value.is_finite():
        raise ValueError(f"simulated event {field_name} must be finite")
    return value


def replay_simulated_financials(
    events: tuple[SimulatedEvent, ...],
) -> ReplayedSimulatedFinancials:
    """Rebuild hedge cash accounting solely from the immutable event ledger."""

    position_quantity = ZERO
    position_average = ZERO
    gross_pnl = ZERO
    entry_fees = ZERO
    tp_fees = ZERO
    stop_fees = ZERO
    funding = ZERO
    for event in events:
        if event.event_type == "FUNDING_APPLIED":
            funding += _payload_decimal(event.payload, "payment")
            continue
        if event.event_type != "EXECUTION":
            continue
        price = _payload_decimal(event.payload, "price")
        quantity = _payload_decimal(event.payload, "quantity")
        fee = _payload_decimal(event.payload, "fee")
        side = event.payload.get("side")
        role = event.payload.get("role")
        if side == "Sell":
            total_notional = position_average * position_quantity + price * quantity
            position_quantity += quantity
            position_average = total_notional / position_quantity
            entry_fees += fee
        elif side == "Buy":
            closed = min(quantity, position_quantity)
            gross_pnl += (position_average - price) * closed
            position_quantity -= closed
            if position_quantity == ZERO:
                position_average = ZERO
            if role == ClientOrderRole.HEDGE_TP.value:
                tp_fees += fee
            elif role == ClientOrderRole.HEDGE_STOP.value:
                stop_fees += fee
        else:
            raise ValueError("simulated execution side must be Buy or Sell")
    total_fees = entry_fees + tp_fees + stop_fees
    return ReplayedSimulatedFinancials(
        gross_trading_pnl=gross_pnl,
        entry_fees=entry_fees,
        tp_fees=tp_fees,
        stop_fees=stop_fees,
        funding=funding,
        net_hedge_pnl=gross_pnl + funding - total_fees,
    )


@dataclass(slots=True)
class _SimulatedOrder:
    request: PlaceOrderRequest
    order_id: str
    created_ms: int
    visible_ms: int
    fill_ms: int
    status: str
    filled_quantity: Decimal = ZERO
    fill_notional: Decimal = ZERO
    partial_planned: bool = False
    partial_done: bool = False
    updated_ms: int = 0


class SimulatedExchange:
    """Virtual-time adapter; no wall-clock sleeps and all randomness is seeded."""

    def __init__(
        self,
        *,
        instrument: InstrumentSpec,
        initial_price: Decimal,
        config: ExecutionModelConfig,
        seed: int,
        start_time_utc: datetime,
        option_quotes: tuple[OptionMarketQuote, ...] = (),
        initial_wallet_balance: Decimal = Decimal("100000"),
        fault_injector: FaultInjector | None = None,
    ) -> None:
        if instrument.category != "linear":
            raise ValueError("simulated exchange requires a linear instrument")
        if start_time_utc.tzinfo is None or start_time_utc.utcoffset() is None:
            raise ValueError("simulation start time must be timezone-aware")
        self.instrument = instrument
        self.config = config
        self.seed = seed
        self._rng = random.Random(seed)
        self._start_time = start_time_utc.astimezone(timezone.utc)
        self._now_ms = 0
        self._market_price = _decimal(initial_price, "initial price")
        if self._market_price <= ZERO:
            raise ValueError("initial price must be positive")
        self._option_quotes = tuple(option_quotes)
        self._fault_injector = fault_injector
        self._wallet_balance = _decimal(initial_wallet_balance, "wallet balance")
        self._orders: dict[str, _SimulatedOrder] = {}
        self._executions: list[ExecutionUpdate] = []
        self._delivery_queue: list[ExecutionUpdate] = []
        self._event_log: list[SimulatedEvent] = []
        self._costs: list[SimulatedExecutionCost] = []
        self._trigger_prices: dict[str, Decimal] = {}
        self._order_sequence = 0
        self._execution_sequence = 0
        self._market_sequence = 0
        self._position_quantity = ZERO
        self._position_average_price = ZERO
        self._realized_pnl = ZERO
        self._funding = ZERO
        self._entry_fees = ZERO
        self._exit_fees = ZERO
        self._tp_fees = ZERO
        self._stop_fees = ZERO
        self._entry_slippage = ZERO
        self._stop_slippage = ZERO
        self._spread_cost = ZERO
        self._rejection_count = 0
        self._partial_fill_count = 0
        self._duplicate_event_count = 0
        self._private_connected = True
        self._public_connected = True
        self._public_disconnected_since_ms: int | None = None
        self._maximum_stale_duration_ms = 0
        self._public_disconnect_count = 0
        self._latest_published_price = self._market_price
        self._unprotected_since_ms: int | None = None
        self._maximum_unprotected_ms = 0
        self._ticker_queue: asyncio.Queue[TickerEvent] = asyncio.Queue()
        self._trade_queue: asyncio.Queue[TradeEvent] = asyncio.Queue()
        self._book_queue: asyncio.Queue[OrderBookEvent] = asyncio.Queue()

    @property
    def event_log(self) -> tuple[SimulatedEvent, ...]:
        return tuple(self._event_log)

    @property
    def current_time_utc(self) -> datetime:
        return self._timestamp()

    @property
    def latest_published_price(self) -> Decimal:
        return self._latest_published_price

    @property
    def execution_costs(self) -> tuple[SimulatedExecutionCost, ...]:
        return tuple(self._costs)

    @property
    def event_log_digest(self) -> str:
        encoded = "\n".join(event.to_json() for event in self._event_log).encode()
        return hashlib.sha256(encoded).hexdigest()

    @property
    def metrics(self) -> SimulatedMetrics:
        return SimulatedMetrics(
            mode="SIMULATED",
            entry_fees=self._entry_fees,
            exit_fees=self._exit_fees,
            tp_fees=self._tp_fees,
            stop_fees=self._stop_fees,
            funding=self._funding,
            gross_trading_pnl=self._realized_pnl,
            net_hedge_pnl=(
                self._realized_pnl
                + self._funding
                - self._entry_fees
                - self._exit_fees
            ),
            entry_slippage=self._entry_slippage,
            stop_slippage=self._stop_slippage,
            spread_cost=self._spread_cost,
            rejection_count=self._rejection_count,
            partial_fill_count=self._partial_fill_count,
            duplicate_event_count=self._duplicate_event_count,
            maximum_stale_duration_ms=self._current_maximum_stale_duration(),
            public_disconnect_count=self._public_disconnect_count,
            maximum_unprotected_ms=self._current_maximum_unprotected(),
        )

    def register_trigger_price(self, order_link_id: str, price: Decimal) -> None:
        self._trigger_prices[order_link_id] = _decimal(price, "trigger price")

    def advance_time(self, elapsed_ms: int) -> None:
        if type(elapsed_ms) is not int or elapsed_ms < 0:
            raise ValueError("elapsed milliseconds cannot be negative")
        self._now_ms += elapsed_ms
        self._maximum_stale_duration_ms = self._current_maximum_stale_duration()
        self._maximum_unprotected_ms = self._current_maximum_unprotected()
        self._process_orders()

    def advance_market(self, price: Decimal, *, elapsed_ms: int) -> None:
        normalized = _decimal(price, "market price")
        if normalized <= ZERO:
            raise ValueError("market price must be positive")
        self._market_price = normalized
        self._market_sequence += 1
        self._publish_market_events()
        self.advance_time(elapsed_ms)

    def set_private_connected(self, connected: bool) -> None:
        if type(connected) is not bool:
            raise ValueError("private connection state must be boolean")
        self._private_connected = connected
        self._record("PRIVATE_CONNECTION", {"connected": connected})

    def set_public_connected(self, connected: bool) -> None:
        if type(connected) is not bool:
            raise ValueError("public connection state must be boolean")
        if connected == self._public_connected:
            return
        if connected:
            self._maximum_stale_duration_ms = self._current_maximum_stale_duration()
            self._public_disconnected_since_ms = None
        else:
            self._public_disconnected_since_ms = self._now_ms
            self._public_disconnect_count += 1
        self._public_connected = connected
        self._record("PUBLIC_CONNECTION", {"connected": connected})

    def drain_execution_events(self) -> tuple[ExecutionUpdate, ...]:
        if not self._private_connected:
            return ()
        events = list(self._delivery_queue)
        self._delivery_queue.clear()
        if events and self._draw(self.config.reorder_probability):
            events.reverse()
            self._record("EXECUTIONS_REORDERED", {"count": len(events)})
        return tuple(events)

    def apply_funding(self, rate: Decimal) -> Decimal:
        normalized_rate = _decimal(rate, "funding rate")
        payment = self._position_quantity * self._market_price * normalized_rate
        self._funding += payment
        self._record("FUNDING_APPLIED", {"rate": rate, "payment": payment})
        return payment

    async def place_order(self, request: PlaceOrderRequest) -> OrderRequestAck:
        self._checkpoint("before_place_order")
        if request.symbol != self.instrument.symbol:
            raise ValueError("simulated order symbol does not match instrument")
        if request.order_link_id in self._orders:
            raise ValueError("simulated client order ID already exists")
        if self._draw(self.config.rejection_probability):
            self._rejection_count += 1
            self._record("ORDER_REJECTED", {"order_link_id": request.order_link_id})
            raise SimulatedOrderRejectedError(
                f"seeded rejection for {request.order_link_id}"
            )
        self._order_sequence += 1
        order = _SimulatedOrder(
            request=request,
            order_id=f"SIM-ORDER-{self._order_sequence:06d}",
            created_ms=self._now_ms,
            visible_ms=self._now_ms + self.config.visibility_delay_ms,
            fill_ms=self._now_ms + self.config.fill_delay_ms,
            status="Untriggered" if request.trigger_price is not None else "New",
            partial_planned=self._draw(self.config.partial_fill_probability),
            updated_ms=self._now_ms,
        )
        self._orders[request.order_link_id] = order
        self._update_protection_state()
        self._record(
            "ORDER_ACCEPTED",
            {
                "order_id": order.order_id,
                "order_link_id": request.order_link_id,
                "visible_ms": order.visible_ms,
                "fill_ms": order.fill_ms,
            },
        )
        self._checkpoint("after_order_accepted")
        self.advance_time(self.config.acknowledgement_delay_ms)
        if self._draw(self.config.uncertain_ack_probability):
            self._record("ACK_UNCERTAIN", {"order_link_id": request.order_link_id})
            raise UncertainOrderOutcomeError(
                order_link_id=request.order_link_id,
                operation="simulated place order",
            )
        return OrderRequestAck(
            request_kind=OrderRequestKind.PLACE,
            order_id=order.order_id,
            order_link_id=request.order_link_id,
            acknowledged_at=self._timestamp(),
        )

    async def amend_order(self, request: AmendOrderRequest) -> OrderRequestAck:
        order = self._required_order(request.order_link_id)
        if order.status not in _ACTIVE_STATUSES:
            raise ValueError("only active simulated orders can be amended")
        order.request = replace(
            order.request,
            quantity=(
                order.request.quantity
                if request.quantity is None
                else request.quantity
            ),
            price=order.request.price if request.price is None else request.price,
            trigger_price=(
                order.request.trigger_price
                if request.trigger_price is None
                else request.trigger_price
            ),
        )
        order.updated_ms = self._now_ms
        self._record("ORDER_AMENDED", {"order_link_id": request.order_link_id})
        return OrderRequestAck(
            request_kind=OrderRequestKind.AMEND,
            order_id=order.order_id,
            order_link_id=request.order_link_id,
            acknowledged_at=self._timestamp(),
        )

    async def cancel_order(self, request: CancelOrderRequest) -> OrderRequestAck:
        order = self._required_order(request.order_link_id)
        if order.status in _ACTIVE_STATUSES:
            order.status = "Cancelled"
            order.updated_ms = self._now_ms
        self._update_protection_state()
        self._record("ORDER_CANCELLED", {"order_link_id": request.order_link_id})
        return OrderRequestAck(
            request_kind=OrderRequestKind.CANCEL,
            order_id=order.order_id,
            order_link_id=request.order_link_id,
            acknowledged_at=self._timestamp(),
        )

    async def cancel_all(
        self,
        category: str,
        symbol: str | None = None,
    ) -> None:
        for order in self._orders.values():
            if order.request.category != category:
                continue
            if symbol is not None and order.request.symbol != symbol:
                continue
            if order.status in _ACTIVE_STATUSES:
                order.status = "Cancelled"
                order.updated_ms = self._now_ms
        self._update_protection_state()
        self._record("ALL_ORDERS_CANCELLED", {"category": category, "symbol": symbol})

    async def get_open_orders(
        self,
        category: str,
        symbol: str | None = None,
    ) -> tuple[ExchangeOrder, ...]:
        return tuple(
            self._exchange_order(order)
            for order in self._orders.values()
            if self._visible(order)
            and order.status in _ACTIVE_STATUSES
            and order.request.category == category
            and (symbol is None or order.request.symbol == symbol)
        )

    async def get_order_history(
        self,
        category: str,
        symbol: str | None = None,
    ) -> tuple[ExchangeOrder, ...]:
        return tuple(
            self._exchange_order(order)
            for order in self._orders.values()
            if self._visible(order)
            and order.request.category == category
            and (symbol is None or order.request.symbol == symbol)
        )

    async def get_order_by_link_id(
        self,
        category: str,
        symbol: str,
        order_link_id: str,
    ) -> ExchangeOrder | None:
        order = self._orders.get(order_link_id)
        if (
            order is None
            or not self._visible(order)
            or order.request.category != category
            or order.request.symbol != symbol
        ):
            return None
        return self._exchange_order(order)

    async def get_execution_history(
        self,
        category: str,
        symbol: str | None = None,
        order_link_id: str | None = None,
    ) -> tuple[ExecutionUpdate, ...]:
        del category
        return tuple(
            execution
            for execution in self._executions
            if (symbol is None or execution.symbol == symbol)
            and (
                order_link_id is None
                or execution.order_link_id == order_link_id
            )
        )

    async def get_positions(
        self,
        category: str,
        symbol: str | None = None,
    ) -> tuple[ExchangePosition, ...]:
        if (
            category != "linear"
            or (symbol is not None and symbol != self.instrument.symbol)
            or self._position_quantity == ZERO
        ):
            return ()
        return (
            ExchangePosition(
                category="linear",
                symbol=self.instrument.symbol,
                side="Sell",
                quantity=self._position_quantity,
                average_price=self._position_average_price,
                mark_price=self._market_price,
                unrealized_pnl=(
                    self._position_average_price - self._market_price
                )
                * self._position_quantity,
                updated_at=self._timestamp(),
            ),
        )

    async def get_wallet_state(self) -> WalletState:
        fees = self._entry_fees + self._exit_fees
        equity = self._wallet_balance + self._realized_pnl + self._funding - fees
        return WalletState(
            account_type="SIMULATED",
            total_equity=equity,
            total_wallet_balance=equity,
            total_available_balance=equity,
            balances=(
                WalletBalance(
                    coin="USDT",
                    equity=equity,
                    wallet_balance=equity,
                    available_balance=equity,
                    unrealized_pnl=(
                        self._position_average_price - self._market_price
                    )
                    * self._position_quantity,
                ),
            ),
            updated_at=self._timestamp(),
        )

    async def get_instrument(self, symbol: str) -> InstrumentSpec:
        if symbol != self.instrument.symbol:
            raise ValueError("unknown simulated instrument")
        return self.instrument

    async def get_option_chain(
        self,
        base_coin: str,
    ) -> tuple[OptionMarketQuote, ...]:
        return tuple(
            quote for quote in self._option_quotes if quote.symbol.startswith(base_coin)
        )

    async def get_orderbook_snapshot(
        self,
        symbol: str,
        depth: int,
    ) -> OrderBookSnapshot:
        if symbol != self.instrument.symbol or depth <= 0:
            raise ValueError("invalid simulated order-book request")
        bid, ask = self._bid_ask()
        return OrderBookSnapshot(
            symbol=symbol,
            bids=((bid, Decimal("100")),),
            asks=((ask, Decimal("100")),),
            update_id=self._market_sequence,
            sequence=self._market_sequence,
            timestamp_utc=self._timestamp(),
            connection_generation=1,
            raw_payload_hash=self._market_hash("book"),
        )

    def stream_ticker(self, symbol: str) -> AsyncIterator[TickerEvent]:
        if symbol != self.instrument.symbol:
            raise ValueError("unknown simulated ticker symbol")
        return self._stream_queue(self._ticker_queue)

    def stream_orderbook(
        self,
        symbol: str,
        depth: int,
    ) -> AsyncIterator[OrderBookEvent]:
        if symbol != self.instrument.symbol or depth <= 0:
            raise ValueError("invalid simulated order-book stream")
        return self._stream_queue(self._book_queue)

    def stream_trades(self, symbol: str) -> AsyncIterator[TradeEvent]:
        if symbol != self.instrument.symbol:
            raise ValueError("unknown simulated trade symbol")
        return self._stream_queue(self._trade_queue)

    async def _stream_queue(
        self,
        queue: asyncio.Queue[_StreamEvent],
    ) -> AsyncIterator[_StreamEvent]:
        while True:
            yield await queue.get()

    def _process_orders(self) -> None:
        for order in self._orders.values():
            if order.status not in _ACTIVE_STATUSES or self._now_ms < order.fill_ms:
                continue
            if not self._eligible(order):
                if (
                    order.request.time_in_force == "IOC"
                    and order.request.trigger_price is None
                ):
                    order.status = "Cancelled"
                    order.updated_ms = self._now_ms
                continue
            self._fill_order(order)

    def _eligible(self, order: _SimulatedOrder) -> bool:
        request = order.request
        if request.trigger_price is not None:
            triggered = (
                self._market_price >= request.trigger_price
                if request.trigger_direction == 1
                else self._market_price <= request.trigger_price
            )
            if not triggered:
                return False
        if request.order_type == "Market":
            return True
        if request.price is None:
            raise AssertionError("limit order must have a price")
        bid, ask = self._bid_ask()
        return bid >= request.price if request.side == "Sell" else ask <= request.price

    def _fill_order(self, order: _SimulatedOrder) -> None:
        remaining = order.request.quantity - order.filled_quantity
        if order.request.reduce_only:
            remaining = min(remaining, self._position_quantity)
        if remaining <= ZERO:
            order.status = "Cancelled"
            order.updated_ms = self._now_ms
            return
        fill_quantity = remaining
        if order.partial_planned and not order.partial_done:
            step = self.instrument.lot_size_filter.qty_step
            proposed = (
                order.request.quantity * self.config.partial_fill_fraction / step
            ).to_integral_value(rounding=ROUND_FLOOR) * step
            if ZERO < proposed < remaining:
                fill_quantity = proposed
                order.partial_done = True
                self._partial_fill_count += 1
        first_available, fill_price, role = self._execution_price(order.request)
        is_maker = order.request.order_type == "Limit"
        fee_rate = (
            self.config.maker_fee_rate if is_maker else self.config.taker_fee_rate
        )
        fee = fill_price * fill_quantity * fee_rate
        self._execution_sequence += 1
        execution = ExecutionUpdate(
            execution_id=f"SIM-EXEC-{self._execution_sequence:06d}",
            order_id=order.order_id,
            order_link_id=order.request.order_link_id,
            symbol=order.request.symbol,
            side=order.request.side,
            price=fill_price,
            quantity=fill_quantity,
            fee=fee,
            is_maker=is_maker,
            executed_at=self._timestamp(),
        )
        order.filled_quantity += fill_quantity
        order.fill_notional += fill_price * fill_quantity
        order.updated_ms = self._now_ms
        order.status = (
            "Filled"
            if order.filled_quantity == order.request.quantity
            else "PartiallyFilled"
        )
        self._executions.append(execution)
        self._delivery_queue.append(execution)
        if self._draw(self.config.duplicate_execution_probability):
            self._delivery_queue.append(execution)
            self._duplicate_event_count += 1
        self._apply_position(execution, role)
        self._update_protection_state()
        self._record_cost(execution, first_available, role)
        self._record(
            "EXECUTION",
            {
                "execution_id": execution.execution_id,
                "order_link_id": execution.order_link_id,
                "price": execution.price,
                "quantity": execution.quantity,
                "fee": execution.fee,
                "side": execution.side,
                "role": role.value,
            },
        )

    def _execution_price(
        self,
        request: PlaceOrderRequest,
    ) -> tuple[Decimal, Decimal, ClientOrderRole]:
        bid, ask = self._bid_ask()
        first_available = bid if request.side == "Sell" else ask
        role = ClientOrderId.parse(request.order_link_id).role
        slippage_bps = ZERO
        if role is ClientOrderRole.HEDGE_ENTRY:
            slippage_bps = self.config.entry_slippage_bps
        elif role is ClientOrderRole.HEDGE_STOP:
            slippage_bps = self.config.stop_slippage_bps
        direction = -ONE if request.side == "Sell" else ONE
        fill_price = first_available * (
            ONE + direction * slippage_bps / BPS
        )
        return first_available, fill_price, role

    def _apply_position(
        self,
        execution: ExecutionUpdate,
        role: ClientOrderRole,
    ) -> None:
        if execution.side == "Sell":
            total_notional = (
                self._position_average_price * self._position_quantity
                + execution.price * execution.quantity
            )
            self._position_quantity += execution.quantity
            self._position_average_price = total_notional / self._position_quantity
            self._entry_fees += execution.fee
            return
        closed = min(execution.quantity, self._position_quantity)
        self._realized_pnl += (
            self._position_average_price - execution.price
        ) * closed
        self._position_quantity -= closed
        self._exit_fees += execution.fee
        if role is ClientOrderRole.HEDGE_TP:
            self._tp_fees += execution.fee
        elif role is ClientOrderRole.HEDGE_STOP:
            self._stop_fees += execution.fee
        if self._position_quantity == ZERO:
            self._position_average_price = ZERO

    def _record_cost(
        self,
        execution: ExecutionUpdate,
        first_available: Decimal,
        role: ClientOrderRole,
    ) -> None:
        trigger = self._trigger_prices.get(
            execution.order_link_id,
            self._orders[execution.order_link_id].request.trigger_price,
        )
        gap = ZERO if trigger is None else abs(execution.price - trigger)
        spread = abs(first_available - self._market_price) * execution.quantity
        model_slippage = abs(execution.price - first_available) * execution.quantity
        self._spread_cost += spread
        if role is ClientOrderRole.HEDGE_ENTRY:
            self._entry_slippage += model_slippage
        elif role is ClientOrderRole.HEDGE_STOP:
            self._stop_slippage += model_slippage
        self._costs.append(
            SimulatedExecutionCost(
                execution_id=execution.execution_id,
                order_link_id=execution.order_link_id,
                trigger_price=trigger,
                first_available_price=first_available,
                fill_price=execution.price,
                gap_slippage=gap,
                spread_cost=spread,
                model_slippage=model_slippage,
                fee=execution.fee,
            )
        )

    def _bid_ask(self) -> tuple[Decimal, Decimal]:
        spread = self.config.perp_spread_bps / BPS
        return (
            self._market_price * (ONE - spread),
            self._market_price * (ONE + spread),
        )

    def _exchange_order(self, order: _SimulatedOrder) -> ExchangeOrder:
        average = (
            None
            if order.filled_quantity == ZERO
            else order.fill_notional / order.filled_quantity
        )
        request = order.request
        return ExchangeOrder(
            category=request.category,
            order_id=order.order_id,
            order_link_id=request.order_link_id,
            symbol=request.symbol,
            status=order.status,
            side=request.side,
            order_type=request.order_type,
            price=request.price,
            quantity=request.quantity,
            cumulative_filled_quantity=order.filled_quantity,
            average_price=average,
            reduce_only=request.reduce_only,
            created_at=self._timestamp(order.created_ms),
            updated_at=self._timestamp(order.updated_ms),
            trigger_price=request.trigger_price,
            trigger_by=request.trigger_by,
            trigger_direction=request.trigger_direction,
            time_in_force=request.time_in_force,
            position_idx=request.position_idx,
        )

    def _visible(self, order: _SimulatedOrder) -> bool:
        return self._now_ms >= order.visible_ms

    def _required_order(self, order_link_id: str) -> _SimulatedOrder:
        order = self._orders.get(order_link_id)
        if order is None:
            raise ValueError("simulated order does not exist")
        return order

    def _draw(self, probability: Decimal) -> bool:
        return self._rng.random() < float(probability)

    def _timestamp(self, elapsed_ms: int | None = None) -> datetime:
        milliseconds = self._now_ms if elapsed_ms is None else elapsed_ms
        return self._start_time + timedelta(milliseconds=milliseconds)

    def _record(self, event_type: str, payload: dict[str, object]) -> None:
        self._event_log.append(
            SimulatedEvent(
                sequence=len(self._event_log) + 1,
                event_type=event_type,
                elapsed_ms=self._now_ms,
                payload=payload,
            )
        )

    def _market_hash(self, event_type: str) -> str:
        value = f"{self.seed}:{self._market_sequence}:{event_type}:{self._market_price}"
        return hashlib.sha256(value.encode()).hexdigest()

    def _publish_market_events(self) -> None:
        if not self._public_connected:
            return
        self._latest_published_price = self._market_price
        timestamp = self._timestamp()
        bid, ask = self._bid_ask()
        digest = self._market_hash("market")
        ticker = TickerEvent(
            symbol=self.instrument.symbol,
            timestamp_utc=timestamp,
            last_price=self._market_price,
            mark_price=self._market_price,
            index_price=self._market_price,
            bid_price=bid,
            ask_price=ask,
            sequence=self._market_sequence,
            connection_generation=1,
            raw_payload_hash=digest,
        )
        trade = TradeEvent(
            symbol=self.instrument.symbol,
            timestamp_utc=timestamp,
            price=self._market_price,
            size=Decimal("1"),
            side="Sell",
            trade_id=f"SIM-TRADE-{self._market_sequence:06d}",
            sequence=self._market_sequence,
            connection_generation=1,
            raw_payload_hash=digest,
        )
        book = OrderBookSnapshot(
            symbol=self.instrument.symbol,
            bids=((bid, Decimal("100")),),
            asks=((ask, Decimal("100")),),
            update_id=self._market_sequence,
            sequence=self._market_sequence,
            timestamp_utc=timestamp,
            connection_generation=1,
            raw_payload_hash=digest,
        )
        self._ticker_queue.put_nowait(ticker)
        self._trade_queue.put_nowait(trade)
        self._book_queue.put_nowait(book)

    def _checkpoint(self, name: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector.checkpoint(name)

    def _current_maximum_stale_duration(self) -> int:
        current = 0
        if self._public_disconnected_since_ms is not None:
            current = self._now_ms - self._public_disconnected_since_ms
        return max(self._maximum_stale_duration_ms, current)

    def _update_protection_state(self) -> None:
        is_unprotected = self._position_quantity > ZERO and not self._has_active_stop()
        if is_unprotected and self._unprotected_since_ms is None:
            self._unprotected_since_ms = self._now_ms
        elif not is_unprotected and self._unprotected_since_ms is not None:
            self._maximum_unprotected_ms = self._current_maximum_unprotected()
            self._unprotected_since_ms = None

    def _current_maximum_unprotected(self) -> int:
        current = 0
        if self._unprotected_since_ms is not None:
            current = self._now_ms - self._unprotected_since_ms
        return max(self._maximum_unprotected_ms, current)

    def _has_active_stop(self) -> bool:
        for order in self._orders.values():
            if order.status not in _ACTIVE_STATUSES:
                continue
            try:
                role = ClientOrderId.parse(order.request.order_link_id).role
            except ValueError:
                continue
            if role is ClientOrderRole.HEDGE_STOP:
                return True
        return False
