# Internal appointment preparation

## Internal workflow

`DebtorCardDraft` and `AssignmentDraft` are separate internal records. A
debtor-linked appointment references one reviewed debtor card; a card can have
multiple appointments. Applicant-role source drafts may remain unbound until an
employee explicitly selects a reviewed debtor.
There is no automatic merge or duplicate decision based on a debtor name or
case number.

Employees create an appointment at `/assignments/new` by explicitly selecting
from reviewed debtor cards. This creates an internal draft only. If the source
card has eligible case-number and court values, they may be prefilled; applicant,
practitioner, classification, comments, protocol, publication, and event-date
fields are not inferred from debtor identity. The employee completes and checks
the draft according to the office's process.

An eligible reviewed document can also seed an appointment through its
source-linked action. The source-task path is idempotent for that task; a
different task may create a separate appointment for the same card or case.

A distinct applicant-role path can create a draft from explicit, role-bound
applicant OCR evidence. It may retain its source task and applicant value with
no debtor card selected. A labeled applicant must appear on the same physical
line as its label. An empty label and a court official's signature never supply
an applicant name. The UI marks the debtor as unselected and requires an
employee to bind an explicitly chosen reviewed card before using that draft;
the applicant evidence is not treated as debtor identity. Source-aware changes
require current review state. Revisions retain the acting employee, and stale
concurrent edits are rejected. Incomplete drafts are allowed; the editor
identifies values that still need attention. These records, links, and audit
history are internal to SRO Robot.

## External SROArbitr boundary

No appointment-form preparation or selector path is qualified. Guarded
person-card and legal-entity/IP adapters exist with scoped fields and preview
checks, but they do not cover appointment creation. The currently authorized
real-site exception is limited to opening a new blank person card, filling
approved fields, inspecting it, and closing without Save. Any real Save or
other external write requires separate explicit owner authorization. An internal appointment,
debtor card, or source-task receipt is not an external appointment and must not
be presented as one.

Do not open an external Add control, fill an appointment form, operate selector
popups, or submit Save/Send. The semantics of those actions and the appointment
field mappings are unverified. Static help text and fields observed on existing
records do not qualify a new-record workflow. Any future qualification must
establish the exact safe opening behavior, field meanings, and readback under
the applicable owner authorization.

For any separately qualified external save, a request or submission alone is
not confirmation. Success requires a verified new record identity and matching
readback. If the outcome is unknown, hold it for read-only reconciliation before
any retry. Do not edit an existing SRO record or convert an unknown result into
success or retryable failure.

## Expert review of appointment acts

The isolated reviewer has a separate `?queue=appointments` view. Import new
originals with `expert_review_kind: appointment` and an accepted local OCR
capture. An existing original retains its queue and review. This view uses
Tesseract readings and displays only source-bound appointment fields. The case,
act date, parties, hearing schedule, and named practitioner have distinct roles.
A hearing schedule comes from the operative clause. A named practitioner needs
an explicit operative approval, and can be reviewed as its own event when no
hearing clause is evidenced.

`capture_appointment_caption` in `dataset_capture.py` reads the upper part of a
local photo again for poorly segmented captions and embedded debtor biographies.
Store those readings in `appointment_caption_readings` of a new capture generation.
Complete readings retain priority for existing fields and supply the hearing
schedule. A supplemental reading with another case number is skipped. INN,
SNILS, birth date and registration address require a named debtor’s bounded
biography and their own labels. The reviewer shows them as debtor details;
they are not external assignment field mappings.

Use `court_dataset.py capture --appointment-photo-hearing` on a separate frozen
corpus generation to enable one additional full-frame Tesseract PSM 4 read when
the primary photo has a case number and one current schedule value. The capture
records the rescan protocol and same-original SHA. The projector checks the
recorded read, case, act date when readable, schedule consistency, and text
offsets. It can add the missing schedule value. Existing proposals retain their
evidence. The direct `dataset_capture.py` command accepts the same option for a
single private original. Standard corpus capture stays on its current protocol.

Approval requires a case number and complete hearing schedule, or a case
number, act date and named practitioner for a practitioner-only act. Partial
reads remain visible and can be rejected with a reason. Successful review
records the exact proposal presented and does not establish absence of omitted
fields. These verdicts feed extraction diagnosis without writing appointment
drafts or external records.

## Maintainer constraints

- Keep internal appointment creation separate from external SRO actions.
- Keep debtor selection explicit; do not auto-match on name, INN, or case.
- Keep only supported case number and court prefill. Leave other values for
  employee entry until source evidence and office semantics qualify them.
- Preserve source-task idempotency, actor-attributed revisions, and stale-edit
  protection.
- Do not infer external selectors, identifiers, requiredness, date meanings,
  or save semantics from a field label or existing-record view.

Implementation: `app/api/routes_assignments.py` and
`app/services/assignments.py`.
