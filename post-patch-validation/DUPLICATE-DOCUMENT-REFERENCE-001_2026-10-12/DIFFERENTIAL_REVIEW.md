# SRO Robot Differential Security Review

## Executive summary

| Severity | Open findings |
|---|---:|
| Critical | 0 |
| High | 0 |
| Medium | 0 |
| Low | 0 |

**Overall risk:** Medium before the patch; Low for the reviewed change.
**Recommendation:** Accept the local patch; retain deployment storage qualification as a release gate.
**Review confidence:** Medium. The changed call path and direct storage helper were reviewed, but deployment storage mounts and same-UID host compromise remain outside this local synthetic review.

The change closes a baseline integrity gap: duplicate intake previously trusted an existing database document row without checking that the referenced bytes still existed under the configured private root. The patch verifies root and file type, ownership, mode, inode consistency, and SHA-256 before reuse. Invalid references stop the request before task/outbox creation and return a fixed 503 response.

## What changed

**Range:** `fddf6326ca1f463e2aeef97b4c4d02fe39714642` to the current working patch.
**Files:** `app/services/tasks.py`, `app/api/routes_tasks.py`, and three focused test modules.
**Production behavior:** one production caller (`upload_documents`) invokes `store_document`; the new stored-reference validator has one production caller inside that service. The worker continues to parse only after a task has been admitted; this patch protects the duplicate-admission path.

| File | Risk | Review focus |
|---|---|---|
| `app/services/tasks.py` | Medium | Stored path confinement and byte-integrity verification before deduplication reuse. |
| `app/api/routes_tasks.py` | Medium | Fail-closed HTTP mapping; no task or outbox intent after storage mismatch. |
| `tests/test_document_storage_security.py` | Low | Mode, ownership, symlink, root and hash cases. |
| `tests/test_task_request_correlation.py` | Low | Fixed safe error and no persistence/dispatch. |
| `tests/test_workflow_concurrency_postgres.py` | Medium | PostgreSQL isolation and retained database row after rejection. |

## Baseline context and historical review

`git blame` attributes the unchecked `if existing: return existing` behavior to the initial implementation (`b7a51df`) and the PostgreSQL advisory lock to `d8191a30`. The later `4655189` change hardened creation and database-rollback orphan recovery: exclusive no-follow file creation, exact mode, service ownership, inode verification, digest comparison and fsync. The baseline path did not apply those checks when a database row already existed. The current patch adds checks only on that reuse branch and does not remove or weaken the earlier creation/recovery safeguards.

No security validation or access-control code was removed. No historical known-good check was reintroduced. The accepted business policy still forbids treating content hashes as operation identity; the patch verifies source integrity but does not deduplicate tasks by hash or infer business equivalence.

## Closed baseline finding: stale or unsafe document reference reuse

**File:** `app/services/tasks.py:465-509`; HTTP boundary `app/api/routes_tasks.py:136-145`.
**Baseline attacker model:** authenticated staff member submitting an upload; malformed/missing state may arise from storage loss, stale metadata or a prior partial/manual recovery. An attacker with database-write access is more privileged than an ordinary employee and is not required for the data-loss case.
**Exploitability:** Medium, conditional on an existing row whose storage file was missing, modified, symlinked, or stored outside the configured root.
**Baseline impact:** the intake service returned that row and could create a fresh task pointing at unavailable or out-of-root bytes. The database uniqueness constraint and same-hash advisory lock did not validate the referenced file.

**Attack sequence:** upload bytes matching the existing row's `content_hash`; `store_document` finds the row and returns it without opening the stored path; the route creates a task and durable OCR intent; subsequent processing uses the stale reference. An out-of-root reference can cause local parsing to open a file outside the private document directory under the service identity.

**Patch behavior:** the stored path must be a direct child of the configured root; the root must remain a real service-owned `0700` directory; the final file must be a regular, service-owned `0600` file opened without following a final-component symlink; the opened inode must match the prior no-follow check and streamed SHA-256 must match. Failure raises `ExistingDocumentUnavailable`; the HTTP route returns a fixed 503 message before task creation or dispatch. The code intentionally does not repair, overwrite or delete the suspect file or database row.

**Evidence:** focused unit and route tests passed. The final full verifier passed 423 discovered tests (58 PostgreSQL cases skipped during discovery), all 58 disposable PostgreSQL cases, restore/adoption and recovery drills, 10,000 task/event identities, 10,001 file-integrity checks, and thread/process 1,000-task claim probes with zero unclaimed tasks. Full log SHA-256: `7da45d09fb92f992b2d6abb306582fde2b2084520f2dd0f9859fdda85d3a208a`. The pinned baseline/patch differential PPV completed with no findings or gaps across eight checks; see [`results-final/report.md`](results-final/report.md) and the harness disposition in [`validation-notes.md`](validation-notes.md).

## Test coverage and blast radius

The production path is narrow: `upload_documents` → `tasks.store_document` → `_stored_document_file_matches` → `_existing_document_file_matches`. The new helper has one production caller; only the duplicate-row branch reads existing file bytes. New tests cover an outside-root row with a PostgreSQL transaction, an HTTP 503 with no task/outbox commit, matching/missing/mismatched bytes, symlinked files, insecure/symlinked root, and normal valid reuse through existing concurrency coverage.

The full local verifier is the broad regression check. It uses only disposable PostgreSQL/Redis and synthetic data; it does not test a deployment's mount replacement, OS ACL configuration, encryption, backup retention, or same-UID attacker model. File hashing adds a bounded local read of an existing document for each distinct intake operation; upload-size, concurrency and timeout limits remain the controlling bounds.

## Recommendations and limits

- Keep suspect references for operator reconciliation; do not automatically delete or replace them.
- Use the existing count-only `storage-check --database` to identify mismatch classes without exposing path names.
- Reconcile the source-to-task identity and operation policy separately; content hash remains insufficient as a cross-user business-operation key.
- Stage 1 remains M1 partial. This local patch and test evidence do not qualify shared-database adoption, deployment mounts, office-scale load, or M2–M4.
