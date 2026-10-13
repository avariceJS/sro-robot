# Intake, OCR, extraction and identity

## Intake paths

Implemented or present in the working tree:

- Single and multiple file upload through the web app. Admission has finite per-process defaults for request bytes (105 MiB), total body-read time (300 seconds), batch bytes (100 MiB), file bytes (25 MiB), file count (20), and concurrent multipart requests (2). The ASGI boundary enforces streamed byte and elapsed body-read limits; the multipart parser and route reject excessive counts, empty files, and per-file/batch byte violations before document/task persistence or queue admission. All limits are configurable; deployment values still need production-like tuning.
- Case-number intake adapter exists but its UI/HTTP admission and worker dispatch are release-gated until safe candidate selection is qualified.
- A shared task/document path after intake.

Not implemented: robust mail/folder connectors, production-scale batch streaming, immutable multi-file provenance, page/document splitting, cross-user semantic operation deduplication, and explicit reprocessing identity. A server-generated per-user form key makes enabled upload HTTP retries idempotent: PostgreSQL stores the key and ordered source manifest hash in `IntakeOperation`, tasks link to that row, equal-key/equal-manifest replay returns the same task set, and equal-key/different-manifest submission conflicts. The requested KAD browser-helper intake imports an exact selected PDF into local OCR with case/date/source binding. The independent experimental Fedresurs API has a separate acquisition job kind. Legacy KAD acquisition records remain readable, but those old outbox intents are withheld and never dispatched. This is request-level identity only; a new form key is a new operation and does not infer whether another employee's submission is the same operation or an independent assignment. Operation-level reprocessing history is not modeled yet. Migration `c73b8e4a19d2` is pending safe adoption to the unversioned shared DB. A SHA-256 content hash deduplicates stored document bytes; PostgreSQL transaction-scoped advisory locking serializes concurrent same-content admission across web processes, and migration `d84f3a210c6b` adds a database unique index as defense against legacy or bypassing writers. Every distinct accepted operation may still create a new task for an existing document, and pre-existing duplicate document rows are not reconciled automatically.

### Accepted intake policy

The owner confirmed on 2026-10-11: repeated submission of the same request must not create duplicate tasks for one operation; new judicial acts in the same case and independent assignments remain valid; explicit reprocessing must retain history. Unknown external outcomes must not auto-retry. The narrower same-user upload-form retry contract is implemented; cross-user semantic operation identity is not. Case number and stored-byte hash alone cannot distinguish these operations. See `50-decisions.md` for the accepted contract and remaining engineering boundaries.

## Local PDF and image processing

- PDF text extraction tries `pdftotext`; OCR is used when extracted text is below the configured threshold. Page-level handling for mixed text/image PDFs is incomplete.
- Tesseract uses multiple PSM modes over preprocessed image variants plus the original and votes across results. Do not reduce this to one pass without measured accuracy evidence.
- INN/SNILS checksums are useful OCR signals. They are warnings and do not prove whose identifiers they are.
- FIO normalization uses Russian morphology and special patronymic handling; case numbers normalize common OCR Latin/Cyrillic confusions.
- Address parsing splits a string into locality/street/building/room fields. Ambiguous parsing must remain reviewable.
- Extraction confidence is heuristic and is not a calibrated probability.

See `core/` and `documents/`. Existing synthetic regression expectations were formerly recorded in `40-extraction.md`; use synthetic fixtures only and never restore deleted/user-removed sample files without instruction.

## Identity and semantic correctness

The current parser searches the whole document and does not reliably bind FIO, INN, address and birth data to a named participant role. A court document can mention applicant, debtor, representative, creditor and manager. Do not assemble a debtor from unrelated text matches. Preserve snippets/source locations and role evidence for extracted fields. Require consistency checks and operator review when identity linkage is ambiguous.

Do not assume:

- case number implies the first/latest visible act is relevant;
- valid checksum implies the identifier belongs to the debtor;
- FIO alone uniquely identifies a person;
- independent field extractions belong to one participant;
- one PDF equals one operation/task.

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
