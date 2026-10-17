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
card controls were inspected. The internal model currently has one primary
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

## Organization/IP review contract (synthetic-only qualification)

The declared local scenario is limited to a single explicitly marked debtor
section for a legal entity or sole proprietor. It does not claim coverage of
every court-act layout or every participant role. The accepted role markers and
type prefixes are the patterns in `sro_robot/core/entity_extract.py`; an
organization or IP mentioned as applicant, creditor, representative, or another
party is not selected. If OCR variants disagree about debtor kind or name, or a
variant contains multiple candidate debtors, identity remains unresolved and
the review screen asks the employee to establish it from the original.

INN length/checksum and OGRN/OGRNIP control digits are deterministic syntax
checks. KPP is length-checked. These checks do not establish a government
registry match, entity activity, legal status, or that the value belongs to the
debtor. Conflicting OCR identifiers stay blank with a warning; a valid value
from one OCR variant is not allowed to silently defeat a different value from
another. Short name is extracted only when explicitly labeled.

One address can be carried in the current primary-address field. The parser
assigns a kind only when the source explicitly labels “адрес регистрации”,
“почтовый адрес”, or “фактический адрес”. A generic address remains untyped.
If the debtor section contains multiple address labels, the parser does not
choose one address; it leaves the field for manual correction. Registration,
postal, and actual are documented in the static SROArbitr help, but the live
card fields and their requiredness were not inspected. The current one-address
internal model cannot represent several addresses at once.

Extracted entity fields carry a character range and a short escaped excerpt
from the normalized OCR variant. The screen labels this as an OCR variant, not
a PDF page. Correcting a value changes its source to employee and removes its
stale OCR range. The source PDF remains available beside the review form.
Unsupported or conflicting evidence stays in review; it never triggers an SRO
write.

### Reproducible evidence and M2 boundary

`tests/fixtures/entity_parser_cases.json` is an independently authored,
fictional parser corpus. It covers complete legal-entity and IP requisites, a
non-debtor organization, untyped and multiple addresses, a conflicting
registration number, conflicting debtor kinds, and invalid check digits. The
unit suite compares 72 field values against those expected labels; it also
checks role invariants, OCR offsets, spaced digits, correction provenance,
internal-card creation, assignment seeding, and fake-platform fail-closed
behavior. This is exact parser agreement on synthetic text, not precision or
recall for OCR, scans, or office documents.

| M2 criterion | Evidence now | Status |
|---|---|---|
| Written rule contract | This section defines the declared parser/review scope, role/field boundaries, validators, corrections and unresolved address semantics; organization/IP card and assignment scope still lacks office-approved rules | Partial |
| Realistic synthetic examples | Eight fictional corpus cases with independent expected values; 72 field assertions | Done for parser inputs |
| Role and identity checks | Other-party isolation, type/name conflicts, multiple candidates, identifier conflicts and syntax failures | Done synthetically |
| Scenario-specific review fields | Entity/IP registration data and address-kind choice; field-level OCR variant/range/snippet; original-document preview | Implemented; route/template integration tests |
| Fake-platform execution | Entity scenario refuses external preflight/run; a test asserts no client call | Partial; fail-closed guard exists, but the prepare/write/reconcile path is not exercised against an isolated fake |
| Assignment optional/pending semantics | Internal drafts can be created from reviewed entity cards and resumed; field values remain visibly internal/unqualified | Partial; employee-confirmed requiredness and date/classification semantics are not established |
| Document/OCR evaluation | Eight text-level cases plus `scripts/dev/quality/entity_scan_smoke.py`, which rendered and locally OCRed two synthetic scanned PDFs (one company, one IP) | Partial; the scan smoke is a two-case plumbing check, not a precision/recall corpus |
| Full mass-upload browser journey | `scripts/dev/bulk_browser_e2e.py --entity-flow` passed against an isolated local PostgreSQL/Redis app: synthetic company, IP and duplicate entity PDFs uploaded together; 3 tasks reviewed; company correction saved and draft resumed after navigation; 2 reviewed cards linked to 2 internal assignment drafts; same-kind duplicate INN refused; no SRO access | Done for this synthetic local user journey; office layouts and OCR quality remain unmeasured |
| External SRO mapping/read-back | Static help and read-only register observations only; live card forms and write/read-back semantics are unverified | Blocked by unknown behavior and the no-write boundary |

The full local browser journey closes one material M2 gap: it exercises the
entity path from mixed batch upload through edited review, durable draft resume,
duplicate refusal, internal card approval, and assignment draft creation. Its
three entity inputs and the scan smoke are synthetic. No office-document
precision/recall or review-burden result is established. No live entity form,
external payload mapping, fake prepare/write/reconcile adapter, or SRO semantic
read-back has been qualified. Accordingly Stage 7 remains M1 partial rather
than claiming the complete M2 gate. Remaining evidence includes a representative
office-approved corpus and qualified workflow semantics; the external integration
stays disabled and read-only.
