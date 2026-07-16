# Production Pilot Approval Record

Status: NOT APPROVED

Legal/account eligibility: UNCONFIRMED

Required attachments:

- Completed D1-D6 demo reviews.
- Accepted mainnet shadow metrics and offline replay evidence.
- Approved finite-limit configuration diff and risk review.
- One option spread, one level, smallest practical quantity configuration.
- Recovery, distributed recovery, and automatic scaling disabled.
- Monitoring, alerts, kill switches, manual intervention, and rollback drill.
- Named operator approval with UTC timestamp and release commit.

This file is intentionally unsigned. It must not be changed to approved by an
automated test or code-generation process; accountable operators and eligibility
owners provide the external evidence.

## Candidate readiness audit (2026-07-16)

- Demo D1-D6, mainnet shadow, the integrated one-hour burn-in, and bounded flat
  closure have evidence attached in their respective review files.
- The pilot profile now requires the execution schema 6 and journal schema 3
  migrations proven by the integrated demo runtime.
- The existing `0.01` ETH pilot quantity cap is not executable for the tested
  strategy configuration, whose smallest demonstrated matched option quantity
  and baseline hedge are `0.1`. No exposure limit was raised automatically.
- Account/legal eligibility, mainnet credential scope, named incident owner,
  release commit, and signed operator approval remain unconfirmed.
