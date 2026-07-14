"""Opt-in live public-chain smoke test."""

import os

import pytest

from eth_credit_hedge.data.bybit_options import BybitOptionClient


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_BYBIT_TESTS") != "1",
    reason="set RUN_LIVE_BYBIT_TESTS=1 to contact Bybit",
)
def test_live_eth_chain_contains_only_valid_put_quotes() -> None:
    chain = BybitOptionClient().fetch_eth_chain()

    assert chain
    assert all(entry.contract.base_coin == "ETH" for entry in chain)
    assert all(entry.contract.option_type == "Put" for entry in chain)
