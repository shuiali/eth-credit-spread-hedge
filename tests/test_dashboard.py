"""Calculation-free dashboard contract and render tests."""

import inspect
from dataclasses import replace
from decimal import Decimal

import matplotlib

matplotlib.use("Agg")

from eth_credit_hedge.core.credit_spread import CreditSpread
from eth_credit_hedge.core.hedge_engine import HedgeEngine
from eth_credit_hedge.visualization import dashboard as dashboard_module
from eth_credit_hedge.visualization.dashboard import C_COMBINED, C_SL, Dashboard
from eth_credit_hedge.visualization.payload import build_dashboard_payload


def make_payload():
    spread = CreditSpread("3010", "3000", "2900", "1", "30")
    result = HedgeEngine(spread, 5).run_with_accounting(["3010", "2890"])
    payload = build_dashboard_payload(
        spread,
        result,
        selected_path_name="test decline",
    )
    return replace(
        payload,
        monte_carlo_combined_paths=(
            tuple(map(Decimal, ("30", "20", "10"))),
            tuple(map(Decimal, ("30", "5", "-1"))),
        ),
        monte_carlo_floor_passes=(True, False),
    )


def test_dashboard_module_contains_no_strategy_or_backtest_dependency() -> None:
    source = inspect.getsource(dashboard_module)

    assert "from core" not in source
    assert "from backtesting" not in source
    assert "HedgeEngine" not in source
    assert "expiry_pnl" not in source


def test_dashboard_launcher_runs_the_proven_unhedged_baseline() -> None:
    source = inspect.getsource(__import__("dashboard_app"))

    assert "StrategyConfig.baseline" in source
    assert "experimental_floor" not in source


def test_dashboard_renders_complete_original_style_panels(tmp_path) -> None:
    dashboard = Dashboard(make_payload())
    figure = dashboard.build_figure()
    titles = {axis.get_title() for axis in figure.axes}
    output = tmp_path / "dashboard.png"
    figure.savefig(output, dpi=80, facecolor=figure.get_facecolor())

    assert len(figure.axes) == 10
    assert "Expiration Payoff" in titles
    assert "Option P&L + Hedge P&L + Combined P&L" in titles
    assert "KPI Summary" in titles
    assert "Terminal Combined-P&L Distribution" in titles
    assert "Recovery Debt + Remaining Premium Stop Budget" in titles
    assert "Per-Level Quantity + State" in titles
    assert any(title.startswith("Price Path + Virtual Levels") for title in titles)
    assert any(title.startswith("Monte Carlo Combined Paths") for title in titles)
    assert any(title.startswith("Event Log") for title in titles)

    pnl_axis = next(
        axis
        for axis in figure.axes
        if axis.get_title() == "Option P&L + Hedge P&L + Combined P&L"
    )
    assert {line.get_label() for line in pnl_axis.lines} >= {
        "Option P&L",
        "Realized hedge P&L",
        "Open hedge P&L",
        "Hedge P&L",
        "Combined P&L",
    }

    monte_carlo_axis = next(
        axis
        for axis in figure.axes
        if axis.get_title().startswith("Monte Carlo Combined Paths")
    )
    monte_carlo_colors = {line.get_color() for line in monte_carlo_axis.lines}
    assert C_COMBINED in monte_carlo_colors
    assert C_SL in monte_carlo_colors
    assert dashboard._path_selector is not None
    dashboard._path_selector.set_val(2)
    assert dashboard._monte_carlo_lines[1].get_linewidth() == 1.8

    risk_axis = next(
        axis
        for axis in figure.axes
        if axis.get_title() == "Recovery Debt + Remaining Premium Stop Budget"
    )
    assert {line.get_label() for line in risk_axis.lines} >= {
        "Recovery debt",
        "Remaining stop budget",
    }

    levels_axis = next(
        axis for axis in figure.axes if axis.get_title() == "Per-Level Quantity + State"
    )
    assert levels_axis.patches
    assert levels_axis.tables

    ledger_axis = next(
        axis for axis in figure.axes if axis.get_title().startswith("Event Log")
    )
    assert ledger_axis.tables
    assert output.stat().st_size > 10_000
