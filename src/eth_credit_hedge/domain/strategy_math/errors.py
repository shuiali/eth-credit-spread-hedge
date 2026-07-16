"""Domain-specific failures raised by the strategy-math contract."""


class StrategyMathError(Exception):
    """Base class for an actionable strategy-math contract failure."""

    def __init__(self, message: str) -> None:
        if not message.strip():
            raise ValueError("strategy-math errors require an actionable message")
        super().__init__(message)


class InvalidUnitsError(StrategyMathError):
    """A value is invalid for its declared unit or mixes incompatible units."""


class InvalidConfigurationError(StrategyMathError):
    """A strategy-math configuration contains missing or mixed fields."""


class UnsupportedValuationError(StrategyMathError):
    """The requested option valuation context is absent, stale, or unsupported."""


class DeltaSpacingUnavailableError(StrategyMathError):
    """True option-delta spacing cannot be produced by the valuation context."""


class NonMonotonicSpacingError(StrategyMathError):
    """A valuation curve cannot support deterministic ordered spacing."""


class RootNotBracketedError(StrategyMathError):
    """A requested valuation or delta target is outside the solver bracket."""


class NonPositiveNetProfitError(StrategyMathError):
    """Expected net take-profit proceeds cannot support sizing."""


class QuantizationCoverageError(StrategyMathError):
    """Submitted quantity cannot provide the required post-rounding coverage."""


__all__ = [
    "DeltaSpacingUnavailableError",
    "InvalidConfigurationError",
    "InvalidUnitsError",
    "NonMonotonicSpacingError",
    "NonPositiveNetProfitError",
    "QuantizationCoverageError",
    "RootNotBracketedError",
    "StrategyMathError",
    "UnsupportedValuationError",
]
