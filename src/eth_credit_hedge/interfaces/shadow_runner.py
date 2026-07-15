"""Market-data-only live shadow runner with replayable observation capture."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterable
from contextlib import aclosing
from dataclasses import dataclass
from typing import Protocol, cast

from eth_credit_hedge.application.shadow_mode import (
    ShadowIntent,
    ShadowModeService,
    ShadowObservation,
)
from eth_credit_hedge.domain.market_data import (
    MarketDataHealthResult,
    TradeEvent,
    TriggerPriceRouter,
    TriggerPriceSource,
)
from eth_credit_hedge.domain.risk import RiskState


class TradeStreamPort(Protocol):
    def stream_trades(self, symbol: str) -> AsyncIterator[TradeEvent]: ...


class ShadowObservationRecorderPort(Protocol):
    def append(self, observation: ShadowObservation) -> None: ...


@dataclass(frozen=True, slots=True)
class ShadowRunResult:
    observation_count: int
    intents: tuple[ShadowIntent, ...]
    decision_digest: str


class ShadowRunner:
    """Consume normalized public trades without holding a trading capability."""

    def __init__(
        self,
        *,
        market_data: TradeStreamPort,
        shadow: ShadowModeService,
        health_provider: Callable[[TradeEvent], MarketDataHealthResult],
        risk_state_provider: Callable[[TradeEvent], RiskState],
        observation_recorder: ShadowObservationRecorderPort,
    ) -> None:
        self._market_data = market_data
        self._shadow = shadow
        self._health_provider = health_provider
        self._risk_state_provider = risk_state_provider
        self._observation_recorder = observation_recorder
        self._router = TriggerPriceRouter(TriggerPriceSource.LAST_TRADE)

    async def run(
        self,
        *,
        maximum_events: int,
        stop_after_intents: int | None = None,
    ) -> ShadowRunResult:
        if type(maximum_events) is not int or maximum_events <= 0:
            raise ValueError("maximum shadow events must be positive")
        if stop_after_intents is not None and (
            type(stop_after_intents) is not int or stop_after_intents <= 0
        ):
            raise ValueError("shadow intent target must be positive")
        intents: list[ShadowIntent] = []
        observation_count = 0
        stream = self._market_data.stream_trades("ETHUSDT")
        if not hasattr(stream, "aclose"):
            raise TypeError("shadow trade stream must support asynchronous close")
        async with aclosing(
            cast(AsyncGenerator[TradeEvent, None], stream)
        ) as trades:
            async for trade in trades:
                trigger = self._router.from_trade(trade)
                if trigger is None:
                    raise AssertionError(
                        "LAST_TRADE router must accept normalized trades"
                    )
                observation = ShadowObservation(
                    event=trigger,
                    market_health=self._health_provider(trade),
                    risk_state=self._risk_state_provider(trade),
                )
                self._observation_recorder.append(observation)
                result = self._shadow.on_trigger(
                    observation.event,
                    observation.market_health,
                    observation.risk_state,
                )
                intents.extend(result.intents)
                observation_count += 1
                if observation_count >= maximum_events or (
                    stop_after_intents is not None
                    and len(intents) >= stop_after_intents
                ):
                    break
        return ShadowRunResult(
            observation_count=observation_count,
            intents=tuple(intents),
            decision_digest=digest_shadow_intents(intents),
        )


def digest_shadow_intents(intents: Iterable[ShadowIntent]) -> str:
    digest_input = "\n".join(intent.decision_digest for intent in intents)
    return hashlib.sha256(digest_input.encode()).hexdigest()
