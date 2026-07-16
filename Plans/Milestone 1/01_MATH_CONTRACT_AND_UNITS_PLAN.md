# Milestone 1.1 — Mathematical Contract and Units

## Objective

Create immutable domain types and one explicit specification that describes the
strategy mathematics without relying on variable names or historical behavior.

## 1. Write the approved strategy specification first

Create:

```text
docs/specification/STRATEGY_MATH_CONTRACT_V1.md
```

It must state:

```text
supported spacing modes
supported stop modes
supported option valuation modes
zone-loss definition
baseline sizing definition
recovery sizing definition
cost components
rounding behavior
rejection behavior
units
```

Mark unresolved decisions explicitly. Do not let implementation choose them
silently.

## 2. Introduce unit-aware aliases or wrapper classes

Minimum types:

```python
Price = Decimal          # USD per ETH
Quantity = Decimal       # ETH
Money = Decimal          # USD
Rate = Decimal           # dimensionless
DeltaExposure = Decimal  # ETH-equivalent
Volatility = Decimal     # annualized decimal
Seconds = Decimal
```

Preferred stronger design:

```python
@dataclass(frozen=True)
class Price:
    value: Decimal

@dataclass(frozen=True)
class Quantity:
    value: Decimal

@dataclass(frozen=True)
class Money:
    value: Decimal

@dataclass(frozen=True)
class DeltaExposure:
    value: Decimal
```

If wrappers are judged too invasive, use `NewType` plus runtime validation.

## 3. Add enums

```python
class LevelSpacingMode(str, Enum):
    PRICE_STEP = "PRICE_STEP"
    LEVEL_COUNT = "LEVEL_COUNT"
    EQUAL_OPTION_LOSS = "EQUAL_OPTION_LOSS"
    DELTA_STEP = "DELTA_STEP"

class StopMode(str, Enum):
    ENTRY_PERCENT = "ENTRY_PERCENT"
    PRICE_STEP_FRACTION = "PRICE_STEP_FRACTION"

class OptionValuationMode(str, Enum):
    EXPIRATION = "EXPIRATION"
    MARK_MODEL = "MARK_MODEL"
    EXECUTABLE_LIQUIDATION = "EXECUTABLE_LIQUIDATION"

class QuantityRoundingMode(str, Enum):
    FLOOR = "FLOOR"
    CEIL = "CEIL"
    NEAREST = "NEAREST"
```

`LEVEL_COUNT` may remain a user-facing convenience, but it must normalize to
`PRICE_STEP` internally.

## 4. Add immutable configuration contracts

```python
@dataclass(frozen=True)
class PriceStepSpacingConfig:
    price_step_usd: Price

@dataclass(frozen=True)
class LevelCountSpacingConfig:
    level_count: int

@dataclass(frozen=True)
class EqualOptionLossSpacingConfig:
    target_zone_loss_usd: Money
    valuation_mode: OptionValuationMode

@dataclass(frozen=True)
class DeltaStepSpacingConfig:
    delta_step: DeltaExposure
    valuation_mode: OptionValuationMode
    minimum_price: Price
    maximum_price: Price
    solver_tolerance: Decimal
    maximum_iterations: int
```

Stop configuration:

```python
@dataclass(frozen=True)
class EntryPercentStopConfig:
    rate: Rate

@dataclass(frozen=True)
class PriceStepFractionStopConfig:
    fraction: Rate
```

## 5. Define result contracts

```python
@dataclass(frozen=True)
class LevelMath:
    level_id: int
    entry_price: Price
    tp_price: Price
    price_distance: Price
    target_delta: DeltaExposure | None
    entry_option_value: Money
    tp_option_value: Money
    zone_option_loss_budget: Money
    stop_price: Price
    stop_distance: Price
    spacing_mode: LevelSpacingMode
    stop_mode: StopMode
    valuation_mode: OptionValuationMode

@dataclass(frozen=True)
class CoverageResult:
    required_budget: Money
    expected_net_profit: Money
    overcoverage: Money
    undercoverage: Money
    fully_covered: bool
```

## 6. Centralize validation

Reject:

```text
nonpositive prices
nonpositive quantities
invalid spread strikes
negative cost rates
price step <= 0
level count <= 0
delta step <= 0
stop rate <= 0
stop fraction <= 0
solver interval outside valid range
stale or absent valuation context
TP not below entry for a short hedge
stop not above entry for a short hedge
```

Use domain-specific exceptions:

```python
class StrategyMathError(Exception): ...
class InvalidUnitsError(StrategyMathError): ...
class UnsupportedValuationError(StrategyMathError): ...
class DeltaSpacingUnavailableError(StrategyMathError): ...
class NonPositiveNetProfitError(StrategyMathError): ...
class QuantizationCoverageError(StrategyMathError): ...
```

## 7. Remove ambiguous names

Search the repository for:

```text
delta_step
delta spacing
delta grid
stop_rate
tp_distance
option_budget
```

Classify every occurrence.

Required renames where the value is USD/ETH:

```text
delta_step -> price_step_usd
delta_spacing -> price_spacing
```

Do not bulk-replace references to real option delta.

## 8. Tests

Create:

```text
tests/domain/strategy_math/test_contracts.py
tests/domain/strategy_math/test_units.py
tests/domain/strategy_math/test_validation.py
```

Required tests:

- enum parsing;
- invalid mixed configurations;
- unit-preserving arithmetic;
- result serialization;
- exact Decimal behavior;
- ambiguous legacy field rejection;
- all domain errors have actionable messages.

## Acceptance gate

```text
[ ] Specification document approved.
[ ] Explicit enums exist.
[ ] Config contracts are immutable.
[ ] Result contracts include units and modes.
[ ] Ambiguous price `delta_step` names are removed from authoritative code.
[ ] No strategy formula changed yet outside controlled migration.
[ ] Contract tests pass.
```
