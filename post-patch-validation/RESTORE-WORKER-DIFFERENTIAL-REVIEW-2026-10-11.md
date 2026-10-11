# Restore and rolling-worker differential security review

## Executive summary

| Severity | Findings |
| --- | ---: |
| Critical | 0 |
| High | 0 |
| Medium | 0 |
| Low | 1 (fixed) |

**Overall risk:** Low for the scoped synthetic development drill.

**Recommendation:** Conditional approval for local verification use. The low-severity file-mode gap found during review is fixed in `8049731`; the worker timeout cleanup gap described below is now directly tested. The evidence must not be presented as a production rolling-deployment qualification.

**Key metrics:** the reviewed range changed two code files across four code commits, plus two maturity-map documentation commits; one low finding was fixed. A follow-up added one bounded marker-wait helper and three process-level tests. The maximum-volume verifier then passed with 194 discovered tests, all 25 PostgreSQL integration tests, 100,000 synthetic task/event/document links and 100,001 restored files. A separate feature-disabled worker retained the same PostgreSQL backend PID across the additive migration and a fresh process verified its task result.

## What changed

**Commit range:** `df8b80f..3275d75`

| Commit | Change |
| --- | --- |
| `a2ca348` | Extract restored-file validation into one helper and add corruption, missing-file, unsafe-mode and symlink negative controls. |
| `08dae89` | Keep a feature-disabled worker process alive across a deliberately failed migration and successful retry; bound readiness/completion waits and clean up its FIFO/process. |
| `22f829b` | Record the expanded file-restore evidence and keep Stage 1 maturity at M1 partial. |
| `3275d75` | Assert the worker reuses the same PostgreSQL backend connection after migration. |
| `d1f4ddf` | Record pooled-connection migration evidence in the Stage 1 maturity map. |
| `8049731` | Require restored files to have exactly mode `0600`; reject special permission bits. |
| Follow-up | Add a directly tested bounded worker-marker wait with SIGTERM/SIGKILL cleanup and use it in the restore drill. |

| File | Change | Risk | Direct callers |
| --- | ---: | --- | ---: |
| `scripts/dev/postgres-restore-drill.sh` | +173/-22 lines | Medium: filesystem and child-process lifecycle, disposable-only | 2 invocation contexts (`verify.sh`, direct drill) |
| `scripts/dev/legacy_worker_schema_smoke.py` | +64/-2 lines | Low: synthetic worker fixture | 1 script caller, four explicit modes |

## Findings

### LOW (fixed): Restored-file validator accepted special permission bits

**File:** `scripts/dev/postgres-restore-drill.sh:500`

**Commit:** fixed in `8049731`

**Blast radius:** local restore drill only

**Test coverage:** yes; synthetic mode `0644` and `04600` controls

The prior predicate rejected group/other access but accepted owner-only modes other than the expected `0600`, including setuid/setgid bits. That could make the validator report a noncanonical restored file as private. The predicate now requires exactly `0600`, and negative controls prove both `0644` and `04600` are rejected. The full 100,000-task verifier passed after the fix.

The file validator uses `lstat`, requires a regular file with exact mode `0600`, constrains database paths to one child under the private drill root, and verifies each restored byte stream against the database SHA-256. Negative controls exercise altered bytes, a missing file, mode `0644`, special mode `04600`, and a symlink. The replaced inline checks are preserved in `verify_one_document`; they were centralized rather than removed.

The rolling-worker fixture uses only a disposable PostgreSQL clone and a synthetic parser. Its FIFO is created mode `0600` under the mode-`0700` temporary root. The worker process is explicitly feature-disabled, warmed against the baseline schema, kept outside a transaction during migration, and terminated on cleanup or a 30-second completion timeout. The same backend PID is asserted before and after migration. The task and audit event are checked from a separate process. No live queue, shared database, document, or SRO operation is part of this path.

## Test coverage analysis

- Full `SRO_DRILL_TASKS=100000 scripts/dev/verify.sh` passed at `8049731`: 191 tests discovered, 166 passed in the non-PostgreSQL phase, and all 25 PostgreSQL tests passed in the disposable drill.
- The run restored 100,001 files at exact mode `0600` and preserved the aggregate signature `0445f18dd99efe5ab618944829390217b8611b72e3fae3b9fea4d75a48affaaf`; 100,000 synthetic task/event/document links had zero checked identity mismatches. Claim diagnostic: 613.1/sec, p95 31.2 ms; not a capacity target.
- Migration refusal/rollback/retry, queue-loss recovery, process read-back and the worker's pre/post migration backend-PID assertion passed.
- `tests/test_worker_marker_wait.py` directly exercises marker success, a bounded missing-marker return, and a synchronized child that ignores SIGTERM. The test waits until that handler is installed, then asserts the helper escalates to SIGKILL after a 200 ms grace period and the parent can reap the child. The restore drill uses this same helper for its 30-second completion bound. Existing drill interruption tests continue to cover private temporary-directory cleanup. The maximum-volume verifier passed after this change; this does not test interruption at every instruction boundary.
- This helper simulates the current feature-disabled legacy handler; it does not execute a separately built prior release artifact. The code therefore proves a constrained additive-schema compatibility case, not actual binary-level rolling deployment.

## Blast radius

The change is confined to development verification. `scripts/dev/verify.sh` invokes the restore drill, and the drill invokes the helper in synthetic modes. No application route, production worker entry point, migration definition, SRO adapter, or runtime configuration changed. No untrusted network input reaches the new paths. The 100,000-volume test and runtime rechecks used only disposable database/Redis containers; shared services remained read-only.

## Historical context

The source file integrity checks were introduced immediately before this range in `df8b80f`. This range centralizes those checks into a helper and reuses them for negative controls. Git history and diff inspection found no security-related validation removal or reintroduction.

## Recommendations

### Before treating this as deployment evidence

- Run a production-like staging rehearsal with an actual previous and current release artifact, owner-approved workload and recovery targets, and supervised workers.
- Keep the legacy/lease-aware OCR overlap prohibited; this rehearsal intentionally keeps lease mode disabled.

### Remaining Stage 1 qualification gates

- Owner-approved task ownership and role/approval policy.
- Verified retained backup, safe migration window and controlled adoption of the currently unversioned shared schema.
- Production-like staging, worker supervision, load/soak targets, RPO/RTO and change-control evidence.

## Analysis methodology

**Strategy:** Focused review of the two code files in `df8b80f..3275d75`.

**Techniques:** baseline/current diff inspection, history/blame review of the replaced validator, call-site search, adversarial review of path handling/FIFO/process cleanup, and comparison with the maximum-volume verifier and cleanup tests.

**Limitations:** This was not a full repository security audit, prior-release binary test, deployment review, or verification of the owner-controlled shared database. Confidence is high for the stated synthetic drill scope and low for extrapolation to production rollout.
