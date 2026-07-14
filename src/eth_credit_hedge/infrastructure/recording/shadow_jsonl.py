"""Append-only canonical shadow-intent recorder."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from eth_credit_hedge.application.shadow_mode import ShadowIntent, ShadowObservation
from eth_credit_hedge.domain.journal import canonical_json
from eth_credit_hedge.domain.market_data import (
    MarketDataEventType,
    MarketDataHealthResult,
    TriggerPriceEvent,
    TriggerPriceSource,
)
from eth_credit_hedge.domain.risk import RiskState


class JsonLinesShadowRecorder:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(self, intent: ShadowIntent) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(intent.to_json())
            handle.write("\n")


class JsonLinesShadowObservationRecorder:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(self, observation: ShadowObservation) -> None:
        event = observation.event
        health = observation.market_health
        risk = observation.risk_state
        payload = canonical_json(
            {
                "event": {
                    "symbol": event.symbol,
                    "source": event.source,
                    "observed_price": event.observed_price,
                    "observed_timestamp": event.observed_timestamp,
                    "connection_generation": event.connection_generation,
                },
                "market_health": {
                    "trading_allowed": health.trading_allowed,
                    "reasons": health.reasons,
                    "event_type": health.event_type,
                },
                "risk_state": {
                    "current_perp_quantity": risk.current_perp_quantity,
                    "current_perp_notional": risk.current_perp_notional,
                    "post_trade_margin_usage": risk.post_trade_margin_usage,
                    "post_trade_liquidation_distance": (
                        risk.post_trade_liquidation_distance
                    ),
                    "confirmed_recovery_debt": risk.confirmed_recovery_debt,
                    "realized_cycle_loss": risk.realized_cycle_loss,
                    "daily_realized_loss": risk.daily_realized_loss,
                    "entries_for_level": risk.entries_for_level,
                    "active_levels": risk.active_levels,
                    "order_requests_last_minute": (
                        risk.order_requests_last_minute
                    ),
                    "consecutive_reconciliation_failures": (
                        risk.consecutive_reconciliation_failures
                    ),
                    "market_data_fresh": risk.market_data_fresh,
                    "reconciliation_succeeded": risk.reconciliation_succeeded,
                },
            }
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.write("\n")


def load_shadow_observations(path: str | Path) -> tuple[ShadowObservation, ...]:
    observations: list[ShadowObservation] = []
    with Path(path).open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                raw: Any = json.loads(line)
                if not isinstance(raw, dict):
                    raise ValueError("observation must be an object")
                event = _object(raw, "event")
                health = _object(raw, "market_health")
                risk = _object(raw, "risk_state")
                reasons = health["reasons"]
                if not isinstance(reasons, list) or not all(
                    isinstance(reason, str) for reason in reasons
                ):
                    raise ValueError("health reasons must be strings")
                observations.append(
                    ShadowObservation(
                        event=TriggerPriceEvent(
                            symbol=str(event["symbol"]),
                            source=TriggerPriceSource(str(event["source"])),
                            observed_price=Decimal(str(event["observed_price"])),
                            observed_timestamp=datetime.fromisoformat(
                                str(event["observed_timestamp"])
                            ),
                            connection_generation=_integer(
                                event,
                                "connection_generation",
                            ),
                        ),
                        market_health=MarketDataHealthResult(
                            trading_allowed=_boolean(
                                health,
                                "trading_allowed",
                            ),
                            reasons=tuple(reasons),
                            event_type=MarketDataEventType(str(health["event_type"])),
                        ),
                        risk_state=RiskState(
                            current_perp_quantity=Decimal(
                                str(risk["current_perp_quantity"])
                            ),
                            current_perp_notional=Decimal(
                                str(risk["current_perp_notional"])
                            ),
                            post_trade_margin_usage=Decimal(
                                str(risk["post_trade_margin_usage"])
                            ),
                            post_trade_liquidation_distance=Decimal(
                                str(risk["post_trade_liquidation_distance"])
                            ),
                            confirmed_recovery_debt=Decimal(
                                str(risk["confirmed_recovery_debt"])
                            ),
                            realized_cycle_loss=Decimal(
                                str(risk["realized_cycle_loss"])
                            ),
                            daily_realized_loss=Decimal(
                                str(risk["daily_realized_loss"])
                            ),
                            entries_for_level=_integer(risk, "entries_for_level"),
                            active_levels=_integer(risk, "active_levels"),
                            order_requests_last_minute=_integer(
                                risk,
                                "order_requests_last_minute",
                            ),
                            consecutive_reconciliation_failures=_integer(
                                risk,
                                "consecutive_reconciliation_failures",
                            ),
                            market_data_fresh=_boolean(risk, "market_data_fresh"),
                            reconciliation_succeeded=_boolean(
                                risk,
                                "reconciliation_succeeded",
                            ),
                        ),
                    )
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(
                    f"invalid shadow observation on line {line_number}"
                ) from exc
    return tuple(observations)


def _object(parent: dict[str, object], name: str) -> dict[str, object]:
    value = parent.get(name)
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _boolean(parent: dict[str, object], name: str) -> bool:
    value = parent.get(name)
    if type(value) is not bool:
        raise ValueError(f"{name} must be boolean")
    return value


def _integer(parent: dict[str, object], name: str) -> int:
    value = parent.get(name)
    if type(value) is not int:
        raise ValueError(f"{name} must be an integer")
    return value
