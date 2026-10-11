# SRO Robot differential review: Stage 1 control-plane worktree

## Executive summary

| Severity | Count |
| --- | ---: |
| Critical | 0 |
| High | 1 |
| Medium | 1 |
| Low | 0 |

**Overall risk:** High for enabling the new KAD intake path on a shared runtime; the findings are bounded to queue dispatch and document identity. This is not a finding that an external registry write occurred. KAD and SRO writes were not attempted.

**Recommendation:** Conditional. Keep KAD activation/deployment blocked until task-to-queue delivery recovery and same-content document handling are defined and tested. Existing task/worker paths passed the disposable verifier, but a passing suite does not remove the identified dispatch gap.

## Review target and changes

- Baseline: `8e0335a` (`Reject future RQ worker heartbeat times`).
- Compared the current worktree with that commit; owner changes remain uncommitted and unstaged.
- Stage 1-sensitive path: `POST /case-search` → `create_kad_task` → database commit → RQ enqueue → atomic `new`-only claim → read-only MKAD fetch → local PDF storage → content-hash update → local parse/result persistence.
- Reviewed the seven changed tracked route/service/worker/queue/test files in this path and the untracked `sro_robot/kad.py`, `tests/test_kad_worker.py`, and `tests/test_kad_duplicate_guard.py` as dependencies/test coverage. The focused Python codebase contains 121 files; this review is surgical around the worker, database and external-fetch boundaries. UI, extraction and browser-inspection changes outside this path were not audited.
- The maximum disposable verifier passed on the reviewed worktree: 252 discovered tests (224 non-PostgreSQL cases and 28 PostgreSQL cases), 100,000 synthetic task/event/document identities across 20 synthetic staff, 100,001 restored files, exact row/sequence signatures, zero checked identity mismatches, migration rollback/retry and RQ duplicate/crash-recovery read-back. This test run did not exercise a real KAD network request or the KAD unique-hash conflict described below.

## Findings

### HIGH — KAD task commit and RQ enqueue are not one recoverable operation

**Location:** `app/api/routes_tasks.py:92-94`
**Changed path:** authenticated `POST /case-search`
**Blast radius:** one route → one enqueue helper → one worker; impact is each accepted KAD task whose queue delivery is lost.
**Coverage:** No route-level `/case-search` integration test was found. Worker duplicate-delivery tests cover the later claim boundary, not this database/Redis handoff.

The route commits the `new` task before calling `enqueue_kad_recognize`. If the process exits after commit or Redis enqueue fails/returns an ambiguous acknowledgement, the task remains durable while its queue delivery may be absent or unknown. The route then returns an error; repeating the form submission calls `create_kad_task` again, which uses a random document token and creates a second task. The current reconciler intentionally handles only local upload/batch OCR, and the accepted worker decision excludes KAD until candidate selection is valid. Therefore this task cannot be automatically replayed safely today.

**Concrete scenario:** an authenticated employee submits a valid case number; PostgreSQL commits; the API process exits before Redis accepts the job. The user retries after seeing an error. The second request creates another task, while the first remains `new` without a proven delivery. Both task rows now represent the same submission attempt from the employee's perspective.

**Baseline and decision context:** the changed route introduces this commit-then-enqueue call. `memory-bank/50-decisions.md` says PostgreSQL is authoritative and RQ is at-least-once, while `memory-bank/.now.md` records the KAD enqueue gap and excludes KAD from automatic OCR reconciliation. Atomic worker claiming prevents duplicate deliveries from repeating acquisition after a worker receives them; it does not close this pre-delivery gap.

**Recommendation:** keep recovery explicit. Before enabling this path in a shared runtime, use a durable dispatch intent/outbox or an equivalently recoverable protocol with a stable submission identity, and test process exit plus enqueue acknowledgement loss. Do not infer that “no RQ job” makes it safe to rerun the external lookup: act selection remains unresolved.

### MEDIUM — A KAD PDF matching an existing document conflicts with the unique content hash

**Location:** `app/workers/jobs.py:145-147`, `app/services/tasks.py:174-180`, `app/db/models.py:78`
**Blast radius:** one KAD worker result for each byte-identical document already stored.
**Coverage:** Existing PostgreSQL tests prove the database rejects duplicate `document.content_hash`; KAD worker tests cover parser failure and commit-acknowledgement loss, but not the KAD result colliding with an existing hash.

KAD task creation stores a SHA-256 of a random token as a placeholder `content_hash`. After fetching the PDF, the worker writes it to the task's private path and then changes the database hash to SHA-256 of the PDF. The database index is unique. If an identical PDF already exists (for example, the same file was uploaded previously), the metadata transaction fails. The worker preserves the uncertain `recognizing` result and does not parse/save the task result; the file already written remains at the path referenced by the KAD document row, whose stored hash remains the placeholder after rollback. This behavior follows the worker's conservative commit-ambiguity rule, but it leaves document identity inconsistent and has no automated KAD recovery path.

**Concrete scenario:** an employee previously uploaded the exact PDF bytes later returned by KAD; the KAD worker writes another copy under a random filename; setting its hash to the existing content hash violates the unique index and the DB transaction rolls back.

**Recommendation:** define a canonical-content/provenance contract before activating the path. The implementation must resolve a same-content result without losing the KAD retrieval provenance or silently reusing a document with incompatible case/source semantics. Add a disposable PostgreSQL test for this exact collision and verify the resulting task, document, file and audit state. Do not weaken the unique index or silently retain a placeholder hash.

## Positive controls observed

- The KAD worker uses an atomic database claim restricted to `new`; duplicate RQ delivery returns before external acquisition (`app/services/tasks.py:205-226`, `app/workers/jobs.py:107-111`).
- The KAD page warning checks stop on empty/challenge/access-restriction responses; no CAPTCHA or access-control bypass was found in the reviewed worker path.
- Retrieved PDF bytes are validated, stored through the exclusive private document writer, and parsed locally. Event messages and exception logs omit the case number, PDF bytes and exception text in the reviewed path.
- A result-persistence acknowledgement error is not converted into a terminal parse failure, preventing a possibly committed result from being overwritten (`app/workers/jobs.py:164-174`).

## Test coverage and blast radius

- `create_kad_task` has an event-privacy unit test; worker tests cover duplicate claim rejection, parse-failure handling and lost result-commit acknowledgement.
- No HTTP integration test for `/case-search` was found. No test covers Redis enqueue failure after task commit, repeated form submission, or a KAD PDF whose hash already exists.
- Direct production-code call chain counts from repository references: one `/case-search` route calls the KAD enqueue helper; one queue helper targets `recognize_from_kad`; the worker uses the shared queued-task claim. The shared claim also serves regular OCR recognition, so its regression risk is broader than the KAD additions.

## Historical context

`claim_unleased_task` was introduced in `5fc9920` (`Fence duplicate unleased OCR deliveries`, 2026-10-11) to prevent an old/unclaimed delivery from overwriting review state or repeating acquisition. The worktree changes `mark_recognizing` from a direct status mutation to that shared atomic claim, preserving the protection while adding a caller. No removed authorization or security check was found in the reviewed diff.

## Out of scope and limitations

- This review did not decide whether selecting the latest act is legally correct; the roadmap already records that as unresolved Stage 2 behavior.
- The new case-search route requires an active authenticated user, but task ownership/role policy is not established by this route. The existing broader ownership gap remains an unresolved Stage 1 requirement; this review does not infer an office access rule.
- No real KAD lookup, real SRO action, shared queue replay, database migration, or shared runtime mutation was performed.
- Full test success does not establish hosted CI status, production worker supervision, production backup/RPO/RTO, or owner-approved workload capacity.

## Analysis methodology

**Strategy:** Focused, risk-first review of the Stage 1-sensitive portion of the current worktree.
**Historical checks:** baseline diff and `git blame` around task transition/claim helpers; reviewed the duplicate-delivery commit history.
**Adversarial scenarios:** authenticated enqueue failure/retry; same-content PDF update against the database uniqueness constraint.
**Verification:** existing full maximum-volume disposable verifier passed; this review added no code or tests.
**Confidence:** High for the database/Redis ordering and unique-index interaction; medium for occurrence frequency, which depends on real duplicate-document patterns and queue failure behavior not measured here.
