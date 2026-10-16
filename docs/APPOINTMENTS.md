# Internal appointment preparation

## Implemented workflow

`DebtorCardDraft` and `AssignmentDraft` are separate internal records. An assignment always references one debtor card; several assignments can reference the same debtor. The system does not merge or deduplicate assignments by case number or debtor identity.

When OCR conservatively prepares a debtor card, or an employee confirms one from a reviewed task, the same transaction creates an internal assignment draft linked to that exact source task. The draft copies only the card's case number and court when the stored extraction has no warning and confidence at least 0.7 (employee-confirmed values are accepted). Applicant, applicant address, insolvency practitioner, comments, protocol/refusal/archive/publication fields and event-specific dates remain blank. The existing parser does not provide role-bound evidence for those assignment fields, and the business meaning of several dates is not established.

The bulk dashboard shows the number of source-linked assignment drafts and links each document to its draft. If a reviewed document belongs to a debtor already represented by another internal card, the employee can select that card from the task page. The operation requires an approved task with a current approval digest, explicitly records the employee as author, links the assignment to that source task and uses only confirmed case details or warning-free, high-confidence court text. No automatic match by name or case number occurs. A source task can seed only one assignment; a different task can seed another assignment for the same debtor and case.

Employees share the debtor-card and assignment lists. Assignment search supports debtor name and case number; status filters cover draft, awaiting information and ready for review. The editor permits incomplete values, lists unfilled fields as items to verify for the individual assignment, and shows source task, debtor link, revision history and the last editing employee. Concurrent edits use row locking and optimistic version checks. PostgreSQL records each assignment revision with its actor and prevents revision mutation. These are internal SRO Robot records only.

## External SROArbitr boundary

The employee login and encrypted per-user SRO session are already implemented. The existing strictly unsaved browser adapter has selectors and guards for a new person card only. No assignment-form field selectors or safe Add/empty-form behavior have been confirmed. The assignment registry inspector deliberately reads the list and does not press Add because existing inspection notes say Add may create a record. The current environment also has no open employee-authenticated browser session. Therefore this release does not offer assignment-form preparation and has not opened or filled a live assignment form. Do not infer an external SRO record from an internal draft.

The internal user interface states this boundary explicitly. The current SRO client guard remains unchanged; adding an assignment-specific preview requires a verified personal session, read-only field inspection, and affirmative evidence that opening a new form cannot persist a record. If the platform's Add control creates a draft or changes state, a live open/fill/close test is not permitted under the present safety rules.

## Migration and verification

Migration `a6c2d8e41b90` adds nullable `source_task_id`, its task foreign key and a unique constraint. Existing internal drafts remain intact and unlinked. The migration is forward-only because automatic downgrade would discard assignment provenance. Runtime schema checks verify the new uniqueness and foreign-key guards.

Synthetic unit and PostgreSQL checks cover bulk OCR to debtor card to assignment, idempotent replay by source task, multiple assignments for the same debtor/case, explicit employee linking from another task, incomplete drafts, concurrent stale edits and actor-attributed revision history. They do not measure real-office extraction accuracy or verify a live SROArbitr assignment form.
