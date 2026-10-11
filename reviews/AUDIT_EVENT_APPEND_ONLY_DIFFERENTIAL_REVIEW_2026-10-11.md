# Audit Event Append-Only Guard — Differential Security Review

**Reviewed commit:** `3174b7502669fe2e9222bd3a111b91d42a1dd331`

**Baseline:** parent commit `2e79a97`

**Review date:** 2026-10-11

**Risk:** Medium-high (persistent audit-state invariant and database migration)

**Recommendation:** Accept for the current candidate branch, with the documented owner/superuser limitation.

## Executive summary

| Severity | Findings |
| --- | ---: |
| Critical | 0 |
| High | 0 |
| Medium | 0 |
| Low | 0 |

The change adds PostgreSQL guards against `UPDATE`, `DELETE` and `TRUNCATE` of audit events, makes the read-only schema checker verify the exact managed function and trigger set, and extends disposable migration, role and recovery checks. I found no exploitable regression in the reviewed change. The guard constrains the runtime role; a database owner or superuser remains able to alter or disable it, so it is not a tamper-evident archive.

## Scope and baseline

The commit changes 14 files (+297/-21). The high-risk paths reviewed were the new migration, `compare_postgresql_objects` / `_audit_event_guard_differences`, restore/adoption scripts, the runtime-role smoke, and the PostgreSQL integration tests. Documentation and unit-test changes were checked for consistency with those paths.

Baseline history shows schema-object inventory predates this change (`4b3e7fa`, with enum, routine and RLS checks extended in later commits). The parent had no audit guard migration or `event_no_truncate` trigger. The new guard is therefore additive and does not remove a previous protection. The checker has two high-level consumers: the read-only `schema-check` CLI and the source-to-disposable-clone adoption rehearsal. The trigger itself is applied once through Alembic.

## Security analysis

### Guard and bypass behavior

The migration creates one pinned-search-path PL/pgSQL trigger function, then binds it to row-level `UPDATE OR DELETE` and statement-level `TRUNCATE` triggers on `public.event` (`migrations/versions/a41c72d93e10_make_events_append_only.py:18-45`). This closes PostgreSQL's row-trigger bypass for `TRUNCATE`. Inserts remain allowed. The function has no data-changing behavior and always raises SQLSTATE `55000`.

The schema checker requires one exact function definition and verifies its language and pinned `search_path`; it checks both trigger names, action bitmasks, enabled state, non-internal status, and function schema/name (`app/cli.py:384-441`). Altered, disabled, missing or duplicate managed triggers are reported as schema drift. Other public triggers/routines remain unexpected objects. No dynamic SQL or user-controlled identifier is introduced.

### Concrete attacker scenario considered

**Attacker:** a compromised ordinary application runtime credential with DML and an unexpectedly granted `TRUNCATE` privilege.

**Entry point:** direct SQL against `public.event`.

**Sequence:** append a synthetic audit row, receive `TRUNCATE` privilege in the disposable role drill, then issue `TRUNCATE public.event`.

**Result:** the statement trigger raises and the row remains present. This is verified by the role smoke and the PostgreSQL integration test. Without the statement trigger, the database's normal `TRUNCATE` privilege would bypass the row-level `UPDATE`/`DELETE` trigger.

A database owner or superuser can disable/drop the trigger or change the function. That authority is outside the claimed runtime-role boundary; the checker will detect drift on the next read-only check, but cannot prevent a privileged actor from both changing the guard and suppressing that check. No claim of independent tamper evidence is made.

### Migration and rollback safety

The migration is additive. Its downgrade raises before dropping the guard, preserving audit history and the revision. The disposable restore drill simulates the pre-guard schema only on a restored clone, checks the missing guard as drift, reapplies migrations, and confirms a refused downgrade preserves both triggers and the function. The source-to-clone rehearsal uses a read-only source transaction, compares row and sequence signatures before and after restore/migration, and leaves the source unchanged.

## Test coverage and evidence

- `SRO_DRILL_TASKS=100000 SRO_ROBOT_PYTHON=/tmp/sro-robot-hash-verify/bin/python bash scripts/dev/verify.sh` completed with exit code 0 before and after review follow-up. The latest run discovered 272 tests; 242 passed in the initial local suite and all 30 PostgreSQL integration cases passed in the disposable drill. UPDATE, DELETE and TRUNCATE each produced the guard's SQLSTATE `55000`, while the original row remained unchanged and INSERT remained available. The role smoke also rejects these operations when TRUNCATE is granted temporarily. The restore/adoption drill verified 100,000 synthetic task/event pairs, 20 staff, 100,001 restored files, exact row/sequence signatures, zero checked identity mismatches, rollback/retry, rolling worker read-back and downgrade refusal.
- Focused catalog tests also passed, including a case that detects the TRUNCATE trigger missing while the row trigger remains.
- `scripts/dev/postgres-existing-db-adoption-drill.py` passed separately: read-only source preflight, exact restored row/sequence signatures, clone-only baseline stamp/migration, current head, and unchanged source rows.
- `git diff --check`, Python byte-compilation (within `verify.sh`), and `bash -n` for both modified shell drills passed.

These results are synthetic local evidence. They do not prove a hosted CI run, retained production backups, production role policy, or resistance to database owners/superusers.

## Change inventory

| Area | Review | Coverage |
| --- | --- | --- |
| Migration and downgrade | Additive DDL; fail-closed function; update/delete/truncate checks | PostgreSQL integration + restore drill |
| Schema drift checker | Exact managed-function and trigger-set comparison; unknown objects remain visible | Unit drift cases + migrated PostgreSQL schema check |
| Runtime privilege boundary | Runtime role cannot mutate events; explicit temporary TRUNCATE grant exercises trigger defense | Disposable PostgreSQL role smoke |
| Source adoption | Source is read-only; clone signatures compared before/after migration | Actual-source clone rehearsal |
| Documentation | Limitations and legacy drift count match current checker behavior | Manual consistency review |

## Limitations and follow-up

- No hosted Actions result was available during this review.
- The trigger is not immutable against its owner/superuser; a separately controlled audit archive is a future operational decision.
- Shared-database adoption remains blocked pending a retained, verified backup and a reviewed maintenance/adoption procedure. This review does not authorize stamping or migrating the shared database.
- Broader Stage 1 authorization/ownership policy and owner-approved load/recovery targets remain open and are outside this code change.

**Confidence:** High for the reviewed guard, checker and disposable PostgreSQL behavior; low for production deployment and privileged-administrator controls, which were not exercised.
