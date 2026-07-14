"""Credit-spread payoff tests."""

from decimal import Decimal

import pytest

from core.credit_spread import CreditSpread


@pytest.fixture
def spread() -> CreditSpread:
    return CreditSpread(
        spot="3010",
        short_put_strike="3000",
        long_put_strike="2900",
        option_quantity="1",
        premium_credit="30",
    )


@pytest.mark.parametrize(
    ("price", "expected"),
    [
        ("3100", "30"),
        ("3000", "30"),
        ("2950", "-20"),
        ("2900", "-70"),
        ("2800", "-70"),
    ],
)
def test_expiry_pnl_is_exact(spread: CreditSpread, price: str, expected: str) -> None:
    assert spread.expiry_pnl(price) == Decimal(expected)


def test_payoff_invariants(spread: CreditSpread) -> None:
    assert spread.max_profit() == Decimal("30")
    assert spread.max_loss() == Decimal("70")
    assert spread.loss_region() == (Decimal("2900"), Decimal("3000"))
    assert spread.loss_slope("2950") == Decimal("1")
    assert spread.loss_slope("3100") == Decimal("0")
    assert spread.loss_slope("2800") == Decimal("0")


def test_rejects_reversed_strikes() -> None:
    with pytest.raises(ValueError, match="short put strike"):
        CreditSpread("3000", "2900", "3000", "1", "30")


def test_rejects_credit_above_total_spread_width() -> None:
    with pytest.raises(ValueError, match="cannot exceed total spread width"):
        CreditSpread("3010", "3000", "2980", "1", "30")
