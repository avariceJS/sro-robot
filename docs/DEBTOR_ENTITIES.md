# Organization and sole-proprietor workflow

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
a clearly read-only link. Separately, the static online help pages (`/docs/#/firms`
and `/docs/#/persons`) were read. They describe card field groups, searches and
relationships, but are not live forms and do not confirm current permission,
requiredness, or operation behavior. See
[SROARBITR_READONLY_RESEARCH.md](SROARBITR_READONLY_RESEARCH.md) for that
distinction and the detailed findings.

## Internal SRO Robot behavior

Manual internal debtor cards support three separate kinds: individual, legal
entity, and sole proprietor. Organization cards have full name, optional legal
form, 10-digit INN, OGRN and KPP fields; sole-proprietor cards have a separate
entrepreneur-name, 12-digit INN and OGRNIP fields. Both can carry an address and
case/court/source-document context. Person cards retain the FIO, SNILS and
12-digit INN workflow. Type-specific labels and INN validation apply on create,
edit, list search, duplicate checks, and assignment linking. Revision snapshots
record the entity kind. Duplicate INNs are prevented within each entity kind.
An IP and its separately maintained physical-person card may carry the same
12-digit INN and be linked after explicit employee selection; this does not
merge the cards.

The local OCR parser now routes documents with an explicit, role-bound legal
entity or IP debtor marker to a dedicated review scenario. It extracts a
suggested entity kind, company name or entrepreneur FIO, INN, OGRN/OGRNIP, KPP,
address, case, court, date and document type from a short debtor section. INN is
checksum-checked, OGRN and OGRNIP control digits are checked, and KPP must have
nine digits when supplied. Manual card edits apply the same checks and normalize
separators out of these numeric identifiers. These checks detect malformed OCR
but do not establish that an identifier exists in the government registry or
is currently active. The FNS describes OGRNIP as a 15-digit identifier with a
control digit ([FNS help](https://www.nalog.gov.ru/rn38/news/activities_fts/15180419/)).
Competing OCR identity/type candidates are held as ambiguous;
representative, applicant and creditor text outside the debtor section does not
become the debtor. These are review suggestions with low confidence, not
verified recognition rates. The employee must correct/confirm the fields and
approve the task before creating a reviewed internal card. The action is
idempotent by source task and the existing INN lock/duplicate guard applies.

The reviewed organization/IP card can seed a separate resumable internal
assignment. Confirmed case number and court are transferred; identity, applicant,
practitioner, event dates and SRO reference values are not inferred. Assignment
fields now represent the documented appointment groups, but classifications and
date semantics remain unqualified and are clearly marked internal. Neither
card approval nor assignment creation invokes an external SRO operation.

After both an IP card and a person card are employee-reviewed, the IP card can
be linked to one explicitly selected person card. The employee searches the
reviewed internal person list and chooses the exact card. No automatic matching
by FIO or INN occurs, and no values are copied. The relationship is stored in
the IP card's revisioned values, carries the employee actor in card history, can
be replaced or removed, and rejects person cards that are drafts or prepared
but unreviewed. This is an optional internal relationship based on the field
group described by the static help page; its equivalence to SRO selector rules
is unverified.

## Qualification boundary

The static help documents separate short name/full name/legal-form; state
registration (including OGRN, KPP and OKVED); typed addresses and public
contacts; bank accounts; employees and NRS; accreditation, payment, inspection
and insurance tabs; and an IP-to-physical-person selector. None of these live
card controls were inspected. The internal model currently has one free-text
address and does not model multiple addresses/contacts, registration history,
bank accounts, employees, membership or those related operational records. Add
those only with a qualified business contract and internal relationship design.

The parser is checked with synthetic unit samples and a disposable PostgreSQL
workflow, including mixed person/company/IP recognition in parallel. No real
office documents or representative OCR corpus were available, so quality on
office scans and unusual legal naming/address layouts is unknown. It does not
derive a short name, structurally parse address components, verify business
registry existence/status, or detect every multi-debtor document. Existing person OCR behavior
is still the only auto-prepared type; organizations/IPs remain explicit manual
review. All external SRO writes remain blocked.
