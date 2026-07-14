"""Instrument constraints, quantization, and post-rounding risk tests."""

from decimal import Decimal

import pytest

from eth_credit_hedge.domain.instrument_rules import (
    PriceQuantizationPolicy,
    ceil_to_step,
    floor_to_step,
    nearest_to_step,
    normalize_and_validate_order,
    quantize_limit_price,
    recalculate_quantized_risk,
)
from eth_credit_hedge.domain.instruments import (
    InstrumentSpec,
    LotSizeFilter,
    OrderSide,
    PriceFilter,
)


def make_linear_spec() -> InstrumentSpec:
    return InstrumentSpec(
        symbol="ETHUSDT",
        category="linear",
        status="Trading",
        base_coin="ETH",
        quote_coin="USDT",
        settle_coin="USDT",
        price_filter=PriceFilter(
            tick_size=Decimal("0.1"),
            min_price=Decimal("10"),
            max_price=Decimal("1000000"),
        ),
        lot_size_filter=LotSizeFilter(
            qty_step=Decimal("0.001"),
            min_order_qty=Decimal("0.001"),
            max_order_qty=Decimal("100"),
            max_market_order_qty=Decimal("50"),
            min_notional=Decimal("5"),
        ),
        contract_multiplier=Decimal("1"),
        delivery_time_utc=None,
    )


def test_step_quantization_is_decimal_exact() -> None:
    assert floor_to_step(Decimal("1.239"), Decimal("0.01")) == Decimal("1.23")
    assert ceil_to_step(Decimal("1.231"), Decimal("0.01")) == Decimal("1.24")
    assert nearest_to_step(Decimal("1.235"), Decimal("0.01")) == Decimal("1.24")


@pytest.mark.parametrize("step", [Decimal("0"), Decimal("-0.1")])
def test_quantization_rejects_non_positive_step(step: Decimal) -> None:
    with pytest.raises(ValueError, match="step must be positive"):
        floor_to_step(Decimal("1"), step)


def test_limit_price_quantization_is_side_and_policy_aware() -> None:
    tick = Decimal("0.1")

    assert quantize_limit_price(
        Decimal("3000.04"),
        tick,
        side="Buy",
        policy=PriceQuantizationPolicy.PASSIVE,
    ) == Decimal("3000.0")
    assert quantize_limit_price(
        Decimal("3000.04"),
        tick,
        side="Sell",
        policy=PriceQuantizationPolicy.PASSIVE,
    ) == Decimal("3000.1")
    assert quantize_limit_price(
        Decimal("3000.04"),
        tick,
        side="Buy",
        policy=PriceQuantizationPolicy.AGGRESSIVE,
    ) == Decimal("3000.1")
    assert quantize_limit_price(
        Decimal("3000.04"),
        tick,
        side="Sell",
        policy=PriceQuantizationPolicy.AGGRESSIVE,
    ) == Decimal("3000.0")


def test_order_validation_normalizes_every_request_before_checks() -> None:
    result = normalize_and_validate_order(
        make_linear_spec(),
        side="Sell",
        quantity=Decimal("0.0029"),
        price=Decimal("3000.04"),
        price_policy=PriceQuantizationPolicy.PASSIVE,
    )

    assert result.accepted
    assert result.normalized_quantity == Decimal("0.002")
    assert result.normalized_price == Decimal("3000.1")
    assert result.errors == ()
    assert "quantity rounded down to instrument step" in result.warnings
    assert "price rounded to instrument tick" in result.warnings


def test_order_validation_rejects_minimum_notional_after_rounding() -> None:
    result = normalize_and_validate_order(
        make_linear_spec(),
        side="Sell",
        quantity=Decimal("0.0019"),
        price=Decimal("3000.04"),
        price_policy=PriceQuantizationPolicy.PASSIVE,
    )

    assert not result.accepted
    assert result.normalized_quantity == Decimal("0.001")
    assert result.normalized_price == Decimal("3000.1")
    assert result.errors == ("notional is below instrument minimum",)


def test_order_validation_rejects_disabled_instrument_and_market_maximum() -> None:
    spec = make_linear_spec()
    disabled = InstrumentSpec(
        symbol=spec.symbol,
        category=spec.category,
        status="Settling",
        base_coin=spec.base_coin,
        quote_coin=spec.quote_coin,
        settle_coin=spec.settle_coin,
        price_filter=spec.price_filter,
        lot_size_filter=spec.lot_size_filter,
        contract_multiplier=spec.contract_multiplier,
        delivery_time_utc=spec.delivery_time_utc,
    )

    disabled_result = normalize_and_validate_order(
        disabled,
        side="Buy",
        quantity=Decimal("1"),
        price=Decimal("3000"),
    )
    market_result = normalize_and_validate_order(
        spec,
        side="Buy",
        quantity=Decimal("50.001"),
        price=None,
        is_market=True,
        reference_price=Decimal("3000"),
    )

    assert "instrument is not Trading" in disabled_result.errors
    assert "quantity exceeds instrument maximum" in market_result.errors


def test_risk_is_recalculated_from_normalized_price_and_quantity() -> None:
    spec = make_linear_spec()
    validation = normalize_and_validate_order(
        spec,
        side="Sell",
        quantity=Decimal("0.0029"),
        price=Decimal("3000.04"),
    )

    risk = recalculate_quantized_risk(
        validation,
        spec,
        entry_side="Sell",
        take_profit_price=Decimal("2900.04"),
        stop_price=Decimal("3005.06"),
        recovery_debt=Decimal("0.21"),
        maximum_notional=Decimal("10"),
        maximum_projected_stop_loss=Decimal("1"),
    )

    assert risk.accepted
    assert risk.quantity == Decimal("0.002")
    assert risk.entry_price == Decimal("3000.1")
    assert risk.take_profit_price == Decimal("2900.0")
    assert risk.stop_price == Decimal("3005.1")
    assert risk.notional == Decimal("6.0002")
    assert risk.tp_profit == Decimal("0.2002")
    assert risk.projected_stop_loss == Decimal("0.0100")
    assert risk.recovery_quantity == Decimal("0.003")
    assert risk.recovery_profit == Decimal("0.3003")

    rejected = recalculate_quantized_risk(
        validation,
        spec,
        entry_side="Sell",
        take_profit_price=Decimal("2900.04"),
        stop_price=Decimal("3005.06"),
        recovery_debt=Decimal("0"),
        maximum_notional=Decimal("6"),
        maximum_projected_stop_loss=Decimal("1"),
    )
    assert not rejected.accepted
    assert rejected.errors == ("normalized notional exceeds risk limit",)


def test_exit_prices_use_explicit_side_aware_policies() -> None:
    spec = make_linear_spec()
    validation = normalize_and_validate_order(
        spec,
        side="Buy",
        quantity=Decimal("0.01"),
        price=Decimal("3000.04"),
    )

    risk = recalculate_quantized_risk(
        validation,
        spec,
        entry_side="Buy",
        take_profit_price=Decimal("3100.06"),
        stop_price=Decimal("2995.04"),
        recovery_debt=Decimal("0"),
        maximum_notional=Decimal("100"),
        maximum_projected_stop_loss=Decimal("1"),
    )

    assert risk.entry_price == Decimal("3000.0")
    assert risk.take_profit_price == Decimal("3100.1")
    assert risk.stop_price == Decimal("2995.0")


@pytest.mark.parametrize(
    ("entry_side", "take_profit", "stop"),
    [
        ("Sell", Decimal("3100"), Decimal("2900")),
        ("Buy", Decimal("2900"), Decimal("3100")),
    ],
)
def test_risk_rejects_exit_prices_on_the_wrong_side_of_entry(
    entry_side: OrderSide,
    take_profit: Decimal,
    stop: Decimal,
) -> None:
    spec = make_linear_spec()
    validation = normalize_and_validate_order(
        spec,
        side="Sell" if entry_side == "Sell" else "Buy",
        quantity=Decimal("0.01"),
        price=Decimal("3000"),
    )

    risk = recalculate_quantized_risk(
        validation,
        spec,
        entry_side=entry_side,
        take_profit_price=take_profit,
        stop_price=stop,
        recovery_debt=Decimal("0"),
        maximum_notional=Decimal("100"),
        maximum_projected_stop_loss=Decimal("10"),
    )

    assert not risk.accepted
    assert "normalized take profit is not profitable" in risk.errors
    assert "normalized stop is not adverse" in risk.errors
