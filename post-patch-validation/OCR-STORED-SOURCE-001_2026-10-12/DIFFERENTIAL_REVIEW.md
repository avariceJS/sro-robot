# SRO Robot Differential Security Review

## Executive summary

| Severity | Open findings |
|---|---:|
| Critical | 0 |
| High | 0 |
| Medium | 0 |
| Low | 0 |

**Overall risk:** Medium before patch; Low for the reviewed change.
**Recommendation:** Accept the local patch; retain deployment filesystem and OCR qualification as release gates.
**Confidence:** Medium for this source-read boundary. The differential probes use isolated PostgreSQL and synthetic bytes; deployed mounts, ACLs and host compromise are outside scope.

The patch makes the OCR worker validate the database-stored source path against the configured private directory and stored SHA-256 after it obtains the existing claim fence and before it calls the document parser. Invalid sources are recorded through the existing fenced failure path, so malformed paths do not become parser input or an automatic retry loop.

## What changed

**Range:** `fb6a43ddd27fcb8dba0865ca77d9ed70e4890378` to the current working patch.
**Files:** `app/services/tasks.py`, `app/workers/jobs.py`, and three focused test modules.

| File | Risk | Review focus |
|---|---|---|
| `app/services/tasks.py` | Medium | Shared fail-closed validator; the duplicate-intake path retains the same behavior. |
| `app/workers/jobs.py` | Medium | OCR source is checked after a successful claim but before parser access; failures use existing lease/transition fencing. |
| `tests/test_task_leases_postgres.py` | Medium | Disposable worker runs on changed bytes, confirms parser is skipped, failure event/state is committed, and failed work is not reconciled automatically. |
| `tests/test_worker_error_safety.py`, `tests/test_worker_recognition.py` | Low | Existing safe error and parse-failure behavior remains covered. |

The production blast radius is one registered OCR handler calling one document parser. KAD acquisition has a separate handler and remains hard-gated; it is outside this patch. Existing SRO write handlers are unchanged.

## Baseline context and historical review

`git blame` shows `recognize` and its direct `profile.parse(path)` call originated in the initial implementation (`b7a51df`). The current-schema preflight, lease claim, legacy claim and fenced failure handling were added in later commits. The baseline therefore had strong state ownership around task mutation but did not verify source integrity between database admission and the parser's filesystem open.

No authorization, claim, parser error, or audit check was removed. The patch invokes the storage validator after the claim commits, avoiding hashing a potentially large document while the task row is locked. On invalid source, the already-claimed task is moved to terminal `failed` using the existing lease or conditional-transition path; the error contains only the exception class, and no path or document content is logged.

## Closed baseline finding: parser opened a stale or out-of-root document

**Files:** `app/workers/jobs.py:50-84`; shared check `app/services/tasks.py:465-493`.
**Attacker/trigger model:** normally a storage loss, damaged restore, altered file, or stale/malformed database path. An authenticated employee can trigger the handler only through normal admitted work and cannot select `stored_path`. Direct database-write access would be privileged and is not assumed for the missing/corruption cases.
**Exploitability:** Medium and conditional on a stored path/file becoming invalid after admission.
**Baseline impact:** the worker passed the stored path directly to the parser. A symlinked or outside-root reference could make local OCR open bytes outside the private document directory; missing or changed bytes produced a parser failure that did not distinguish storage-integrity failure from an ordinary bad document.

**Patch behavior:** after a successful claim and transaction commit, `validate_stored_document_file` checks direct-child root confinement, root owner/mode/type, final-file type/owner/mode, no-follow open, inode identity and streamed SHA-256. It raises a fixed internal storage error before parser invocation. The surrounding worker handler records the failure through lease fencing or the legacy conditional transition. Failed tasks are terminal and do not re-enter OCR reconciliation automatically; operator repair/reprocessing semantics remain explicit.

**Evidence:** pinned differential PPV at [`results-final/report.md`](results-final/report.md) completed with no findings or gaps across eight checks. The baseline helper's synthetic parser records its call only after it reads the test path; patched outside-root, symlink and tampered-byte references are withheld before that parser marker. Valid files retain identical public task-state behavior. Full `scripts/dev/verify.sh` passed 424 discovered tests (59 PostgreSQL cases skipped in discovery), all 59 disposable PostgreSQL tests, restore/adoption/recovery, 10,000 task/event identities, 10,001 file integrity checks, and both 1,000-task claim probes with zero unclaimed work. Full log SHA-256: `35a0a396785cd66887262a0bca199c8df3eb071714e932a585f1c431cae2dbcb`.

## Test coverage and residual limits

The direct path is `jobs.recognize` → claim (`leases.claim_ocr` or `claim_unleased_task`) → storage validator → `profile.parse` → existing success/failure persistence. New PostgreSQL coverage runs the lease-enabled path with modified bytes and verifies failed status, one claim attempt, safe failure/audit events and no reconcilable lease. Runtime PPV separately exercises the legacy unleased handler on valid, outside-root, symlink and tampered-file sources.

Local tests do not establish that deployment mounts are the intended mounts, that PostgreSQL restore and filesystem restore are coordinated, or that a hostile process with the service UID cannot replace a path in the narrow interval after validation and before the parser opens it. The private root's mode/ownership is the current filesystem boundary; protection against same-UID compromise requires process/container isolation. This patch does not implement operator repair or a retry policy for failed storage references.

## Recommendations

- Keep invalid-source tasks failed and visible for operator reconciliation; do not automatically reopen or requeue them.
- Reconcile file/database pairs using the existing count-only storage audit before explicit reprocessing.
- Qualify mount identity, filesystem recovery and same-UID threat assumptions as part of deployment security review.
- Keep Stage 1 at M1 partial. Local synthetic evidence does not establish shared-database adoption, deployed supervision, production filesystem security or M2–M4 qualification.
