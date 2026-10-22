# OCR investigation and release preparation — 2026-10-22

The owner explicitly authorized local inspection of the two screenshot samples
and designation images, production OCR verification, and release of the latest
reviewed develop. Real SROArbitr writes remain prohibited. Production started at
`0eaf5e6d3174e91664ace7c0b8c2a123f99d0c46` (sro-v1.1.0); main/develop delta
contains no OCR parser change.

## Findings and limits

All four local images completed the real parse_file path with five OCR variants.
The screenshots are application UI, not document acceptance inputs. Both actual
designation originals yielded document metadata but withheld debtor identity.
The role association gate cannot establish that a name belongs to the debtor.
A filing party is not necessarily the debtor. The same two designation images
were already documented as role-unresolved on 2026-10-16, so a new regression
has not been established. No real OCR text, field values or source hashes are
retained here.

Additional local grayscale/autocontrast/180/210/Otsu passes used rus+eng PSM
4/11/12: 12 full-page +24 enlarged-band passes on one original, 12+12 on the
other. No explicit self-bankruptcy or named insolvency-target grammar was found;
primary-plus-extra results still had four document-metadata fields and no safe
identity. Sol independently reviewed the fully deidentified grammar evidence.
No nearest-name, pronoun repair or guessed identity rule was added. This
investigation does not establish real-office accuracy or universal extraction.

Production read-only ops-status: one uploaded task at needs_review, no failed
or recognizing tasks, all queues/outbox/registries empty, OCR/preview/SRO workers
1/1/0. Tesseract eng/osd/rus available. All seven HTTPS readiness checks passed.
A fresh backup service completed Result=success, ExecMainStatus=0 at
`/var/backups/sro-robot/daily-20261021T232540Z`; isolated restore is still pending.

Safari showed a fraudulent-website warning at the actual HTTPS intake URL.
It was not bypassed. A read-only unauthenticated HTTP inventory found no external
login asset hosts/inline scripts and all three assets exactly matched main;
this does not establish absence of conditional compromise or explain reputation
classification. Browser acceptance remains blocked until that warning is resolved
by the owner/provider. Releasing code alone does not establish reclassification.

## Reviewed engineering increments

- `54705bc648cbe8120f0b661dd58f554ef61dbe04`: explicit allowlisted same-schema
  rehearsal mode, init-db parity, exact row/table/sequence/index comparisons and
  restored old-release schema check. 16 focused tests pass. Actual disposable
  PostgreSQL qualification passed on 26 tables, 5 synthetic rows including a
  task-linked prepared card and revision, 18 sequences and 74 indexes. Assignment
  revisions/reprocessing populations are unit-snapshot coverage only.
- `37fe08e8239587130f803e667710e926cc692885`: fixed aggregate-only OCR diagnostic.
  Checkout role-policy transaction is rolled back, then a REPEATABLE READ READ
  ONLY transaction starts before SessionLocal binding/hooks. No document path,
  OCR value, identifier, filename or arbitrary warning/error is printed. Default
  tests pass, plus a fresh restricted-role PostgreSQL qualification confirming
  SQLSTATE 25006 for a write the role would otherwise permit; private canaries
  do not reach output. No production execution claim yet.

Luna High implemented; Sol 6.1 High independently reviewed both increments with
zero open source findings. These are source/test results, not release acceptance.

## Private reproducible evidence

The local source inputs remain under samples (ignored); do not stage them.
The following private aggregate evidence and extra-pass script are retained:

- `/tmp/sro-ocr-diagnostic-20261022.json`; SHA-256 `111373804fd5c9d3efe02b9415233472c94fb6461aefdf96cdf50957a4903726`.
- `/tmp/sro_local_ocr_extra_20261022.py`; SHA-256 `dbd97b5905a7a641a88d072d354ec71148d122c2e3c1513f64ce4a5f71a37ac6`.
- `/tmp/sro-same-schema-qualification-2026-10-22.json`; SHA-256 `c5dbd5533cfd799b3961d6520feb7e3acaca8f3665af2a3756bc77cf1b739fdb`.
- `/tmp/sro-ocr-diagnostic-qualification-20261022.json`; SHA-256 `366a562140ff93bc26a9913e99ff27358cf992ec94830dd6195010255c0ea366`.

The final exact clean gate, production clone rehearsal, promotion/tag, deployment
and post-deploy source/readiness readback must be recorded after they actually
complete. No source promotion or deployment is implied by this preparation.

## OCR review-candidate follow-up — 2026-10-22

After the owner reported that earlier scans filled person fields, source history
identified the behavioral change: `15138ee` removed document-wide person-field
fallbacks and required explicit debtor-role evidence. This protected against
assigning an applicant's identifiers to a debtor, but left useful name and
nearby labeled-field evidence invisible when a court act named the person in an
application or insolvency-case phrase without a recognized role anchor.

Commit `08c6054228344632b6748c1a413ce36e4e97c82c` adds bounded, separately
persisted review candidates for those two textual forms. Candidates include
OCR source positions and short excerpts. The employee may transfer supported
identity values into the ordinary review form and must use the normal Save or
Approve action. The authoritative OCR debtor fields remain `role_ambiguous`;
automatic internal-card preparation and its role gate are unchanged. Partial
addresses and birth date/place are display-only candidates; birth details never
enter the SRO card. Separate occurrences are never combined into one person's
record, and ambiguous candidate data is capped before storage/rendering.

Both private designation samples were run through local `parse_file` after the
change. The first produced four separately anchored candidate records and the
second three; each had zero role-confirmed person fields. The first completed
in 4.43 s and the second in 6.57 s. These counts establish extraction and review
visibility only; they do not prove that any candidate is the debtor or establish
office-wide accuracy. Raw OCR, field values and document hashes are not retained.

Focused validation passed 111 Python tests, one Node transfer test and
`git diff --check`; Sol independently reviewed the final source diff and its
synthetic adversarial cases. These checks do not include browser UI acceptance,
the full exact-source release gate, production clone/backup restore, deployment
or production readback. Production remains at the previously verified
`0eaf5e6d3174e91664ace7c0b8c2a123f99d0c46`. Existing tasks do not gain candidate
data automatically because raw OCR text is not persisted; use the normal
explicit OCR retry flow if a task must be reprocessed.
