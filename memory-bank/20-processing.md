# Intake, OCR, extraction and identity

## Intake paths

Implemented or present in the working tree:

- Single and multiple file upload through the web app. One employee action accepts up to 100 files and the browser submits them sequentially in chunks of at most 20. Each chunk remains a distinct, same-user idempotent `IntakeOperation`; the page derives a stable key per chunk, so retrying the same selected files on that page reuses completed chunk tasks. A partial run is reported explicitly and is not one atomic 100-file database transaction. Browser-persisted ordered-file fingerprints retain retry keys and acknowledged operation IDs across reloads and successful completion. A shareable `/?intake=...` dashboard groups the chunk operations without changing their transaction boundary. Admission has finite defaults for request bytes (105 MiB), total body-read time (300 seconds), batch bytes (100 MiB across the UI selection), file bytes (25 MiB), per-request file count (20), UI selection count (100), and concurrent multipart requests (2). The ASGI boundary enforces streamed per-request byte and elapsed body-read limits; the multipart parser and route reject excessive counts, empty files, and per-file/batch byte violations before document/task persistence or queue admission. All limits are configurable; deployment values still need production-like tuning.
- Case-number intake adapter exists but its UI/HTTP admission and worker dispatch are release-gated until safe candidate selection is qualified.
- A shared task/document path after intake.

Not implemented: robust mail/folder connectors, production-scale batch streaming, cross-user semantic operation deduplication, and explicit reprocessing identity. The per-chunk server key makes enabled upload HTTP retries idempotent: PostgreSQL stores the key and ordered source manifest hash in `IntakeOperation`, tasks link to that row, equal-key/equal-manifest replay returns the same task set, and equal-key/different-manifest submission conflicts. The browser retains chunk identities and operation IDs across reloads; clearing browser storage or using another browser does not retain this client-side association. The requested KAD browser-helper intake imports an exact selected PDF into local OCR with case/date/source binding. Historical KAD acquisition records remain readable, but those outbox intents are withheld and never dispatched. This is request-level identity only; a new form key is a new operation and does not infer whether another employee's submission is the same operation or an independent assignment. Operation-level reprocessing history is not modeled yet. A SHA-256 content hash deduplicates stored document bytes; PostgreSQL transaction-scoped advisory locking serializes concurrent same-content admission across web processes, and migration `d84f3a210c6b` adds a database unique index as defense against legacy or bypassing writers. Every distinct accepted operation may still create a new task for an existing document, and pre-existing duplicate document rows are not reconciled automatically.

### Accepted intake policy

The owner confirmed on 2026-10-11: repeated submission of the same request must not create duplicate tasks for one operation; new judicial acts in the same case and independent assignments remain valid; explicit reprocessing must retain history. Unknown external outcomes must not auto-retry. The narrower same-user upload-form retry contract is implemented; cross-user semantic operation identity is not. Case number and stored-byte hash alone cannot distinguish these operations. See `50-decisions.md` for the accepted contract and remaining engineering boundaries.

## Local PDF and image processing

- PDF text extraction uses `pdftotext` by page. Searchable pages stay on the text-layer fast path; blank/fragmentary pages are OCRed and recombined in order. Rasterization is bounded to at most four pages per batch, and blank separator pages skip Tesseract. This fixes mixed searchable-cover/scanned-act PDFs and bounds long-PDF raster memory.
- Scanned PDF pages use grayscale/autocontrast with a 215 binary cutoff across the four configured PSM modes. If those modes produce no text, processing falls back to legacy enlarged/sharpened variants plus the original image. Photo uploads retain the legacy path. The cutoff remains unqualified for real-office documents. See `../docs/quality-benchmark-2026-10-16/README.md` for synthetic before/after measurements.
- Tesseract uses multiple PSM modes and field-level voting. Do not reduce this to one pass without measured accuracy evidence.
- INN/SNILS checksums are useful OCR signals. They are warnings and do not prove whose identifiers they are.
- FIO normalization uses Russian morphology and special patronymic handling; case numbers normalize common OCR Latin/Cyrillic confusions.
- Address parsing splits a string into locality/street/building/room fields. Ambiguous parsing must remain reviewable.
- Extraction confidence is heuristic and is not a calibrated probability.

See `core/` and `documents/`. Existing synthetic regression expectations were formerly recorded in `40-extraction.md`; use synthetic fixtures only and never restore deleted/user-removed sample files without instruction.

## Identity and semantic correctness

Current develop extraction binds FIO, personal INN, SNILS and address to explicit
single debtor-person context. Unlabelled applicants, debtor representatives,
organizations/IP, multiple names, contradictory OCR variants and uncertain
morphological conversion are left for review. Case/date conflicts also block
unattended internal preparation. The `debtor_role:v1` field source records the
parser contract; it is a heuristic role marker, not calibrated confidence or
page/span provenance. A single checksum-valid INN can remain visible as a
warning-only suggestion when other OCR variants damage the token; conflicting
valid values remain withheld. The automatic-card gate still rejects all warned
or `role_partial` values. Real-office accuracy remains unmeasured.

## Internal debtor cards and assignments

Internal `DebtorCardDraft` and separate `AssignmentDraft` records were released
in the consolidated baseline. Develop now adds `preparation_state=prepared` for
conservative automatic OCR candidates and `reviewed` for employee-reviewed
cards. Automatic preparation never approves the task or creates an external
operation. It requires role evidence, full name, a checksum-valid 12-digit INN,
case number and act date without material uncertainty. Missing/ambiguous data
stays in task review. Same-INN cards from another source are possible duplicates,
never auto-merged. Source-task uniqueness and an INN advisory lock serialize
creation; reviewed/edited cards cannot be silently replaced by OCR/source edits.

The one-action review endpoint saves corrections, attributes approval and
prepares the internal card. Duplicate conflicts retain employee corrections.
Review snapshot tokens reject stale forms; card edits retain their existing
version check. Migration `f4b7c2d091a6` adds automatic/reviewed state and attributed
append-only card snapshots, backfilling only a current-version migration baseline
for older cards. It is forward-only. Migration `a6c2d8e41b90` links internal
assignment drafts to the source task, creates conservative drafts alongside
new automatic/reviewed debtor cards, and records source-task changes in
append-only revisions. Exact source-task replay is idempotent; independent
documents may create separate appointments for the same debtor/case. Employees
can explicitly link an approved task to a selected existing debtor card. Case
and court are copied only when employee-confirmed or warning-free with
confidence >= 0.7; applicant, practitioner, their addresses and ambiguous event
dates are not inferred. Existing assignments/cards are not automatically
backfilled or merged; use the card's explicit create action. Assignment form
preparation in SROArbitr remains unimplemented pending safe live form
qualification. The common employee access policy is retained. Source-document
downloads require active authentication and verify private, bounded,
hash-matching storage bytes.

See `../docs/BULK_PROCESSING.md` and `../docs/APPOINTMENTS.md` for the UI
workflow and current limits. These changes are development functionality, not
deployment or real-document accuracy evidence. Organization/IP cards and
external assignment actions remain absent.

## Admission and review

The operator must be able to correct extracted values. Current approval digest binds the submitted value map to an audit event. Any changed values invalidate approval, and runner startup rejects stale approvals. This is an implemented safeguard, not an identity proof or write idempotency mechanism.

Target admission should combine document class, operation, party role, field provenance, validation and calibrated confidence. Unsupported/ambiguous work must stop for review instead of being guessed.

## Future domain extensibility

Support separate domain models and scenario/profile contracts for at least:

- natural-person debtor cards;
- organizations and sole proprietors;
- appointments/assignments linking an existing debtor card, case, applicant, manager and operation;
- additional acts such as applications, rulings and decisions.

Shared workflow infrastructure should not encode assumptions unique to the current person-card scenario.

## Staff-reported assignment workflow and business rules

**Source and status:** The owner relayed these requirements from staff who use SROArbitr daily (2026-10-10). They are staff-reported business knowledge, not independently observed platform behavior or proof that SRO Robot implements the workflow. Preserve the confidence labels below; unresolved rules must not become hard validation.

### Entity and lifecycle boundary

Staff report that the debtor card is created first. Staff then open the separate Assignments section and locate the debtor through that existing card. A debtor card and an assignment are related but distinct entities and operations. Creating a debtor card does not create an assignment.

The desired future workflow allows SRO Robot to prepare an assignment from known case and debtor facts before an insolvency practitioner is known. Persist that work as an **internal SRO Robot draft** that staff can resume later. Staff may enter the practitioner and confirm other facts, then initiate a separately approved external assignment operation. An internal draft must never imply that an SROArbitr draft, record, or mutation exists. Keep internal draft, ready-for-review/submission, external attempt, unknown outcome, externally saved and semantically verified as distinct lifecycle facts. The current prohibition on all real SRO changes remains in force.

### Assignment fields reported by staff

The reported working fields are: case number; arbitration court; request/incoming number; debtor; applicant; applicant address; insolvency practitioner; comments; outgoing number; protocols; refusal/archive/publication items; next hearing; procedure date; and practitioner appointment date.

- **Applicant:** staff say an applicant is absent in many cases. Their rough estimate is about 90%; it is not measured data. The workflow must support an absent applicant. Applicant requirements may vary by assignment type.
- **Applicant address:** staff require direct manual entry in SRO Robot. When an employee supplies and confirms this value, assignment preparation must use that value, preserving who supplied/confirmed it and when; do not silently replace it with an extracted value.
- **Insolvency practitioner:** often unknown at initial debtor-card creation. Staff describe a later response to office inquiries, sometimes the next day, after which they resume assignment work. This is a normal incomplete-information state, not by itself an error or terminal failure.
- **Refusal:** staff report that the word `отказ` is written in Comments when a refusal is received. The exact triggering events, affected operations and downstream handling remain unconfirmed.
- **Next hearing:** staff report that the right-hand field/column for Next hearing receives a date from the judicial ruling. The exact UI control, date format and act-selection rules remain to be verified.
- **Outgoing number, protocols, refusal/archive/publication items:** staff report that these are used. Their exact meanings, allowed values and transition rules are unresolved.
- **Procedure date and practitioner appointment date:** staff preliminarily associate these dates with invoicing. The precise event that sets each date, whether either is required, and correction rules are unknown.
- **Request/incoming number:** the staff response did not establish whether it is technically or procedurally required.

Do not map these labels to platform selectors or impose form-level validation from labels alone. Verify field identity and technical requirements only through a safe, read-only inspection or isolated fixture; Add may have side effects and remains prohibited on the real platform.

### Reported SROArbitr areas and future scope

Staff listed these areas as part of their work environment:

- **Registers:** SRO register; legal entities and sole proprietors; individuals; assignments; accreditations.
- **Reports:** payments; insurance; checks; documents.

This is a potential product-scope inventory, not a specification or evidence that every section needs automation. Business rules and permissions for these areas are not yet known. Future demos may use synthetic data only and must be labeled as simulations, not verified office workflows.

### Open business questions

- Which assignment types exist, and which fields are conditional for each type?
- Which fields are technically required by each SRO form versus required only by office policy at a particular workflow stage?
- How is an assignment linked to an existing debtor card and proceeding, and can one case/person have multiple assignments, corrections or replacements?
- What precisely do request number, outgoing number, protocols, refusal/archive/publication items mean, and which staff actions change them?
- What events determine the procedure date and practitioner appointment date, including their relationship to invoicing?
- How is an insolvency practitioner selected, validated, replaced and associated with a proceeding? What source and approval are required?
- When is applicant data present or required for each assignment type? Who may supply or correct applicant address?
- What are the complete business rules and access needs for the other listed registers and reports?

Keep these questions visible in future business discovery. Do not infer answers from apparent UI labels or from a single sample.
