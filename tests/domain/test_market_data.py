"""Trigger-source, local-book, and market-data health tests."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from eth_credit_hedge.domain.market_data import (
    DEFAULT_TRIGGER_PRICE_SOURCE,
    LocalOrderBook,
    MarketDataEventType,
    MarketDataHealthPolicy,
    MarketDataHealthSnapshot,
    OrderBookDelta,
    OrderBookSnapshot,
    TickerEvent,
    TradeEvent,
    TriggerPriceRouter,
    TriggerPriceSource,
    evaluate_market_data_health,
    normalize_instrument_change,
)
from eth_credit_hedge.domain.instruments import (
    InstrumentSpec,
    LotSizeFilter,
    PriceFilter,
)


NOW = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)


def snapshot(
    *,
    generation: int = 1,
    update_id: int = 10,
    bids: tuple[tuple[Decimal, Decimal], ...] = (
        (Decimal("3000"), Decimal("2")),
        (Decimal("2999.9"), Decimal("1")),
    ),
    asks: tuple[tuple[Decimal, Decimal], ...] = (
        (Decimal("3000.1"), Decimal("3")),
    ),
) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        symbol="ETHUSDT",
        bids=bids,
        asks=asks,
        update_id=update_id,
        sequence=100 + update_id,
        timestamp_utc=NOW,
        connection_generation=generation,
        raw_payload_hash="a" * 64,
    )


def delta(
    *,
    generation: int = 1,
    update_id: int = 11,
    bids: tuple[tuple[Decimal, Decimal], ...] = (),
    asks: tuple[tuple[Decimal, Decimal], ...] = (),
) -> OrderBookDelta:
    return OrderBookDelta(
        symbol="ETHUSDT",
        bids=bids,
        asks=asks,
        update_id=update_id,
        sequence=100 + update_id,
        timestamp_utc=NOW + timedelta(milliseconds=100),
        connection_generation=generation,
        raw_payload_hash="b" * 64,
    )


def test_snapshot_then_delta_updates_and_zero_size_deletes_levels() -> None:
    book = LocalOrderBook("ETHUSDT")

    book.apply_snapshot(snapshot())
    applied = book.apply_delta(
        delta(
            bids=(
                (Decimal("3000"), Decimal("0")),
                (Decimal("2999.8"), Decimal("4")),
            ),
            asks=((Decimal("3000.1"), Decimal("2.5")),),
        )
    )

    assert applied
    assert book.synchronized
    assert book.update_id == 11
    assert Decimal("3000") not in book.bids
    assert book.bids[Decimal("2999.8")] == Decimal("4")
    assert book.asks[Decimal("3000.1")] == Decimal("2.5")
    assert book.best_bid == (Decimal("2999.9"), Decimal("1"))
    assert book.best_ask == (Decimal("3000.1"), Decimal("2.5"))


def test_new_snapshot_replaces_entire_book() -> None:
    book = LocalOrderBook("ETHUSDT")
    book.apply_snapshot(snapshot())

    book.apply_snapshot(
        snapshot(
            generation=2,
            update_id=1,
            bids=((Decimal("2900"), Decimal("5")),),
            asks=((Decimal("2900.1"), Decimal("6")),),
        )
    )

    assert book.connection_generation == 2
    assert book.update_id == 1
    assert book.bids == {Decimal("2900"): Decimal("5")}
    assert book.asks == {Decimal("2900.1"): Decimal("6")}
    assert book.synchronized


def test_sequence_fault_unsynchronizes_book_and_old_generation_is_ignored() -> None:
    book = LocalOrderBook("ETHUSDT")
    book.apply_snapshot(snapshot(generation=2))

    assert not book.apply_delta(delta(generation=1, update_id=11))
    assert book.synchronized
    assert book.update_id == 10

    assert not book.apply_delta(delta(generation=2, update_id=12))
    assert not book.synchronized


def test_stale_book_is_not_execution_ready() -> None:
    book = LocalOrderBook("ETHUSDT")
    book.apply_snapshot(snapshot())

    assert book.is_execution_ready(NOW + timedelta(seconds=1), Decimal("2"))
    assert not book.is_execution_ready(NOW + timedelta(seconds=3), Decimal("2"))


def test_authoritative_trigger_source_is_last_trade() -> None:
    trade = TradeEvent(
        symbol="ETHUSDT",
        timestamp_utc=NOW,
        price=Decimal("3000.1"),
        size=Decimal("0.5"),
        side="Sell",
        trade_id="trade-1",
        sequence=10,
        connection_generation=3,
        raw_payload_hash="c" * 64,
    )
    ticker = TickerEvent(
        symbol="ETHUSDT",
        timestamp_utc=NOW,
        last_price=Decimal("3000.2"),
        mark_price=Decimal("3001"),
        index_price=Decimal("3002"),
        bid_price=Decimal("3000.1"),
        ask_price=Decimal("3000.2"),
        sequence=11,
        connection_generation=3,
        raw_payload_hash="d" * 64,
    )
    router = TriggerPriceRouter(DEFAULT_TRIGGER_PRICE_SOURCE)

    assert DEFAULT_TRIGGER_PRICE_SOURCE is TriggerPriceSource.LAST_TRADE
    assert router.from_ticker(ticker) is None
    trigger = router.from_trade(trade)
    assert trigger is not None
    assert trigger.source is TriggerPriceSource.LAST_TRADE
    assert trigger.observed_price == Decimal("3000.1")
    assert trigger.observed_timestamp == NOW
    assert trigger.connection_generation == 3


def test_mark_router_ignores_trades_instead_of_mixing_sources() -> None:
    trade = TradeEvent(
        symbol="ETHUSDT",
        timestamp_utc=NOW,
        price=Decimal("3000"),
        size=Decimal("1"),
        side="Buy",
        trade_id="trade-1",
        sequence=1,
        connection_generation=1,
        raw_payload_hash="e" * 64,
    )
    ticker = TickerEvent(
        symbol="ETHUSDT",
        timestamp_utc=NOW,
        last_price=Decimal("3000"),
        mark_price=Decimal("3001"),
        index_price=Decimal("3002"),
        bid_price=None,
        ask_price=None,
        sequence=2,
        connection_generation=1,
        raw_payload_hash="f" * 64,
    )
    router = TriggerPriceRouter(TriggerPriceSource.MARK_PRICE)

    assert router.from_trade(trade) is None
    trigger = router.from_ticker(ticker)
    assert trigger is not None
    assert trigger.observed_price == Decimal("3001")
    assert trigger.source is TriggerPriceSource.MARK_PRICE


def test_stale_trigger_blocks_market_data_health_gate() -> None:
    policy = MarketDataHealthPolicy(
        max_trigger_age_seconds=Decimal("5"),
        max_option_quote_age_seconds=Decimal("10"),
        max_order_book_age_seconds=Decimal("2"),
    )
    healthy = MarketDataHealthSnapshot(
        trigger_timestamp_utc=NOW - timedelta(seconds=1),
        instrument_loaded=True,
        websocket_connected=True,
        option_quote_timestamps_utc=(NOW - timedelta(seconds=2),),
        order_book_synchronized=True,
        order_book_timestamp_utc=NOW - timedelta(seconds=1),
        clock_synchronized=True,
    )

    healthy_result = evaluate_market_data_health(
        healthy,
        policy,
        as_of_utc=NOW,
        order_book_required=True,
    )
    stale_result = evaluate_market_data_health(
        MarketDataHealthSnapshot(
            trigger_timestamp_utc=NOW - timedelta(seconds=6),
            instrument_loaded=True,
            websocket_connected=True,
            option_quote_timestamps_utc=(NOW - timedelta(seconds=2),),
            order_book_synchronized=True,
            order_book_timestamp_utc=NOW - timedelta(seconds=1),
            clock_synchronized=True,
        ),
        policy,
        as_of_utc=NOW,
        order_book_required=True,
    )

    assert healthy_result.trading_allowed
    assert healthy_result.reasons == ()
    assert not stale_result.trading_allowed
    assert stale_result.reasons == ("trigger price is stale",)
    assert stale_result.event_type is MarketDataEventType.MARKET_DATA_STALE


def test_health_gate_reports_every_missing_prerequisite() -> None:
    result = evaluate_market_data_health(
        MarketDataHealthSnapshot(
            trigger_timestamp_utc=None,
            instrument_loaded=False,
            websocket_connected=False,
            option_quote_timestamps_utc=(),
            order_book_synchronized=False,
            order_book_timestamp_utc=None,
            clock_synchronized=False,
        ),
        MarketDataHealthPolicy(
            max_trigger_age_seconds=Decimal("5"),
            max_option_quote_age_seconds=Decimal("10"),
            max_order_book_age_seconds=Decimal("2"),
        ),
        as_of_utc=NOW,
        order_book_required=True,
    )

    assert not result.trading_allowed
    assert set(result.reasons) == {
        "trigger price is unavailable",
        "instrument is not loaded",
        "public websocket is disconnected",
        "option quotes are unavailable",
        "order book is not synchronized",
        "clock is not synchronized",
    }


def test_instrument_disable_is_a_normalized_market_event() -> None:
    def instrument(status: str) -> InstrumentSpec:
        return InstrumentSpec(
            symbol="ETHUSDT",
            category="linear",
            status=status,
            base_coin="ETH",
            quote_coin="USDT",
            settle_coin="USDT",
            price_filter=PriceFilter(Decimal("0.1"), None, None),
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

    event = normalize_instrument_change(
        instrument("Trading"),
        instrument("Settling"),
        observed_at_utc=NOW,
        connection_generation=2,
    )

    assert event.event_type is MarketDataEventType.INSTRUMENT_DISABLED
    assert event.symbol == "ETHUSDT"
    assert event.status == "Settling"
