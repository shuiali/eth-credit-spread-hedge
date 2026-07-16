"""Mainnet shadow intents are deterministic and cannot submit orders."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

from eth_credit_hedge.application.shadow_mode import (
    ShadowModeService,
    ShadowObservation,
    replay_shadow_observations,
)
from eth_credit_hedge.config.deployment import load_all_environment_profiles
from eth_credit_hedge.core.virtual_levels import HedgeLevel
from eth_credit_hedge.domain.instruments import (
    InstrumentSpec,
    LotSizeFilter,
    PriceFilter,
)
from eth_credit_hedge.domain.market_data import (
    MarketDataEventType,
    MarketDataHealthResult,
    TriggerPriceEvent,
    TriggerPriceSource,
)
from eth_credit_hedge.domain.risk import RiskEngine, RiskState
from eth_credit_hedge.infrastructure.recording.shadow_jsonl import (
    JsonLinesShadowRecorder,
)


NOW = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)


def instrument() -> InstrumentSpec:
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


def level() -> HedgeLevel:
    return HedgeLevel(
        level_id=1,
        entry_price=Decimal("3000"),
        tp_price=Decimal("2900"),
                stop_price=Decimal("3015"),
        option_budget=Decimal("1"),
    )


def risk_state() -> RiskState:
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


def health(allowed: bool = True) -> MarketDataHealthResult:
    return MarketDataHealthResult(
        trading_allowed=allowed,
        reasons=() if allowed else ("trigger price is stale",),
        event_type=(
            MarketDataEventType.MARKET_DATA_RECOVERED
            if allowed
            else MarketDataEventType.MARKET_DATA_STALE
        ),
    )


def trigger(price: str, generation: int = 1) -> TriggerPriceEvent:
    return TriggerPriceEvent(
        symbol="ETHUSDT",
        source=TriggerPriceSource.LAST_TRADE,
        observed_price=Decimal(price),
        observed_timestamp=NOW,
        connection_generation=generation,
    )


def service(recorder=None) -> ShadowModeService:
    profile = load_all_environment_profiles()[3]
    return ShadowModeService(
        cycle_id="shadow-cycle-1",
        levels=(level(),),
        desired_quantity=Decimal("0.010"),
        option_spread_open=True,
        instrument=instrument(),
        risk_engine=RiskEngine(),
        risk_limits=profile.risk_limits,
        recorder=recorder,
    )


def test_shadow_crossing_records_complete_hypothetical_intent_without_order(
    tmp_path,
) -> None:
    recorder = JsonLinesShadowRecorder(tmp_path / "shadow.jsonl")
    shadow = service(recorder)

    assert shadow.on_trigger(trigger("3001"), health(), risk_state()).intents == ()
    result = shadow.on_trigger(trigger("2999"), health(), risk_state())

    assert len(result.intents) == 1
    intent = result.intents[0]
    assert intent.virtual_crossing_price == Decimal("3000")
    assert intent.desired_quantity == Decimal("0.010")
    assert intent.quantized_quantity == Decimal("0.010")
    assert intent.risk_approved
    assert intent.risk_reasons == ()
    assert intent.hypothetical_entry_price == Decimal("3000")
    assert intent.hypothetical_take_profit_price == Decimal("2900")
    assert intent.hypothetical_stop_price == Decimal("3015")
    assert intent.expected_tp_pnl == Decimal("1.000")
    assert "order_id" not in intent.to_dict()
    assert (tmp_path / "shadow.jsonl").read_text(encoding="utf-8").splitlines() == [
        intent.to_json()
    ]


def test_offline_replay_produces_identical_decisions_and_digest() -> None:
    observations = (
        ShadowObservation(trigger("3001"), health(), risk_state()),
        ShadowObservation(trigger("2999"), health(), risk_state()),
    )

    first = replay_shadow_observations(service(), observations)
    second = replay_shadow_observations(service(), observations)

    assert first == second
    assert first[0].decision_digest == second[0].decision_digest


def test_stale_data_and_risk_veto_never_create_approved_intent() -> None:
    stale_shadow = service()
    stale_shadow.on_trigger(trigger("3001"), health(), risk_state())
    assert stale_shadow.on_trigger(trigger("2999"), health(False), risk_state()).intents == ()
    fresh = stale_shadow.on_trigger(trigger("2998"), health(), risk_state())
    assert fresh.intents[0].risk_approved

    veto_shadow = service()
    veto_shadow.on_trigger(trigger("3001"), health(), risk_state())
    veto = veto_shadow.on_trigger(
        trigger("2999"),
        health(),
        replace(risk_state(), post_trade_margin_usage=Decimal("0.9")),
    )
    assert not veto.intents[0].risk_approved
    assert veto.intents[0].risk_reasons == ("maximum margin usage exceeded",)
