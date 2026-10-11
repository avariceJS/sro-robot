# Durable engineering decisions

This file records accepted constraints. Reopen a decision only when new evidence materially changes its assumptions or the owner changes the business requirement.

## Data and external actions

- Keep OCR/extraction local because documents contain personal data. A future ML extractor must run locally or use a provider explicitly approved by the owner; never send source documents or OCR text to a generic cloud model.
- Human review is required for uncertain legal data. Do not invent unreadable fields.
- Real SRO mutations are prohibited for agents, including `--live`, Save, Send, deletion and update. Tests use isolated fakes. This prohibition is stronger than the current code's explicit live flags.
- The current build fails closed for all live writes. `live=False` is local validation only: no SRO browser, registry search, form fill, Add, Save, or Close action is permitted. Its audit event is `local_validation_completed` with `saved: false`; this state must never be presented as a saved/verified registry record. Form-preparation tests require a local fixture or fake.
- `SroClient` state-changing controls are disabled by default. Do not enable them in this deployment or use a real registry for tests. Historical dry-run completions without the new local-validation marker remain unverified and are not retry authorization.
- Do not automatically resubmit an SRO write after a durable pre-Save intent marker. Keep the attempt `write_result_unknown` until read-only reconciliation and semantic verification prove an outcome. Empty search does not establish failure.
- Successful Save must not be inferred from click success, current URL or disappearance of a form. Reopen the record and compare identity plus approved fields.
- Reserve tenant/registry + debtor identity + create operation across tasks. A task-level duplicate check is not sufficient. Local reservations cannot guarantee no race with independent humans/clients.

### Debtor identity and creation reservations

- High-impact identity decision (2026-10-10): automatic creation requires a valid natural-person INN with explicit debtor-role attribution and operator approval bound to that payload. FIO, date of birth, and case number are review signals only; they are not automatic reservation keys. Missing, invalid, conflicting, or ambiguously attributed INN blocks creation.
- Before write enforcement is enabled, confirm whether one debtor has one reusable card per SRO registry realm, and determine which accounts share that realm. The business cardinality and registry scope are currently unknown, so live creation remains disabled.
- When implemented, reservation keys must be a versioned HMAC-SHA-256 token using a dedicated secret and domain separator, with atomic uniqueness on `(target_scope, identity_scheme, identity_token)`. Do not store or log raw identifiers in reservation records. Key rotation must not permit duplicate reservations.
- Keep one shared unresolved registry scope until account-to-registry boundaries are verified. Unresolved scope, existing submission, duplicate candidate, or identity ambiguity blocks creation; human review may reconcile an existing record but cannot silently authorize a second creation.
- Owner-provided examples may already exist in SRO. They may still be used for local OCR, extraction, review, and no-SRO validation. Never bypass a duplicate finding to create or submit a real record; existing-record status does not weaken the external-action prohibition.

### Judicial-act selection from MKAD

- Never select the first or latest visible act as an implicit match. A case may contain petitions, scheduling orders, appointment acts, final decisions, corrections and later procedural events. Publication order and generic `определение` labels do not establish operation relevance.
- First establish an explicit target operation. Build a complete bounded candidate inventory with document/event ID, title/type, act and publication dates separately, source URL, case linkage and pagination completeness. Limit downloads to five candidates per operation and ten per submitted package; exceeding either budget or incomplete inventory requires review rather than selecting a prefix.
- Metadata may narrow candidates but cannot establish that an act supports the requested operation. Local content checks must verify operation-specific dispositive text and party linkage, preserve source snippets/pages for material fields, and detect conflicts, corrections and replacements.
- Automatic selection is permitted only when the inventory is complete, case/subject match, one approved operation rule is satisfied, all required fields have source evidence, OCR uncertainty is resolved, and no competing or superseding candidate remains. Without a confirmed act-priority rule, multiple plausible acts require operator review.
- A package may contain separate operations supported by separate acts. Record each operation-to-act decision independently, including rejected candidate reasons and operator decisions. MKAD unavailable/empty page means source unavailable, not no matching document.
- Current product rules for initial card basis, manager appointment/replacement, corrections and act priority are not yet confirmed. Until they are, case-number intake must not automatically treat a latest act as selected; real SRO mutations remain prohibited.

## Workflow architecture

- Model intake as document/task work with scenario-defined fields; do not make a physical-person card the universal domain object.
- Staff-reported process confirms that a debtor card and an assignment are distinct business entities and actions: create/find the debtor card first, then separately create an assignment linked to that card. Do not infer assignment creation from card creation or assume one assignment per debtor/case.
- An internal SRO Robot assignment draft is a resumable local work item, not an SROArbitr draft or successful external action. Keep internal draft, readiness/approval, external attempt, unknown result, externally saved and semantically verified states distinct. Preserve the link to the existing debtor card and assignment-specific fields/provenance. These are target architecture constraints; no assignment model or lifecycle is implemented yet.
- OCR is stateless and can use a measured worker pool. SRO WebForms has page/session state; serialize per SRO account/session until isolation is proven.
- Preserve source, candidate act list, selection rationale, extracted-field provenance, operator edits/approvals, attempt identity, errors and verified result.
- Fail closed on unsupported act types, ambiguous participant identity, CAPTCHA/access challenge, invalid or mismatched case identifiers, and unknown external outcomes.
- Keep dry-run, saved and verified results distinct.

## Operator approval

- Approval binds to an exact field payload. Implemented as SHA-256 over canonical JSON in the `approved` audit event; field edits invalidate it; worker startup rechecks it. Older approvals without a digest require re-approval.
- Approval does not prove identity, eliminate concurrent task races, or authorize any real write by an agent.

## Shared employee access

**Accepted owner policy (2026-10-11), not a claim of completed enforcement:** All employees have equal access to all cases and ordinary work functions. Do not introduce individual case/task visibility restrictions or assignment-scoped permissions. Technical administration may be separated from ordinary employee functions.

- Authenticate individual employees and audit their actions with attributable actor identity. Shared case access does not mean shared application identities or anonymous actions.
- Employee access and worker execution ownership are separate contracts: shared visibility does not remove atomic claims, leases, fencing or concurrency guards.
- The HTTP integration suite verifies active authentication on every currently registered task route before task lookup. Continue to audit newly added routes at the boundary; production identity provisioning, optional technical-administrator separation and complete action-audit coverage remain unqualified. Existing lifecycle validation and the prohibition on real SRO writes remain in force.

## Operation intake, duplicates and explicit reprocessing

**Accepted owner policy (2026-10-11):** Repeated submission of the same request must not create duplicate tasks for one operation. New judicial acts in the same case and independent assignments are legitimate separate work. Explicit reprocessing is permitted with history retained.

- Keep case, source act, assignment, logical operation and processing attempt distinct. Neither case number alone nor document-byte hash alone is a task uniqueness key. Stored-document deduplication is not operation deduplication.
- Apply duplicate suppression across employee submissions of the same operation; changing the submitting employee must not create a duplicate. Record an explicit reprocessing decision and preserve links to prior work, approvals and attempts rather than overwriting history.
- An unknown external result must never be automatically repeated. Explicit reprocessing does not bypass read-only reconciliation, identity/duplicate checks or the current real-write prohibition.
- Versioned operation identity, atomic admission, durable enqueue/recovery and replay-key retention are engineering contracts. A narrower same-user form-request key is implemented: server-generated keys are unique per user; a stored ordered source manifest must match for replay; same-key/different-manifest use conflicts; tasks link to the operation; and the replay is actor/request-attributed. This prevents same-form/HTTP retry duplicates across web processes. It does not identify semantic duplicates across employees, establish operation identity from document bytes, model explicit reprocessing or make Redis enqueue transactional. Do not invent an act-selection or registry-cardinality rule from this policy; those external/domain questions remain separate.

## Audit event immutability

- Migration `a41c72d93e10` makes PostgreSQL `Event` rows append-only through a row-level `BEFORE UPDATE OR DELETE` trigger and a statement-level `BEFORE TRUNCATE` trigger. Corrections use a new event so existing actor/action evidence is preserved. The trigger prevents a granted TRUNCATE privilege from bypassing the row-level guard; database owners and superusers can still alter or disable it.
- The trigger function pins `search_path` to `pg_catalog`; `schema-check` verifies the function body, path and both enabled trigger shapes. Disposable tests prove inserts work and update/delete/truncate fail while preserving the original row.
- This constrains the ordinary runtime role; a schema owner or superuser can alter or disable the trigger. It is not a tamper-evident archive. Downgrade is blocked and changes are forward-only.

## OCR worker ownership and recovery

- PostgreSQL is the source of truth; RQ delivery is at-least-once. OCR execution may be reclaimed after an expired lease because parsing an immutable uploaded file has no external write side effect. Lease expiry for any future external operation must remain an unknown outcome and never authorize retry by itself.
- Persist OCR claim owner token, attempt number, fencing generation and lease expiry in a dedicated task-lease row. Acquire by locking the task row and updating lease/task state in one transaction. Refresh locked ORM rows after lock waits, and compare expiry against PostgreSQL wall-clock time (`clock_timestamp()`), not transaction-start time. Result and failure writes must verify the current token, attempt, generation, task state and unexpired lease in the same transaction as their state change.
- Bound OCR attempts to three; exhaustion becomes an actionable `failed` task for human review. Use a 900-second lease formed from the 600-second RQ job timeout plus a 300-second recovery grace. Keep the timeout as one shared constant across worker queue construction and `ocr-reconcile`; a unit contract verifies an RQ job created from the configured queue uses that timeout and the lease remains longer. Do not add automatic lease renewal until measured OCR duration requires it.
- Apply this protocol only to immutable local upload/batch recognition. KAD acquisition remains excluded until candidate selection is valid; SRO execution is excluded and must never be automatically replayed.
- Existing `recognizing` tasks without a lease are legacy unknowns and are not auto-claimed. They require read-only reconciliation. Before enabling lease-aware workers, apply the additive migration, stop all pre-lease OCR workers, deploy lease-aware workers, and reconcile legacy `recognizing` tasks. Mixed-version OCR workers are unsafe because the old handler does not enforce lease fencing. Keep the feature disabled by default in an unadopted environment.
- A reconciler may enqueue new local OCR tasks lacking an active lease and expired eligible OCR leases. Duplicate RQ deliveries are safe only because database claim and fencing are authoritative. Redis/job enqueue success is not the durable task state.

## Local failure transition fence

- A local `failed` result may replace only `recognizing` (local document processing) or `approved` (local validation). The transition is conditional and atomic in PostgreSQL; a stale failure after a newer completion, rejection, duplicate classification, or external-outcome state is a no-op and produces no failure event. Callers treat a rejected stale transition as non-fatal and correlate it using the task ID only.
- This boundary is derived from the current local worker and runner call sites. It is not a general task transition matrix and does not authorize retries or define recovery from any state that could represent an external effect.

## HTTP intake-to-task correlation

- The server-generated 32-character lowercase hexadecimal request ID may be copied into task creation and employee-action audit events. The event's `task_id` links task actions to the durable task; session events have no task. Batch items from one HTTP request intentionally share the request ID. Existing coverage includes upload/KAD intake, field saves, approvals, local-validation requests, preview decisions, and successful session start/end.
- Accept this value only from `RequestIdMiddleware` state after format validation. Do not trust a caller-supplied request-ID header, use request IDs as authorization/idempotency keys, or add document names, case numbers, file bytes or OCR text to the payload. Worker diagnostics continue to correlate by task ID. Other routes must opt in explicitly.

## Request-level intake idempotency

- Upload and KAD forms receive independent random 192-bit keys from the authenticated task page. `IntakeOperation` scopes uniqueness by user and key, stores only a SHA-256 manifest over source type and ordered item digests, and links created tasks. Identical-key/identical-manifest replays return and re-enqueue those task IDs; database claims prevent duplicate OCR/KAD work from running twice. A key reused with a different manifest returns a conflict. Replay events record the employee and server request ID without storing the key, filenames, case number, or source text.
- This contract covers retry of the same user-submitted form, including a concurrent double submit or lost HTTP response. A new page render has a new key and creates a new operation. The current product does not yet determine whether separate employees' submissions represent the same business operation or independent assignments; it must not deduplicate those by case number or content hash alone. If Redis admission fails after commit, HTTP returns a generic 503 with Retry-After; upload/KAD retry with the same form key reuses the task IDs and safely attempts delivery again. A client timeout may follow successful Redis admission, so delivery is at-least-once and worker claims remain authoritative. A transactional queue outbox and automatic dispatch retry service remain separate engineering work.

## Local document file creation

- Treat stored source bytes as immutable once a database row references their path. Create new files exclusively with private permissions at the time of creation; never follow a symlink or truncate an existing path. Sync file content and the containing directory before committing metadata that references the file. If writing/syncing fails, remove only the exact partial inode created by that call.
- If a database rollback or process failure leaves a content-addressed file without a row, a later admission may reuse it only when it is a regular, non-symlink file owned by the service user with exact mode `0600`, its opened inode matches the no-follow path check, and its streamed SHA-256 equals the expected content hash. Re-sync the file and parent directory before inserting metadata. A partial, mismatched, non-private or non-regular path fails closed; never overwrite or automatically delete it. This recovers retryable admission without a cross-system transaction or destructive orphan cleanup.
- This protects against permissive process umasks, stale path collisions and partial file creation. It does not provide encryption, backups, retention/deletion, orphan cleanup, or protection against a compromised service account.

## Migration database target

- The effective migration URL must target the same database as the effective runtime URL in every mode, including debug and development fallback configurations. Validate this before constructing engines or invoking Alembic so a local `init-db` cannot migrate a database different from the one the application uses.
- Production adds separate requirements: both URLs must be explicit PostgreSQL connections with distinct database role names. Debug mode may use one role or separate roles for the same target.

## Production runtime PostgreSQL privilege ceiling

- In `APP_DEBUG=0`, the application runtime engine rejects PostgreSQL superuser/admin attributes, database or `public` schema CREATE, any role membership, ownership of `public` relations/routines/types, TRUNCATE/REFERENCES/TRIGGER privileges on `public` tables, UPDATE on `public` sequences, write privileges on `public.alembic_version`, and EXECUTE access to a `SECURITY DEFINER` routine in `public`. The guard runs after `search_path` is pinned on connection creation and every pool checkout; the separate migration engine is exempt so its schema-owner role can apply Alembic.
- This is a fail-closed runtime capability ceiling, not a complete least-privilege ACL policy. It does not audit arbitrary non-public objects, exact allowed DML grants, network controls or deployment policy. `APP_DEBUG=1` remains a local-development exemption and is not an acceptable production setting.
- The disposable role drill accepts the direct DML runtime profile and rejects direct admin, role membership, public-object ownership, TRUNCATE, REFERENCES, TRIGGER, sequence UPDATE, Alembic revision-write, and executable `SECURITY DEFINER` profiles without disclosing role identity. A synthetic definer function demonstrated owner-privilege DML before production checkout rejected that runtime identity. `db-role-check` reports only the aggregate count of executable public definer routines; it does not reveal routine/role/database names. The drill also verifies pooled privilege changes are rechecked, concurrent checkouts are bounded, and the migrator remains separate.

## PostgreSQL schema resolution

- ORM metadata and Alembic migrations use unqualified table names and the schema checker inventories `public`; both runtime and migration sessions must therefore resolve application relations against `public` regardless of role defaults.
- PostgreSQL engine connections pin session `search_path` to `public` before dialect inspection and reset it on every pool checkout. Alembic installs the same hooks before opening its migration connection. This prevents the default `"$user", public` path, role-specific settings, or a session-level change left on a pooled connection from resolving a same-named private schema first.
- The disposable split-role drill creates decoy `runtime_probe` and `migration_probe` schemas and proves fresh and reused runtime/migration connections plus Alembic migrations use `public`. The shared database was not changed. A future move to multiple application schemas requires an explicit schema contract and migration plan.

## Browser automation

- SRO fields are found through inspected IDs, not labels.
- Prefer direct page URLs over duplicate menu entries.
- Refresh element references after WebForms postbacks.
- Never click Add merely to inspect a form: it may create a draft.
- Do not bypass external access controls.

## Product and deployment constraints

- The target is 20+ staff and large document batches, but concurrency and throughput must be measured before enabling more workers.
- Preserve a simple supportable stack where evidence supports it. Avoid visual redesign while core workflow correctness, durability and recovery remain incomplete.
- Do not call the system production-ready until operational, security, recovery, load and end-to-end evidence exists.
