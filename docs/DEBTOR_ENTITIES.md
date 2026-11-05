# Debtor entity cards

## Supported internal kinds

Internal debtor cards are typed as individual (`person`), legal entity
(`legal_entity`), or sole proprietor (`sole_proprietor`). They are distinct
records with type-specific fields and validation. A card is an internal SRO
Robot record; its existence does not establish or change an external SROArbitr
record.

- **Individual:** name components, INN, SNILS, address and address type, plus
  case, court and source-document context.
- **Legal entity:** full legal name, optional short name and legal form, INN,
  OGRN, KPP, address and address type, plus case/document context.
- **Sole proprietor:** entrepreneur name, INN, OGRNIP, address and address
  type, plus case/document context.

The card display-name limit is 512 characters. Longer legal names found by OCR
are truncated with a warning for review; manual saves enforce the card limit.
The parser examines a bounded debtor section (up to 800 characters) and warns
when that boundary is reached. A bounded excerpt or page is shown only when its
source location is known; unknown page boundaries are not inferred.

## Extraction and employee review

Local OCR and the entity parser may suggest a legal-entity or sole-proprietor
card only from an explicit debtor-role section. Applicant, creditor,
representative, and other-party mentions are not debtor evidence. Conflicting
entity kinds, names, identifiers, or multiple possible debtors remain
unresolved for the employee. Suggestions require comparison with the source
document and employee confirmation before they become a reviewed card. OCR
does not verify registry existence, legal status, or identifier ownership.

INN has entity-specific length and checksum validation. OGRN and OGRNIP validate
their expected length and control digit; KPP is length checked. These are format
checks only. Conflicting values are not silently resolved in favor of one OCR
variant. Short name is suggested only when explicitly labeled.

The current internal model holds one primary address. A type is assigned only
when the source explicitly labels it as registration, postal, or actual. A
generic address remains untyped for employee choice. Multiple address mentions
are not collapsed into one guessed value; the employee must resolve them from
the document. Address components and multiple typed addresses are not modeled.

Corrections become employee-sourced values and lose stale OCR source spans.
Source pointers, where available, locate text only; they do not establish role
or correctness. Unsupported layouts and weak or conflicting evidence stay in
review. Document text and identifiers remain on the local processing path.

## Relationships and appointments

A sole-proprietor card may be linked to a separately reviewed individual card
only after an employee explicitly selects that card. The system does not link by
name or INN automatically and does not merge the records.

An employee can create an internal appointment by selecting a reviewed card at
`/assignments/new`. The employee chooses the card; there is no automatic match
by debtor name or case number. Only eligible case-number and court values may be
prefilled. Other appointment details remain for employee entry and review.
Neither card review nor appointment creation invokes an external SRO operation.

## Qualification boundary

The parser is conservative and review-oriented; performance on office scans,
unusual naming, and unrepresented court layouts is not established by the
parser's syntax checks. It does not verify government registry records or
structurally model address parts. Internal cards do not yet model multiple
addresses, contacts, bank accounts, employees, membership, or registration
history. Live organization/IP SRO forms, external field semantics, selectors,
and save/readback behavior are not qualified. Preserve these limits when adding
fields or external operations.

Implementation: `sro_robot/core/entity_extract.py`,
`sro_robot/scenarios/entity_review.py`, and `app/services/assignments.py`.
