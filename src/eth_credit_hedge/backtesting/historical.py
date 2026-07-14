"""Explicit ordered-tick and OHLC historical replay."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Iterable

from eth_credit_hedge.core.credit_spread import DecimalLike, to_decimal
from eth_credit_hedge.core.hedge_engine import HedgeEngine
from eth_credit_hedge.core.ledger import StrategyResult


class IntrabarPath(str, Enum):
    OPEN_HIGH_LOW_CLOSE = "O_H_L_C"
    OPEN_LOW_HIGH_CLOSE = "O_L_H_C"


@dataclass(frozen=True, slots=True)
class Candle:
    timestamp: str
    open: DecimalLike
    high: DecimalLike
    low: DecimalLike
    close: DecimalLike

    def __post_init__(self) -> None:
        for field_name in ("open", "high", "low", "close"):
            object.__setattr__(self, field_name, to_decimal(getattr(self, field_name)))
        if self.low <= 0:
            raise ValueError("candle prices must be positive")
        if self.high < max(self.open, self.close):
            raise ValueError("candle high is below its open or close")
        if self.low > min(self.open, self.close):
            raise ValueError("candle low is above its open or close")


@dataclass(frozen=True, slots=True)
class HistoricalReplay:
    tick_path: tuple[Decimal, ...]
    saved_path: Path
    intrabar_path: IntrabarPath | None
    result: StrategyResult


def load_candles_csv(path: str | Path) -> list[Candle]:
    """Load timestamp/open/high/low/close columns without choosing path order."""
    with Path(path).open(newline="", encoding="utf-8") as handle:
        rows = csv.DictReader(handle)
        required = {"timestamp", "open", "high", "low", "close"}
        if rows.fieldnames is None or not required.issubset(rows.fieldnames):
            raise ValueError("CSV must contain timestamp, open, high, low, close columns")
        return [
            Candle(
                timestamp=row["timestamp"],
                open=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
            )
            for row in rows
        ]


def reconstruct_tick_path(
    candles: Iterable[Candle],
    intrabar_path: IntrabarPath | str,
    ticks_per_leg: int = 1,
) -> tuple[Decimal, ...]:
    """Reconstruct a saved ordered path under one explicit OHLC assumption."""
    if ticks_per_leg <= 0:
        raise ValueError("ticks per leg must be positive")
    assumption = IntrabarPath(intrabar_path)
    candle_list = list(candles)
    if not candle_list:
        raise ValueError("at least one candle is required")

    ticks: list[Decimal] = []
    previous_close: Decimal | None = None
    for candle in candle_list:
        if previous_close is not None and candle.open != previous_close:
            raise ValueError("candle gap handling is postponed; open must equal prior close")
        anchors = (
            (candle.open, candle.high, candle.low, candle.close)
            if assumption is IntrabarPath.OPEN_HIGH_LOW_CLOSE
            else (candle.open, candle.low, candle.high, candle.close)
        )
        if not ticks:
            ticks.append(anchors[0])
        for start, end in zip(anchors, anchors[1:]):
            for step in range(1, ticks_per_leg + 1):
                ticks.append(
                    start + (end - start) * Decimal(step) / Decimal(ticks_per_leg)
                )
        previous_close = candle.close
    return tuple(ticks)


def replay_ticks(
    engine: HedgeEngine,
    ticks: Iterable[DecimalLike],
    output_path: str | Path,
    *,
    source: str = "ordered_ticks",
) -> HistoricalReplay:
    """Persist and replay the exact ordered ticks supplied by the caller."""
    exact_ticks = tuple(to_decimal(tick) for tick in ticks)
    if not exact_ticks:
        raise ValueError("tick path cannot be empty")
    saved_path = _save_ticks(
        output_path,
        exact_ticks,
        {"source": source, "intrabar_path": None},
    )
    result = engine.run_with_accounting(list(exact_ticks))
    return HistoricalReplay(exact_ticks, saved_path, None, result)


def replay_candles(
    engine: HedgeEngine,
    candles: Iterable[Candle],
    intrabar_path: IntrabarPath | str,
    output_path: str | Path,
    *,
    ticks_per_leg: int = 1,
) -> HistoricalReplay:
    """Persist and replay candles only after an explicit intrabar reconstruction."""
    assumption = IntrabarPath(intrabar_path)
    ticks = reconstruct_tick_path(candles, assumption, ticks_per_leg)
    saved_path = _save_ticks(
        output_path,
        ticks,
        {
            "source": "ohlc_reconstruction",
            "intrabar_path": assumption.value,
            "ticks_per_leg": ticks_per_leg,
        },
    )
    result = engine.run_with_accounting(list(ticks))
    return HistoricalReplay(ticks, saved_path, assumption, result)


def _save_ticks(
    output_path: str | Path,
    ticks: tuple[Decimal, ...],
    metadata: dict[str, object],
) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {**metadata, "ticks": [str(tick) for tick in ticks]}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path
