# Local OCR processing

Tesseract (`rus`, primary PSM 3) remains the supported OCR. No additional neural
runtime, experimental model download or experiment environment is required.
Install the service's existing requirements and system Tesseract/Poppler.

## Primary reading and recovery

Useful PDF text keeps the existing fast path. Scanned PDF pages retain bounded
300-DPI raster batches and binary preparation. TIFF frames are decoded sequentially
and preserve empty page positions. Photos retain the existing orientation-aware
loader, preprocessing and bounded HEIC resize. Source TSV comes from the same
Tesseract invocation as text; no second process is needed for word coordinates.

There is one primary PSM 3 reading per nonblank image/scanned page. At most one
ordinary alternate is requested for an observable defect:

| Evidence in the primary reading | Alternate |
| --- | --- |
| Empty or fewer than 32 text characters | PSM 11; photos first check orientation, then PSM 3 if confidently rotated |
| Labelled INN/SNILS could not be extracted | PSM 6; original image detail for INN |
| Apartment/office token without a number | PSM 6 on original image detail |
| Residence/registration label and known identity, but no address | PSM 6 |
| Case heading without an extracted case number | PSM 4 |
| Act heading without an extracted date | PSM 4 |
| Person/debtor label without an extracted name | PSM 11 |

Absence of a field without source evidence does not request another pass. Recovery
uses the same parsers, ownership rules and checksums; no missing digits/apartment
or participant role is invented. Existing specifically gated damaged-INN grayscale/
crop recovery and low-resolution PDF date cross-check remain. These can exceed two
calls for the affected page; they are not mandatory segmentation sweeps. All calls
share the document deadline and existing pixel/page/frame limits.

Timeouts in an optional alternate retain a usable primary result; the overall
deadline still fails closed. Failed/corrupt later TIFF frames never produce a partial document.
Primary and recovery variants keep independent page slots, including blanks; text
from the primary is never copied to manufacture corroborating votes.

Fewer readings must not create false unanimous authority. Critical fields from an
OCR-bearing document retain manual-review warnings and partial/candidate role
sources, including organizations and mixed PDFs. Fully searchable PDFs keep their
existing policy. Values, employee suggestions, source spans and coordinate mapping
remain available. Review-only suggestions are not automatic card approval.

## Evidence and limits

A bounded local CPU check on an interactive M4 Pro Mac used four existing PNG scans
and two HEIC photos (person and organization), same-byte SHA-256-checked temporary
copies, fresh processes and direct `parse_file` calls, without DB/Redis/results
reuse. Tesseract 5.5.3, Russian model, OpenMP/BLAS thread limits of one. The baseline
was `97d2cec5ca82719673251a3fa163c015ac298c7d`. Timings include decode, preparation,
Tesseract startup, text/TSV and field parsing; exclude dependency installation and
interpreter imports. Nested OCR time is not added again to total time.

| Retained path | Six-document total | Per-document median | Completed OCR/OSD calls |
| --- | ---: | ---: | ---: |
| Previous mandatory passes | 44.57 s | 4.90 s | 32 |
| Primary + selected recovery | 12.46 s | 1.93 s | 10 |

PNG total: 18.98 -> 7.40 s; HEIC total: 25.59 -> 5.05 s. Of 35 populated baseline
field values, 32 are unchanged, none are missing, and three differ (two court names
and one already uncertain organization name). Five values are newly populated,
including four person-name fields and a legal form. These are observations against
the previous output, **not independent correctness labels**. No fully checked
reference exists for this subset; no absolute error/accuracy claim is warranted.
The complete apartment-bearing address suggestion survived; an additional shorter
address alternative and several organization-name alternatives disappeared with
the removed readings. Candidate generation itself remains in use.

A second comparison tried recovery for an unclosed organization name: it created
participant conflicts and lost fields, so that rule was rejected and removed.
Diagnostic execution totaled 84.58 seconds (180-second ceiling); no further OCR
runs were performed. This is a small first-stage check, not a VPS measurement or
a broad quality qualification. Detailed personal results and temporary code were
deleted after retaining these aggregate observations.

Native `tesserocr` was not adopted. A two-photo probe took 1.92/7.89 s versus the
retained CLI's 2.09/2.96 s and produced different readings/recovery decisions.
The current RQ worker forks each job, so a loaded model cannot simply persist
across documents. This does not establish a native speed benefit; keep the CLI's
hard subprocess timeout rather than change worker architecture for an unproven
saving. The probe environment and its experiment-only dependency were removed.

OCR subprocess time was 10.76 of 12.46 s on the retained path; it remains the main
latency cost. Further work should isolate expensive damaged text regions and check
uncertain organization/court readings against originals before changing recovery.

## Focused verification

All checks below use synthetic data and mocked OCR; they do not consume a real
corpus OCR budget or need production services:

```sh
APP_DEBUG=1 \
APP_SECRET_KEY=synthetic-verification-session-key-0000000000000000 \
DATABASE_URL=postgresql+psycopg://synthetic:synthetic@127.0.0.1:1/sro \
MIGRATION_DATABASE_URL=postgresql+psycopg://synthetic:synthetic@127.0.0.1:1/sro \
REDIS_URL=redis://synthetic:synthetic@127.0.0.1:1/0 \
.venv/bin/python -m unittest \
  tests.test_ocr tests.test_ocr_recovery tests.test_heic_ingestion \
  tests.test_source_locations tests.test_entity_extraction \
  tests.test_mixed_classification tests.test_person_detection \
  tests.test_person_attributes tests.test_person_detection_review \
  tests.test_applicant_evidence tests.test_review_field_evidence
```
