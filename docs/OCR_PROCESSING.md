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
sources. This blocks automatic internal person-card preparation and keeps the
restricted candidate form-preview path; it is a material automation tradeoff,
not merely a visual warning. A checksum or a single OCR reading is insufficient
to remove that hold. Existing parser ambiguity warnings are never cleared.

Fully searchable PDFs keep their existing policy. A mixed PDF is exempt from the
additional OCR hold only if **every populated Field**, including case/date/court,
is bound to a usable embedded-text page left unchanged by the reader. An unrelated
scanned appendix alone must not disable a text-layer debtor card. Any populated
OCR-sourced/unknown-origin field retains the entire conservative gate, protecting
both auto-draft and explicit form-preview admission. Missing `address_kind` page
provenance currently keeps some typed-address mixed PDFs in review; it is not
inferred from a nearby address. Values, employee suggestions, source spans and
coordinate mapping remain available. Review suggestions never approve a card.

## Lossless scratch transport

The CLI still owns recognition and hard subprocess cancellation. Prepared `L`/`RGB`
images with no explicit encoder options or color-key transparency use lossless BMP
scratch input. BMP is explicitly written with zero DPI: its Pillow default of 96
would change Tesseract's interpretation. Alpha/palette/1-bit images, color-key
transparency (including value 0), and explicit encoder options use the existing path;
eligible PNG fallbacks use compression level zero. Non-PNG formats keep their
encoder. The BMP format override and caller encoder options are restored;
pytesseract still owns temporary cleanup.
No pixels, preparation, OCR passes, models, parser policy or stored originals change.
See [Pillow format options](https://pillow.readthedocs.io/en/stable/handbook/image-file-formats.html).

BMP `L` has a grayscale palette, unlike PNG `L`; do not assume raw Leptonica Pix
identity. The actual OCR equivalence test below covers text, TSV and coordinates
on installed system libraries, including RGB/ICC and transparency. BMP decoding
also buffers input bytes: memory is a tradeoff, not a claimed improvement.

## Measured evidence and limits

CPU measurements used an interactive M4 Pro Mac (24 GiB, 12 logical CPUs), existing
4 PNG + 21 HEIC originals, fresh processes/private copies verified by SHA-256,
alternating versions and one-thread limits. Direct `parse_file` calls bypass DB,
Redis, queues and saved results. Every measured call completed real Tesseract OCR.
Times include decode/preparation/OCR/field parsing; imports and dependency downloads
are outside the interval. Nested OCR time is not added to total a second time.

| Matched comparison, 25 documents | Before | After | Full payload matches |
| --- | ---: | ---: | ---: |
| PNG compression 6 -> 0, against `7dd89f6` | 142.96 s | 133.39 s | 25/25 |
| PNG0 -> eligible BMP, against `1e1e2a4` | 133.21 s | 130.03 s | 25/25 |
| Final implementation vs `7dd89f6` | 138.27 s | 125.61 s | 25/25 |

The final matched improvement is 9.2%. All 47 completed OCR calls per variant are
retained; 43 eligible final inputs use BMP, four retain PNG. Equality includes raw
text, complete TSV, all fields/warnings, employee suggestions, ownership evidence,
source spans/boxes and recovery decisions. No changed values or new omissions were
observed relative to `7dd89f6`. This is **not independent proof of correctness**.
Three-repeat six-document stage checks also matched all 18 pairs each: mean batch
12.76 -> 12.42 s for PNG0; then 12.42 -> 12.24 s for BMP. Host load varies, which is
why each accepted step has its own paired baseline. These are not VPS measurements.

Memory admission checked system available percentage, swap growth and thermal/power
warnings; OCR stayed sequential. Availability was 41-50%, pre-existing swap declined,
and native thermal state was nominal. A separate 50-ms PID-tree RSS probe on the
largest baseline upper-bound case measured PNG0 511.0 -> BMP 529.1 MiB (+18.1 MiB).
This sampled combined RSS is not an exact instantaneous peak or PSS. Page/pixel,
worker-count and concurrency safeguards are unchanged. Deployment-library behavior
needs separate release verification; Linux CI uses synthetic inputs, never samples.

The earlier five-pass six-document observation was 44.57 -> 12.46 s. Its three
changed values (two courts, one uncertain organization name), five additions and
removed alternatives still lack independent original-image adjudication. A targeted
four-document audit replay reproduced them in 46.65 s; private audit evidence stays
local for owner review. Faster transport leaves those results and raster auto-card
holds unchanged. Never label a filled field or high confidence as correctness.

The final OCR subprocess phase still consumed 116.12 of 125.61 s. Further work must
address measured native recognition cost without discarding useful recovery data.
A P5/PGM transport probe was rejected: native decoding was slower despite cheap
encoding. Previous resident `tesserocr`/PIL-SetImage results were not reliably faster
or equivalent; the current RQ worker forks jobs, and CLI cancellation stays intact.
Rejected rules for unclosed organization names must not be reinstated without new
evidence: they produced role conflicts and lost fields in the earlier comparison.

## Focused verification

Checks use synthetic data and mocked OCR, except `test_ocr_transport`, which runs
four tiny real text/TSV comparisons when system Tesseract/rus and a Cyrillic font
are available. CI is configured to run it with its installed Tesseract/Leptonica.
They never OCR the private corpus or access production services:

```sh
APP_DEBUG=1 \
APP_SECRET_KEY=synthetic-verification-session-key-0000000000000000 \
DATABASE_URL=postgresql+psycopg://synthetic:synthetic@127.0.0.1:1/sro \
MIGRATION_DATABASE_URL=postgresql+psycopg://synthetic:synthetic@127.0.0.1:1/sro \
REDIS_URL=redis://synthetic:synthetic@127.0.0.1:1/0 \
.venv/bin/python -m unittest \
  tests.test_ocr tests.test_ocr_recovery tests.test_ocr_transport tests.test_heic_ingestion \
  tests.test_source_locations tests.test_entity_extraction \
  tests.test_mixed_classification tests.test_person_detection \
  tests.test_person_attributes tests.test_person_detection_review \
  tests.test_applicant_evidence tests.test_review_field_evidence \
  tests.test_bulk_preparation tests.test_mixed_lifecycle
```
