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

Current task statuses are `new`, `recognizing`, `needs_review`, `approved`, `writing`, `awaiting_preview`, `done`, `failed`, `duplicate`, and `rejected`. They do not yet model `write_result_unknown`, `saved`, or `verified` separately. Generic status changes do not provide atomic ownership or idempotency.

### Current transition guards (observed implementation)

The current code has local row-lock, conditional-update and lease-fencing guards, but no single allowed-transition table, version/CAS column, or transaction-wide lifecycle owner:

- `save_values` accepts only `needs_review` and `approved`; changing values on an approved task returns it to review and clears the approver. `approve` accepts those same two states. The runner separately requires `approved` and checks the approval digest before local validation.
- Operator `save_values` and `approve` refresh the task under a row lock before checking the source state. The local-validation runner takes the same lock before checking status/digest and holds it through its transaction, so a concurrent edit waits and then observes the committed terminal state. Disposable PostgreSQL tests cover both edit-after-approval invalidation and edit-versus-runner completion. This is a local guard, not a complete transition table or ownership policy.
- The `TaskLease.task_id` ORM foreign key now explicitly matches migration `7af0f5c2d441` with `ON DELETE CASCADE`; fresh migrated-schema checks catch this metadata contract. Audit-event foreign keys remain restrictive.
- OCR lease claims lock the task row and accept `new`, or `recognizing` with an expired existing lease. Locked task and lease ORM entities are refreshed after lock acquisition, and expiry checks use PostgreSQL wall-clock time so a long-lived identity map or transaction cannot use stale lease state. A legacy `recognizing` task without a lease is deliberately excluded from automatic claim/reconciliation. Result/failure writes are fenced by lease owner, attempt and generation.
- Current SRO execution is local-only: a real-write request is rejected, and successful local validation writes `done` with an explicit `saved: false` audit marker.
- Lifecycle writes for rejection, writing, completion, duplicate classification, local validation, preview decisions and preview timeout now re-read under a row lock and require the expected source status; OCR result/failure paths retain their lease or conditional-update fences. Preview polling exits as soon as another process leaves `awaiting_preview`. Save/approve and runner startup retain their lock and approval-digest checks; local-validation queue admission also verifies current approval under the task lock before audit commit/enqueue. A confirmed operator rejection is available only for queued, review and approved tasks; it preserves the document/history, clears current approval and serializes with worker claim. The UI returns 409 if the state becomes active before POST. These guarded service paths have concurrent disposable PostgreSQL tests; they do not provide a general transition-graph constraint at the database layer. The legacy/non-lease OCR and KAD paths still use separate ownership controls where local leases do not apply.

These are code facts, not approval of all transitions as valid business behavior. The accepted access policy gives every active employee equal case/ordinary-function access; current task routes require active authentication and the HTTP integration suite checks each registered task route before lookup. No task-ownership filter is intended. Attributable audit coverage is still partial and optional technical-administrator separation is not modeled. Same-request intake idempotency exists, but cross-user semantic operation identity and explicit reprocessing history are incomplete. Unknown external results must not auto-retry. These remaining lifecycle, idempotency, deployment and shared-schema adoption gaps keep Stage 1 below M2. Do not introduce individual visibility restrictions or confuse employee access with exclusive worker claims.

Operator field submissions now append a `fields_saved` event at the service boundary, including the authenticated actor, whether normalized values changed, field count and a canonical SHA-256 digest. Successful login and logout append actor-attributed session events. Approved-task local-validation requests are audited and committed before Redis enqueue. Intake and covered employee-action events carry the validated server-generated request ID for correlation with privacy-safe HTTP logs. These events do not contain field values, credentials or login names. Other manual actions still need an explicit audit coverage review; this is partial policy enforcement, not proof that every employee action is attributable.

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

Current tables: `User`, `Document`, `IntakeOperation`, `Task`, `TaskLease`, `JobOutbox`, `Event`. There is no `case_claim`, assignment domain entity, internal assignment-draft lifecycle, `sro_session`, durable write-attempt or identity-reservation model. Do not overload `Task` or the physical-person card payload as the permanent assignment model. The future model must preserve its relationship to an existing debtor card while allowing assignment-specific status, fields, provenance, review and resumption. Alembic baseline revision `bc518c7514d3` describes the original workflow schema; head `c73b8e4a19d2` adds request-level intake identity; current head `f28c7a913bd4` adds durable dispatch intents for safe internal jobs. PostgreSQL row-level UPDATE/DELETE and statement-level TRUNCATE triggers reject audit-event mutation. Disposable tests verify inserts remain available and prohibited mutations leave rows unchanged, including when a synthetic role is explicitly granted TRUNCATE. This is an application-runtime immutability guard, not tamper-evident storage against a database owner/superuser. `IntakeOperation` uniquely scopes an idempotency key to a user, stores only the source-manifest hash and links created tasks; this handles same-user form retries but is not semantic cross-user operation identity. `app.cli init-db` upgrades through Alembic and refuses its legacy destructive reset flag. Fresh and populated-clone migrations pass the disposable verifier; the shared database was compared read-only and not migrated or stamped. Existing installations require a verified backup and exact schema/enum comparison before an operator stamps the baseline. Downgrades from the audit guard, intake-identity and safe-job-outbox migrations are intentionally blocked.

## UI scope

Keep the current functional UI. Add statuses, reconciliation actions, batch controls, health/diagnostics or review affordances only when needed by system correctness and operations. Visual redesign is not a current priority.
