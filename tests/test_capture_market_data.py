"""Bounded normalized market-data capture launcher tests."""

import argparse
import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

import capture_market_data
from eth_credit_hedge.domain.market_data import TradeEvent


def test_capture_arguments_require_an_output_path() -> None:
    args = capture_market_data.parse_args(
        ["--output", "capture.jsonl", "--seconds", "2", "--depth", "50"]
    )

    assert args.output == Path("capture.jsonl")
    assert args.seconds == 2
    assert args.depth == 50
    assert args.option_symbol == []


def test_capture_surfaces_stream_failure_instead_of_silently_succeeding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def failing_stream() -> AsyncIterator[TradeEvent]:
        raise RuntimeError("stream failed")
        yield  # pragma: no cover

    class FailingWebSocketClient:
        def __init__(self, **_: object) -> None:
            pass

        def stream_trades(self, *_: object) -> AsyncIterator[TradeEvent]:
            return failing_stream()

        def stream_ticker(self, *_: object) -> AsyncIterator[TradeEvent]:
            return failing_stream()

        def stream_orderbook(self, *_: object) -> AsyncIterator[TradeEvent]:
            return failing_stream()

    monkeypatch.setattr(
        capture_market_data,
        "BybitPublicWebSocketClient",
        FailingWebSocketClient,
    )
    args = argparse.Namespace(
        output=tmp_path / "capture.jsonl",
        seconds=1.0,
        depth=50,
        option_symbol=[],
        testnet=False,
    )

    with pytest.raises(RuntimeError, match="stream failed"):
        asyncio.run(capture_market_data.capture(args))


def test_capture_rejects_a_duration_with_no_stream_data(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def silent_stream() -> AsyncIterator[TradeEvent]:
        await asyncio.Event().wait()
        yield  # pragma: no cover

    class SilentWebSocketClient:
        def __init__(self, **_: object) -> None:
            pass

        def stream_trades(self, *_: object) -> AsyncIterator[TradeEvent]:
            return silent_stream()

        def stream_ticker(self, *_: object) -> AsyncIterator[TradeEvent]:
            return silent_stream()

        def stream_orderbook(self, *_: object) -> AsyncIterator[TradeEvent]:
            return silent_stream()

    monkeypatch.setattr(
        capture_market_data,
        "BybitPublicWebSocketClient",
        SilentWebSocketClient,
    )
    args = argparse.Namespace(
        output=tmp_path / "capture.jsonl",
        seconds=0.01,
        depth=50,
        option_symbol=[],
        testnet=False,
    )

    with pytest.raises(RuntimeError, match="without data from every stream"):
        asyncio.run(capture_market_data.capture(args))
