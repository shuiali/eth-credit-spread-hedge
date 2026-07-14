"""Public Bybit fixture provenance tests."""

import json
from pathlib import Path


def test_option_pair_fixture_records_capture_provenance() -> None:
    fixture = Path(__file__).parents[1] / "fixtures" / "bybit_eth_option_pair.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))

    assert payload["exchange"] == "Bybit"
    assert payload["environment"] == "mainnet-public"
    assert payload["captured_at_utc"]
    assert len(payload["requests"]) == 4
