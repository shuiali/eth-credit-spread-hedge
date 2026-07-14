"""Capture normalized ETH perpetual and selected-option public data as JSONL."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import TypeAlias

from eth_credit_hedge.domain.market_data import (
    DEFAULT_TRIGGER_SYMBOL,
    OrderBookEvent,
    TickerEvent,
    TradeEvent,
)
from eth_credit_hedge.infrastructure.bybit.public_ws import (
    BybitPublicWebSocketClient,
)
from eth_credit_hedge.infrastructure.recording.jsonl import (
    JsonLinesMarketDataRecorder,
)


MarketEvent: TypeAlias = TickerEvent | TradeEvent | OrderBookEvent


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--depth", type=int, default=50)
    parser.add_argument("--option-symbol", action="append", default=[])
    parser.add_argument("--testnet", action="store_true")
    return parser.parse_args(argv)


async def _record_stream(
    stream: AsyncIterator[MarketEvent],
    recorder: JsonLinesMarketDataRecorder,
    ready: asyncio.Event,
) -> None:
    async for event in stream:
        recorder.append(event)
        ready.set()


async def capture(args: argparse.Namespace) -> None:
    if args.seconds <= 0:
        raise ValueError("capture duration must be positive")
    if args.depth <= 0:
        raise ValueError("order-book depth must be positive")
    recorder = JsonLinesMarketDataRecorder(args.output)
    linear = BybitPublicWebSocketClient(category="linear", testnet=args.testnet)
    streams: list[AsyncIterator[MarketEvent]] = [
        linear.stream_trades(DEFAULT_TRIGGER_SYMBOL),
        linear.stream_ticker(DEFAULT_TRIGGER_SYMBOL),
        linear.stream_orderbook(DEFAULT_TRIGGER_SYMBOL, args.depth),
    ]
    if args.option_symbol:
        option = BybitPublicWebSocketClient(category="option", testnet=args.testnet)
        streams.extend(option.stream_ticker(symbol) for symbol in args.option_symbol)
    ready = [asyncio.Event() for _ in streams]
    tasks = [
        asyncio.create_task(_record_stream(stream, recorder, stream_ready))
        for stream, stream_ready in zip(streams, ready, strict=True)
    ]
    try:
        done, _ = await asyncio.wait(
            tasks,
            timeout=args.seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            exception = task.exception()
            if exception is not None:
                raise exception
        if done:
            raise RuntimeError("market-data stream ended before capture duration")
        if not all(stream_ready.is_set() for stream_ready in ready):
            raise RuntimeError("capture completed without data from every stream")
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def main() -> None:
    asyncio.run(capture(parse_args()))


if __name__ == "__main__":
    main()
