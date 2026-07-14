"""Explicit option-market scenario families for simulation validation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class OptionStressPoint:
    spot: Decimal
    short_iv: Decimal
    long_iv: Decimal
    short_mark: Decimal
    long_mark: Decimal
    days_to_expiry: int

    def __post_init__(self) -> None:
        for field_name in ("spot", "short_iv", "long_iv", "short_mark", "long_mark"):
            value = Decimal(getattr(self, field_name))
            if not value.is_finite() or value < 0:
                raise ValueError(f"{field_name.replace('_', ' ')} cannot be negative")
            object.__setattr__(self, field_name, value)
        if self.spot == 0:
            raise ValueError("spot must be positive")
        if type(self.days_to_expiry) is not int or self.days_to_expiry < 0:
            raise ValueError("days to expiry cannot be negative")


@dataclass(frozen=True, slots=True)
class OptionStressScenario:
    name: str
    points: tuple[OptionStressPoint, ...]

    def __post_init__(self) -> None:
        if not self.name.strip() or len(self.points) < 2:
            raise ValueError("option stress scenario requires a name and two points")


def build_option_stress_scenarios(
    *,
    spot: Decimal,
    short_iv: Decimal,
    long_iv: Decimal,
    short_mark: Decimal,
    long_mark: Decimal,
) -> tuple[OptionStressScenario, ...]:
    """Build the four predeclared Plan 6 option-volatility scenarios."""

    start = OptionStressPoint(
        spot,
        short_iv,
        long_iv,
        short_mark,
        long_mark,
        7,
    )
    return (
        OptionStressScenario(
            "SPOT_UNCHANGED_IV_RISE",
            (
                start,
                OptionStressPoint(
                    spot,
                    short_iv * Decimal("1.20"),
                    long_iv * Decimal("1.20"),
                    short_mark * Decimal("1.15"),
                    long_mark * Decimal("1.15"),
                    6,
                ),
            ),
        ),
        OptionStressScenario(
            "SPOT_FALL_IV_FALL",
            (
                start,
                OptionStressPoint(
                    spot * Decimal("0.90"),
                    short_iv * Decimal("0.80"),
                    long_iv * Decimal("0.80"),
                    short_mark * Decimal("1.30"),
                    long_mark * Decimal("1.35"),
                    6,
                ),
            ),
        ),
        OptionStressScenario(
            "SKEW_STEEPENS",
            (
                start,
                OptionStressPoint(
                    spot,
                    short_iv * Decimal("1.02"),
                    long_iv * Decimal("1.18"),
                    short_mark,
                    long_mark * Decimal("1.10"),
                    6,
                ),
            ),
        ),
        OptionStressScenario(
            "NEAR_EXPIRY_DECAY",
            (
                start,
                OptionStressPoint(
                    spot,
                    short_iv,
                    long_iv,
                    short_mark * Decimal("0.40"),
                    long_mark * Decimal("0.35"),
                    1,
                ),
            ),
        ),
    )
