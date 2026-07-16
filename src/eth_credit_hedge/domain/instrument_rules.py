"""Decimal quantization, order validation, and post-rounding risk amounts."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from enum import Enum

from eth_credit_hedge.domain.instruments import InstrumentSpec, OrderSide
from eth_credit_hedge.domain.strategy_math.sizing import (
    zero_cost_directional_profit_and_loss_per_unit,
)
from eth_credit_hedge.domain.strategy_math.units import Price


ZERO = Decimal("0")


def _decimal(value: Decimal, field_name: str) -> Decimal:
    try:
        normalized = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be finite") from exc
    if not normalized.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return normalized


def _quantize_step(value: Decimal, step: Decimal, rounding: str) -> Decimal:
    normalized_value = _decimal(value, "value")
    normalized_step = _decimal(step, "step")
    if normalized_step <= ZERO:
        raise ValueError("step must be positive")
    units = (normalized_value / normalized_step).to_integral_value(rounding=rounding)
    return units * normalized_step


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    return _quantize_step(value, step, ROUND_FLOOR)


def ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    return _quantize_step(value, step, ROUND_CEILING)


def nearest_to_step(value: Decimal, step: Decimal) -> Decimal:
    return _quantize_step(value, step, ROUND_HALF_UP)


class PriceQuantizationPolicy(str, Enum):
    PASSIVE = "PASSIVE"
    AGGRESSIVE = "AGGRESSIVE"


def quantize_limit_price(
    value: Decimal,
    tick_size: Decimal,
    *,
    side: OrderSide,
    policy: PriceQuantizationPolicy,
) -> Decimal:
    if side not in ("Buy", "Sell"):
        raise ValueError("side must be Buy or Sell")
    normalized_policy = PriceQuantizationPolicy(policy)
    round_up = (side == "Sell") == (
        normalized_policy is PriceQuantizationPolicy.PASSIVE
    )
    quantizer = ceil_to_step if round_up else floor_to_step
    return quantizer(value, tick_size)


@dataclass(frozen=True, slots=True)
class OrderValidationResult:
    accepted: bool
    normalized_price: Decimal | None
    normalized_quantity: Decimal
    errors: tuple[str, ...]
    warnings: tuple[str, ...]


def normalize_and_validate_order(
    instrument: InstrumentSpec,
    *,
    side: OrderSide,
    quantity: Decimal,
    price: Decimal | None,
    is_market: bool = False,
    reference_price: Decimal | None = None,
    price_policy: PriceQuantizationPolicy = PriceQuantizationPolicy.PASSIVE,
) -> OrderValidationResult:
    if side not in ("Buy", "Sell"):
        raise ValueError("side must be Buy or Sell")
    requested_quantity = _decimal(quantity, "quantity")
    normalized_quantity = floor_to_step(
        requested_quantity,
        instrument.lot_size_filter.qty_step,
    )
    errors: list[str] = []
    warnings: list[str] = []
    if normalized_quantity != requested_quantity:
        warnings.append("quantity rounded down to instrument step")

    normalized_price: Decimal | None = None
    if is_market:
        if price is not None:
            warnings.append("price ignored for market order")
    elif price is None:
        errors.append("limit order requires price")
    else:
        requested_price = _decimal(price, "price")
        normalized_price = quantize_limit_price(
            requested_price,
            instrument.price_filter.tick_size,
            side=side,
            policy=price_policy,
        )
        if normalized_price != requested_price:
            warnings.append("price rounded to instrument tick")

    if instrument.status != "Trading":
        errors.append("instrument is not Trading")
    lot_filter = instrument.lot_size_filter
    if normalized_quantity < lot_filter.min_order_qty:
        errors.append("quantity is below instrument minimum")
    quantity_maximum = (
        lot_filter.max_market_order_qty
        if is_market and lot_filter.max_market_order_qty is not None
        else lot_filter.max_order_qty
    )
    if normalized_quantity > quantity_maximum:
        errors.append("quantity exceeds instrument maximum")

    price_filter = instrument.price_filter
    if normalized_price is not None:
        if (
            price_filter.min_price is not None
            and normalized_price < price_filter.min_price
        ):
            errors.append("price is below instrument minimum")
        if (
            price_filter.max_price is not None
            and normalized_price > price_filter.max_price
        ):
            errors.append("price exceeds instrument maximum")

    notional_price = normalized_price
    if is_market and reference_price is not None:
        notional_price = _decimal(reference_price, "reference price")
    if lot_filter.min_notional is not None:
        if notional_price is None:
            errors.append("reference price is required for minimum notional")
        else:
            notional = (
                normalized_quantity
                * notional_price
                * instrument.contract_multiplier
            )
            if notional < lot_filter.min_notional:
                errors.append("notional is below instrument minimum")

    return OrderValidationResult(
        accepted=not errors,
        normalized_price=normalized_price,
        normalized_quantity=normalized_quantity,
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


@dataclass(frozen=True, slots=True)
class QuantizedRiskResult:
    accepted: bool
    quantity: Decimal
    entry_price: Decimal
    take_profit_price: Decimal
    stop_price: Decimal
    notional: Decimal
    tp_profit: Decimal
    projected_stop_loss: Decimal
    errors: tuple[str, ...]


def recalculate_quantized_risk(
    validation: OrderValidationResult,
    instrument: InstrumentSpec,
    *,
    entry_side: OrderSide,
    take_profit_price: Decimal,
    stop_price: Decimal,
    maximum_notional: Decimal,
    maximum_projected_stop_loss: Decimal,
) -> QuantizedRiskResult:
    if entry_side not in ("Buy", "Sell"):
        raise ValueError("entry side must be Buy or Sell")
    if validation.normalized_price is None:
        raise ValueError("normalized entry price is required for risk calculation")
    entry_price = validation.normalized_price
    tick_size = instrument.price_filter.tick_size
    exit_side: OrderSide = "Sell" if entry_side == "Buy" else "Buy"
    normalized_tp = quantize_limit_price(
        take_profit_price,
        tick_size,
        side=exit_side,
        policy=PriceQuantizationPolicy.PASSIVE,
    )
    normalized_stop = quantize_limit_price(
        stop_price,
        tick_size,
        side=exit_side,
        policy=PriceQuantizationPolicy.AGGRESSIVE,
    )
    quantity = validation.normalized_quantity
    multiplier = instrument.contract_multiplier
    notional = entry_price * quantity * multiplier
    if entry_side == "Buy":
        profit_distance = normalized_tp - entry_price
        stop_distance = entry_price - normalized_stop
    else:
        profit_distance = entry_price - normalized_tp
        stop_distance = normalized_stop - entry_price
    errors = list(validation.errors)
    if profit_distance <= ZERO:
        errors.append("normalized take profit is not profitable")
    if stop_distance <= ZERO:
        errors.append("normalized stop is not adverse")
    if multiplier != Decimal("1"):
        raise ValueError("cost-aware ETH sizing requires contract multiplier 1")
    profit_per_unit = ZERO
    stop_loss_per_unit = ZERO
    if profit_distance > ZERO and stop_distance > ZERO:
        net_tp, net_stop = zero_cost_directional_profit_and_loss_per_unit(
            entry_price=Price(entry_price),
            tp_price=Price(normalized_tp),
            stop_price=Price(normalized_stop),
            side=entry_side,
        )
        profit_per_unit = net_tp.value
        stop_loss_per_unit = net_stop.value
    tp_profit = profit_per_unit * quantity
    projected_stop_loss = stop_loss_per_unit * quantity
    if notional > _decimal(maximum_notional, "maximum notional"):
        errors.append("normalized notional exceeds risk limit")
    if projected_stop_loss > _decimal(
        maximum_projected_stop_loss,
        "maximum projected stop loss",
    ):
        errors.append("normalized stop loss exceeds risk limit")
    return QuantizedRiskResult(
        accepted=not errors,
        quantity=quantity,
        entry_price=entry_price,
        take_profit_price=normalized_tp,
        stop_price=normalized_stop,
        notional=notional,
        tp_profit=tp_profit,
        projected_stop_loss=projected_stop_loss,
        errors=tuple(errors),
    )
