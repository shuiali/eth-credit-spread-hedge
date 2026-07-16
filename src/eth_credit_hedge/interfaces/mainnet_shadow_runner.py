"""Run order-free mainnet shadow capture, replay, and acceptance."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from eth_credit_hedge.application.deployment_acceptance import (
    ShadowAcceptanceMetrics,
    evaluate_shadow_acceptance,
)
from eth_credit_hedge.application.shadow_mode import (
    ShadowIntent,
    ShadowModeService,
    ShadowObservation,
    replay_shadow_observations,
)
from eth_credit_hedge.config.deployment import (
    EnvironmentProfile,
    load_all_environment_profiles,
)
from eth_credit_hedge.config.schema import RuntimeEnvironment
from eth_credit_hedge.core.virtual_levels import (
    HedgeLevel,
    build_price_step_virtual_levels,
)
from eth_credit_hedge.domain.instruments import InstrumentSpec
from eth_credit_hedge.domain.market_data import (
    MarketDataHealthResult,
    MarketDataHealthPolicy,
    MarketDataHealthSnapshot,
    TradeEvent,
    evaluate_market_data_health,
)
from eth_credit_hedge.domain.risk import RiskEngine, RiskState
from eth_credit_hedge.domain.strategy_math import EntryPercentStopConfig, Rate
from eth_credit_hedge.infrastructure.bybit.public_market_data import (
    BybitPublicMarketData,
)
from eth_credit_hedge.infrastructure.bybit.public_rest import (
    BybitPublicRestClient,
)
from eth_credit_hedge.infrastructure.recording.shadow_jsonl import (
    JsonLinesShadowObservationRecorder,
    load_shadow_observations,
)
from eth_credit_hedge.interfaces.demo_runner import select_demo_option_pair
from eth_credit_hedge.interfaces.shadow_runner import (
    ShadowRunResult,
    ShadowRunner,
    digest_shadow_intents,
)


ZERO = Decimal("0")


async def run_mainnet_shadow(
    *,
    attempt_seconds: int = 15,
    maximum_attempts: int = 12,
) -> dict[str, object]:
    if attempt_seconds <= 0 or maximum_attempts <= 0:
        raise ValueError("shadow attempt limits must be positive")
    profile = _shadow_profile()
    if profile.external_order_mutations_enabled:
        raise RuntimeError("mainnet shadow profile permits mutations")
    if profile.rest_base_url != "https://api.bybit.com":
        raise RuntimeError("mainnet shadow profile is not mainnet-bound")

    public = BybitPublicRestClient()
    market_data = BybitPublicMarketData(rest=public)
    for attempt in range(1, maximum_attempts + 1):
        instrument, book, option_instruments, option_quotes = await asyncio.gather(
            public.get_instrument("ETHUSDT"),
            public.get_orderbook_snapshot("ETHUSDT", 1),
            public.list_instruments("option", base_coin="ETH"),
            public.get_option_chain("ETH"),
        )
        as_of = datetime.now(timezone.utc)
        long_quote, short_quote, long_instrument, short_instrument = (
            select_demo_option_pair(
                option_quotes,
                option_instruments,
                as_of_utc=as_of,
                maximum_loss=profile.risk_limits.maximum_realized_cycle_loss,
            )
        )
        levels = _shadow_levels(instrument, book.bids[0][0])
        capture = _capture_path(as_of, attempt)
        service = _shadow_service(profile, instrument, levels)
        runner = ShadowRunner(
            market_data=market_data,
            shadow=service,
            health_provider=lambda trade: _market_health(
                trade,
                (long_quote.timestamp_utc, short_quote.timestamp_utc),
                profile,
            ),
            risk_state_provider=lambda trade: _risk_state(),
            observation_recorder=JsonLinesShadowObservationRecorder(capture),
        )
        try:
            async with asyncio.timeout(attempt_seconds):
                live = await runner.run(
                    maximum_events=200_000,
                    stop_after_intents=1,
                )
        except TimeoutError:
            continue
        if not live.intents or not any(
            intent.risk_approved for intent in live.intents
        ):
            continue

        observations = load_shadow_observations(capture)
        replayed = replay_shadow_observations(
            _shadow_service(profile, instrument, levels),
            observations,
        )
        expiry = short_instrument.delivery_time_utc
        if expiry is None or long_instrument.delivery_time_utc != expiry:
            raise RuntimeError("shadow option pair has no common expiry")
        metrics = _acceptance_metrics(
            live,
            replayed,
            observations,
            instrument,
            profile,
            expiry,
            as_of,
        )
        acceptance = evaluate_shadow_acceptance(metrics)
        if not acceptance.accepted:
            raise RuntimeError(
                "mainnet shadow acceptance failed: "
                + "; ".join(acceptance.reasons)
            )
        return {
            "accepted": True,
            "attempt": attempt,
            "capture_path": str(capture),
            "capture_sha256": hashlib.sha256(capture.read_bytes()).hexdigest(),
            "observation_count": live.observation_count,
            "recorded_intents": metrics.recorded_intents,
            "reproduced_intents": metrics.reproduced_intents,
            "decision_digest": live.decision_digest,
            "replay_digest": digest_shadow_intents(replayed),
            "approved_intents": sum(
                intent.risk_approved for intent in live.intents
            ),
            "option_long_symbol": long_quote.symbol,
            "option_short_symbol": short_quote.symbol,
            "option_expiry_utc": expiry.isoformat(),
            "level_entry_prices": [
                str(level.entry_price) for level in levels
            ],
            "metrics": {
                "contradictory_transitions": metrics.contradictory_transitions,
                "stale_data_approved_intents": (
                    metrics.stale_data_approved_intents
                ),
                "invalid_quantized_intents": metrics.invalid_quantized_intents,
                "nondeterministic_risk_decisions": (
                    metrics.nondeterministic_risk_decisions
                ),
                "expiry_cutoff_violations": metrics.expiry_cutoff_violations,
            },
            "external_order_mutations_enabled": False,
            "trading_adapter_constructed": False,
        }
    raise TimeoutError("mainnet shadow produced no approved crossing intent")


def _shadow_profile() -> EnvironmentProfile:
    matches = tuple(
        profile
        for profile in load_all_environment_profiles()
        if profile.environment is RuntimeEnvironment.SHADOW_MAINNET
    )
    if len(matches) != 1:
        raise RuntimeError("expected exactly one SHADOW_MAINNET profile")
    return matches[0]


def _shadow_levels(
    instrument: InstrumentSpec,
    reference_price: Decimal,
) -> tuple[HedgeLevel, ...]:
    tick = instrument.price_filter.tick_size
    price_step_usd = tick * Decimal("10")
    quantity = Decimal("0.10")
    levels = tuple(
        build_price_step_virtual_levels(
            short_put_strike=reference_price - price_step_usd,
            long_put_strike=reference_price - price_step_usd * Decimal("21"),
            option_quantity=quantity,
            price_step_usd=price_step_usd,
            stop=EntryPercentStopConfig(Rate(Decimal("0.0015"))),
        )
    )
    if any(level.tp_price <= ZERO for level in levels):
        raise RuntimeError("shadow level geometry is invalid")
    return levels


def _shadow_service(
    profile: EnvironmentProfile,
    instrument: InstrumentSpec,
    levels: tuple[HedgeLevel, ...],
) -> ShadowModeService:
    return ShadowModeService(
        cycle_id="SHADOW-MAINNET",
        levels=levels,
        desired_quantity=Decimal("0.10"),
        option_spread_open=True,
        instrument=instrument,
        risk_engine=RiskEngine(),
        risk_limits=profile.risk_limits,
    )


def _market_health(
    trade: TradeEvent,
    option_quote_timestamps: tuple[datetime, ...],
    profile: EnvironmentProfile,
) -> MarketDataHealthResult:
    maximum_age = Decimal(profile.maximum_market_data_age_ms) / 1000
    return evaluate_market_data_health(
        MarketDataHealthSnapshot(
            trigger_timestamp_utc=trade.timestamp_utc,
            instrument_loaded=True,
            websocket_connected=True,
            option_quote_timestamps_utc=option_quote_timestamps,
            order_book_synchronized=False,
            order_book_timestamp_utc=None,
            clock_synchronized=True,
        ),
        MarketDataHealthPolicy(
            max_trigger_age_seconds=maximum_age,
            max_option_quote_age_seconds=Decimal("10"),
            max_order_book_age_seconds=maximum_age,
        ),
        as_of_utc=datetime.now(timezone.utc),
        order_book_required=False,
    )


def _risk_state() -> RiskState:
    return RiskState(
        current_perp_quantity=ZERO,
        current_perp_notional=ZERO,
        post_trade_margin_usage=Decimal("0.05"),
        post_trade_liquidation_distance=Decimal("0.50"),
        confirmed_recovery_debt=ZERO,
        realized_cycle_loss=ZERO,
        daily_realized_loss=ZERO,
        entries_for_level=0,
        active_levels=0,
        order_requests_last_minute=0,
        consecutive_reconciliation_failures=0,
        market_data_fresh=True,
        reconciliation_succeeded=True,
    )


def _acceptance_metrics(
    live: ShadowRunResult,
    replayed: tuple[ShadowIntent, ...],
    observations: tuple[ShadowObservation, ...],
    instrument: InstrumentSpec,
    profile: EnvironmentProfile,
    option_expiry: datetime,
    as_of: datetime,
) -> ShadowAcceptanceMetrics:
    health_by_event = {
        (
            observation.event.observed_timestamp,
            observation.event.connection_generation,
        ): observation.market_health
        for observation in observations
    }
    stale_approved = sum(
        intent.risk_approved
        and not health_by_event[
            (
                intent.observed_at_utc,
                intent.connection_generation,
            )
        ].trading_allowed
        for intent in live.intents
    )
    invalid_quantized = sum(
        _invalid_quantized_intent(intent, instrument, profile)
        for intent in live.intents
    )
    sequences = tuple(intent.sequence for intent in live.intents)
    contradictory = int(
        sequences != tuple(range(1, len(sequences) + 1))
        or len({intent.level_id for intent in live.intents})
        != len(live.intents)
    )
    nondeterministic = int(
        replayed != live.intents
        or digest_shadow_intents(replayed) != live.decision_digest
    )
    expiry_violation = int(
        not as_of + timedelta(days=14)
        <= option_expiry
        <= as_of + timedelta(days=90)
    )
    return ShadowAcceptanceMetrics(
        recorded_intents=len(live.intents),
        reproduced_intents=len(replayed),
        contradictory_transitions=contradictory,
        stale_data_approved_intents=stale_approved,
        invalid_quantized_intents=invalid_quantized,
        nondeterministic_risk_decisions=nondeterministic,
        expiry_cutoff_violations=expiry_violation,
    )


def _invalid_quantized_intent(
    intent: ShadowIntent,
    instrument: InstrumentSpec,
    profile: EnvironmentProfile,
) -> bool:
    if not intent.risk_approved:
        return False
    quantity = intent.quantized_quantity
    return bool(
        quantity is None
        or quantity % instrument.lot_size_filter.qty_step != ZERO
        or quantity > profile.risk_limits.maximum_perp_quantity
        or intent.hypothetical_entry_price is None
        or intent.hypothetical_take_profit_price is None
        or intent.hypothetical_stop_price is None
        or intent.projected_stop_loss is None
    )


def _capture_path(as_of: datetime, attempt: int) -> Path:
    stamp = as_of.strftime("%Y%m%dT%H%M%S%fZ")
    return Path("artifacts") / f"shadow-mainnet-{stamp}-a{attempt}.jsonl"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempt-seconds", type=int, default=15)
    parser.add_argument("--maximum-attempts", type=int, default=12)
    args = parser.parse_args()
    result = asyncio.run(
        run_mainnet_shadow(
            attempt_seconds=args.attempt_seconds,
            maximum_attempts=args.maximum_attempts,
        )
    )
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
