"""Fail-closed translation from private exchange fills to canonical M2 events."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from eth_credit_hedge.domain.accounting.events import (
    EventSource,
    ExitReason,
    HedgeExecutionRecorded,
    HedgeRole,
    OptionExecutionRecorded,
    OptionLeg,
    ReferenceType,
)
from eth_credit_hedge.domain.accounting.fills import (
    ConfirmedExecution,
    InstrumentKind,
    Side,
)
from eth_credit_hedge.domain.accounting.reconstruction import CombinedLedgerState
from eth_credit_hedge.domain.client_order_ids import ClientOrderId, ClientOrderRole
from eth_credit_hedge.domain.execution import ExecutionUpdate, ExecutionUpdateBatch
from eth_credit_hedge.domain.strategy_math.units import Money, Price, Quantity


AccountingPrivateExecution = OptionExecutionRecorded | HedgeExecutionRecorded


class PrivateExecutionClassificationError(ValueError):
    def __init__(self, execution: ExecutionUpdate, reason: str) -> None:
        self.execution = execution
        self.reason = reason
        super().__init__(
            f"cannot classify private execution {execution.execution_id}: {reason}"
        )

    def __str__(self) -> str:
        return str(self.args[0])


@dataclass(slots=True)
class _OpenHedgeLot:
    lot_id: str
    level_id: int
    attempt: int
    role: HedgeRole
    open_quantity: Decimal


class PrivateExecutionClassifier:
    """Classify only parseable strategy-owned executions; ambiguity is an error."""

    def __init__(
        self,
        *,
        cycle_id: str,
        cycle_number: int,
        strategy_instance: str,
        reference_prices: Mapping[str, Price] | None = None,
    ) -> None:
        if not cycle_id.strip():
            raise ValueError("accounting cycle ID cannot be empty")
        self.cycle_id = cycle_id
        self.cycle_number = cycle_number
        self.strategy_instance = strategy_instance
        self._reference_prices = dict(reference_prices or {})

    def classify_batch(
        self,
        batch: ExecutionUpdateBatch,
        state: CombinedLedgerState,
        *,
        source: EventSource = EventSource.PRIVATE_STREAM,
    ) -> tuple[AccountingPrivateExecution, ...]:
        open_lots = {
            lot.lot_id: _OpenHedgeLot(
                lot_id=lot.lot_id,
                level_id=lot.level_id,
                attempt=lot.attempt,
                role=lot.role,
                open_quantity=lot.open_quantity,
            )
            for lot in state.hedge.lots
            if lot.open_quantity > 0
        }
        events: list[AccountingPrivateExecution] = []
        for execution in batch.executions:
            event = self._classify(
                execution,
                batch.raw_payload_hash,
                open_lots,
                source,
            )
            events.append(event)
            if isinstance(event, HedgeExecutionRecorded):
                self._update_open_lots(event, open_lots)
        return tuple(events)

    def _classify(
        self,
        update: ExecutionUpdate,
        correlation_id: str,
        open_lots: dict[str, _OpenHedgeLot],
        source: EventSource,
    ) -> AccountingPrivateExecution:
        try:
            client_id = ClientOrderId.parse(update.order_link_id)
        except ValueError as error:
            raise PrivateExecutionClassificationError(
                update, "order link ID is not a strategy client order ID"
            ) from error
        if (
            client_id.strategy_instance != self.strategy_instance
            or client_id.cycle != self.cycle_number
        ):
            raise PrivateExecutionClassificationError(
                update, "execution belongs to a different strategy cycle"
            )
        side = Side.BUY if update.side == "Buy" else Side.SELL
        role = client_id.role
        kind = (
            InstrumentKind.OPTION
            if role in (ClientOrderRole.OPTION_LONG, ClientOrderRole.OPTION_SHORT)
            else InstrumentKind.PERPETUAL
        )
        execution = ConfirmedExecution(
            execution_id=update.execution_id,
            symbol=update.symbol,
            instrument_kind=kind,
            side=side,
            price=Price(update.price),
            quantity=Quantity(update.quantity),
            fee=Money(update.fee),
            fee_currency=_fee_currency(update.symbol),
            timestamp=update.executed_at,
            order_id=update.order_id,
            order_link_id=update.order_link_id,
        )
        metadata: dict[str, Any] = {
            "event_id": (
                f"accounting-execution:{source.value}:"
                f"{correlation_id}:{update.execution_id}"
            ),
            "event_version": 1,
            "cycle_id": self.cycle_id,
            "timestamp": update.executed_at,
            "source": source,
            "correlation_id": correlation_id,
            "level_id": client_id.level or None,
            "execution_id": update.execution_id,
            "order_id": update.order_id,
            "order_link_id": update.order_link_id,
            "symbol": update.symbol,
        }
        if role is ClientOrderRole.OPTION_LONG:
            return OptionExecutionRecorded(
                **metadata,
                execution=execution,
                leg=OptionLeg.LONG,
            )
        if role is ClientOrderRole.OPTION_SHORT:
            return OptionExecutionRecorded(
                **metadata,
                execution=execution,
                leg=OptionLeg.SHORT,
            )
        if role is ClientOrderRole.HEDGE_ENTRY:
            if side is not Side.SELL:
                raise PrivateExecutionClassificationError(
                    update, "hedge entry must be a Sell execution"
                )
            return self._hedge_event(
                metadata, execution, client_id, exit_reason=None, lot_id=None
            )
        if role in (ClientOrderRole.HEDGE_TP, ClientOrderRole.HEDGE_STOP):
            if side is not Side.BUY:
                raise PrivateExecutionClassificationError(
                    update, "hedge exit must be a Buy execution"
                )
            lot_id = _lot_id(self.cycle_id, client_id.level, client_id.attempt)
            lot = open_lots.get(lot_id)
            if lot is None or update.quantity > lot.open_quantity:
                raise PrivateExecutionClassificationError(
                    update, "hedge exit does not match an open hedge lot"
                )
            return self._hedge_event(
                metadata,
                execution,
                client_id,
                exit_reason=(
                    ExitReason.TAKE_PROFIT
                    if role is ClientOrderRole.HEDGE_TP
                    else ExitReason.STOP
                ),
                lot_id=lot_id,
            )
        if role is ClientOrderRole.EMERGENCY_CLOSE:
            if side is not Side.BUY:
                raise PrivateExecutionClassificationError(
                    update, "manual hedge close must be a Buy execution"
                )
            candidates = tuple(
                lot
                for lot in open_lots.values()
                if client_id.level == 0 or lot.level_id == client_id.level
            )
            if len(candidates) != 1:
                raise PrivateExecutionClassificationError(
                    update,
                    "manual close does not identify exactly one open hedge lot",
                )
            lot = candidates[0]
            if update.quantity > lot.open_quantity:
                raise PrivateExecutionClassificationError(
                    update, "manual close quantity exceeds the identified hedge lot"
                )
            emergency_metadata = {**metadata, "level_id": lot.level_id}
            return HedgeExecutionRecorded(
                **emergency_metadata,
                execution=execution,
                lot_id=lot.lot_id,
                attempt=lot.attempt,
                role=lot.role,
                exit_reason=ExitReason.EMERGENCY,
            )
        raise PrivateExecutionClassificationError(update, f"unsupported role {role.value}")

    @staticmethod
    def _update_open_lots(
        event: HedgeExecutionRecorded,
        open_lots: dict[str, _OpenHedgeLot],
    ) -> None:
        quantity = event.execution.quantity.value
        if event.execution.side is Side.SELL:
            lot = open_lots.get(event.lot_id)
            if lot is None:
                open_lots[event.lot_id] = _OpenHedgeLot(
                    lot_id=event.lot_id,
                    level_id=event.level_id or 0,
                    attempt=event.attempt,
                    role=event.role,
                    open_quantity=quantity,
                )
            else:
                lot.open_quantity += quantity
            return
        lot = open_lots.get(event.lot_id)
        if lot is None or quantity > lot.open_quantity:
            raise AssertionError("classified hedge close lost its open lot")
        lot.open_quantity -= quantity
        if lot.open_quantity == 0:
            del open_lots[event.lot_id]

    def _hedge_event(
        self,
        metadata: dict[str, Any],
        execution: ConfirmedExecution,
        client_id: ClientOrderId,
        *,
        exit_reason: ExitReason | None,
        lot_id: str | None,
    ) -> HedgeExecutionRecorded:
        reference = self._reference_prices.get(execution.order_link_id or "")
        return HedgeExecutionRecorded(
            **metadata,
            execution=execution,
            lot_id=lot_id or _lot_id(self.cycle_id, client_id.level, client_id.attempt),
            attempt=client_id.attempt,
            role=(
                HedgeRole.BASELINE
                if client_id.attempt == 1
                else HedgeRole.RECOVERY
            ),
            exit_reason=exit_reason,
            reference_type=ReferenceType.TRIGGER if reference is not None else None,
            reference_price=reference,
        )


def _lot_id(cycle_id: str, level_id: int, attempt: int) -> str:
    return f"{cycle_id}:L{level_id:02d}:A{attempt:02d}"


def _fee_currency(symbol: str) -> str:
    if "-" not in symbol:
        if symbol.endswith("USDT"):
            return "USDT"
        raise ValueError(f"cannot infer fee currency from symbol {symbol}")
    currency = symbol.rsplit("-", 1)[-1].upper()
    if currency not in {"USD", "USDT", "USDC"}:
        raise ValueError(f"unsupported fee currency in symbol {symbol}")
    return currency
