# Person-card actions and autosave — 2026-11-05

After the one-time document confirmation, the editable person card has three
actions: Close, Create assignment, and Fill SRO form (or open the existing SRO
result). Each action saves the submitted card edits first. Close returns to the
person registry. No separate draft-save or data-confirmation button is shown.
Validation errors and concurrent changes retain submitted values for review.

Unfinished person work is displayed as Draft independently of the internal
reviewed/prepared validation state. Card created requires a verified saved SRO
person-record URL and a non-dry-run saved receipt; an unsaved form preview does
not qualify. Unknown external outcomes remain Reconciliation required.

Assignment creation saves edits, synchronizes an eligible approved source and
creates its assignment in one transaction. A repeated browser action returns
the same assignment; changed data under that old action key is rejected. Fresh
actions can create additional independent assignments. Source-aware writes lock
the task before the receipt and card and reject stale source review tokens.

SRO preparation saves edits and updates the existing source approval through
the native audit path before using the existing preview admission handler.
For an already started, closed or unknown attempt, the action saves local edits
and opens the existing result without enqueueing another external attempt.
No external Save is performed automatically.

# Two-action document review — released sro-v1.3.4

The normal editable OCR task review exposes Confirm and Cancel. Confirm reuses
the existing save/approve/internal-card transaction and opens the debtor card;
Cancel links to the home worklist with the existing unsaved-change guard. The
standalone Save,approval-only,batch-next and long duplicate confirmation actions
are no longer shown. A conflict with an existing reviewed card returns409 with
submitted values retained instead of a server error or an implicit overwrite.

The card-level prepare-form/screenshots action introduced in this release is
superseded by the autosaving three-action form described above. Screenshot
Save/Close remains a separate employee decision.

Deployed as sro-v1.3.4 (a37f992bb87262d88de364beca166d064535c0f8) after180
focused Python/16 Node tests and independent review. Authenticated Brave GETs
verified two actions on task4 and the SRO prepare button on internal card2.
Confirmation-to-card service behavior and conflict handling were tested with
synthetic data; the agent did not invoke real confirmation or SRO preparation.
Public version/readiness and verified DB checkpoint/activation passed.

# Screenshot review and explicit SRO Save — 2026-10-29

The owner selected server-browser screenshots rather than ordinary-browser form
handoff. No extension update or new SRO host permissions are part of this flow.
From an approved person task, the robot opens a new blank SRO person form,
fills the supported fields, reads them back, and shows screenshots in the task.
The browser stays open while the initiating employee reviews the screenshots.
“Save card in SROArbitr” is a distinct explicit decision; “Close without saving”
and timeout never authorize a Save. The card registry links to this source task.

Save decisions bind the employee, exact preview screenshots, approved values,
and current preview interval. The worker claims preparation once and consumes
one Save authorization before submitting the known main person-card Save.
Generic external writes and nested address controls remain outside this flow.
After Save, only a new identified person-card URL plus matching form readback
can establish success. Any missing/uncertain response is shown as unknown and
must be reconciled read-only before another attempt. A requested Save is never
reported as a completed save. Source changes are tested with synthetic inputs;
no real SRO record is created during development or validation.

# Registry navigation — 2026-10-29

The sidebar exposes Physical persons, Legal entities / sole proprietors, and
Appointments. The person worklist is `/debtors`; organizations use
`/debtors?registry=organizations`. Filtering is applied in SQL before count and
pagination. Explicit appointment debtor selection still includes all types.
Document upload and KAD entry are available from each registry; their supporting
page is `/debtors/intake?registry=persons|organizations|assignments`. Navigation,
upload redirects, KAD completion, review links, and card closing preserve the
selected registry. Processing history is explicitly shared across all registries;
the entry point does not force a recognized identity to a different entity type.
Appointments use the existing reviewed-card workflow after document review.
Case packages remain accessible through existing contextual links, outside the
primary registry menu. No schema or OCR extraction changes are part of this UI work.

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
The card history links to a read-only page for each exact saved revision. It
compares only the fixed user-facing fields with the immediately previous
stored revision, identifies the employee and time, and shows the initial
snapshot without implying an earlier version. The link opens separately so an
employee's unsaved card edits stay in their original tab; viewing history never
restores or changes card data.

An employee can explicitly link a reviewed sole-proprietor card to a reviewed
person card. If that person card is later returned to draft, the stored link is
preserved for auditability but is shown as unconfirmed; the employee must
review the person card again before treating the relationship as current.

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

Editable task review, manual card creation, debtor-card and assignment forms
warn before leaving the page with changed unsaved values. Read-only task forms
do not prompt. Returning fields to their original values clears the warning
condition, and submitting a form proceeds without an unload prompt.
On manual-card creation, changing entity kind after entering values asks before
clearing that kind's fields; canceling the switch keeps the old kind and data.
After confirming, the cleared values become the new comparison baseline, so the
intentional discard does not trigger another unsaved warning when leaving.
Values returned after a validation error remain marked unsaved, so leaving that
form still prompts even before the employee edits the retained values again.
The same protection applies when a debtor-card or assignment save is rejected
as a duplicate or stale-version conflict. Conflict pages show the current saved
values beside the submitted values; applying a submitted value marks the form
as changed. A stale task-review response is read-only, but warns before leaving
when its submitted values differ from the current task snapshot.
Staff can explicitly post those proposals to a recovery view. It issues a fresh
review token for the current task snapshot and keeps saved values separate from
the stale proposals. Each proposed field must be copied deliberately; the
recovery page itself does not write task or audit data. A later concurrent edit
still invalidates the token and returns the task to a read-only conflict.

No assignment is created by card creation alone. Multiple assignments for a
single debtor remain supported. Existing source-task replay protection,
manual card selection for task-derived appointments, optimistic edit checks,
and actor-attributed revision history remain in force.

The assignment history links each saved version to a separate read-only page.
It compares the case number, internal status, and fixed assignment fields with
the previous existing revision, preserving multiline comments and protocol
text. It displays only those labeled values and never restores or changes the
current assignment; opening the detail in a new tab preserves unsaved fields.

The assignment editor previews the explicitly linked source act beside its
form and lets staff switch to other acts in the source task's active case
packages. It uses the existing hash-verified private preview/download routes;
switching keeps unsaved form values in place and does not copy OCR fields. A
manual assignment without `source_task_id` does not use the debtor card's task
as assignment evidence. Active membership is resolved in one database query
snapshot so a concurrent package change cannot mix incompatible memberships.

The case-package page also supports deliberate membership for existing
assignments. Staff can search by debtor/case or exact assignment `#ID`, inspect
the selected identity, and add/remove it independently of task membership. The
append-only membership history records the actor and time; assignment content,
debtor identity and source evidence remain unchanged. Assignment pages show
their active package backlinks. Candidate and history pagination retain both
task and assignment searches. Search submissions and membership changes keep
the other filter and the current history/page position; status labels match
the assignment editor.

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

## Staff workspace visual shell

Authenticated staff pages use a shared responsive shell with a persistent
section navigation, a page heading, and consistent worklist cards. The debtor,
assignment, and case-package lists use a responsive search/filter row, a
scrollable data table, status badges, and empty states. Their existing routes,
form fields, query parameters, HTMX behavior, and record actions remain the
source of workflow behavior. Tailwind CSS and Flowbite are pinned and built to
local static assets; no external CDN or Flowbite JavaScript is required for
these static navigation and list components. Regenerate the stylesheet with
`npm ci --ignore-scripts` and `npm run build:css`.

Visual patterns are adapted from the free Flowbite component examples:
[sidebar](https://flowbite.com/docs/components/sidebar/),
[search input](https://flowbite.com/docs/forms/search-input/),
[table](https://flowbite.com/docs/components/tables/), and
[badge](https://flowbite.com/docs/components/badge/).
Only free component patterns are used; no paid Flowbite blocks or assets are
included. The distributed stylesheet is built locally from the pinned packages,
and their MIT notices are retained at `app/web/static/licenses/flowbite.txt`
and `app/web/static/licenses/tailwindcss.txt`.
