"""Validated historical inputs used by exact and enriched replays."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


def _decimal(value: object, field_name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{field_name} must be a finite decimal")
    return result


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must be an ISO-8601 string")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp must be a valid ISO-8601 value") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return result.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class NormalizedCaptureReplay:
    symbol: str
    trade_prices: tuple[Decimal, ...]
    timestamps: tuple[datetime, ...]
    sequences: tuple[int, ...]
    source_hashes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HistoricalMarketSample:
    timestamp: datetime
    trade_price: Decimal
    mark_price: Decimal
    index_price: Decimal
    option_symbol: str
    option_bid: Decimal
    option_ask: Decimal
    option_mark: Decimal
    option_iv: Decimal
    funding_rate: Decimal
    instrument_status: str


def load_normalized_capture(
    path: Path,
    *,
    symbol: str,
) -> NormalizedCaptureReplay:
    """Load trade events without changing their captured order or values."""

    prices: list[Decimal] = []
    timestamps: list[datetime] = []
    sequences: list[int] = []
    hashes: list[str] = []
    last_sequence_by_generation: dict[int, int] = {}
    last_timestamp_by_generation: dict[int, datetime] = {}

    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                raw: Any = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON on line {line_number}") from exc
            if not isinstance(raw, dict):
                raise ValueError(f"line {line_number} must contain an object")
            if raw.get("symbol") != symbol or raw.get("event_type") != "TradeObserved":
                continue

            timestamp = _timestamp(raw.get("timestamp"))
            sequence = raw.get("sequence")
            generation = raw.get("connection_generation")
            source_hash = raw.get("raw_payload_hash")
            if not isinstance(sequence, int) or sequence < 0:
                raise ValueError(f"invalid sequence on line {line_number}")
            if not isinstance(generation, int) or generation < 0:
                raise ValueError(f"invalid connection generation on line {line_number}")
            if (
                not isinstance(source_hash, str)
                or len(source_hash) != 64
                or any(character not in "0123456789abcdefABCDEF" for character in source_hash)
            ):
                raise ValueError(f"invalid payload hash on line {line_number}")
            if sequence <= last_sequence_by_generation.get(generation, -1):
                raise ValueError(f"non-increasing sequence on line {line_number}")
            prior_timestamp = last_timestamp_by_generation.get(generation)
            if prior_timestamp is not None and timestamp < prior_timestamp:
                raise ValueError(f"decreasing timestamp on line {line_number}")

            price = _decimal(raw.get("price"), "trade price")
            if price <= 0:
                raise ValueError(f"trade price must be positive on line {line_number}")
            last_sequence_by_generation[generation] = sequence
            last_timestamp_by_generation[generation] = timestamp
            prices.append(price)
            timestamps.append(timestamp)
            sequences.append(sequence)
            hashes.append(source_hash.lower())

    return NormalizedCaptureReplay(
        symbol=symbol,
        trade_prices=tuple(prices),
        timestamps=tuple(timestamps),
        sequences=tuple(sequences),
        source_hashes=tuple(hashes),
    )


def load_historical_market_samples_csv(path: Path) -> tuple[HistoricalMarketSample, ...]:
    """Load enriched market samples used by simulated historical replay."""

    required = {
        "timestamp",
        "trade_price",
        "mark_price",
        "index_price",
        "option_symbol",
        "option_bid",
        "option_ask",
        "option_mark",
        "option_iv",
        "funding_rate",
        "instrument_status",
    }
    samples: list[HistoricalMarketSample] = []
    with path.open(encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            missing = sorted(required.difference(reader.fieldnames or ()))
            raise ValueError(f"historical CSV is missing columns: {', '.join(missing)}")
        for line_number, row in enumerate(reader, start=2):
            sample = HistoricalMarketSample(
                timestamp=_timestamp(row["timestamp"]),
                trade_price=_decimal(row["trade_price"], "trade price"),
                mark_price=_decimal(row["mark_price"], "mark price"),
                index_price=_decimal(row["index_price"], "index price"),
                option_symbol=row["option_symbol"].strip(),
                option_bid=_decimal(row["option_bid"], "option bid"),
                option_ask=_decimal(row["option_ask"], "option ask"),
                option_mark=_decimal(row["option_mark"], "option mark"),
                option_iv=_decimal(row["option_iv"], "option IV"),
                funding_rate=_decimal(row["funding_rate"], "funding rate"),
                instrument_status=row["instrument_status"].strip(),
            )
            if not sample.option_symbol or not sample.instrument_status:
                raise ValueError(f"empty metadata on line {line_number}")
            if min(sample.trade_price, sample.mark_price, sample.index_price) <= 0:
                raise ValueError(f"underlying prices must be positive on line {line_number}")
            if min(sample.option_bid, sample.option_ask, sample.option_mark) < 0:
                raise ValueError(f"option prices cannot be negative on line {line_number}")
            if sample.option_bid > sample.option_ask:
                raise ValueError(f"option bid exceeds ask on line {line_number}")
            if sample.option_iv < 0:
                raise ValueError(f"option IV cannot be negative on line {line_number}")
            samples.append(sample)
    return tuple(samples)
