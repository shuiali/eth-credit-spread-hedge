"""Historical reconstruction and Monte Carlo tests."""

import json
from decimal import Decimal

import pytest

from eth_credit_hedge.backtesting.historical import (
    Candle,
    IntrabarPath,
    reconstruct_tick_path,
    replay_candles,
)
from eth_credit_hedge.backtesting.market_path import expand_price_anchors
from eth_credit_hedge.backtesting.monte_carlo import MonteCarloConfig, run_monte_carlo
from eth_credit_hedge.core.credit_spread import CreditSpread
from eth_credit_hedge.core.hedge_engine import HedgeEngine


def make_spread() -> CreditSpread:
    return CreditSpread("3010", "3000", "2900", "1", "30")


def test_intrabar_order_is_explicit_and_changes_the_tick_path() -> None:
    candle = Candle("t0", "3000", "3005", "2980", "2990")

    high_first = reconstruct_tick_path([candle], IntrabarPath.OPEN_HIGH_LOW_CLOSE)
    low_first = reconstruct_tick_path([candle], IntrabarPath.OPEN_LOW_HIGH_CLOSE)

    assert high_first == tuple(map(Decimal, ("3000", "3005", "2980", "2990")))
    assert low_first == tuple(map(Decimal, ("3000", "2980", "3005", "2990")))
    assert high_first != low_first


def test_historical_replay_persists_the_exact_reconstructed_ticks(tmp_path) -> None:
    candles = [Candle("t0", "3010", "3010", "2890", "2890")]
    output = tmp_path / "historical_ticks.json"
    replay = replay_candles(
        HedgeEngine(make_spread(), level_count=5),
        candles,
        IntrabarPath.OPEN_HIGH_LOW_CLOSE,
        output,
    )
    saved = json.loads(output.read_text(encoding="utf-8"))

    assert replay.saved_path == output
    assert saved["intrabar_path"] == "O_H_L_C"
    assert saved["ticks"] == [str(tick) for tick in replay.tick_path]
    assert replay.result.metrics.combined_pnl == Decimal("30")
    assert replay.result.metrics.number_of_tps == 5


def test_candle_gaps_are_rejected_until_gap_execution_is_defined() -> None:
    candles = [
        Candle("t0", "3010", "3010", "3000", "3000"),
        Candle("t1", "2999", "3001", "2990", "2995"),
    ]

    with pytest.raises(ValueError, match="gap handling is postponed"):
        reconstruct_tick_path(candles, IntrabarPath.OPEN_HIGH_LOW_CLOSE)


def test_seeded_monte_carlo_saves_every_path_and_required_metrics(tmp_path) -> None:
    output = tmp_path / "monte_carlo_paths.json"
    config = MonteCarloConfig(
        path_count=4,
        tick_count=401,
        macro_step_count=4,
        horizon_years=1 / 365,
        annual_volatility=0.8,
        seed=7,
    )
    first = run_monte_carlo(make_spread(), 5, config, output)
    second = run_monte_carlo(make_spread(), 5, config, tmp_path / "repeat.json")
    saved = json.loads(output.read_text(encoding="utf-8"))

    assert first.tick_paths == second.tick_paths
    assert len(first.tick_paths) == 4
    assert all(len(path) >= 401 for path in first.tick_paths)
    assert all(
        abs(current - previous) == Decimal("0.1")
        for path in first.tick_paths
        for previous, current in zip(path, path[1:])
    )
    assert len(saved["paths"]) == 4
    assert saved["config"]["tick_size"] == "0.1"
    assert first.summary.path_count == 4
    assert Decimal("0") <= first.summary.floor_pass_rate <= Decimal("1")
    assert len(first.summary.terminal_pnl_distribution) == 4
    assert len(first.path_metrics) == 4
    assert all(
        len(pnl_path) == len(strategy_result.snapshots) >= len(tick_path)
        for pnl_path, strategy_result, tick_path in zip(
            first.combined_pnl_paths,
            first.strategy_results,
            first.tick_paths,
        )
    )
    assert Decimal("0") <= first.summary.floor_pass_rate <= Decimal("1")
    assert saved["config"]["lock_policy"] == "UNHEDGED"


def test_market_bridge_is_long_noisy_and_uses_exact_ten_cent_ticks() -> None:
    first = expand_price_anchors(
        ["3010", "3000", "3007", "2980"],
        ticks_per_segment=1_000,
        seed=23,
    )
    second = expand_price_anchors(
        ["3010", "3000", "3007", "2980"],
        ticks_per_segment=1_000,
        seed=23,
    )
    moves = [current - previous for previous, current in zip(first, first[1:])]

    assert first == second
    assert len(first) >= 3_001
    assert first[0] == Decimal("3010")
    assert first[-1] == Decimal("2980")
    assert set(map(abs, moves)) == {Decimal("0.1")}
    assert Decimal("0.1") in moves
    assert Decimal("-0.1") in moves
