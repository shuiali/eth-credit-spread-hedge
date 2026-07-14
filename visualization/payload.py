"""Backend adapter that prepares a complete dashboard result object."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from backtesting.monte_carlo import MonteCarloResult
from core.credit_spread import CreditSpread, ZERO
from core.ledger import LedgerEventType, StrategyResult


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
    kpi_rows: tuple[tuple[str, str, bool | None], ...]


def build_dashboard_payload(
    spread: CreditSpread,
    result: StrategyResult,
    monte_carlo: MonteCarloResult | None = None,
    *,
    selected_path_name: str = "selected path",
    payoff_point_count: int = 201,
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
    )

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
        kpi_rows=kpi_rows,
    )
