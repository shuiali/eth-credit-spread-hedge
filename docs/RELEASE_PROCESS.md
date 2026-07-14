# Release Process

No release may infer evidence from a prior environment. Attach each item to the
release record and evaluate it with the fail-closed deployment gate.

- [ ] Versioned migration — schema versions and forward compatibility recorded.
- [ ] Changelog — user-visible behavior and safety boundary updated.
- [ ] Configuration diff — secret-free TOML diff reviewed against the prior tag.
- [ ] Risk review — every finite limit and change in maximum exposure approved.
- [ ] Rollback procedure — compatibility preflight and rollback drill passed.
- [ ] Demo smoke test — the required D-stage evidence is attached.
- [ ] Shadow replay — recorded intents reproduce offline with no acceptance failures.
- [ ] Operator approval — named operator, UTC timestamp, and scope recorded.

For a pilot, additionally attach legal/account eligibility confirmation, prove
monitoring and manual intervention availability, use one option spread and one
virtual level at the smallest practical quantity, and keep recovery/distributed
recovery/automatic scaling disabled.

The configuration diff command must name two immutable refs and exclude local
`.env` files:

```powershell
git diff <previous-tag>..<release-commit> -- src/eth_credit_hedge/config/environments
```

Release artifacts must not contain API keys, secrets, session tokens, or signed
request material.
