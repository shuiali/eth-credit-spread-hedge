"""Backend adapter that prepares a complete dashboard result object."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from eth_credit_hedge.backtesting.monte_carlo import MonteCarloResult
from eth_credit_hedge.core.credit_spread import CreditSpread, ZERO
from eth_credit_hedge.core.ledger import LedgerEventType, StrategyResult
from eth_credit_hedge.domain.instruments import OptionMarketQuote
from eth_credit_hedge.domain.option_position import (
    DEFAULT_QUOTE_VALIDATION_POLICY,
    OptionQuoteValidationPolicy,
    PutCreditSpreadPosition,
)


@dataclass(frozen=True, slots=True)
class DashboardEvent:
    sequence: int
    tick_index: int
    series_index: int
    event_type: str
    level_id: int
    price: Decimal
    quantity: Decimal
    realized_pnl: Decimal
    state: str


@dataclass(frozen=True, slots=True)
class DashboardLevel:
    level_id: int
    entry_price: Decimal
    tp_price: Decimal
    stop_price: Decimal
    state: str
    attempts: int
    active_quantity: Decimal
    active_is_floor: bool
    maximum_entry_quantity: Decimal
    recovery_debt: Decimal
    realized_stop_losses: Decimal
    realized_tp_profit: Decimal


@dataclass(frozen=True, slots=True)
class DashboardOptionPositionSummary:
    state: str
    short_symbol: str
    long_symbol: str
    short_fill_count: int
    long_fill_count: int
    short_filled_quantity: Decimal
    long_filled_quantity: Decimal
    short_average_entry_price: Decimal
    long_average_entry_price: Decimal
    matched_quantity: Decimal
    actual_net_credit: Decimal
    mark_pnl: Decimal
    liquidation_pnl: Decimal
    expiration_underlying_price: Decimal
    expiration_pnl: Decimal
    time_to_expiry_seconds: Decimal
    short_quote_age_seconds: Decimal
    long_quote_age_seconds: Decimal
    short_mark_iv: Decimal | None
    short_delta: Decimal | None
    short_gamma: Decimal | None
    short_vega: Decimal | None
    short_theta: Decimal | None
    long_mark_iv: Decimal | None
    long_delta: Decimal | None
    long_gamma: Decimal | None
    long_vega: Decimal | None
    long_theta: Decimal | None


@dataclass(frozen=True, slots=True)
class DashboardPayload:
    selected_path_name: str
    spread_inputs: tuple[tuple[str, str], ...]
    payoff_curve: tuple[tuple[Decimal, Decimal], ...]
    level_boundaries: tuple[Decimal, ...]
    prices: tuple[Decimal, ...]
    option_pnl: tuple[Decimal, ...]
    realized_hedge_pnl: tuple[Decimal, ...]
    open_hedge_pnl: tuple[Decimal, ...]
    hedge_pnl: tuple[Decimal, ...]
    combined_pnl: tuple[Decimal, ...]
    recovery_debt: tuple[Decimal, ...]
    remaining_stop_budget: tuple[Decimal, ...]
    events: tuple[DashboardEvent, ...]
    levels: tuple[DashboardLevel, ...]
    monte_carlo_combined_paths: tuple[tuple[Decimal, ...], ...]
    monte_carlo_floor_passes: tuple[bool, ...]
    terminal_pnl_distribution: tuple[Decimal, ...]
    floor_pass_rate: Decimal
    floor_pass_count: int
    floor_path_count: int
    minimum_combined_pnl: Decimal
    option_position: DashboardOptionPositionSummary | None
    kpi_rows: tuple[tuple[str, str, bool | None], ...]


def build_option_position_dashboard_summary(
    position: PutCreditSpreadPosition,
    short_quote: OptionMarketQuote,
    long_quote: OptionMarketQuote,
    *,
    as_of_utc: datetime,
    validation_policy: OptionQuoteValidationPolicy = DEFAULT_QUOTE_VALIDATION_POLICY,
    short_instrument_status: str = "Trading",
    long_instrument_status: str = "Trading",
    expiration_underlying_price: Decimal | None = None,
) -> DashboardOptionPositionSummary:
    """Validate and precompute every actual-position value shown by the UI."""
    if as_of_utc.tzinfo is None or as_of_utc.utcoffset() is None:
        raise ValueError("dashboard valuation time must be timezone-aware")
    as_of = as_of_utc.astimezone(timezone.utc)
    mark_pnl = position.mark_pnl(
        short_quote,
        long_quote,
        as_of_utc=as_of,
        validation_policy=validation_policy,
        short_instrument_status=short_instrument_status,
        long_instrument_status=long_instrument_status,
    )
    liquidation_pnl = position.liquidation_pnl(
        short_quote,
        long_quote,
        as_of_utc=as_of,
        validation_policy=validation_policy,
        short_instrument_status=short_instrument_status,
        long_instrument_status=long_instrument_status,
    )
    terminal_price = (
        short_quote.index_price
        if expiration_underlying_price is None
        else Decimal(str(expiration_underlying_price))
    )
    expiry = position.short_put.contract.expiry_time_utc
    return DashboardOptionPositionSummary(
        state=position.state.value,
        short_symbol=position.short_put.contract.symbol,
        long_symbol=position.long_put.contract.symbol,
        short_fill_count=len(position.short_put.fills),
        long_fill_count=len(position.long_put.fills),
        short_filled_quantity=position.short_put.filled_quantity,
        long_filled_quantity=position.long_put.filled_quantity,
        short_average_entry_price=position.short_put.average_entry_price,
        long_average_entry_price=position.long_put.average_entry_price,
        matched_quantity=position.matched_quantity,
        actual_net_credit=position.actual_net_credit,
        mark_pnl=mark_pnl,
        liquidation_pnl=liquidation_pnl,
        expiration_underlying_price=terminal_price,
        expiration_pnl=position.expiration_pnl(terminal_price),
        time_to_expiry_seconds=Decimal(str((expiry - as_of).total_seconds())),
        short_quote_age_seconds=Decimal(
            str((as_of - short_quote.timestamp_utc).total_seconds())
        ),
        long_quote_age_seconds=Decimal(
            str((as_of - long_quote.timestamp_utc).total_seconds())
        ),
        short_mark_iv=short_quote.mark_iv,
        short_delta=short_quote.delta,
        short_gamma=short_quote.gamma,
        short_vega=short_quote.vega,
        short_theta=short_quote.theta,
        long_mark_iv=long_quote.mark_iv,
        long_delta=long_quote.delta,
        long_gamma=long_quote.gamma,
        long_vega=long_quote.vega,
        long_theta=long_quote.theta,
    )


def build_dashboard_payload(
    spread: CreditSpread,
    result: StrategyResult,
    monte_carlo: MonteCarloResult | None = None,
    *,
    selected_path_name: str = "selected path",
    payoff_point_count: int = 201,
    option_position: DashboardOptionPositionSummary | None = None,
) -> DashboardPayload:
    """Prepare every value the visualization needs before it renders anything."""
    if payoff_point_count < 2:
        raise ValueError("payoff point count must be at least two")
    lower = spread.long_put_strike - (
        spread.short_put_strike - spread.long_put_strike
    ) * Decimal("0.25")
    upper = spread.short_put_strike + (
        spread.short_put_strike - spread.long_put_strike
    ) * Decimal("0.25")
    step = (upper - lower) / Decimal(payoff_point_count - 1)
    payoff_curve = tuple(
        (price := lower + step * Decimal(index), spread.expiry_pnl(price))
        for index in range(payoff_point_count)
    )

    maximum_quantities: dict[int, Decimal] = {}
    for event in result.events:
        if event.event_type in (
            LedgerEventType.ENTRY,
            LedgerEventType.FLOOR_ENTRY,
        ):
            maximum_quantities[event.level_id] = max(
                maximum_quantities.get(event.level_id, ZERO), event.quantity
            )

    event_series_indices = {
        snapshot.event_sequence: index
        for index, snapshot in enumerate(result.snapshots)
        if snapshot.event_sequence is not None
    }
    events = tuple(
        DashboardEvent(
            sequence=event.sequence,
            tick_index=event.tick_index,
            series_index=event_series_indices.get(event.sequence, event.tick_index),
            event_type=event.event_type.value,
            level_id=event.level_id,
            price=event.price,
            quantity=event.quantity,
            realized_pnl=event.realized_pnl,
            state=event.level_state.value,
        )
        for event in result.events
    )
    levels = tuple(
        DashboardLevel(
            level_id=level.level_id,
            entry_price=level.entry_price,
            tp_price=level.tp_price,
            stop_price=level.stop_price,
            state=level.state.value,
            attempts=level.attempts,
            active_quantity=level.active_quantity,
            active_is_floor=level.active_is_floor,
            maximum_entry_quantity=maximum_quantities.get(level.level_id, ZERO),
            recovery_debt=level.recovery_debt,
            realized_stop_losses=level.realized_stop_losses,
            realized_tp_profit=level.realized_tp_profit,
        )
        for level in result.levels
    )
    snapshots = result.snapshots

    if monte_carlo is None:
        mc_paths: tuple[tuple[Decimal, ...], ...] = ()
        mc_floor_passes: tuple[bool, ...] = ()
        terminal = (result.metrics.combined_pnl,)
        floor_count = int(result.metrics.floor_pass)
        path_count = 1
        floor_rate = Decimal(floor_count)
        minimum = result.metrics.minimum_combined_pnl
    else:
        mc_paths = monte_carlo.combined_pnl_paths
        mc_floor_passes = tuple(
            metric.floor_pass for metric in monte_carlo.path_metrics
        )
        terminal = monte_carlo.summary.terminal_pnl_distribution
        path_count = monte_carlo.summary.path_count
        floor_rate = monte_carlo.summary.floor_pass_rate
        floor_count = int(floor_rate * Decimal(path_count))
        minimum = monte_carlo.summary.minimum_combined_pnl

    option_pnl = tuple(item.option_terminal_value_pnl for item in snapshots)
    realized_hedge_pnl = tuple(item.realized_hedge_pnl for item in snapshots)
    open_hedge_pnl = tuple(item.open_hedge_pnl for item in snapshots)
    hedge_pnl = tuple(
        realized + open_position
        for realized, open_position in zip(realized_hedge_pnl, open_hedge_pnl)
    )
    combined_pnl = tuple(item.combined_terminal_value_pnl for item in snapshots)
    final_snapshot = snapshots[-1]
    metrics = result.metrics
    state_abbreviations = {"READY": "R", "ACTIVE": "A", "PAID": "P", "LOCKED": "L"}
    level_states = "  ".join(
        f"L{level.level_id}:{state_abbreviations[level.state.value]}"
        + ("F" if level.active_is_floor else "")
        for level in result.levels
    )
    kpi_rows = (
        ("ETH spot", str(spread.spot), None),
        (
            "Put spread",
            f"{spread.short_put_strike} / {spread.long_put_strike}",
            None,
        ),
        ("Premium credit", f"${spread.premium_credit}", None),
        ("Selected ticks", f"{len(result.input_prices):,}", None),
        ("Accounting samples", f"{len(result.snapshots):,}", None),
        ("Final option P&L", f"${final_snapshot.option_terminal_value_pnl}", None),
        (
            "Final hedge P&L",
            f"${final_snapshot.realized_hedge_pnl + final_snapshot.open_hedge_pnl}",
            None,
        ),
        (
            "Final combined P&L",
            f"${metrics.combined_pnl}",
            metrics.combined_pnl >= ZERO,
        ),
        (
            "Path minimum",
            f"${metrics.minimum_combined_pnl}",
            metrics.floor_pass,
        ),
        (
            "Entries / stops / TPs",
            f"{metrics.number_of_entries} / {metrics.number_of_stops} / {metrics.number_of_tps}",
            None,
        ),
        (
            "Floor entries / exits",
            f"{metrics.floor_entry_count} / {metrics.breakeven_exit_count}",
            None,
        ),
        ("Maximum hedge", f"{metrics.maximum_quantity} ETH", None),
        (
            "Stop budget used",
            f"${metrics.premium_budget_consumed} / ${spread.premium_credit}",
            None,
        ),
        (
            "Recovery debt",
            f"${metrics.outstanding_recovery_debt}",
            metrics.outstanding_recovery_debt == ZERO,
        ),
        (
            "Locked levels",
            str(metrics.locked_levels or "None"),
            not metrics.locked_levels,
        ),
        ("Monte Carlo paths", str(path_count if monte_carlo else 0), None),
        ("Floor-pass rate", f"{float(floor_rate):.1%}", floor_rate == Decimal("1")),
        (
            "Loss paths",
            str(path_count - floor_count if monte_carlo else 0),
            (path_count - floor_count == 0) if monte_carlo else None,
        ),
        ("Monte Carlo minimum", f"${minimum}", minimum >= ZERO),
        ("Level states", level_states, None),
    ) + _option_position_kpi_rows(option_position)

    return DashboardPayload(
        selected_path_name=selected_path_name,
        spread_inputs=(
            ("ETH spot", str(spread.spot)),
            ("Short put", str(spread.short_put_strike)),
            ("Long put", str(spread.long_put_strike)),
            ("Option quantity", str(spread.option_quantity)),
            ("Premium credit", str(spread.premium_credit)),
            ("Maximum profit", str(spread.max_profit())),
            ("Maximum loss", str(spread.max_loss())),
        ),
        payoff_curve=payoff_curve,
        level_boundaries=tuple(level.entry_price for level in result.levels)
        + (result.levels[-1].tp_price,),
        prices=result.prices,
        option_pnl=option_pnl,
        realized_hedge_pnl=realized_hedge_pnl,
        open_hedge_pnl=open_hedge_pnl,
        hedge_pnl=hedge_pnl,
        combined_pnl=combined_pnl,
        recovery_debt=tuple(item.outstanding_recovery_debt for item in snapshots),
        remaining_stop_budget=tuple(
            item.remaining_premium_stop_budget for item in snapshots
        ),
        events=events,
        levels=levels,
        monte_carlo_combined_paths=mc_paths,
        monte_carlo_floor_passes=mc_floor_passes,
        terminal_pnl_distribution=terminal,
        floor_pass_rate=floor_rate,
        floor_pass_count=floor_count,
        floor_path_count=path_count,
        minimum_combined_pnl=minimum,
        option_position=option_position,
        kpi_rows=kpi_rows,
    )


def _option_position_kpi_rows(
    summary: DashboardOptionPositionSummary | None,
) -> tuple[tuple[str, str, bool | None], ...]:
    if summary is None:
        return (("Actual option position", "Not supplied", None),)
    return (
        ("Option position state", summary.state, None),
        (
            "Actual option fills",
            f"Short {summary.short_fill_count}: "
            f"{summary.short_filled_quantity} ETH @ "
            f"${summary.short_average_entry_price} / "
            f"Long {summary.long_fill_count}: "
            f"{summary.long_filled_quantity} ETH @ "
            f"${summary.long_average_entry_price}",
            None,
        ),
        ("Matched option quantity", f"{summary.matched_quantity} ETH", None),
        (
            "Actual net credit",
            f"${summary.actual_net_credit}",
            summary.actual_net_credit > ZERO,
        ),
        ("Mark P&L", f"${summary.mark_pnl}", summary.mark_pnl >= ZERO),
        (
            "Liquidation P&L",
            f"${summary.liquidation_pnl}",
            summary.liquidation_pnl >= ZERO,
        ),
        (
            f"Expiration projection @ {summary.expiration_underlying_price}",
            f"${summary.expiration_pnl}",
            summary.expiration_pnl >= ZERO,
        ),
        (
            "Time to option expiry",
            _format_duration(summary.time_to_expiry_seconds),
            summary.time_to_expiry_seconds > ZERO,
        ),
        (
            "Quote freshness",
            f"Short {summary.short_quote_age_seconds}s / "
            f"Long {summary.long_quote_age_seconds}s",
            None,
        ),
        (
            "Short IV / Greeks",
            _format_iv_and_greeks(
                summary.short_mark_iv,
                summary.short_delta,
                summary.short_gamma,
                summary.short_vega,
                summary.short_theta,
            ),
            None,
        ),
        (
            "Long IV / Greeks",
            _format_iv_and_greeks(
                summary.long_mark_iv,
                summary.long_delta,
                summary.long_gamma,
                summary.long_vega,
                summary.long_theta,
            ),
            None,
        ),
    )


def _format_duration(seconds: Decimal) -> str:
    total_seconds = max(0, int(seconds))
    days, remainder = divmod(total_seconds, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, _ = divmod(remainder, 60)
    return f"{days}d {hours}h {minutes}m"


def _format_iv_and_greeks(
    mark_iv: Decimal | None,
    delta: Decimal | None,
    gamma: Decimal | None,
    vega: Decimal | None,
    theta: Decimal | None,
) -> str:
    def value(item: Decimal | None) -> str:
        return "N/A" if item is None else str(item)

    return (
        f"IV {value(mark_iv)}  Δ {value(delta)}  Γ {value(gamma)}  "
        f"V {value(vega)}  Θ {value(theta)}"
    )
