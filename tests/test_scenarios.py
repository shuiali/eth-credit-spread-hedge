"""Named deterministic scenario runner tests."""

from run_scenarios import named_scenarios, run_scenario


def test_all_ten_scenarios_match_their_complete_expected_ledgers() -> None:
    scenarios = named_scenarios()
    assert len(scenarios) == 10
    assert len({scenario.name for scenario in scenarios}) == 10

    for scenario in scenarios:
        run = run_scenario(scenario)
        assert run.actual_ledger == run.expected_ledger, scenario.name
        assert run.passed, scenario.name


def test_below_long_put_adds_no_events() -> None:
    scenario = next(
        item for item in named_scenarios() if item.name == "below_long_put_no_more_hedges"
    )
    run = run_scenario(scenario)

    assert len(run.result.events) == 10
    assert max(event.tick_index for event in run.result.events) == 1
