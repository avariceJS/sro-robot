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

### SRO register

The register navigation entry is present in the authenticated application.
The list content, search behavior, card fields, member status semantics, and
relationships were not examined in this pass.

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
