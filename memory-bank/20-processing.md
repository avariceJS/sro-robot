# Intake, OCR, extraction and identity

## Intake paths

Implemented or present in the working tree:

- Single and multiple file upload through the web app.
- Case-number intake intended to fetch a judicial act through MKAD.
- A shared task/document path after intake.

Not implemented: robust mail/folder connectors, large-batch admission controls, immutable multi-file provenance, page/document splitting, duplicate-task suppression and durable intake idempotency. A file content hash deduplicates stored document bytes but does not prevent a new task being created for an already-seen document.

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
- appointments/assignments linking case, debtor, manager and operation;
- additional acts such as applications, rulings and decisions.

Shared workflow infrastructure should not encode assumptions unique to the current person-card scenario.
