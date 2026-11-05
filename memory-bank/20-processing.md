# Document processing contracts

## Intake and queues

Uploads preserve original bytes with bounded/hash-verified private storage.
Content deduplication does not collapse independent employee operations. Intake
keys are employee/request scoped; uncertain HTTP responses may replay the same
ordered selection without creating duplicate tasks. Changed/new acts are separate
sources/generations. Do not infer document equivalence from case number alone.

The UI accepts up to 100 selected files and sends bounded sequential chunks;
current client chunking and server admission constants are authoritative. Partial
acceptance remains visible. Outbox state is committed with task/audit state before
dispatch. OCR claims/finalization use locks and optional durable leases/generations.
Historical external-acquisition jobs stay withheld; never send them to a browser
as a fallback. Reprocessing preserves history and reviews conflicting outcomes.

## Reader and extraction

Useful PDF text bypasses OCR. Page-count/pixel/deadline bounds apply before reading;
scanned/mixed pages use bounded raster batches. TIFF frames are read one at a time.
Photographs retain multiple local OCR variants and rotation/source transforms.
Do not remove recovery passes based only on mean OCR confidence or speed.

Person/entity parsing requires context and exposes uncertainty. ID checksums do
not establish ownership. Morphological normalization may be uncertain; original
readings and source spans must remain reviewable. Date/case references can belong
to earlier acts. Do not infer an apartment, digits, role or actual address type
from a generic label. Missing extraction does not establish source absence.

Source spans/word boxes are programmatically bound to the provided PDF/page/image
variant and inverse photo transform. Keep field value, source excerpt, normalized
value, warnings and candidate suggestions separate. Page/line evidence is not
precise word evidence; preserve viewer limitations from FIELD_SOURCE_HIGHLIGHTS.

## Review and internal saves

Staff can edit person/organization/entrepreneur cards independently of documents.
Task approval binds the current value snapshot; editing invalidates approval.
Internal card and assignment revisions preserve actor/version history and reject
stale edits. Automatic person-card preparation is conservative and does not approve
the task or create an external record. Entity suggestions remain review-only.

A reviewed card and an assignment are distinct entities. Creating a card does not
silently create an assignment; assignment creation/linking is explicit. Existing
INN matches are possible duplicates, not automatic identity merges. Manual
confirmed applicant/address facts must not be overwritten by extraction.

See `../docs/BULK_PROCESSING.md`, `../docs/DEBTOR_ENTITIES.md`,
`../docs/APPOINTMENTS.md`, `../docs/CASE_PACKAGES.md` and current source limits.
