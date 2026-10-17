# SROArbitr read-only register reconnaissance

## Scope and evidence

On 2026-10-17, the owner-authorized authenticated Safari session was used to
navigate list pages only. The inspection covered the SRO register, legal entity
and sole-proprietor register, accreditations, payments, insurance, inspections,
and documents. This records visible page structure and control labels, not a
complete business-process specification. No row-level personal data, real
identifiers, balances, document content, or screenshots are retained here.

No Add, edit, delete, export, publish, confirm, send, group-operation, or
record-detail control was invoked. No form was opened. List results and filters
can expose staff and member personal data; only the columns and control groups
are summarized below. Labels alone do not establish the semantics of an
operation.

## Observed list structures

### Legal entities and sole proprietors

The combined register exposes text search, status and region filters, numbered
pagination, and a mixed list with name, contact, website, employee, insurance,
bank, membership, conformity, publication, and registry-number columns. Visible
organization rows also show legal form and INN. Search behavior, identity
verification rules, card fields, case linkage, and record lifecycle were not
tested. The Add control was not opened because its side effect is unknown.
See [DEBTOR_ENTITIES.md](DEBTOR_ENTITIES.md) for the internal implementation
and its qualification boundary.

### Physical persons

The person register exposes text search, status filtering, membership and
export controls, and a paginated list with registry number, name, contacts,
member/publication/suspension indicators, status, and join/exclusion dates. The
list makes status and contact fields visible while legal-form/INN display is
specific to the mixed organization/IP list. This is a list-level distinction;
the two card forms and their rules were not compared. The Add control was not
opened.

### SRO members register

The SRO register exposes search, year/month, status, representation, and region
filters. Its list includes registry number, name, contact, website,
representation, member, publication, suspension, conformity, notification,
status, admission/reinstatement, and exclusion columns. The page shows summary
groups for members, excluded entries, and publication. A "Send message" control
is present alongside export; neither was invoked. Message semantics and the
meaning of each status/date remain unverified.

### Assignments

The assignments area has separate list views labeled for appointments, court
hearings, creditors' meetings, reporting, and statistics. The list controls
include search, year/month, assignment type, procedure, and archive selection.
The page arrived with an existing query and no rows displayed, so no assignment
columns or date/status semantics could be observed. Its Add link points to an
assignment card, but the target was not opened. The visible tabs do not establish
whether a date is an appointment date, hearing date, or report deadline.

### Accreditations

The list exposes search, status, period, and activity-type filters and a
paginated table. Columns identify registry number, recipient, period,
certificate, work types, order number/date, contract, payment, action type,
publication, and a comment/site note. The visible action-type values include
issuance, renewal, and cancellation. These are observed list values only; no
rules for eligibility, dates, payment state, or permitted transitions were
inferred. Recipient rows show organization identifiers in addition to names.

### Payments

The page has separate tabs labeled for the payment list, debtors, payment
schedule, and group operations. The payment list offers search, year/month,
payment-type, an additional "all payments" selector, and payer controls. Its table includes payer,
registry number, member status, date, control, purpose, period, amount,
confirmation, publication, payment document, and comment. A period summary
groups amounts by payment type and confirmation state. This confirms a
relationship to registered members and payment types, but does not qualify
accounting rules, debt calculations, payment confirmation, or group actions.
No payment or group operation was initiated.

### Insurance

The insurance page has contract and insurer tabs, search, branch, contract-type,
insurer and several review filters. The visible filters include control,
membership, expired, published, and error states. The contract list exposes
insured member, coverage period, notification, insured amount and policy
reference/date, contract type, premium and payouts, insurer, participation,
control, and contact columns. An information panel says expired contracts are
marked red, contracts expiring within 30 calendar days orange, collective
contracts can cover all SRO members, and auto-notification can notify members
of expiry. These are displayed descriptions, not verified notification or
collective-contract behavior. No contract or notification was changed.

### Inspections

The page has list and calendar views, search, year/month, inspection-type,
method, and status filters, plus export. The list columns include member name,
registry number, status, period, type/basis/kind, notifications, documents,
suspension, extension, short description, completion, and publication. A yearly
summary distinguishes scheduled and unscheduled inspections. Visible short
descriptions can indicate whether a violation was found. No inspection detail,
document, calendar event, or workflow action was opened.

### Documents

The page has views labeled incoming/outgoing, mandatory, and registry excerpts.
Visible controls include search, year/month, direction, type, status, sending
mode, control, marked items, and task-related filters. The current list was
already filtered on arrival; no search or filter was submitted and no result
was opened. Document fields, routing, attachment behavior, required-document
rules, and task semantics remain unverified. The Add link target was not opened.

## Product implications

The observed reporting registers are operational worklists with useful
filters, status and date dimensions, linked people/organizations, and document
references. They should not be mirrored as isolated CRUD screens. Internal
payment, insurance, and inspection workflows need confirmed ownership,
accounting rules, alert behavior, lifecycle semantics, and a safe sample data
contract before implementation. SRO Robot currently has no qualified external
read adapter for these registers; no live data was imported. Existing internal
document intake, local OCR, resumable debtor cards, assignment drafts, audit,
and per-user encrypted SRO session mechanisms remain distinct from these
observations.

## Published SROArbitr help pages (2026-10-17)

After opening the site's Help link, the authenticated Safari session exposed a
static online help viewer at `/docs/#/`. The `firms` and `persons` help pages
were read. This is product-published descriptive documentation, not an
inspection of a live card form or a test of a live operation. The details below
are documented capabilities; they do not establish current account permissions,
requiredness, selector behavior, or allowed state transitions.

### Organizations and sole proprietors (`/docs/#/firms`)

The help describes a combined organization/IP register. Search is documented
for short, full and former name, INN, registry number, phone/email and region.
The organization card is described with separate short name, full name and
legal-form reference; INN; activity regions; representation/branch and access
flags; private comment; membership status; suspension, publication and
notification controls; and Federal Law compliance fields. IP identity is
linked through a selector to the physical-person register. That relationship
is documented, but selector matching and whether it is mandatory were not
verified.

The documented state-registration group includes OGRN and an EGRUL search
dialog (name, INN, OGRN), external links to Kartoteka/FNS, OKONH, OKPO, KPP,
primary and additional OKVED, affiliation, registration date, and
registration-document details. The card also documents bank-account rows and
typed contact collections for registration, postal and actual addresses,
phones and email recipients. Separate public-contact fields are described.
Employee rows select physical persons and carry position, employment dates and
role/publication flags. Other tabs describe NRS specialists, accreditations,
payments, inspections and insurance. No optional/required rule or data value
from these examples has been inferred for SRO Robot.

### Person cards and appointments (`/docs/#/persons`)

The help describes the persons register as containing members, SRO employees,
and employees/managers linked to organizations. Person search is documented by
name/former name, registry number, email/phone, region and INN. Its appointment
group documents case number/date; court chosen by case-code prefix; incoming
request; judge; procedure, debtor category/type and appointment type references;
debtor selection across people and organizations; creditors; applicant and
address; practitioner selection; assets; additional-insurance flag; outgoing
number and protocol details; remuneration; removal reason; suspension; archive
and publication.

The help gives distinct dates for appointment, next hearing, procedure
termination/completion, creditors' meeting and release/removal. A calendar is
described with event dependencies, day offsets, planned/actual dates,
notifications and downstream recomputation. These labels and rules come from
the help page only; they are not verified as current runtime behavior or
qualified against office policy. See [APPOINTMENTS.md](APPOINTMENTS.md) for
which values SRO Robot currently stores as internal drafts.

The help also describes organization-card tabs for accreditations, payments,
checks and insurance. In particular, it calls a payment “confirmed” when funds
have reached the SRO account; describes planned versus actual dates; and lists
payment purpose, payer, period, amount and accounting details. Inspection text
describes planned/unplanned, desk/site, periods, notifications, received
documents, suspension/extension, result and completion. Insurance text lists
policy identity/period, amounts/premium, claims/payouts, insurer and review or
publication flags. These are leads for future rule discovery only. We have not
verified accounting behavior, status transitions, notification effects, or
safe internal representations, so no external synchronization or calculation
is implemented on this basis alone.

## Next safe research steps

- Ask an owner-authorized process specialist for synthetic or safely redacted
  form specifications for organizations/IPs, payments, insurance, inspections,
  and mandatory documents.
- If further browser research is authorized, first establish a documented
  read-only path to record detail and verify that any navigation control cannot
  mutate state. Do not use Add or row-level edit actions as a way to discover
  forms.
- Keep external SRO writes disabled; separate internal drafts from external
  records and reconcile external results only after a verified read-back path
  and explicit authorization.
- To qualify the documented fields for actual office use, obtain an
  owner-approved synthetic or redacted process specification and validate
  values against the responsible staff; do not open Add or Save controls as a
  discovery technique.
