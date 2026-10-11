# SRO RQ Failure Privacy Differential Review — 2026-10-11

## Executive Summary

| Severity | Findings |
| --- | ---: |
| Critical / High | 0 |
| Medium | 0 |
| Low | 0 |

**Overall risk:** Low after the patch. **Recommendation:** Approve for the current local-validation-only deployment, subject to the documented limit that a failed external attempt still requires reconciliation before retry.

The change prevents unexpected exceptions from `write_to_sro` being persisted verbatim in RQ's Redis failure record. It logs only a task ID and exception type, then raises a fixed error that preserves the failure state and reconciliation requirement. It does not alter task state or enable retry.

## What Changed

**Review range:** working-tree diff against `39a373e`.

| File | Change | Risk | Coverage |
| --- | --- | --- | --- |
| `app/workers/jobs.py` | Catch unexpected SRO job exceptions, emit a safe diagnostic, and raise a generic failure marker | Medium: queue failure and external-outcome boundary | Unit test plus disposable RQ/Redis failure-registry smoke |
| `tests/test_worker_error_safety.py` | Verify logs/traceback are safe and task state is unchanged | Low | Direct unit test |
| `scripts/dev/rq_sro_error_privacy_smoke.py` | Execute a synthetic failing SRO job through RQ and inspect the persisted failure | Low | Invoked only by the guarded disposable restore drill |
| `scripts/dev/postgres-restore-drill.sh` | Add the smoke to the isolated verification path | Low | Full verifier |
| `tests/test_dev_compose.py` | Assert the guarded smoke remains wired into the drill | Low | Contract test |

There is one production call site: `app/api/routes_tasks.py:156` enqueues `write_to_sro` with `live=False`. `enqueue_write` does not configure RQ retries. No new caller or external operation was introduced.

## Historical Context

`git blame` shows the original `write_to_sro` handler dates to the initial queue implementation (`b7a51df`, 2026-09-23); it handled `NotApproved` but allowed all other exceptions to reach RQ. Earlier OCR hardening (`a816f9f`, 2026-10-10) established the local pattern of logging exception types without exception text and persisting generic errors. This patch applies that privacy boundary to SRO worker failures while retaining their unknown-outcome semantics.

No security validation was removed. The explicit `NotApproved` path remains unchanged and currently receives fixed messages from `runner.py`.

## Security Analysis

Before the patch, any unexpected database, driver or platform exception unwound through the RQ worker. RQ 2.12.0 formats the exception and traceback and stores that text in both `FailedJobRegistry` and its failure result. An exception can contain connection details or document-derived data. A user or service that can read Redis failure records could then see details that application logs intentionally suppress.

After the patch, RQ stores the fixed message `SRO job failed; outcome requires read-only reconciliation before retry`. The structured application log retains `task_id` and exception class for correlation, but excludes the exception message. The task row is not changed by the wrapper. The job remains failed; no task or queue retry is created. This avoids interpreting an uncertain result as success or retry authorization.

**Adversarial scenario:** a platform/database exception contains a synthetic URL with a password and document text. Trigger the synthetic `write_to_sro` job, let RQ process it, then inspect the failed-job traceback. The disposable smoke verifies the job is failed with the generic marker, the sentinel is absent from the RQ traceback and logs, the queue is empty, and task state is unchanged.

## Test Coverage

- `tests.test_worker_error_safety` passed the focused unit case for generic exception formatting, safe logging and unchanged task state.
- `tests.test_dev_compose` passed the contract that the guarded smoke is included in the disposable drill.
- The real RQ integration smoke passed against a disposable Redis container pinned to the repository digest. It verifies the stored failure text, safe log, unchanged task, empty queue and absence of the sentinel. The container was stopped and removed by its `--rm` lifecycle.
- The full `scripts/dev/verify.sh` passed: all 31 PostgreSQL integration cases, populated 10,000-task restore/adoption, worker compatibility/read-back, rollback and queue recovery, plus thread/process claim probes.

No shared Redis/PostgreSQL state or SRO system was used by verification.

## Blast Radius and Limitations

The handler affects the single SRO worker entry point. It wraps database lookup, runner execution and session commit, so failures at any of those boundaries receive the same generic RQ record. Expected `NotApproved` outcomes continue to be logged as before.

This does not add a durable external-attempt state machine, operator reconciliation controls, or production write support. The repository still blocks live SRO writes. If future code enables writes, a failed RQ job must remain non-retryable until read-only reconciliation establishes the actual result; this patch's error marker does not itself perform reconciliation.

## Analysis Methodology

Focused review of the changed worker boundary, its route/queue caller, RQ 2.12.0 failure persistence, the baseline implementation and related OCR privacy pattern. The failure path was exercised in a disposable RQ/Redis worker and the full repository verifier. Confidence is high for exception-message redaction and failure-state preservation in the tested synthetic path; production deployment, Redis ACLs and future external-write behavior remain outside this review.
