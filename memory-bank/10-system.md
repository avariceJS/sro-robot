# Product and system architecture

## Product objective

Turn the experimental SRO Robot MVP into a dependable legal-office automation platform for 20+ concurrent staff and large daily batches of bankruptcy-court documents. The intended system accepts electronic PDFs, scans/photos and case identifiers; acquires the correct act; classifies the act and participants; extracts and validates fields locally; routes ambiguity to a person; prevents duplicate processing; prepares the correct SRO operation; verifies any external result; and leaves a complete audit/recovery trail.

This is a target, not the current capability. Current SRO support is limited to one physical-person debtor-card scenario. Do not describe the product as production-ready.

## Architecture today

Two packages share one repository:

- `sro_robot/`: local processing, SRO browser client, scenario/profile contracts and CLI.
- `app/`: FastAPI, SQLAlchemy/PostgreSQL, Jinja/HTMX, Redis/RQ, task services and web workers.

Dependency direction is `app -> sro_robot`. The core processing layer must remain independent of browser/network I/O. SRO platform adapters should not parse documents. Scenarios translate validated domain data into platform operations. New entity types and operations must be added through explicit domain/scenario contracts, not by growing a physical-person model into a universal record.

Key contracts in code:

- `Field(value, confidence, source, warning)` preserves extraction provenance at field level.
- `Scenario.fields` declares the UI/review fields.
- A scenario provides validation, preflight and execution operations.
- `ReviewGate` pauses a filled form for a human decision; it currently keeps the SRO worker/browser occupied during review.
- `Task.recognized` and `Task.values` are JSONB. `Event` stores audit metadata.

Target domain boundary informed by staff-reported workflow: a debtor card and an assignment are separate entities and operations. Staff create/find the debtor card first, then create an assignment linked to it. An assignment may remain an internal SRO Robot draft while business information, often the insolvency practitioner, is pending. This target domain and lifecycle are not implemented or verified against SROArbitr; see `20-processing.md` for reported fields, confidence and open questions.

## Current workflow

```text
upload or case number
  -> document/task row
  -> OCR or KAD worker
  -> extracted fields
  -> operator review/edit
  -> approval
  -> SRO preflight/duplicate search
  -> form fill and operator preview
  -> Save or close
```

Current task statuses are `new`, `recognizing`, `needs_review`, `approved`, `writing`, `awaiting_preview`, `done`, `failed`, `duplicate`, and `rejected`. The application now declares one allowed-transition graph in `app/services/tasks.py` and routes service-layer status writes through its guard. These task states still do not model `write_result_unknown`, `saved`, or `verified` separately; the graph is an application contract, not a database-level transition constraint, and it does not provide operation idempotency.

### Current transition guards (observed implementation)

The current code has a single allowed-transition table plus local row-lock, conditional-update and lease-fencing guards. It does not have a version/CAS column or transaction-wide lifecycle owner:

- `save_values` accepts only `needs_review` and `approved`; changing values on an approved task returns it to review and clears the approver. `approve` accepts those same two states. The runner separately requires `approved` and checks the approval digest before local validation.
- Operator `save_values` and `approve` refresh the task under a row lock before checking the source state. The local-validation runner takes the same lock before checking status/digest and holds it through its transaction, so a concurrent edit waits and then observes the committed terminal state. Disposable PostgreSQL tests cover both edit-after-approval invalidation and edit-versus-runner completion. This is a local guard, not a complete transition table or ownership policy.
- The `TaskLease.task_id` ORM foreign key now explicitly matches migration `7af0f5c2d441` with `ON DELETE CASCADE`; fresh migrated-schema checks catch this metadata contract. Audit-event foreign keys remain restrictive.
- OCR lease claims lock the task row and accept `new`, or `recognizing` with an expired existing lease. Locked task and lease ORM entities are refreshed after lock acquisition, and expiry checks use PostgreSQL wall-clock time so a long-lived identity map or transaction cannot use stale lease state. A legacy `recognizing` task without a lease is deliberately excluded from automatic claim/reconciliation. Result/failure writes are fenced by lease owner, attempt and generation.
- Current SRO execution is local-only: a real-write request is rejected, and successful local validation writes `done` with an explicit `saved: false` audit marker.
- `TASK_STATUS_TRANSITIONS` enumerates every `TaskStatus`; terminal states have no outgoing edges, while OCR's same-state `recognizing` re-claim is explicit. `transition_task_status` checks every ORM status assignment in task services, the local runner and OCR lease path. Conditional SQL updates for unleased claim/failure retain their atomic source-state predicates and assert the graph's intended edge(s). A forward-only PostgreSQL trigger enforces changed-status edges at the database boundary; schema diagnostics verify its function and trigger definition, and a disposable PostgreSQL matrix compares every status pair with the application graph. The legacy unleased claim appends one privacy-safe `task_claimed` event in the same transaction as its conditional status update; writing, preview-ready and completion transitions also append fixed/minimal events without field values or screenshot paths. Preview close/save/timeout decisions are row-locked and one-shot; timeout remains `awaiting_preview` with an explicit `expired` marker because no downstream business transition is qualified. Caller-provided completion text is no longer copied into the audit event. A disposable PostgreSQL test verifies claim concurrency uniqueness and rollback atomicity. The trigger prevents invalid current-state changes but does not make status history append-only or protect against a database owner disabling managed triggers.
- Lifecycle writes for rejection, writing, completion, duplicate classification, local validation, preview decisions and preview timeout re-read under a row lock and require the expected source status; OCR result/failure paths retain their lease or conditional-update fences. Preview polling exits as soon as another process leaves `awaiting_preview`. Save/approve and runner startup retain their lock and approval-digest checks; local-validation queue admission also verifies current approval under the task lock before audit commit/enqueue. A confirmed operator rejection is available only for queued, review and approved tasks; it preserves the document/history, clears current approval and serializes with worker claim. The UI returns 409 if the state becomes active before POST. These guarded service paths have concurrent disposable PostgreSQL tests. The legacy/non-lease OCR and KAD paths still use separate ownership controls where local leases do not apply.

These are code facts, not approval of all transitions as valid business behavior. The accepted access policy gives every active employee equal case/ordinary-function access; a route-graph contract requires active authentication on every task route, and an HTTP integration test dynamically checks every registered task route redirects before task lookup. No task-ownership filter is intended. Attributable audit coverage is still partial and optional technical-administrator separation is not modeled. Same-request intake idempotency exists, but cross-user semantic operation identity and explicit reprocessing history are incomplete. Unknown external results must not auto-retry. These remaining lifecycle, idempotency, deployment and shared-schema adoption gaps keep Stage 1 below M2. Do not introduce individual visibility restrictions or confuse employee access with exclusive worker claims.

Successful supported task mutations are actor-attributed and carry the validated server-generated request ID through task intake/replay, field save, approval, approval invalidation, rejection, local-validation request and preview decision; successful login/logout transitions also have actor-attributed session events. Login uses a per-browser random bearer token, stores only its SHA-256 digest in PostgreSQL, checks revocation/expiry on every authenticated request and revokes that session on logout or successful re-login; independent sessions remain valid. Authenticated state-precondition conflicts append `operator_action_not_applied` with a fixed reason and allowlisted action code, actor and valid request ID, without copying submitted values or changing task state. Field values, credentials and login names are excluded. The accepted local-validation request is committed before queue dispatch. Route tests verify actor/request propagation for save, approve, reject, local validation and preview decisions; service tests verify privacy-safe event payloads and the synthetic PostgreSQL integration test verifies rejected-transition audit commit without task mutation. Failed login attempts are not attributed to a user, and production identity provisioning/retention remain unqualified. This is tested local coverage, not a claim of complete production audit compliance.

The application audit-event helper now applies a fixed event-kind/message catalog and per-kind allowlists for small typed payloads. Caller-supplied messages are not persisted; unsupported kinds, fields, or values fail before insertion. Migration `c4a781b6d902` enforces the same contract on direct SQL inserts, and the schema checker fingerprints its managed function and trigger. Baseline-versus-patch runtime evidence reproduces dynamic-message and arbitrary-payload persistence on the prior revision and verifies both are blocked while benign duplicate-event output and approval/login audit behavior remain unchanged. A database owner or superuser can still replace or disable managed triggers; production credentials and deployment controls are not qualified.

The login endpoint admits at most ten attempts per 60-second rolling window for each direct connection-peer/normalized-login pair. Keys are process-keyed HMAC digests; the in-memory LRU holds at most 4,096 buckets, and a successful login clears its pair. This bounds repeated Argon2 and failed-login audit work without persistent account lockout. It is process-local, resets on restart, is bypassable across application workers or changing peers, and does not trust forwarded IP headers; deployment ingress and distributed abuse controls remain required.

## Target workflow and engineering properties

```text
intake -> immutable source/provenance -> classify/select act -> extract
       -> role-bound identity + validation -> review/approval
       -> atomic identity reservation + attempt intent -> external write
       -> read-back + semantic verification -> durable completion
```

Required properties: idempotent intake, explicit state transitions, atomic task/identity claims, retry classification by side-effect boundary, write-ahead intent, unknown outcomes, verified completion, append-only audit events, per-field provenance, observability, retention controls, backup/restore and replayable recovery. Parallelize OCR and independent acquisition based on measured limits. Serialize live SRO writes per session until isolation is demonstrated.

## Scale and current infrastructure limits

Development uses PostgreSQL 16 and Redis 7 with Python-launched FastAPI/RQ workers. OCR can be parallelized independently. SRO RQ queue is currently configured as one queue/session, with shared browser credentials/profile and a 900-second job timeout. The 10-minute review pause consumes this worker. There is no measured capacity for 20+ employees and no production deployment stack in the repository. Do not claim horizontal safety from increasing worker count.

## Data model and migrations

Current tables: `User`, `Document`, `IntakeOperation`, `Task`, `TaskStatusHistory`, `LoginSession`, `TaskLease`, `JobOutbox`, `Event`. There is no `case_claim`, assignment domain entity, internal assignment-draft lifecycle, `sro_session`, durable write-attempt or identity-reservation model. Do not overload `Task` or the physical-person card payload as the permanent assignment model. The future model must preserve its relationship to an existing debtor card while allowing assignment-specific status, fields, provenance, review and resumption. Alembic baseline revision `bc518c7514d3` describes the original workflow schema; head `c73b8e4a19d2` adds request-level intake identity; `f28c7a913bd4` adds durable dispatch intents for safe internal jobs; revision `3b6a9f2d41c8` enforces the reviewed task-status transition graph with a PostgreSQL trigger; revision `5e2d91c7a640` records each changed task status in immutable `TaskStatusHistory`; revision `8cd3a71f204e` stores only a SHA-256 digest for each browser login token; current head `c4a781b6d902` adds a database-enforced audit event contract, allowing one session to be revoked server-side at logout while other sessions stay valid. The application schema checker verifies the transition-guard function and trigger definition; a disposable PostgreSQL matrix test compares every status pair against the application graph; another verifies atomic history capture and immutable read-back. PostgreSQL `event_contract_validate` fixes messages and validates event kinds and payloads on every insert; `schema-check` fingerprints its body and verifies its trigger shape. Row-level UPDATE/DELETE and statement-level TRUNCATE triggers also reject audit-event mutation. Disposable tests verify inserts remain available and prohibited mutations leave rows unchanged, including when a synthetic role is explicitly granted TRUNCATE. These are runtime guardrails, not tamper-evident storage against a database owner/superuser. `IntakeOperation` uniquely scopes an idempotency key to a user, stores only the source-manifest hash and links created tasks; this handles same-user form retries but is not semantic cross-user operation identity. `app.cli init-db` upgrades through Alembic and refuses its legacy destructive reset flag. Fresh and populated-clone migrations pass the disposable verifier; the shared database was compared read-only and not migrated or stamped. Existing installations require a verified backup and exact schema/enum comparison before an operator stamps the baseline. Downgrades from the audit guard, intake-identity, safe-job-outbox, task-transition-guard, status-history, revocable-login-session and audit-contract migrations are intentionally blocked.

## UI scope

Keep the current functional UI. Add statuses, reconciliation actions, batch controls, health/diagnostics or review affordances only when needed by system correctness and operations. Visual redesign is not a current priority.
