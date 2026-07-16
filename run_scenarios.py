"""Named deterministic scenarios with full-ledger expectations."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from eth_credit_hedge.core.credit_spread import CreditSpread
from eth_credit_hedge.core.hedge_engine import HedgeEngine
from eth_credit_hedge.core.ledger import LedgerEvent, StrategyResult


@dataclass(frozen=True, slots=True)
class ExpectedEvent:
    tick_index: int
    event_type: str
    level_id: int
    price: str
    quantity: str
    realized_pnl: str
    level_state: str
    attempt: int
    projected_stop_loss: str = "0"
    zone_profit_component: str = "0"
    recovery_profit_component: str = "0"
    recovery_allocations: tuple[tuple[int, str], ...] = ()

    def canonical(self, sequence: int) -> tuple[object, ...]:
        return (
            sequence,
            self.tick_index,
            self.event_type,
            self.level_id,
            Decimal(self.price),
            Decimal(self.quantity),
            Decimal(self.realized_pnl),
            self.level_state,
            self.attempt,
            Decimal(self.projected_stop_loss),
            Decimal(self.zone_profit_component),
            Decimal(self.recovery_profit_component),
            tuple(
                (level_id, Decimal(amount))
                for level_id, amount in self.recovery_allocations
            ),
        )


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    prices: tuple[str, ...]
    expected_events: tuple[ExpectedEvent, ...]
    expected_combined_pnl: str
    expected_final_states: tuple[str, ...]
    premium_credit: str = "30"
    long_put_strike: str = "2900"
    level_count: int = 5


@dataclass(frozen=True, slots=True)
class ScenarioRun:
    scenario: Scenario
    result: StrategyResult
    expected_ledger: tuple[tuple[object, ...], ...]
    actual_ledger: tuple[tuple[object, ...], ...]
    passed: bool


def _event(
    tick: int,
    kind: str,
    level: int,
    price: str,
    quantity: str,
    pnl: str,
    state: str,
    attempt: int,
    *,
    projected: str = "0",
    zone: str = "0",
    recovery: str = "0",
    allocations: tuple[tuple[int, str], ...] = (),
) -> ExpectedEvent:
    return ExpectedEvent(
        tick_index=tick,
        event_type=kind,
        level_id=level,
        price=price,
        quantity=quantity,
        realized_pnl=pnl,
        level_state=state,
        attempt=attempt,
        projected_stop_loss=projected,
        zone_profit_component=zone,
        recovery_profit_component=recovery,
        recovery_allocations=allocations,
    )


def _entry(
    tick: int,
    level: int,
    price: str,
    quantity: str = "1",
    attempt: int = 1,
    projected: str | None = None,
) -> ExpectedEvent:
    base_projected = {
        1: "3",
        2: "3",
        3: "3",
        4: "3",
        5: "3",
    }[level]
    return _event(
        tick,
        "ENTRY",
        level,
        price,
        quantity,
        "0",
        "ACTIVE",
        attempt,
        projected=projected or base_projected,
    )


def _tp(
    tick: int,
    level: int,
    price: str,
    quantity: str = "1",
    pnl: str = "20",
    attempt: int = 1,
    recovery: str = "0",
    allocations: tuple[tuple[int, str], ...] = (),
) -> ExpectedEvent:
    return _event(
        tick,
        "TP",
        level,
        price,
        quantity,
        pnl,
        "PAID",
        attempt,
        zone="20",
        recovery=recovery,
        allocations=allocations,
    )


def _full_decline_events(tick: int = 1) -> tuple[ExpectedEvent, ...]:
    boundaries = (
        (1, "3000", "2980"),
        (2, "2980", "2960"),
        (3, "2960", "2940"),
        (4, "2940", "2920"),
        (5, "2920", "2900"),
    )
    events: list[ExpectedEvent] = []
    for level, entry_price, tp_price in boundaries:
        events.extend((_entry(tick, level, entry_price), _tp(tick, level, tp_price)))
    return tuple(events)


def named_scenarios() -> tuple[Scenario, ...]:
    first_entry = _entry(1, 1, "3000")
    first_stop = _event(2, "STOP", 1, "3003", "1", "-3", "READY", 1)
    recovery_entry = _entry(3, 1, "3000", "1.15", 2, "3.45")
    recovery_tp = _tp(
        4,
        1,
        "2980",
        "1.15",
        "23",
        2,
        "3",
        ((1, "3"),),
    )

    return (
        Scenario(
            name="smooth_decline",
            prices=("3010", "3000", "2980"),
            expected_events=(first_entry, _tp(2, 1, "2980")),
            expected_combined_pnl="20",
            expected_final_states=("PAID",),
            premium_credit="20",
            long_put_strike="2980",
            level_count=1,
        ),
        Scenario(
            name="entry_immediate_stop",
            prices=("3010", "3000", "3003"),
            expected_events=(first_entry, first_stop),
            expected_combined_pnl="17",
            expected_final_states=("READY",),
            premium_credit="20",
            long_put_strike="2980",
            level_count=1,
        ),
        Scenario(
            name="stop_reentry_recovery",
            prices=("3010", "3000", "3003", "3000", "2980"),
            expected_events=(first_entry, first_stop, recovery_entry, recovery_tp),
            expected_combined_pnl="20",
            expected_final_states=("PAID",),
            premium_credit="20",
            long_put_strike="2980",
            level_count=1,
        ),
        Scenario(
            name="two_stops_then_recovery",
            prices=("3010", "3000", "3003", "3000", "3003", "3000", "2980"),
            expected_events=(
                first_entry,
                first_stop,
                recovery_entry,
                _event(4, "STOP", 1, "3003", "1.15", "-3.45", "READY", 2),
                _entry(5, 1, "3000", "1.3225", 3, "3.9675"),
                _tp(
                    6, 1, "2980", "1.3225", "26.45", 3, "6.45", ((1, "6.45"),)
                ),
            ),
            expected_combined_pnl="20",
            expected_final_states=("PAID",),
            premium_credit="20",
            long_put_strike="2980",
            level_count=1,
        ),
        Scenario(
            name="repeated_entry_oscillation",
            prices=("3010", "3000", "3003", "3000", "3003", "3000", "3003"),
            expected_events=(
                first_entry,
                first_stop,
                recovery_entry,
                _event(4, "STOP", 1, "3003", "1.15", "-3.45", "READY", 2),
                _entry(5, 1, "3000", "1.3225", 3, "3.9675"),
                _event(6, "STOP", 1, "3003", "1.3225", "-3.9675", "READY", 3),
            ),
            expected_combined_pnl="9.5825",
            expected_final_states=("READY",),
            premium_credit="20",
            long_put_strike="2980",
            level_count=1,
        ),
        Scenario(
            name="fast_v_shaped_reversal",
            prices=("3010", "2950", "3010"),
            expected_events=(
                _entry(1, 1, "3000"),
                _tp(1, 1, "2980"),
                _entry(1, 2, "2980"),
                _tp(1, 2, "2960"),
                _entry(1, 3, "2960"),
                _event(2, "STOP", 3, "2963", "1", "-3", "READY", 1),
            ),
            expected_combined_pnl="67",
            expected_final_states=("PAID", "PAID", "READY", "READY", "READY"),
        ),
        Scenario(
            name="large_single_downward_segment",
            prices=("3010", "2970"),
            expected_events=(
                _entry(1, 1, "3000"),
                _tp(1, 1, "2980"),
                _entry(1, 2, "2980"),
            ),
            expected_combined_pnl="30",
            expected_final_states=("PAID", "ACTIVE", "READY", "READY", "READY"),
        ),
        Scenario(
            name="decline_through_every_level",
            prices=("3010", "2890"),
            expected_events=_full_decline_events(),
            expected_combined_pnl="30",
            expected_final_states=("PAID", "PAID", "PAID", "PAID", "PAID"),
        ),
        Scenario(
            name="stop_budget_exhaustion",
            prices=("3010", "3000", "3003", "3000", "3003", "3000"),
            expected_events=(
                first_entry,
                first_stop,
                recovery_entry,
                _event(4, "STOP", 1, "3003", "1.15", "-3.45", "READY", 2),
                _event(
                    5,
                    "LOCKED",
                    1,
                    "3000",
                    "1.3225",
                    "0",
                    "LOCKED",
                    2,
                    projected="3.9675",
                ),
            ),
            expected_combined_pnl="3.55",
            expected_final_states=("LOCKED",),
            premium_credit="10",
            long_put_strike="2980",
            level_count=1,
        ),
        Scenario(
            name="below_long_put_no_more_hedges",
            prices=("3010", "2890", "2800"),
            expected_events=_full_decline_events(),
            expected_combined_pnl="30",
            expected_final_states=("PAID", "PAID", "PAID", "PAID", "PAID"),
        ),
    )


def _actual_event(event: LedgerEvent) -> tuple[object, ...]:
    return (
        event.sequence,
        event.tick_index,
        event.event_type.value,
        event.level_id,
        event.price,
        event.quantity,
        event.realized_pnl,
        event.level_state.value,
        event.attempt,
        event.projected_stop_loss,
        event.zone_profit_component,
        event.recovery_profit_component,
        tuple(sorted(event.recovery_allocations.items())),
    )


def run_scenario(scenario: Scenario) -> ScenarioRun:
    spread = CreditSpread(
        spot="3010",
        short_put_strike="3000",
        long_put_strike=scenario.long_put_strike,
        option_quantity="1",
        premium_credit=scenario.premium_credit,
    )
    result = HedgeEngine(spread, level_count=scenario.level_count).run_with_accounting(
        list(scenario.prices)
    )
    expected_ledger = tuple(
        event.canonical(sequence)
        for sequence, event in enumerate(scenario.expected_events, start=1)
    )
    actual_ledger = tuple(_actual_event(event) for event in result.events)
    states = tuple(level.state.value for level in result.levels)
    passed = (
        actual_ledger == expected_ledger
        and result.metrics.combined_pnl == Decimal(scenario.expected_combined_pnl)
        and states == scenario.expected_final_states
    )
    return ScenarioRun(scenario, result, expected_ledger, actual_ledger, passed)


def main() -> None:
    runs = [run_scenario(scenario) for scenario in named_scenarios()]
    for run in runs:
        status = "PASS" if run.passed else "FAIL"
        print(f"{status:4}  {run.scenario.name}")
    if not all(run.passed for run in runs):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
