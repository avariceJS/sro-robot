# Differential Security Review — Current Owner Worktree

**Baseline:** `b9e91f2` (`Record migration target validation evidence`)
**Scope:** Uncommitted worktree diff at review time; 17 modified/deleted paths and 8 untracked paths were present. Review focused on authenticated KAD intake, task persistence/queueing, local PDF storage, worker state changes and PII-safe events. No files were changed by this review.
**Verification:** `scripts/dev/verify.sh` passed: 187 discovered tests, 25 PostgreSQL integration tests in the disposable drill, migration/restore/queue recovery, and bounded claim diagnostic (660.3 claims/s, 28.5 ms p95). This is local synthetic evidence only.

## Executive Summary

| Severity | Findings |
| --- | ---: |
| Critical | 0 |
| High | 1 |
| Medium | 1 |
| Low | 0 |

**Overall risk:** High for unattended KAD legal-act selection; medium for recoverability of newly created KAD tasks.

## Findings

### HIGH — KAD intake automatically selects the latest act

**Locations:** `sro_robot/kad.py:201-204`, `app/workers/jobs.py:130-135`
**Changed path:** Authenticated `POST /case-search` creates a task, then `recognize_from_kad` calls `fetch_latest_ruling` and processes the selected PDF.
**Evidence:** The acquisition helper chooses a candidate via `_latest_document_link`; the worker treats that candidate as the document for the case without recording an operator selection. Repository instructions state that a case number does not establish that the first/latest act is appropriate and that incomplete candidate inventories cannot be auto-selected.
**Scenario:** A valid case has multiple published acts, and the latest item is not the act applicable to the requested operation. The worker extracts that item and sends it into review as if it were the case's intended source. An operator may then approve facts attributed to the wrong act.
**Impact:** Incorrect source attribution can lead to a legally incorrect debtor-card preparation. The task is not safe for unattended downstream progression.
**Coverage:** Tests exercise transport handling, parsing failure and persistence-acknowledgement loss, but do not establish complete candidate inventory or selection correctness.
**Recommendation:** Keep KAD results explicitly pending review with candidate provenance and an operator selection boundary. Do not infer correctness from recency or enable downstream execution from this result alone.

### MEDIUM — KAD task commit and Redis enqueue are not recoverable as one operation

**Locations:** `app/api/routes_tasks.py:92-95`; `app/services/leases.py:176-197`
**Scenario:** The API commits a KAD task as `new`; Redis enqueue then fails or the web process exits before enqueue. The task remains in PostgreSQL without a queue entry. The current `reconcilable_ocr_task_ids` query only includes `upload` and `batch`, so the existing OCR reconciliation command will not discover this KAD task.
**Impact:** Accepted work can remain stranded indefinitely. `ops-status` may expose aggregate `new` age but cannot enqueue/recover the specific source safely. The failed enqueue response can also leave the user unsure whether a task exists.
**Coverage:** The suite tests queue recovery for eligible local OCR work and KAD duplicate delivery, but no enqueue-failure or process-crash boundary for KAD task creation.
**Recommendation:** Add a durable dispatch/outbox or a source-aware reconciler with an explicit safe rule for `new` KAD tasks. Preserve the current fail-closed behavior for `recognizing` KAD work, because its acquisition outcome may be unknown.

## Blast Radius and Coverage

`recognize_from_kad` has one production enqueue caller and one worker entry point. `claim_unleased_task` is shared by the existing OCR worker and KAD worker; its database concurrency contract is covered by PostgreSQL tests, while the KAD-specific path is covered by offline worker tests. The worktree verifier exercises the current tree but does not qualify live KAD behavior or its result-selection policy.

## Limitations

- The reviewed worktree was actively uncommitted; findings describe that snapshot and may become stale as it changes.
- No live MKAD request or SROArbitr write was performed.
- No production deployment, owner-approved task ownership policy, workload target or RPO/RTO was available.
- This review does not authorize retrying any existing external job or starting a worker against shared runtime queues.

**Recommendation:** Conditional. Keep the acquisition path local/offline and do not treat latest-act selection as verified. Close or explicitly track the KAD queue-recovery boundary before claiming Stage 1 M2 recovery evidence.
