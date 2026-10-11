# Differential Security Review — Stage 1 Operational Diagnostics

**Commit range:** `8ddfa5e..26b85b7`, with one follow-up parser hardening change in the current worktree.
**Scope:** queue-head timestamp reporting in `ops-status` and the disposable OCR lease claim diagnostic's separate-process mode.
**Recommendation:** Conditional. The reviewed local diagnostic changes are suitable for synthetic verification; they do not qualify the shared runtime, a production workload, or Stage 1 M4.

## Executive summary

| Severity | Findings |
| --- | ---: |
| Critical | 0 |
| High | 0 |
| Medium | 0 |
| Low | 1, fixed in worktree |

The queue diagnostic reads only the head job's `enqueued_at` Redis hash field and does not deserialize RQ payloads. The new process probe uses argument-vector subprocess execution, caps concurrency at 20, verifies the pinned disposable PostgreSQL container in the parent and every child, and emits only latency/error aggregates. Parent-side PostgreSQL read-back remains authoritative for claim outcomes.

## Changes and blast radius

| Area | Entry points | Direct callers | Tests/evidence |
| --- | --- | ---: | --- |
| Queue-head timestamp | `app/cli.py:428-429`, helper at `app/cli.py:513` | 2 queues per `ops-status` invocation | 5 focused timestamp cases; 15 `test_ops_cli` cases pass after the fix |
| Disposable DB guard | `scripts/dev/lease_claim_load_safety.py:11` | Load parent and process worker | Refusal tests for missing marker, non-loopback host, missing identity/container, and child process |
| Separate-process claim mode | `scripts/dev/lease_claim_load_smoke.py:77-125` | One drill invocation per configured mode | 5,000 claims across 20 processes passed twice on separate 100k synthetic restores |

The commit range changes eight files (+320/-65). The operational timestamp helper has no task or queue mutation path. The worker process only claims synthetic OCR rows after independent container verification; the drill removes its disposable resources on exit.

## Finding

### Low — Extreme timestamp offset could abort `ops-status` (fixed)

**Location:** `app/cli.py:524-535`
**Introduced:** `a28bb98`
**Coverage:** Regression test added in `tests/test_ops_cli.py`.

**Scenario:** A Redis queue entry contains a syntactically valid extreme timestamp such as `0001-01-01T00:00:00+23:59`. Parsing succeeds, but converting this value to UTC can raise `OverflowError`. Before the follow-up fix, that exception escaped the metadata parser and made the entire queue check return unavailable.

**Impact:** An untrusted or corrupt queue metadata field could make the read-only operator diagnostic unavailable. No task state or external record is changed by this path.

**Resolution:** UTC normalization now occurs inside the fail-closed parse block; `OverflowError` maps to `unknown`. `tests.test_ops_cli` passes all 15 cases, including the extreme offset. No timestamp or job identifier is included in an error message.

## Adversarial analysis

- Redis queue IDs and job hashes are treated as untrusted metadata. The code reads one queue ID and one scalar hash field; it never calls `Job.fetch()` or deserializes a pickle.
- A malicious/malformed timestamp maps to `unknown`; an empty queue maps to `none`; a dequeue race maps to `unknown`.
- Process-worker JSON input is validated as a nonempty list of positive integer IDs. It is passed on stdin without a shell. Captured subprocess output is parsed and only numeric latencies and exception class counts are used; exception text, connection strings and IDs are not printed.
- Every process rechecks the disposable marker, PostgreSQL backend, loopback host, container-name pattern, exact pinned image digest and port-to-container mapping before claiming. The integration probe additionally validates status, attempt, owner-token uniqueness and task read-back from PostgreSQL.
- No production/shared service is restarted, migrated, enqueued into, or written by the validation described here.

## Verification

- Before the parser edge-case fix, the maximum-volume `SRO_DRILL_TASKS=100000 scripts/dev/verify.sh` passed with 243 discovered tests, 28/28 PostgreSQL integration cases, 100,000 synthetic task/event rows, 20 synthetic staff, 100,001 restored files, exact row/sequence read-back and zero identity mismatches. The standard drill included 1,000-claim thread and process probes.
- Two additional independent 100,000-row disposable restores each completed 5,000 claims across 20 worker processes with unique leases and no client errors (1,031.6/s at 22.2 ms p95; 1,204.4/s at 18.6 ms p95). A targeted smaller-database run also completed 5,000/5,000.
- The final parser fix was followed by `python -m unittest tests.test_ops_cli`: 15 tests passed, plus `git diff --check`.
- Earlier same-volume thread diagnostics using the default process-local pool recorded five checkout timeouts; a pool-size-20 control passed. These synthetic rates are sensitive to execution shape and are not office capacity or SLA evidence.

## Historical context and limitations

`git log -S` shows the disposable identity guard was moved from the original single-process script into a shared helper so both parent and child processes execute it; the guard's exact pinned-image and loopback checks were preserved. No authentication, authorization, lease-fencing or external-write guard was removed in this commit range.

This review covers the changed diagnostic paths and their direct callers, not a full repository security audit, Redis server/network exposure, actual task ownership policy, production deployment, worker supervision or approved workload/RPO/RTO targets. The shared development DB remains unversioned with five schema differences, the OCR queue has unresolved work and no registered worker, and M4 qualification remains open.
