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

The current code has several local guards, but no single allowed-transition table, version/CAS check for operator transitions, or transaction-wide lifecycle owner:

- `save_values` accepts only `needs_review` and `approved`; changing values on an approved task returns it to review and clears the approver. `approve` accepts those same two states. The runner separately requires `approved` and checks the approval digest before local validation.
- Operator `save_values` and `approve` refresh the task under a row lock before checking the source state. The local-validation runner takes the same lock before checking status/digest and holds it through its transaction, so a concurrent edit waits and then observes the committed terminal state. Disposable PostgreSQL tests cover both edit-after-approval invalidation and edit-versus-runner completion. This is a local guard, not a complete transition table or ownership policy.
- The `TaskLease.task_id` ORM foreign key now explicitly matches migration `7af0f5c2d441` with `ON DELETE CASCADE`; fresh migrated-schema checks catch this metadata contract. Audit-event foreign keys remain restrictive.
- OCR lease claims lock the task row and accept `new`, or `recognizing` with an expired existing lease. Locked task and lease ORM entities are refreshed after lock acquisition, and expiry checks use PostgreSQL wall-clock time so a long-lived identity map or transaction cannot use stale lease state. A legacy `recognizing` task without a lease is deliberately excluded from automatic claim/reconciliation. Result/failure writes are fenced by lease owner, attempt and generation.
- Current SRO execution is local-only: a real-write request is rejected, and successful local validation writes `done` with an explicit `saved: false` audit marker.
- The general task helpers `mark_recognizing`, `save_recognized`, `reject`, `mark_writing`, `finish`, `fail` and `mark_duplicate` assign statuses without checking an allowed source transition. The legacy/non-lease OCR handler and pending KAD handler do not use OCR lease fencing. No global transition contract prevents duplicate task creation for one document/operation.

These are code facts, not approval of all transitions as valid business behavior. The source does not establish who may see/claim a shared task, when rejection/cancellation/retry is allowed, whether the same source can legitimately create several operations, or how unknown external outcomes transition. Do not add a broad transition graph, uniqueness key or cross-user ownership restriction until those semantics are supplied and reviewed. These omissions keep lifecycle/idempotency and authorization below the Stage 1 M2 gate.

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

Current tables: `User`, `Document`, `Task`, `Event`. There is no `case_claim`, assignment domain entity, internal assignment-draft lifecycle, `sro_session`, durable write-attempt or identity-reservation model. Do not overload `Task` or the physical-person card payload as the permanent assignment model. The future model must preserve its relationship to an existing debtor card while allowing assignment-specific status, fields, provenance, review and resumption. Alembic baseline revision `bc518c7514d3` describes the original workflow schema; current head `a41c72d93e10` installs PostgreSQL row-level UPDATE/DELETE and statement-level TRUNCATE triggers that reject audit-event mutation. Disposable tests verify inserts remain available and prohibited mutations leave rows unchanged, including when a synthetic role is explicitly granted TRUNCATE. This is an application-runtime immutability guard, not tamper-evident storage against a database owner/superuser. `app.cli init-db` upgrades through Alembic and refuses its legacy destructive reset flag. Fresh and populated-clone migrations pass the disposable verifier; the shared database was compared read-only and not migrated or stamped. Existing installations require a verified backup and exact schema/enum comparison before an operator stamps the baseline. Downgrade from the audit guard is intentionally blocked.

## UI scope

Keep the current functional UI. Add statuses, reconciliation actions, batch controls, health/diagnostics or review affordances only when needed by system correctness and operations. Visual redesign is not a current priority.
