# SRO Robot Platform Diagnostics Differential Security Review — 2026-10-10

## Executive summary

| Severity | Open findings in reviewed changes |
| --- | ---: |
| Critical | 0 |
| High | 0 |
| Medium | 0 |
| Low | 0 |

**Recommendation:** Accept the reviewed local diagnostics and synthetic load helper for continued development use with the documented limits. This focused review does not qualify Stage 1 or production deployment. One safety weakness found during review was fixed before completion: the claim-load helper originally accepted a marker plus loopback PostgreSQL URL without establishing that the mapped port belonged to the disposable drill. The final helper checks the running drill container, exact image and published port before inserting synthetic rows.

## Scope and baseline

- Baseline: `4cda971`; reviewed committed changes through `0e4796e` (HEAD when this review was performed).
- Scope: `app/cli.py` read-only PostgreSQL role-capability output and aggregate RQ failed-registry counts; `scripts/dev/lease_claim_load_smoke.py`; its invocation in `scripts/dev/postgres-restore-drill.sh`; `tests/test_db_role_cli.py`, `tests/test_lease_claim_load_safety.py`, and the added `tests/test_ops_cli.py` coverage; supporting README and Memory Bank updates.
- Excluded: all owner-pending worktree changes. No source documents, `.env` content, real identifiers, shared database mutations, or external SRO actions were used.
- Historical comparison: `git blame` shows the existing database/Redis error handling, queue diagnostics and cleanup behavior in `cmd_ops_status` predate this range; the reviewed changes add the aggregate failed counts and do not remove existing checks. `get_job_count(cleanup=False)` was checked against the pinned RQ source: it executes a Redis sorted-set count without expiry cleanup, unlike the `count` property, which requests cleanup.
- Blast radius: `cmd_ops_status` is a single CLI command. `claim_ocr` has its production worker caller and PostgreSQL integration callers; the load helper is called by the disposable restore drill and has standalone pre-write guards.

## Findings and remediation

### Resolved — Synthetic claim helper did not prove disposable database identity

The initial helper guard accepted an explicit marker and a loopback PostgreSQL URL. A local development PostgreSQL service also satisfies those conditions, so an accidental invocation could insert synthetic tasks into a non-disposable database.

The final implementation additionally requires a container name matching the restore-drill convention, confirms through Docker that the container is running the exact `postgres:16-alpine` image, and matches the URL port to the container's published `127.0.0.1` port. The restore drill passes its generated container identity. Negative subprocess tests verify rejection before any database session for a missing marker, non-loopback host, missing identity, and nonexistent drill container. This is an accidental-misconfiguration guard for the local developer workflow, not an isolation boundary against a host operator able to manufacture matching Docker resources; use only the documented disposable drill.

## Adversarial checks and coverage

- `ops-status` uses aggregate task/queue/worker data and `get_job_count(cleanup=False)`. Unit tests assert no task identifiers, no enqueue/mutation, cleanup disabled, client closure and fail-closed handling when registry counts fail.
- The disposable Redis drill plants active and expired synthetic failed-registry entries, verifies only aggregate counts are reported, and confirms both entries remain after diagnostics.
- `db-role-check` is read-only, refuses non-PostgreSQL dialects before querying, omits role/database names, and reports only exception type on connection failure. Unit tests cover those behaviors. The observed development role has broad privileges; this command is a diagnostic and does not establish an approved privilege policy.
- The claim-load helper validates configured task/worker bounds, explicit drill marker and Docker/container/port identity before opening `SessionLocal` or inserting rows. Its postconditions require one first-attempt lease, a unique owner token and the expected task state for every synthetic task. Unit refusal cases run in subprocesses.
- The restore drill executes the helper only after starting its named disposable PostgreSQL container and supplies that container name. The latest full verification completed successfully; the load diagnostic claimed 1,000 tasks using 20 threads at 651.9 claims/second and 29.0 ms p95 in that run. These numbers are environment-specific diagnostic evidence only.

## Verification and limits

- `scripts/dev/verify.sh`: passed on 2026-10-10; unittest discovery ran 113 cases, 103 non-PostgreSQL tests passed, 10 PostgreSQL integration tests passed inside the disposable drill; byte-compilation, diff checks, shell validation, migration/recovery/restore drill also passed.
- A separate maximum-size restore run passed with 100,000 synthetic task/event pairs and 20 synthetic staff; the claim probe used a bounded 1,000-task workload.
- Docker inventory after verification showed no remaining `sro-restore-drill-*` container.
- No SRO page was opened and no external write was attempted. The shared PostgreSQL database was not changed.
- This is a focused differential review, not a full threat model, penetration test, PostgreSQL privilege review, service deployment review, or production capacity qualification. It does not resolve task ownership/RBAC, production role separation, backup objectives, worker supervision, or owner-approved workload targets.

## Recommendation

Retain the pre-write disposable-container guard and refusal tests. Keep role-capability output informational until the deployment principal design is approved. Continue to treat shared-database migration/adoption, multi-user authorization, production recovery and production-like workload qualification as Stage 1 blockers; local synthetic evidence does not satisfy the M4 gate.
