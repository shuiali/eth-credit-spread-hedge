"""Installed package namespace tests."""

from eth_credit_hedge import CreditSpread, HedgeEngine


def test_public_package_exports_reference_engine() -> None:
    spread = CreditSpread("3010", "3000", "2900", "1", "30")

    result = HedgeEngine(spread).run_with_accounting(["3010", "2890"])

    assert result.metrics.number_of_tps == 1
