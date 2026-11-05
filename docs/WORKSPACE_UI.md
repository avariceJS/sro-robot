# Staff workspace

## Main work areas

The authenticated home page is the debtor-card registry at `/debtors`. Staff
work with debtor cards, appointments, document intake and case packages through
the workspace navigation. Person cards and organization/IP cards are separate
internal records. Appointment drafts are separate records: debtor-linked drafts
reference a selected reviewed card, while applicant-role source drafts can await explicit
debtor selection. Creating or editing either internal object does not write to
SROArbitr.

The debtor registry supports search and lifecycle filters. Manual card entry is
available alongside document intake. A new manual card is an internal draft
until an employee reviews and saves it as reviewed. Review fields, source
document and any OCR suggestions stay available for comparison. An SRO record
link is not shown as verified unless the application has a confirmed saved
record reference.

Document intake and processing are supporting workflows. Employees review the
recognized fields against the original, correct them, and explicitly confirm or
cancel. Confirmation may create or update an internal reviewed card under the
normal duplicate and concurrency checks. OCR suggestions do not establish the
debtor identity, authorize external actions, or replace review of the source.

## Appointment drafts

Employees can start a separate internal appointment from a reviewed debtor
card at `/assignments/new`. The page lists reviewed cards for explicit employee
selection; it does not match by name or case automatically. The new appointment
is an internal draft. Only case number and court may be copied from warning-free,
sufficiently confident source values; other fields remain for employee entry.

A separate source path may create an applicant-role appointment draft from
role-bound applicant evidence. That draft can retain its source task and
applicant value while `debtor_card_id` is empty. The UI identifies the debtor
as unselected and requires staff to bind an explicitly chosen reviewed card
before using the draft. It is not treated as a debtor match. Both paths permit
incomplete drafts, record revisions and their authors, and reject stale
concurrent edits. One reviewed card may support multiple distinct appointments.

## SRO preparation and external-save boundary

SRO form preparation is separate from internal card and appointment work. The
code has guarded person-card and legal-entity/IP adapters with scoped fields
and preview/save checks. Their presence does not authorize their use or qualify
every external action. Assignment form preparation, assignment selectors, and
a safe external assignment Add path are not qualified. An internal card or
appointment must never be described as an external SRO record.

Where the current guarded path presents a save decision, the authenticated
employee reviews the current preview and explicitly chooses Save or Close
without saving. The initiating employee, current preview token and existing
API/worker/client guards constrain that action. Save is not implied by OCR
confirmation, internal card approval, appointment creation, preview
completion, timeout, or a worker request. Do not broaden selectors or bypass
guards. The currently authorized real-site exception is limited to opening a
new blank person card, filling approved fields, inspecting it, and closing
without Save. Any real Save or other external write requires separate explicit
owner authorization. A confirmed external save requires a verified new record
identity and matching readback. A requested or submitted save is not success by
itself.

If an external result is unknown, keep it in reconciliation-required state.
Use read-only evidence to reconcile the outcome before any retry; do not turn an
unknown result into success or retryable failure. Never edit an existing SRO
record, use Save/Send during testing, or treat a preview as persisted data.

## Runtime flags

`SRO_RECOGNITION_ONLY` restricts SRO actions. The separate
`SRO_MANUAL_PREVIEW_ENABLED` can admit the narrow manual person-card preview
path while recognition-only mode is active; that path is restricted to review
and close without saving. `SRO_EXISTING_CARD_TEST_MODE` disables duplicate
lookup and blocks Save for the existing-card test path. These flags default off
in application settings. Web and worker configuration must agree; do not
change flags while work is active. A flag does not grant authority to save a
real record or qualify an unverified flow.

## Maintainer guidance

- Keep internal card, appointment, source task, and external SRO receipt states
  distinct in labels and actions.
- Keep destructive or external decisions explicit and attributable to the
  initiating employee. Preserve unsaved edits and stale-write conflicts.
- Keep unknown external outcomes on hold for read-only reconciliation.
- Route manual debtor-linked appointment creation through `/assignments/new`
  and reviewed-card selection. Preserve applicant-source drafts awaiting an
  explicit debtor link; do not add automatic matching.
- Treat screenshots, OCR values, static help text, and internal drafts as
  evidence with limits, not as external SRO qualification.

Relevant implementation: `app/api/routes_assignments.py`,
`app/api/routes_tasks.py`, `app/services/assignments.py`,
`app/web/templates/review.html`, and `app/settings.py`.
