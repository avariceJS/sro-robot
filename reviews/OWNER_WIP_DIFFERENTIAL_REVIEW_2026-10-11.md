# Owner WIP Differential Security Review

**Review target:** tracked and untracked owner changes in the worktree, compared with `HEAD` `866e6b8` on 2026-10-11. The worktree changes remain owner-owned and uncommitted; this review did not stage or alter them.

**Recommendation:** Conditional. Keep case-number acquisition disabled from any workflow that treats its output as a selected legal act until the act-selection and acquisition trust-boundary findings below are resolved. Do not enable real SRO writes.

## Findings

### HIGH — KAD chooses the first or latest visible document without operation evidence

**Evidence:** `sro_robot/kad.py:88-128` selects `published[0]` or `files[0]`. `app/workers/jobs.py:90-107` immediately downloads and parses that one file into task fields. `app/api/routes_tasks.py:82-95` exposes this path as “find an act in KAD and recognize it.” There is one production call site for `fetch_latest_ruling`.

**Attack / failure scenario:** An authenticated employee enters a case with several procedural acts. The page orders a later publication first, so the worker selects it without enumerating candidates or checking the target operation, party linkage, corrections, or supersession. Its extracted facts appear in review as if they came from the chosen source act. A reviewer can then approve facts supported by an irrelevant act. SRO mutation is currently prohibited, which limits immediate external harm; the wrong source/facts can still mislead internal review and must not become an admission path for a future writer.

**Baseline contract:** `memory-bank/50-decisions.md:26-31` explicitly forbids first/latest selection and requires a complete bounded candidate inventory, an explicit operation, source/party evidence, and review when candidates remain ambiguous. `memory-bank/30-integrations.md:15-17` records that this adapter selects the first candidate and that live acquisition/selection is unverified.

**Coverage:** The KAD unit tests cover page warnings, PDF response shape and URL-path recognition. They do not exercise candidate ordering/completeness, party/case linkage, operation relevance, or a multi-act fixture. No real KAD navigation was run during this review.

**Required direction:** Keep all candidates and provenance; make selection an explicit review decision until an approved operation rule and completeness checks exist. Do not treat a case number or publication order as selection evidence.

### MEDIUM — Untrusted KAD document URLs are not constrained to approved origins

**Evidence:** `sro_robot/kad.py:207` resolves a page-provided `href` with `urljoin` but does not validate scheme, hostname, or port. Popup-provided URLs are treated similarly at line 230. The adapter then clicks the link and may issue `context.request.get(document_url)` at lines 234 and 240. Its response listener also accepts PDF-looking responses without checking their origin. The only caller runs this browser in the application worker (`app/workers/jobs.py:90`).

**Attacker model and scenario:** A compromised or manipulated KAD response supplies an absolute URL such as an internal service URL whose path matches `/Document/Pdf/` or ends in `.pdf`. A logged-in employee submits a valid case number; the worker follows or requests the URL from its own network context and stores a returned PDF as the case document. This creates a server-side request path from externally sourced page data. Exploitability is medium because it requires control of, or injection into, the external response; the case-number field alone does not supply the URL.

**Coverage:** Tests assert that a path such as `/Kad/Card` is recognized and that HTTP errors/non-PDF bodies are rejected. They do not test cross-origin, loopback, private-network, redirect, or final-response URL rejection. The actual official PDF host set has not been verified in this environment.

**Required direction:** Determine the official document origins from verified read-only evidence; allow only those exact HTTPS origins, reject private/loopback destinations and unexpected ports, constrain redirects and response size, and verify the final response origin before accepting bytes. Do not guess an allowlist that could silently break valid official document delivery.

### MEDIUM — KAD task commit and queue enqueue are not one recoverable admission operation

**Evidence:** `app/api/routes_tasks.py:92-95` commits the new task before calling `enqueue_kad_recognize`. If Redis enqueue fails, the request errors after the durable task exists. `reconcilable_ocr_task_ids` is upload/batch-only (`app/services/leases.py`), so an unqueued KAD task is not recovered by the current OCR reconciler. A browser/client retry creates another task because this endpoint has no request idempotency key. `app/workers/jobs.py:82-83` prevents a duplicate worker delivery from repeating acquisition, but does not recover the committed task that never entered RQ.

**Failure scenario:** Redis is unavailable after the database commit. The employee sees an error and retries. Two `new` KAD tasks persist, neither has a guaranteed queue entry, and the current reconciler does not report either as eligible. Later manual processing can produce duplicate acquisition and review work.

**Coverage:** Worker unit tests verify the compare-and-set prevents a duplicate RQ delivery from fetching again. They do not cover enqueue failure after commit, retrying the HTTP form, or process loss between task commit and RQ enqueue.

**Required direction:** Define KAD admission/recovery separately from OCR lease replay, then make durable queue intent and request idempotency explicit (for example, a transactional outbox with a stable intake key). Do not make KAD replay imply that selecting the same act is semantically safe.

### MEDIUM — KAD worker crash after claiming leaves an unleased task unrecoverable

**Evidence:** `app/workers/jobs.py:82` moves a KAD task from `new` to `recognizing`; the process then fetches the act and writes bytes at lines 90-94, updates document metadata at lines 95-101, and parses/saves at lines 103-107. Unlike upload OCR, this flow has no lease token, heartbeat, fencing, or KAD reconciler. Redelivered jobs return at the initial claim because the task is no longer `new`.

**Failure scenario:** The worker exits after the state transition or during file/database persistence. The task remains `recognizing`; depending on the crash point, an orphan file or downloaded document may exist while the database still has placeholder metadata. Redelivery deliberately returns without inspecting or reconciling the result. A human must currently investigate, but no KAD-specific recovery command or durable acquisition-stage marker identifies which boundary completed.

**Coverage:** Current tests cover a duplicate delivery and a parser exception after a successful mocked download. They do not inject process loss at fetch/write/metadata/parse boundaries or demonstrate operator reconciliation.

**Required direction:** Model KAD acquisition stages and recovery separately from replayable local OCR. Preserve the request/candidate/source identity and make partial download/database outcomes inspectable before any retry. Do not reuse OCR's automatic expired-lease replay until the acquisition semantics are reviewed.

## Test and blast-radius summary

- Recent local verification passed 170 discovered tests (152 non-PostgreSQL tests and 18 PostgreSQL integration tests in the disposable drill). This is repository-wide evidence, not KAD browser end-to-end coverage.
- KAD changed entry path: one form route → one queue helper → one worker → one adapter call. The route is authenticated, but the documented cross-user task ownership/role policy is still unresolved; this review does not infer one.
- KAD transport tests are offline mocks. No KAD external request was made. No SRO action, real document, environment secret, or sample image was used during this review.
- The `inspect_form` refactor still uses `Registry.click_add` only in its person-like path; `SroClient.require_registry_mutations` denies Add by default. The new assignment inspection path does not click Add. This review did not run the command or access SRO.
- New FIO, birth-date, and address extraction patterns in `sro_robot/core/extract.py` have no added synthetic regression cases in this diff. Add synthetic field-level cases before using them as verified extraction behavior; do not move owner documents into fixtures.

## Method and limits

Reviewed the worktree diff against `HEAD`, the relevant task/worker/transport callers, current unit tests, project KAD/selection decisions, and the committed mutation guard. Call sites were counted with `rg`; baseline history was checked with `git blame`. The KAD browser flow was not run because it would make an external request and its selection behavior is already unqualified. This is a focused review of the current worktree, not a full application penetration test, an external-site assessment, or a review of production deployment/network controls.
