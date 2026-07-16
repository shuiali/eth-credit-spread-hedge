"""Calculation-free dashboard contract and render tests."""

import inspect
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import matplotlib

matplotlib.use("Agg")

from eth_credit_hedge.core.credit_spread import CreditSpread
from eth_credit_hedge.core.hedge_engine import HedgeEngine
from eth_credit_hedge.domain.instruments import (
    OptionContract,
    OptionFill,
    OptionMarketQuote,
)
from eth_credit_hedge.domain.option_position import (
    OptionLegPosition,
    OptionPositionState,
    OptionQuoteValidationPolicy,
    PutCreditSpreadPosition,
)
from eth_credit_hedge.visualization import dashboard as dashboard_module
from eth_credit_hedge.visualization.dashboard import C_COMBINED, C_SL, Dashboard
from eth_credit_hedge.visualization.payload import (
    build_dashboard_payload,
    build_option_position_dashboard_summary,
)


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


def test_dashboard_reports_unambiguous_stop_geometry_labels() -> None:
    spread = CreditSpread("3010", "3000", "2900", "1", "30")
    result = HedgeEngine(spread, 5).run_with_accounting(["3010", "3000"])
    payload = build_dashboard_payload(spread, result)
    rows = {label: value for label, value, _ in payload.kpi_rows}

    assert rows["Stop mode"] == "ENTRY_PERCENT"
    assert rows["Stop parameter"] == "0.15% of entry"
    assert rows["Stop distance"] == "$4.50"


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


def test_dashboard_payload_keeps_actual_option_valuations_separate() -> None:
    as_of = datetime(2026, 7, 14, 12, 0, 1, tzinfo=timezone.utc)
    expiry = datetime(2026, 7, 31, 8, tzinfo=timezone.utc)
    short_symbol = "ETH-31JUL26-3000-P-USDT"
    long_symbol = "ETH-31JUL26-2900-P-USDT"

    def contract(symbol: str, strike: str) -> OptionContract:
        return OptionContract(
            symbol=symbol,
            base_coin="ETH",
            quote_coin="USDT",
            settle_coin="USDT",
            option_type="Put",
            strike=Decimal(strike),
            expiry_time_utc=expiry,
            contract_multiplier=Decimal("1"),
        )

    def leg(
        *,
        option_contract: OptionContract,
        leg_side: str,
        fill_side: str,
        price: str,
    ) -> OptionLegPosition:
        fill = OptionFill(
            order_id=f"order-{fill_side}",
            execution_id=f"execution-{fill_side}",
            symbol=option_contract.symbol,
            side=fill_side,  # type: ignore[arg-type]
            price=Decimal(price),
            quantity=Decimal("1"),
            fee=Decimal("0.1"),
            timestamp_utc=as_of - timedelta(seconds=1),
        )
        return OptionLegPosition.from_fills(
            contract=option_contract,
            side=leg_side,  # type: ignore[arg-type]
            requested_quantity=Decimal("1"),
            fills=(fill,),
        )

    def quote(
        symbol: str,
        *,
        mark: str,
        bid: str,
        ask: str,
        delta: str,
    ) -> OptionMarketQuote:
        return OptionMarketQuote(
            symbol=symbol,
            timestamp_utc=as_of - timedelta(seconds=1),
            bid_price=Decimal(bid),
            bid_size=Decimal("5"),
            ask_price=Decimal(ask),
            ask_size=Decimal("5"),
            mark_price=Decimal(mark),
            underlying_price=Decimal("3011"),
            index_price=Decimal("3010"),
            bid_iv=Decimal("0.49"),
            ask_iv=Decimal("0.51"),
            mark_iv=Decimal("0.50"),
            delta=Decimal(delta),
            gamma=Decimal("0.001"),
            vega=Decimal("2.1"),
            theta=Decimal("-1.2"),
        )

    position = PutCreditSpreadPosition(
        short_put=leg(
            option_contract=contract(short_symbol, "3000"),
            leg_side="Short",
            fill_side="Sell",
            price="60",
        ),
        long_put=leg(
            option_contract=contract(long_symbol, "2900"),
            leg_side="Long",
            fill_side="Buy",
            price="30",
        ),
        state=OptionPositionState.OPEN,
    )
    policy = OptionQuoteValidationPolicy(
        max_quote_age_seconds=Decimal("5"),
        max_leg_timestamp_difference_seconds=Decimal("2"),
        max_index_price_difference_ratio=Decimal("0.001"),
    )
    summary = build_option_position_dashboard_summary(
        position,
        quote(short_symbol, mark="55", bid="54", ask="56", delta="-0.3"),
        quote(long_symbol, mark="28", bid="27", ask="29", delta="-0.2"),
        as_of_utc=as_of,
        validation_policy=policy,
    )

    spread = CreditSpread("3010", "3000", "2900", "1", "29.8")
    result = HedgeEngine(spread, 5).run_with_accounting(["3010", "2890"])
    payload = build_dashboard_payload(
        spread,
        result,
        option_position=summary,
    )
    rows = {label: value for label, value, _ in payload.kpi_rows}

    assert payload.option_position is summary
    assert summary.actual_net_credit == Decimal("29.8")
    assert summary.mark_pnl == Decimal("2.8")
    assert summary.liquidation_pnl == Decimal("0.8")
    assert summary.expiration_pnl == Decimal("29.8")
    assert rows["Actual option fills"] == (
        "Short 1: 1 ETH @ $60 / Long 1: 1 ETH @ $30"
    )
    assert rows["Actual net credit"] == "$29.8"
    assert rows["Mark P&L"] == "$2.8"
    assert rows["Liquidation P&L"] == "$0.8"
    assert rows["Expiration projection @ 3010"] == "$29.8"
    assert rows["Matched option quantity"] == "1 ETH"
    assert rows["Quote freshness"] == "Short 1.0s / Long 1.0s"
    assert "IV 0.50" in rows["Short IV / Greeks"]
    assert "IV 0.50" in rows["Long IV / Greeks"]

    dashboard = Dashboard(payload)
    figure = dashboard.build_figure()
    kpi_axis = next(axis for axis in figure.axes if axis.get_title() == "KPI Summary")
    rendered_text = {text.get_text() for text in kpi_axis.texts}
    assert "Actual net credit:" in rendered_text
    assert "Liquidation P&L:" in rendered_text
