"""Historical inputs, option costs, stress paths, and predeclared gates."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from eth_credit_hedge.backtesting.historical_inputs import (
    load_historical_market_samples_csv,
    load_normalized_capture,
)
from eth_credit_hedge.backtesting.option_execution import (
    simulate_option_liquidation,
    simulate_option_spread_entry,
)
from eth_credit_hedge.backtesting.simulation_validation import (
    PREDECLARED_THRESHOLDS,
    SimulationValidationMetrics,
    evaluate_simulation_acceptance,
)
from eth_credit_hedge.backtesting.stress_paths import (
    generate_historical_bootstrap_path,
    generate_jump_diffusion_path,
    generate_regime_switching_path,
    generate_repeated_oscillation_path,
    generate_v_shaped_path,
    generate_volatility_clustered_path,
)
from eth_credit_hedge.backtesting.option_stress import build_option_stress_scenarios
from eth_credit_hedge.domain.instruments import OptionMarketQuote


NOW = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)


def quote(symbol: str, bid: str, ask: str) -> OptionMarketQuote:
    return OptionMarketQuote(
        symbol=symbol,
        timestamp_utc=NOW,
        bid_price=Decimal(bid),
        bid_size=Decimal("10"),
        ask_price=Decimal(ask),
        ask_size=Decimal("10"),
        mark_price=(Decimal(bid) + Decimal(ask)) / Decimal("2"),
        underlying_price=Decimal("3000"),
        index_price=Decimal("3000"),
        bid_iv=Decimal("0.7"),
        ask_iv=Decimal("0.72"),
        mark_iv=Decimal("0.71"),
        delta=None,
        gamma=None,
        vega=None,
        theta=None,
    )


def test_option_entry_and_liquidation_use_executable_bid_ask_and_fees() -> None:
    short = quote("ETH-31JUL26-3000-P-USDT", "60", "62")
    long = quote("ETH-31JUL26-2900-P-USDT", "29", "30")

    entry = simulate_option_spread_entry(
        short_quote=short,
        long_quote=long,
        requested_quantity=Decimal("1"),
        fill_fraction=Decimal("0.4"),
        fee_rate=Decimal("0.0003"),
    )
    liquidation = simulate_option_liquidation(
        short_quote=short,
        long_quote=long,
        quantity=entry.filled_quantity,
        fee_rate=Decimal("0.0003"),
    )

    assert entry.short_fill_price == Decimal("60")
    assert entry.long_fill_price == Decimal("30")
    assert entry.filled_quantity == Decimal("0.4")
    assert entry.gross_credit == Decimal("12.0")
    assert entry.fees == Decimal("0.01080")
    assert entry.net_credit == Decimal("11.98920")
    assert liquidation.short_buy_price == Decimal("62")
    assert liquidation.long_sell_price == Decimal("29")
    assert liquidation.net_cash_flow < Decimal("0")


def test_normalized_capture_replays_exact_trade_order(tmp_path: Path) -> None:
    capture = tmp_path / "capture.jsonl"
    capture.write_text(
        "\n".join(
            (
                '{"timestamp":"2026-07-14T12:00:00+00:00","symbol":"ETHUSDT",'
                '"event_type":"TradeObserved","sequence":1,"update_id":null,'
                '"price":"3000","size":"1","book_side":null,'
                '"connection_generation":1,"raw_payload_hash":"' + "a" * 64 + '"}',
                '{"timestamp":"2026-07-14T12:00:01+00:00","symbol":"ETHUSDT",'
                '"event_type":"TickerUpdated","sequence":2,"update_id":null,'
                '"price":"2999.5","size":null,"book_side":null,'
                '"connection_generation":1,"raw_payload_hash":"' + "b" * 64 + '"}',
                '{"timestamp":"2026-07-14T12:00:02+00:00","symbol":"ETHUSDT",'
                '"event_type":"TradeObserved","sequence":3,"update_id":null,'
                '"price":"2999","size":"2","book_side":null,'
                '"connection_generation":1,"raw_payload_hash":"' + "c" * 64 + '"}',
            )
        )
        + "\n",
        encoding="utf-8",
    )

    replay = load_normalized_capture(capture, symbol="ETHUSDT")

    assert replay.trade_prices == (Decimal("3000"), Decimal("2999"))
    assert replay.sequences == (1, 3)
    assert replay.source_hashes == ("a" * 64, "c" * 64)


def test_historical_input_includes_marks_options_funding_and_metadata(
    tmp_path: Path,
) -> None:
    source = tmp_path / "history.csv"
    source.write_text(
        "timestamp,trade_price,mark_price,index_price,option_symbol,option_bid,"
        "option_ask,option_mark,option_iv,funding_rate,instrument_status\n"
        "2026-07-14T12:00:00+00:00,3000,3000.1,3000.2,"
        "ETH-31JUL26-3000-P-USDT,60,62,61,0.71,0.0001,Trading\n",
        encoding="utf-8",
    )

    samples = load_historical_market_samples_csv(source)

    assert len(samples) == 1
    assert samples[0].trade_price == Decimal("3000")
    assert samples[0].option_bid == Decimal("60")
    assert samples[0].funding_rate == Decimal("0.0001")
    assert samples[0].instrument_status == "Trading"


def test_seeded_stress_models_are_reproducible_and_labeled_separately() -> None:
    jump_a = generate_jump_diffusion_path(
        Decimal("3000"), steps=20, seed=1, jump_probability=Decimal("0.2")
    )
    jump_b = generate_jump_diffusion_path(
        Decimal("3000"), steps=20, seed=1, jump_probability=Decimal("0.2")
    )
    clustered = generate_volatility_clustered_path(
        Decimal("3000"), steps=20, seed=2
    )
    regime = generate_regime_switching_path(Decimal("3000"), steps=20, seed=3)
    bootstrap = generate_historical_bootstrap_path(
        Decimal("3000"),
        returns=(Decimal("0.01"), Decimal("-0.02"), Decimal("0.005")),
        steps=20,
        seed=4,
    )
    v_shape = generate_v_shaped_path(
        Decimal("3000"),
        bottom_price=Decimal("2700"),
        recovery_price=Decimal("3050"),
        steps_per_leg=10,
    )
    oscillation = generate_repeated_oscillation_path(
        upper_price=Decimal("3010"),
        lower_price=Decimal("2990"),
        cycles=4,
    )

    assert jump_a == jump_b
    assert {path.model for path in (jump_a, clustered, regime, bootstrap, v_shape)} == {
        "JUMP_DIFFUSION",
        "VOLATILITY_CLUSTERING",
        "REGIME_SWITCHING",
        "HISTORICAL_BOOTSTRAP",
        "V_SHAPED_STRESS",
    }
    assert all(price > 0 for path in (jump_a, clustered, regime) for price in path.prices)
    assert oscillation.model == "REPEATED_ENTRY_OSCILLATION"
    assert oscillation.prices == (
        Decimal("3010"),
        Decimal("2990"),
        Decimal("3010"),
        Decimal("2990"),
        Decimal("3010"),
        Decimal("2990"),
        Decimal("3010"),
        Decimal("2990"),
        Decimal("3010"),
    )


def test_option_volatility_scenario_families_are_explicitly_labeled() -> None:
    scenarios = build_option_stress_scenarios(
        spot=Decimal("3000"),
        short_iv=Decimal("0.70"),
        long_iv=Decimal("0.75"),
        short_mark=Decimal("60"),
        long_mark=Decimal("30"),
    )

    assert tuple(scenario.name for scenario in scenarios) == (
        "SPOT_UNCHANGED_IV_RISE",
        "SPOT_FALL_IV_FALL",
        "SKEW_STEEPENS",
        "NEAR_EXPIRY_DECAY",
    )
    assert all(len(scenario.points) >= 2 for scenario in scenarios)


def test_predeclared_thresholds_pass_and_fail_without_posthoc_changes() -> None:
    passing = SimulationValidationMetrics(
        maximum_unprotected_ms=500,
        duplicate_executions_counted=0,
        unknown_state_order_count=0,
        restart_attempts=10,
        restart_successes=10,
        risk_limit_bypass_count=0,
        reproducible_pnl_runs=10,
        total_pnl_runs=10,
    )
    failing = SimulationValidationMetrics(
        maximum_unprotected_ms=5000,
        duplicate_executions_counted=1,
        unknown_state_order_count=1,
        restart_attempts=10,
        restart_successes=9,
        risk_limit_bypass_count=1,
        reproducible_pnl_runs=9,
        total_pnl_runs=10,
    )

    passed = evaluate_simulation_acceptance(passing, PREDECLARED_THRESHOLDS)
    failed = evaluate_simulation_acceptance(failing, PREDECLARED_THRESHOLDS)

    assert passed.accepted
    assert passed.failures == ()
    assert not failed.accepted
    assert len(failed.failures) == 6
