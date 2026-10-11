# SRO Robot Differential Security Review — 2026-10-11

## Executive summary

| Severity | Outstanding findings |
| --- | ---: |
| Critical | 0 |
| High | 0 |
| Medium | 0 |
| Low | 0 |

**Overall risk:** Medium, conditional on production configuration and deployment review.
**Recommendation:** Accept the bounded runtime-role guard for production use after configuring `APP_DEBUG=0` and a direct DML-only runtime role; this review does not qualify the shared development database or the overall platform for production.
**Initial review finding:** the first guard version rejected only memberships in roles with administrative flags. A non-administrative role can still own a table and grant its member owner-level DDL, so that rule was insufficient. The patch now rejects all role memberships and ownership of modeled-schema relations, routines and types. Disposable PostgreSQL negative controls cover those paths.

## Scope and change summary

**Compared:** `bdec5ee..f28aa6d` (4 commits). **Codebase size:** 114 Python files; focused review of the six changed files below and their one-hop connection/migration callers. Existing uncommitted owner work was excluded from this diff.

| File | Change | Risk | Blast radius |
| --- | --- | --- | --- |
| `app/db/base.py` | Adds a production-only runtime connection policy callback on connect and pool checkout; migration connections retain the schema-only callback. | High | App-wide DB boundary: routes, workers, CLI, readiness and service database sessions. |
| `app/db/role_policy.py` | Queries PostgreSQL role attributes, memberships, modeled-schema ownership and dangerous grants; raises generic failures. | High | Called by the runtime connection callback. |
| `scripts/dev/postgres-runtime-role-smoke.sh` | Adds disposable accept/deny profiles, including direct and inherited escalation, object ownership, destructive grants, revision writes, pool recheck and concurrent checkouts. | Medium | Verification only; ephemeral digest-pinned PostgreSQL. |
| `tests/test_db_role_policy.py` | Covers accepted/denied/missing/error metadata and diagnostic redaction. | Medium | Policy helper. |
| `tests/test_dependency_timeouts.py` | Updates the engine event-registration contract. | Low | Engine factory regression test. |
| `memory-bank/50-decisions.md` | Records the enforced capability ceiling and its limits. | Low | Internal contract. |

## Attack analysis and result

The production runtime engine pins `search_path=public` and checks the connected identity at physical connection creation and every pool checkout. It rejects superuser/admin attributes, database or `public` schema CREATE, any role membership, ownership of `public` relations/routines/types, TRUNCATE/REFERENCES/TRIGGER on `public` tables, UPDATE on `public` sequences, and write privileges on `public.alembic_version`. The distinct migration engine is exempt so Alembic can use its schema-owner role.

Concrete attack paths exercised in the isolated role drill:

1. A runtime login with `CREATEDB` attempts to obtain an application connection. Checkout fails with a generic privilege-ceiling error and does not disclose the role name.
2. A non-admin runtime member of a no-login table-owner group attempts the same. Membership is denied even though the group has no administrative role flags; this closes the bypass found in the initial review.
3. A role that directly owns a `public` table is refused even without schema CREATE.
4. A role with TRUNCATE on application tables or UPDATE on `alembic_version` is refused before application SQL runs.
5. A previously accepted pooled connection is returned, its role is changed to `CREATEDB` by the disposable administrator, and the next checkout is refused. Forty read-only checks across 20 threads also completed under production settings.

The test keeps migration DDL on the separate migrator and confirms runtime schema DDL and revision writes are denied. No database changes were made outside the disposable role-smoke container.

## Historical context and regression review

`_set_public_search_path` was introduced by the earlier PostgreSQL namespace hardening (`a27cffa`) and pool-checkout correction (`c8759c9`). The change retains that behavior for Alembic through `_set_public_search_path`; only the production runtime engine receives the additional role check. No prior security validation was removed. The role query uses only PostgreSQL catalog/privilege functions and does not print role, database or object names.

## Test coverage

- `tests.test_db_role_policy`, `tests.test_dependency_timeouts`, settings and role CLI tests passed.
- The production-settings disposable split-role smoke passed its valid DML profile and all listed rejection controls.
- `SRO_DRILL_TASKS=100000 scripts/dev/verify.sh` passed on the reviewed source: 222 tests discovered, all 28 PostgreSQL integration cases passed, 100,000 synthetic task/event/document rows and 100,001 private-file restores verified, and migration/restore/old-worker/queue-recovery drills passed. Synthetic OCR claim diagnostic: 624.6 claims/s, p95 29.5 ms; this is not a production workload target.

The PostgreSQL smoke separately exercises the most consequential grants and ownership bypasses. It does not independently vary every individual REFERENCES/TRIGGER/sequence-UPDATE clause; those clauses were reviewed in the query, while only representative dangerous-grant profiles are isolated end to end.

## Blast radius

The runtime engine is consumed through `get_session` by authentication and task routes and through `session_scope` by workers and services; `app.main` also uses it for readiness, and the CLI uses it for runtime diagnostics. A policy failure therefore denies all database access for that runtime identity, rather than only mutating requests. The migration engine remains separate. The smoke proves a direct DML runtime can connect and the migrator can inspect/apply the schema, but it does not test every application route under production settings.

## Residual limitations

- The guard is active only when `APP_DEBUG=0`; debug mode deliberately remains exempt for the local development profile. The observed shared development database still uses an overprivileged debug connection and was not changed.
- This is a capability ceiling, not an exact ACL allowlist. It does not inventory arbitrary non-public schemas, verify every permitted per-table DML grant, establish network/TLS policy, or prove deployment controls. `DELETE` access can still be broad; authorization and task ownership remain separate unresolved business policy.
- Checks run at connection creation and checkout. An administrator changing role privileges while an application connection is already checked out can affect that in-flight transaction before the next checkout; production role changes still require coordinated connection recycling and review.
- The 20-thread check is a bounded smoke with `SELECT 1`, not an office load or soak test. Production throughput, recovery objectives, staging, backup retention and the deployed predecessor remain unqualified.

## Review conclusion

No outstanding bypass was found in the reviewed guard after closing the role-membership and public-object-ownership paths. Accept this patch as one production hardening control, not as proof of a complete least-privilege deployment or M4 qualification. Production must use `APP_DEBUG=0`, a distinct migration identity and a runtime role conforming to the tested direct-DML profile.
