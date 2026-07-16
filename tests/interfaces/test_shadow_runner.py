"""A market-data-only shadow runner records replayable decision inputs."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from eth_credit_hedge.application.shadow_mode import (
    ShadowModeService,
    replay_shadow_observations,
)
from eth_credit_hedge.config.deployment import load_all_environment_profiles
from eth_credit_hedge.core.virtual_levels import HedgeLevel
from eth_credit_hedge.domain.instruments import InstrumentSpec, LotSizeFilter, PriceFilter
from eth_credit_hedge.domain.market_data import (
    MarketDataEventType,
    MarketDataHealthResult,
    TradeEvent,
)
from eth_credit_hedge.domain.risk import RiskEngine, RiskState
from eth_credit_hedge.infrastructure.recording.shadow_jsonl import (
    JsonLinesShadowObservationRecorder,
    load_shadow_observations,
)
from eth_credit_hedge.interfaces.mainnet_shadow_runner import _shadow_levels
from eth_credit_hedge.interfaces.shadow_runner import ShadowRunner, digest_shadow_intents


NOW = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)


def instrument() -> InstrumentSpec:
    return InstrumentSpec(
        symbol="ETHUSDT",
        category="linear",
        status="Trading",
        base_coin="ETH",
        quote_coin="USDT",
        settle_coin="USDT",
        price_filter=PriceFilter(Decimal("0.1"), Decimal("10"), Decimal("100000")),
        lot_size_filter=LotSizeFilter(
            Decimal("0.001"),
            Decimal("0.001"),
            Decimal("100"),
            Decimal("50"),
            Decimal("5"),
        ),
        contract_multiplier=Decimal("1"),
        delivery_time_utc=None,
    )


def service() -> ShadowModeService:
    return ShadowModeService(
        cycle_id="cycle-1",
        levels=(
            HedgeLevel(
                level_id=1,
                entry_price=Decimal("3000"),
                tp_price=Decimal("2900"),
                stop_price=Decimal("3015"),
                option_budget=Decimal("1"),
            ),
        ),
        desired_quantity=Decimal("0.010"),
        option_spread_open=True,
        instrument=instrument(),
        risk_engine=RiskEngine(),
        risk_limits=load_all_environment_profiles()[3].risk_limits,
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


def test_mainnet_shadow_levels_use_entry_percent_stops() -> None:
    levels = _shadow_levels(instrument(), Decimal("3000"))

    for current, following in zip(levels, levels[1:]):
        assert current.entry_price - following.entry_price == current.tp_distance
        assert current.stop_distance == current.entry_price * Decimal("0.0015")


def trade(price: str, sequence: int) -> TradeEvent:
    return TradeEvent(
        symbol="ETHUSDT",
        timestamp_utc=NOW + timedelta(seconds=sequence),
        price=Decimal(price),
        size=Decimal("1"),
        side="Sell",
        trade_id=f"trade-{sequence}",
        sequence=sequence,
        connection_generation=1,
        raw_payload_hash=str(sequence) * 64,
    )


class TradeStream:
    def stream_trades(self, symbol: str):
        assert symbol == "ETHUSDT"

        async def events():
            yield trade("3001", 1)
            yield trade("2999", 2)

        return events()


def test_shadow_runner_capture_replays_to_identical_intent_digest(tmp_path) -> None:
    capture = tmp_path / "shadow-observations.jsonl"
    health = MarketDataHealthResult(
        trading_allowed=True,
        reasons=(),
        event_type=MarketDataEventType.MARKET_DATA_RECOVERED,
    )
    runner = ShadowRunner(
        market_data=TradeStream(),  # type: ignore[arg-type]
        shadow=service(),
        health_provider=lambda event: health,
        risk_state_provider=lambda event: state(),
        observation_recorder=JsonLinesShadowObservationRecorder(capture),
    )

    live = asyncio.run(runner.run(maximum_events=2))
    loaded = load_shadow_observations(capture)
    replayed = replay_shadow_observations(service(), loaded)

    assert live.observation_count == 2
    assert len(live.intents) == 1
    assert replayed == live.intents
    assert digest_shadow_intents(replayed) == live.decision_digest


def test_shadow_runner_can_stop_after_first_recorded_intent(tmp_path) -> None:
    runner = ShadowRunner(
        market_data=TradeStream(),  # type: ignore[arg-type]
        shadow=service(),
        health_provider=lambda event: MarketDataHealthResult(
            trading_allowed=True,
            reasons=(),
            event_type=MarketDataEventType.MARKET_DATA_RECOVERED,
        ),
        risk_state_provider=lambda event: state(),
        observation_recorder=JsonLinesShadowObservationRecorder(
            tmp_path / "observations.jsonl"
        ),
    )

    result = asyncio.run(
        runner.run(maximum_events=100, stop_after_intents=1)
    )

    assert result.observation_count == 2
    assert len(result.intents) == 1
