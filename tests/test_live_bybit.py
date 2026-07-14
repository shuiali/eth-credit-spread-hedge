"""Opt-in live public-chain smoke test."""

import os

import pytest

from data.bybit_options import BybitOptionClient


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_BYBIT_TESTS") != "1",
    reason="set RUN_LIVE_BYBIT_TESTS=1 to contact Bybit",
)
def test_live_eth_chain_contains_only_valid_put_quotes() -> None:
    chain = BybitOptionClient().fetch_eth_chain()

    assert chain
    assert all(quote.instrument.base_coin == "ETH" for quote in chain)
    assert all(quote.instrument.option_type == "Put" for quote in chain)
