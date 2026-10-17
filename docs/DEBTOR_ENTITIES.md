# Internal debtor cards for organizations and sole proprietors

## Read-only SROArbitr observation

On 2026-10-17, the authenticated `Юридические лица, ИП` register was opened in
Safari and inspected without invoking its Add, edit, export, or state-changing
controls. The shared register page exposed a text search, status filter, region
selector, membership/export controls, and numbered pagination. Its visible
columns were name, contacts, website, employees, insurance, bank, member,
conformity, publication, and registry number. Visible organization rows also
displayed the legal form and INN. No names, identifiers, addresses, row IDs, or
screenshots are retained here.

The Add control was not opened because its side effect is unknown. No existing
record was opened because the row's card-navigation behavior was not exposed as
a clearly read-only link. Therefore the exact external card fields, OGRN/KPP
requirements, IP-specific controls, external case linkage, and record lifecycle
remain unverified. The observation establishes the list structure only.

## Internal SRO Robot behavior

Manual internal debtor cards now support three separate kinds: individual,
legal entity, and sole proprietor. Organization cards have a full-name,
optional legal-form, and 10-digit INN field; sole-proprietor cards have a
separate entrepreneur-name and 12-digit INN field. Both can carry the existing
internal address and case/court/source-document context. Person cards retain
their existing FIO, SNILS, and 12-digit INN workflow. Type-specific labels and
validation are used on create, edit, list search, duplicate checks, and
assignment linking. Revision snapshots record the entity kind.

These are resumable internal drafts and employee-reviewed records only. OCR
automatic card preparation remains limited to the existing physical-person
scenario. An organization or IP can be linked to an internal assignment after
employee review, using only its explicitly confirmed case and court values.
This does not create or identify an SROArbitr record.

## Qualification boundary

The visible register supports searching and browsing a mixed organization/IP
inventory, but its card form was deliberately not opened. Do not add OGRN, KPP,
representative, bank, staff, insurance, membership, publication, or external
record-status rules until a safe read-only card inspection or owner-supplied
business specification confirms their meaning. Do not connect OCR extraction
to organization/IP cards until synthetic and approved office documents qualify
entity and role binding. All external SRO writes remain blocked.
