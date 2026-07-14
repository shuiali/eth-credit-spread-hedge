# Rollback Procedure

1. Activate `SOFT_PAUSE`; use `STRATEGY_CLOSE` if exposure cannot remain safely
   protected during rollback.
2. Capture current orders, executions, positions, wallet, health, configuration
   hash, application commit, and database backup.
3. Verify the target artifact supports the current execution and journal schema
   versions. Database down-migrations are not performed during an incident.
4. Deploy the previously signed/identified artifact and its matching secret-free
   environment profile. Do not copy a database from another environment.
5. Start with entries blocked; run integrity checks, event replay, migration
   version checks, clock synchronization, and exchange reconciliation.
6. Run the local deterministic scenarios and the environment-appropriate smoke
   test. For shadow, reproduce the captured intent window offline.
7. Reset the kill switch only after readiness is green and an operator records
   approval.
8. Record timings, evidence, differences, and follow-up work in the release and
   incident review.

Rollback is unsuccessful if active state is not explainable, schema compatibility
is uncertain, protection is missing, or reconciliation is not `MATCHED`.
