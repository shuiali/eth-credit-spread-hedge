# Changelog

## Unreleased

### Added

- Persistence-first live entry, protection, recovery, and emergency-close flows.
- Restart replay, exchange reconciliation, repair classification, and finite risk
  gates.
- Seeded realistic exchange simulation, fault injection, historical inputs,
  stress models, event-ledger P&L replay, and predeclared validation thresholds.
- Six isolated deployment profiles and deterministic zero-order mainnet shadow
  decisions.
- Health/status endpoints, structured secret-safe logs, Prometheus metrics,
  alerts, and durable kill switches.
- Fail-closed shadow, release, pilot, and gradual-rollout acceptance evaluators.

### Changed

- Automatic short-perpetual entry requests use market IOC execution so downward
  gaps fill at the first available executable price.

### Safety

- Demo/mainnet burn-in, legal eligibility, operator approval, and any production
  order authority remain explicit unsigned deployment gates.
