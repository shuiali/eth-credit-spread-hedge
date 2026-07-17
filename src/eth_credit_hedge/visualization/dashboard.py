"""Original-style renderer for a precomputed dashboard payload."""

from __future__ import annotations

import math
from typing import Any, Iterable

import matplotlib.pyplot as plt
from matplotlib.figure import Figure
from matplotlib.widgets import Slider

from eth_credit_hedge.visualization.payload import DashboardPayload


C_BG = "#0d1117"
C_PANEL = "#161b22"
C_PRICE = "#58a6ff"
C_COMBINED = "#3fb950"
C_OPTION = "#ff7b72"
C_HEDGE = "#79c0ff"
C_GRID = "#30363d"
C_ENTRY = "#f0883e"
C_TP = "#3fb950"
C_SL = "#f85149"
C_FLOOR = "#d29922"
C_LOCKED = "#8b949e"
C_TEXT = "#c9d1d9"
C_ACCENT = "#8b949e"


class Dashboard:
    """Render only values already present in ``DashboardPayload``."""

    def __init__(self, payload: DashboardPayload) -> None:
        self.payload = payload
        self.figure: Figure | None = None
        self._monte_carlo_lines: list[Any] = []
        self._path_selector: Slider | None = None

    def build_figure(self) -> Figure:
        figure = plt.figure(figsize=(22, 16), facecolor=C_BG)
        figure.suptitle(
            "ETH Credit-Spread Hedge — Exact $0.10-Tick Backtester",
            color=C_TEXT,
            fontsize=15,
            fontweight="bold",
        )
        grid = figure.add_gridspec(
            5,
            4,
            left=0.05,
            right=0.98,
            top=0.95,
            bottom=0.09,
            hspace=0.48,
            wspace=0.30,
            height_ratios=(4.5, 3.5, 3.0, 3.0, 2.7),
            width_ratios=(1.2, 1.2, 1.0, 1.0),
        )

        ax_price = figure.add_subplot(grid[0, :3])
        ax_payoff = figure.add_subplot(grid[0, 3])
        ax_pnl = figure.add_subplot(grid[1, :3])
        ax_kpi = figure.add_subplot(grid[1:, 3])
        ax_mc = figure.add_subplot(grid[2, :2])
        ax_terminal = figure.add_subplot(grid[2, 2])
        ax_risk = figure.add_subplot(grid[3, :2])
        ax_levels = figure.add_subplot(grid[3, 2])
        ax_log = figure.add_subplot(grid[4, :3])

        for axis in figure.axes:
            self._style_axis(axis)

        self._draw_price(ax_price)
        self._draw_payoff(ax_payoff)
        self._draw_pnl(ax_pnl)
        self._draw_kpis(ax_kpi)
        self._draw_monte_carlo(ax_mc)
        self._draw_terminal_distribution(ax_terminal)
        self._draw_risk_controls(ax_risk)
        self._draw_levels(ax_levels)
        self._draw_ledger(ax_log)
        self._build_path_selector(figure)

        self.figure = figure
        return figure

    def show(self) -> None:
        if self.figure is None:
            self.build_figure()
        plt.show(block=True)

    @staticmethod
    def _style_axis(axis: Any) -> None:
        axis.set_facecolor(C_PANEL)
        axis.tick_params(colors=C_ACCENT, labelsize=7)
        axis.grid(True, color=C_GRID, alpha=0.30, linewidth=0.5)
        for spine in axis.spines.values():
            spine.set_color(C_GRID)

    @staticmethod
    def _title(axis: Any, text: str) -> None:
        axis.set_title(text, color=C_TEXT, fontsize=10, fontweight="bold", pad=5)

    def _draw_price(self, axis: Any) -> None:
        self._title(
            axis,
            f"Price Path + Virtual Levels — {self.payload.selected_path_name}",
        )
        indices, prices = self._sample(self.payload.prices)
        axis.plot(
            indices,
            self._floats(prices),
            color=C_PRICE,
            linewidth=1.2,
            zorder=4,
            label="ETH price",
        )
        for boundary in self.payload.level_boundaries:
            axis.axhline(
                float(boundary),
                color=C_GRID,
                linestyle="--",
                linewidth=0.7,
                zorder=1,
            )

        marker_styles = {
            "ENTRY": ("v", C_ENTRY, "Fixed-stop entry"),
            "FLOOR_ENTRY": ("v", C_FLOOR, "Breakeven-floor entry"),
            "TP": ("^", C_TP, "Take profit"),
            "STOP": ("x", C_SL, "Stop"),
            "BREAKEVEN": ("x", C_FLOOR, "Breakeven exit"),
            "LOCKED": ("X", C_LOCKED, "Locked"),
        }
        for event_type, (marker, color, label) in marker_styles.items():
            events = [
                event for event in self.payload.events if event.event_type == event_type
            ]
            if events:
                axis.scatter(
                    [event.series_index for event in events],
                    [float(event.price) for event in events],
                    marker=marker,
                    color=color,
                    s=46,
                    zorder=6,
                    label=label,
                )
        axis.set_ylabel("ETH price (USD)", color=C_TEXT, fontsize=8)
        axis.set_xlabel("Accounting sample", color=C_ACCENT, fontsize=7)
        axis.legend(
            loc="best",
            fontsize=7,
            framealpha=0.30,
            facecolor=C_PANEL,
            labelcolor=C_TEXT,
            ncols=2,
        )

    def _draw_pnl(self, axis: Any) -> None:
        self._title(axis, "Option P&L + Hedge P&L + Combined P&L")
        series = (
            (self.payload.option_pnl, C_OPTION, "--", "Option P&L", 1.1),
            (
                self.payload.realized_hedge_pnl,
                C_FLOOR,
                ":",
                "Realized hedge P&L",
                0.9,
            ),
            (
                self.payload.open_hedge_pnl,
                C_ENTRY,
                ":",
                "Open hedge P&L",
                0.9,
            ),
            (self.payload.combined_pnl, C_COMBINED, "-", "Combined P&L", 1.8),
        )
        for values, color, line_style, label, line_width in series:
            indices, sampled = self._sample(values)
            axis.plot(
                indices,
                self._floats(sampled),
                color=color,
                linestyle=line_style,
                linewidth=line_width,
                alpha=0.9,
                label=label,
            )
        axis.axhline(0, color=C_ACCENT, linestyle=":", linewidth=0.8)
        axis.set_ylabel("P&L (USD)", color=C_TEXT, fontsize=8)
        axis.set_xlabel("Accounting sample", color=C_ACCENT, fontsize=7)
        axis.legend(
            loc="upper left",
            fontsize=7.5,
            framealpha=0.30,
            facecolor=C_PANEL,
            labelcolor=C_TEXT,
            ncols=2,
        )

    def _draw_payoff(self, axis: Any) -> None:
        self._title(axis, "Expiration Payoff")
        prices, pnl = zip(*self.payload.payoff_curve)
        float_prices = self._floats(prices)
        float_pnl = self._floats(pnl)
        axis.plot(float_prices, float_pnl, color=C_OPTION, linewidth=1.6)
        axis.fill_between(
            float_prices,
            float_pnl,
            0,
            where=[value >= 0 for value in float_pnl],
            color=C_TP,
            alpha=0.15,
        )
        axis.fill_between(
            float_prices,
            float_pnl,
            0,
            where=[value < 0 for value in float_pnl],
            color=C_SL,
            alpha=0.15,
        )
        axis.axhline(0, color=C_ACCENT, linestyle=":", linewidth=0.8)
        for boundary in self.payload.level_boundaries:
            axis.axvline(
                float(boundary),
                color=C_GRID,
                linestyle="--",
                linewidth=0.6,
            )
        axis.set_xlabel("ETH price", color=C_ACCENT, fontsize=7)
        axis.set_ylabel("Option P&L", color=C_TEXT, fontsize=8)

    def _draw_monte_carlo(self, axis: Any) -> None:
        self._monte_carlo_lines = []
        if not self.payload.monte_carlo_combined_paths:
            self._title(axis, "Monte Carlo Combined Paths")
            axis.text(
                0.5,
                0.5,
                "Run with --mc-paths to display the stochastic batch",
                color=C_ACCENT,
                ha="center",
                va="center",
                transform=axis.transAxes,
            )
            return

        loss_count = sum(
            not floor_pass for floor_pass in self.payload.monte_carlo_floor_passes
        )
        self._title(
            axis,
            "Monte Carlo Combined Paths — "
            f"{loss_count} loss / "
            f"{len(self.payload.monte_carlo_combined_paths)} total",
        )
        labels_drawn: set[bool] = set()
        for path, floor_pass in zip(
            self.payload.monte_carlo_combined_paths,
            self.payload.monte_carlo_floor_passes,
            strict=True,
        ):
            color = C_COMBINED if floor_pass else C_SL
            label = "Profitable throughout" if floor_pass else "Unprofitable (< $0)"
            indices, sampled = self._sample(path, max_points=3_000)
            line = axis.plot(
                indices,
                self._floats(sampled),
                color=color,
                alpha=0.26 if floor_pass else 0.72,
                linewidth=0.75 if floor_pass else 1.0,
                label=label if floor_pass not in labels_drawn else None,
            )[0]
            self._monte_carlo_lines.append(line)
            labels_drawn.add(floor_pass)
        axis.axhline(0, color=C_ACCENT, linestyle=":", linewidth=0.8)
        axis.set_ylabel("Combined P&L", color=C_TEXT, fontsize=8)
        axis.set_xlabel("Tick", color=C_ACCENT, fontsize=7)
        axis.legend(
            loc="best",
            fontsize=7,
            framealpha=0.30,
            facecolor=C_PANEL,
            labelcolor=C_TEXT,
        )

    def _draw_terminal_distribution(self, axis: Any) -> None:
        self._title(axis, "Terminal Combined-P&L Distribution")
        values = self._floats(self.payload.terminal_pnl_distribution)
        colors = [C_COMBINED if value >= 0 else C_SL for value in values]
        if len(values) == 1:
            axis.bar(values, [1], width=1.0, color=colors, alpha=0.8)
        else:
            axis.hist(
                values,
                bins=min(20, max(5, round(math.sqrt(len(values))))),
                color=C_COMBINED,
                alpha=0.65,
            )
            for value in values:
                if value < 0:
                    axis.axvline(value, color=C_SL, alpha=0.18, linewidth=0.8)
        axis.axvline(0, color=C_SL, linestyle="--", linewidth=0.9)
        axis.set_xlabel("Terminal combined P&L", color=C_ACCENT, fontsize=7)
        axis.set_ylabel("Paths", color=C_TEXT, fontsize=8)

    def _draw_risk_controls(self, axis: Any) -> None:
        self._title(axis, "Recovery Debt + Remaining Premium Stop Budget")
        for values, color, label in (
            (self.payload.recovery_debt, C_SL, "Recovery debt"),
            (
                self.payload.remaining_stop_budget,
                C_FLOOR,
                "Remaining stop budget",
            ),
        ):
            indices, sampled = self._sample(values)
            axis.plot(
                indices,
                self._floats(sampled),
                color=color,
                linewidth=1.3,
                label=label,
            )
        axis.axhline(0, color=C_ACCENT, linestyle=":", linewidth=0.8)
        axis.set_xlabel("Accounting sample", color=C_ACCENT, fontsize=7)
        axis.set_ylabel("USD", color=C_TEXT, fontsize=8)
        axis.legend(
            loc="best",
            fontsize=7,
            framealpha=0.30,
            facecolor=C_PANEL,
            labelcolor=C_TEXT,
        )

    def _draw_levels(self, axis: Any) -> None:
        self._title(axis, "Per-Level Quantity + State")
        labels = [f"L{level.level_id}" for level in self.payload.levels]
        quantities = [
            float(level.maximum_entry_quantity) for level in self.payload.levels
        ]
        bars = axis.bar(labels, quantities, color=C_ENTRY, alpha=0.75, zorder=1)
        if quantities:
            axis.set_ylim(0, max(quantities) * 1.9)
        axis.set_ylabel("Maximum ETH", color=C_TEXT, fontsize=7)
        axis.grid(axis="x", visible=False)
        rows = [
            [
                level.state,
                level.attempts,
                level.active_quantity,
                level.recovery_debt,
            ]
            for level in self.payload.levels
        ]
        table = axis.table(
            cellText=rows,
            rowLabels=labels,
            colLabels=("State", "Tries", "Active", "Debt"),
            cellLoc="center",
            bbox=(0.0, -0.02, 1.0, 0.44),
        )
        table.set_zorder(5)
        for bar in bars:
            bar.set_zorder(1)
        self._style_table(table, len(rows))

    def _build_path_selector(self, figure: Figure) -> None:
        if not self._monte_carlo_lines:
            return
        selector_axis = figure.add_axes(
            (0.10, 0.025, 0.50, 0.018),
            facecolor=C_PANEL,
        )
        selector_axis.tick_params(colors=C_ACCENT, labelsize=7)
        self._path_selector = Slider(
            selector_axis,
            "MC path (0 = all)",
            0,
            len(self._monte_carlo_lines),
            valinit=0,
            valstep=1,
            color=C_HEDGE,
        )
        self._path_selector.label.set_color(C_TEXT)
        self._path_selector.valtext.set_color(C_TEXT)
        self._path_selector.on_changed(self._select_monte_carlo_path)

    def _select_monte_carlo_path(self, value: float) -> None:
        selected = int(value)
        for index, line in enumerate(self._monte_carlo_lines, start=1):
            is_selected = selected == index
            line.set_alpha(0.8 if is_selected else 0.05 if selected else 0.26)
            line.set_linewidth(1.8 if is_selected else 0.75)
        if self.figure is not None:
            self.figure.canvas.draw_idle()

    def _draw_ledger(self, axis: Any) -> None:
        visible_events = self.payload.events[-20:]
        self._title(
            axis,
            f"Event Log — last {len(visible_events)} of {len(self.payload.events)}",
        )
        axis.axis("off")
        if not visible_events:
            axis.text(
                0.5,
                0.5,
                "No hedge events",
                color=C_ACCENT,
                ha="center",
                va="center",
            )
            return
        rows = [
            [
                event.sequence,
                event.tick_index,
                event.event_type,
                f"L{event.level_id}",
                event.price,
                event.quantity,
                event.realized_pnl,
                event.state,
            ]
            for event in visible_events
        ]
        table = axis.table(
            cellText=rows,
            colLabels=("#", "Tick", "Event", "Level", "Price", "Qty", "P&L", "State"),
            loc="center",
            cellLoc="center",
        )
        self._style_table(table, len(rows))

    def _draw_kpis(self, axis: Any) -> None:
        self._title(axis, "KPI Summary")
        axis.axis("off")
        row_height = 1.0 / (len(self.payload.kpi_rows) + 1)
        for index, (label, value, passed) in enumerate(self.payload.kpi_rows):
            y = 1 - row_height * (index + 0.8)
            value_color = C_TEXT
            if passed is True:
                value_color = C_TP
            elif passed is False:
                value_color = C_SL
            axis.text(
                0.03,
                y,
                f"{label}:",
                color=C_ACCENT,
                fontsize=7.8,
                transform=axis.transAxes,
                va="center",
            )
            axis.text(
                0.48,
                y,
                value,
                color=value_color,
                fontsize=7.8,
                fontweight="bold",
                transform=axis.transAxes,
                va="center",
            )

    @staticmethod
    def _style_table(table: Any, row_count: int) -> None:
        table.auto_set_font_size(False)
        table.set_fontsize(max(5.0, min(7.0, 9.5 - row_count * 0.15)))
        table.scale(1.0, max(0.55, min(1.05, 10.0 / max(row_count, 1))))
        for (row, _), cell in table.get_celld().items():
            cell.set_edgecolor(C_GRID)
            cell.set_facecolor(C_GRID if row == 0 else C_PANEL)
            cell.set_alpha(1.0)
            cell.set_zorder(5)
            cell.get_text().set_color(C_ACCENT if row == 0 else C_TEXT)
            cell.get_text().set_zorder(6)

    @staticmethod
    def _sample(
        values: Iterable[Any], *, max_points: int = 6_000
    ) -> tuple[list[int], list[Any]]:
        values_list = list(values)
        if len(values_list) <= max_points:
            return list(range(len(values_list))), values_list
        stride = math.ceil(len(values_list) / max_points)
        indices = list(range(0, len(values_list), stride))
        if indices[-1] != len(values_list) - 1:
            indices.append(len(values_list) - 1)
        return indices, [values_list[index] for index in indices]

    @staticmethod
    def _floats(values: Iterable[Any]) -> list[float]:
        return [float(value) for value in values]
