# Staff workspace and business-object workflows

The authenticated home route opens `/debtors`. Navigation is organized around
the two staff-owned work objects: debtor cards and appointments. Document
intake and the processing queue remain supporting tools at `/debtors/intake`;
they use the existing worker, OCR, batch idempotency, KAD helper, and task
review paths.

## Debtor cards

`/debtors` searches cards by display name, INN, case number, and address, and
filters by lifecycle state. The SRO registry link is explicitly shown as
unconfirmed because the current model has no verified external-card reference.

The create page offers the existing KAD intake, batch document upload, and
manual entry paths. Manual entry starts as an unsaved form. Cancel returns to
the list without a row. Save draft creates an internal card with state `draft`
and no source task; confirmation requires surname and given name and changes
the state to `reviewed`. Editing a reviewed card and saving as a draft removes
its reviewed eligibility until an employee confirms it again. INN format,
checksum, and duplicate checks still apply. Every persisted version is
attributed to its editor and recorded by the append-only database revision
trigger. Persisted cards are not deleted through this workflow.

Each manual create form carries a one-use opaque action key. The server stores
only its hash and a digest keyed by that action key over the submitted entity
type, action and fields, with the created card in one transaction. A repeated
POST after an uncertain response redirects to that same card; reusing the key
with changed values is rejected. Opening a fresh form allows a separate card,
including an unfinished draft that has no INN yet.

OCR-created cards retain the existing `prepared` review state and evidence.
For extracted debtor names, role-bound INN/SNILS and directly scoped debtor
addresses (including accepted immediate blank-line continuations), entity
fields, act type, date, case number and court, the task review shows the
recognizer variant, character range and a short OCR excerpt to help staff find
the suggestion in the source. When the recognized text retains form-feed page
separators, the review also shows the one-based PDF page; for photos or sources
without explicit page boundaries it shows no guessed page number.
The card editor shows persisted field warnings beside their values, including
after an employee confirms the card, so a warning that prevents copying a value
into an assignment stays visible during later work. The task source links back
to its case and original PDF. PDFs can be viewed inline only through a
hash-verified, private, sandboxed response; original downloads remain available.
Non-PDF source files remain download-only.

The batch form accepts drag-and-drop while preserving the existing 100-file
batch limit, bounded multipart chunks, persistent idempotency keys, per-file
error isolation, and task dashboard polling. Successful upload results return
to the card-intake dashboard.

## Appointments

`/assignments` is a separate searchable worklist with status and actor
information. “Create appointment” opens a reviewed-card chooser. Selecting a
card creates a separate internal assignment linked to that card and imports
only its case number and court. Applicant, practitioner, dates, protocols,
comments, and other appointment-only values remain employee-entered unless
there is an explicit supported source. Unknown practitioner information does
not prevent saving or resuming the draft.

The chooser shows only reliable case and court values as transferable. An
existing value with an OCR warning or confidence below the assignment import
threshold is labeled as requiring review and is not displayed as though it will
be copied; the employee can open the debtor card to inspect its warning and
source before creating the assignment. Blank fields remain shown as blank.

No assignment is created by card creation alone. Multiple assignments for a
single debtor remain supported. Existing source-task replay protection,
manual card selection for task-derived appointments, optimistic edit checks,
and actor-attributed revision history remain in force.

## SROArbitr boundary

No SROArbitr record is linked until read-only evidence confirms its existence.
There is no automatic-save endpoint, no live appointment form-fill worker, and
no handoff of a server Playwright session to an employee's ordinary browser.
The UI labels both external modes as unavailable and has no active control that
could create or mutate a real record. The server browser context cannot be
adopted by the employee browser without transferring credentials/cookies, and
SROArbitr's new appointment-form opening semantics have not been established
as non-persistent. A future employee-controlled handoff needs a
platform-supported or browser-local design and a separately qualified
form-opening path. Automatic save remains an unimplemented, owner-gated future
mode.

## Local verification

Synthetic UI tests cover cancel-without-insert, incomplete draft save, employee
confirmation, card selection/import behavior, and sandboxed PDF preview.
PostgreSQL integration verifies that a manual draft with no task can be resumed
and confirmed, appends revisions, and seeds an assignment with only the
confirmed case/court values. The standard `scripts/dev/verify.sh` gate checks
migrations, database roles, backup/restore, queues, and existing KAD and bulk
behavior. None of these tests writes to the real SROArbitr registry.
