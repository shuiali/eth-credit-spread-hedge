"""Append-only JSON Lines capture of normalized public market events."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path

from eth_credit_hedge.domain.market_data import (
    OrderBookDelta,
    OrderBookSnapshot,
    TickerEvent,
    TradeEvent,
)


RecordableMarketEvent = (
    TickerEvent | TradeEvent | OrderBookSnapshot | OrderBookDelta
)


@dataclass(frozen=True, slots=True)
class NormalizedMarketDataRecord:
    timestamp: str
    symbol: str
    event_type: str
    sequence: int | None
    update_id: int | None
    price: str | None
    size: str | None
    book_side: str | None
    connection_generation: int
    raw_payload_hash: str


class JsonLinesMarketDataRecorder:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(self, event: RecordableMarketEvent) -> None:
        records = _normalize_records(event)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            for record in records:
                handle.write(json.dumps(asdict(record), separators=(",", ":")))
                handle.write("\n")


def _normalize_records(
    event: RecordableMarketEvent,
) -> tuple[NormalizedMarketDataRecord, ...]:
    if isinstance(event, (OrderBookSnapshot, OrderBookDelta)):
        levels = tuple(("bid", price, size) for price, size in event.bids) + tuple(
            ("ask", price, size) for price, size in event.asks
        )
        if not levels:
            return (_record(event, price=None, size=None, book_side=None),)
        return tuple(
            _record(event, price=price, size=size, book_side=side)
            for side, price, size in levels
        )

    price: Decimal | None = None
    size: Decimal | None = None
    if isinstance(event, TradeEvent):
        price = event.price
        size = event.size
    elif isinstance(event, TickerEvent):
        price = event.last_price or event.mark_price or event.index_price
    return (_record(event, price=price, size=size, book_side=None),)


def _record(
    event: RecordableMarketEvent,
    *,
    price: Decimal | None,
    size: Decimal | None,
    book_side: str | None,
) -> NormalizedMarketDataRecord:
    return NormalizedMarketDataRecord(
        timestamp=event.timestamp_utc.isoformat(),
        symbol=event.symbol,
        event_type=event.event_type.value,
        sequence=event.sequence,
        update_id=(
            event.update_id
            if isinstance(event, (OrderBookSnapshot, OrderBookDelta))
            else None
        ),
        price=None if price is None else str(price),
        size=None if size is None else str(size),
        book_side=book_side,
        connection_generation=event.connection_generation,
        raw_payload_hash=event.raw_payload_hash,
    )
