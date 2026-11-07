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

## Optional host-qualified native CLI (macOS ARM64)

The system 5.5.3 NEON integer matrix kernel uses multiply/widen/reduce operations.
Our small [downstream patch](../scripts/native/tesseract-5.5.3-dotprod.patch) uses
four ARM signed dot-product accumulators with the same packed weights, modular
int32 sums, bias, float conversion and output ordering. It does not change models,
image preparation, segmentation, recovery or extraction. The old branch remains
for compilation without AArch64/DOTPROD. See the [pinned upstream implementation](https://github.com/tesseract-ocr/tesseract/blob/5.5.3/src/arch/intsimdmatrixneon.cpp)
and [Arm's feature specification](https://arm-software.github.io/acle/main/acle.html#dot-product-extension).

This is an **optional host-local executable**, not a portable binary or resident
API. Selection is restricted to native Darwin arm64 with `FEAT_DotProd=1`, exact
qualified binary/model/Leptonica/default-CLI/library/patch hashes and a successful bounded
load/version probe. Only `rus`, `osd` and used text/TSV configurations are hashed;
unrelated installed languages are not read. Genuine custom CLI commands are
preserved. Decoder-link targets are re-resolved; custom DYLD loading is withheld.
A changed/missing qualification falls back before OCR. Started OCR is
never replayed on another executable. RQ selects at its single-job boundary,
after stored-document integrity validation and inside the document deadline.
An owned selection is revalidated on subsequent jobs; no selection changes during
an active parse. Linux and hosts without qualification retain system Tesseract.

```sh
.venv/bin/python scripts/dev/tesseract_native.py build
.venv/bin/python scripts/dev/tesseract_native.py qualify
```

Build requires existing Homebrew Tesseract 5.5.3, Leptonica 1.87.0, model directory
`/opt/homebrew/share/tessdata`, Apple command-line tools and Python 3.13. An explicit
`--tessdata` must match the effective system model directory. Official source and
isolated CMake 3.31.10 wheel are pinned by SHA-256; no service packages, shared
libraries or models are installed/replaced. Compilation pins Clang, removes
inherited build flags, uses two jobs and stops for serious heat, low available
memory, swap growth or a three-minute compilation work bound (health probes and
reaping have bounded extra allowances). Temporary sources/tools
are deleted on exit. A synthetic check compiles the actual old/new kernel against
an independent modular scalar reference, including overflow, output tails and
unaligned buffers. No document content is sent anywhere.

Qualification uses the existing 4 PNG + 21 HEIC with one-thread CPU limits, fresh
processes/private byte-checked copies, actual completed OCR counters and exact
full text/TSV/card/suggestion/source/recovery comparison. It requires a measured
speed benefit, publishes an immutable hash-named executable and atomically
replaces the selector **last**, after final load validation. Failed upgrades keep
any previous selector. A build/qualification lock prevents concurrent operations.
Owned child groups are cleaned on failure, timeout and deferred SIGINT/SIGTERM.
The supervisor reserves the exited leader with `waitid(WNOWAIT)` until group cleanup
and reaping; it never signals after losing custody. Darwin's zombie-only group
permission result is ignored only after verifying no live group member remains.
Any difference stays private for manual review and never qualifies the candidate.
Local runtime/license/manifest live under ignored `.local-run/tesseract-native`;
no model/result cache is used. `SRO_TESSERACT_NATIVE=0` disables selection. Existing
workers must normally reload verified source to use the hook; no worker, database,
production or deployment is started/restarted by either command.

The native line-image transport also keeps the existing PNG round trip but uses
compression zero on a separate deep copy only when the raster is at most 8 MiB
and has no colormap or text metadata. Larger/metadata-bearing images retain the
original encoder. This avoids shared-header mutation and Leptonica's partial
metadata-copy failure case. Copy allocation failure or PNG0 failure retains the
original PNG encoder, followed by the existing PNM fallback. Pixels, resolution,
alpha and coordinates are not scaled/changed. The upstream experimental Pix flag
changes serialization/aliasing contracts and remains disabled.

The builder links a synthetic check against the **actual patched ImageData class**:
94 comparisons cover depths, palette/alpha, DPI/text, shared callers, eligible
branch liveness, the large-buffer fallback, metadata-copy failure avoidance and
forced pixel-copy failure. The 8 MiB bound limits the extra raster copy, not total
process memory; encoded buffers and RSS are qualified separately.

## Measured evidence and limits

CPU measurements used an interactive M4 Pro Mac (24 GiB, 12 logical CPUs), existing
4 PNG + 21 HEIC originals, fresh processes/private copies verified by SHA-256,
alternating versions and one-thread limits. Direct `parse_file` calls bypass DB,
Redis, queues and saved results. Every measured call completed real Tesseract OCR.
Times include decode/preparation/OCR/field parsing; core imports and dependency downloads
are outside the interval. Nested OCR time is not added to total a second time.

| Matched comparison, 25 documents | Before | After | Full payload matches |
| --- | ---: | ---: | ---: |
| PNG compression 6 -> 0, against `7dd89f6` | 142.96 s | 133.39 s | 25/25 |
| PNG0 -> eligible BMP, against `1e1e2a4` | 133.21 s | 130.03 s | 25/25 |
| Both scratch changes vs `7dd89f6` | 138.27 s | 125.61 s | 25/25 |
| System CLI -> same-source Release rebuild | 123.93 s | 109.36 s | 25/25 |
| Same rebuild -> SDOT kernel (same three-way run) | 109.36 s | 92.22 s | 25/25 |
| Final guarded native bundle vs transport-optimized system CLI | 127.07 s | 77.52 s | 25/25 |
| All four changes, including selector, vs `7dd89f6` | 139.43 s | 77.62 s | 25/25 |

Transport alone improved its matched run by 9.2%. The combined final path improved
its own paired baseline by **44.3%**. A plain Release rebuild accounts for part of
the native gain; do not attribute all of it to SDOT. All 47 completed OCR calls
per variant are
retained; 43 eligible final inputs use BMP, four retain PNG. Equality includes raw
text, complete TSV, all fields/warnings, employee suggestions, ownership evidence,
source spans/boxes and recovery decisions. No changed values or new omissions were
observed relative to `7dd89f6`. This is **not independent proof of correctness**.
The integrated final path also
matched all 18 six-document pairs in three alternating repeats: batch means
12.77 -> 8.68 s (selection overhead included). In that same three-way run the
previous native bundle averaged 8.67 s: internal PNG savings concentrate on the
other heavy photos, not this original six-file subset. A separate standalone native
comparison against the transport-optimized system CLI was 11.81 -> 8.40 s; it
excluded host-selection overhead and used a different load interval.
Three-repeat six-document stage checks also matched all 18 pairs each: mean batch
12.76 -> 12.42 s for PNG0; then 12.42 -> 12.24 s for BMP. Host load varies, which is
why each accepted step has its own paired baseline. These are not VPS measurements.

Memory admission checked system available percentage, swap growth and thermal/power
warnings; OCR stayed sequential. Availability was 40-50%, pre-existing swap declined,
and native thermal state was nominal. A separate 50-ms PID-tree RSS probe on the
largest baseline upper-bound case measured PNG0 511.0 -> BMP 529.1 MiB (+18.1 MiB).
The pre-line-codec native bundle
measured 540.8 -> 532.4 MiB against `7dd89f6` on that control.
Final line-codec controls measured sampled RSS 476.2 -> 527.5 MiB and
535.1 -> 535.2 MiB; short preparation peaks can fall between samples. Separate
parent/maximum-child peak sums were 535.0 -> 543.5 MiB on the first control. In the
final paired 25-file run the largest such upper bounds were 548.8 -> 534.8 MiB.
These sums need not occur simultaneously. Sampled RSS is not an exact peak/PSS. Page/pixel,
worker-count and concurrency safeguards are unchanged. Deployment-library behavior
needs separate release verification; Linux CI uses synthetic inputs, never samples.

The earlier five-pass six-document observation was 44.57 -> 12.46 s. Its three
changed values (two courts, one uncertain organization name), five additions and
removed alternatives still lack independent original-image adjudication. A targeted
four-document audit replay reproduced them in 46.65 s; private audit evidence stays
local for owner review. Faster transport leaves those results and raster auto-card
holds unchanged. Never label a filled field or high confidence as correctness.

The final native CLI phase consumed 67.46 of 77.62 s, including process/model
startup and recognition. This is not a pure kernel-only timer. Preparation/read
took about 6.59 s; parser work remained under 1%. Further work must address
measured native recognition cost without discarding useful recovery data.
A P5/PGM transport probe was rejected: native decoding was slower despite cheap
encoding. Previous resident `tesserocr`/PIL-SetImage results were not reliably faster
or equivalent; the current RQ worker forks jobs, and CLI cancellation stays intact.
Rejected rules for unclosed organization names must not be reinstated without new
evidence: they produced role conflicts and lost fields in the earlier comparison.

Further compile probes were rejected on four heavy documents, two alternating
repeats, with all eight payload pairs identical: forced ThinLTO 42.68 -> 42.09 s
(1.4%, insufficient to justify overriding upstream's ARM restriction), M4 scheduling
42.74 -> 43.46 s (1.7% slower). Their binaries, sources and tools were removed.

## Bounded batch observation

The existing worker entrypoint supports 1-4 processes and refuses parallel OCR
without durable OCR leases. No worker count, lease setting, queue or real database
was changed. A separate **different six-document heavy batch** on the same Mac
used fresh Python/Tesseract processes with one-thread limits and warm OS caches:

| Processes | Batch median (range), three repeats | Max sampled tree RSS |
| --- | ---: | ---: |
| 1 | 24.94 s (24.38-25.16) | 538 MiB |
| 2 | 15.11 s (14.83-15.12) | 1016 MiB |
| 4 | 8.65 s (8.61-8.67) | 1741 MiB |

All 54 document results matched verified single-process full-payload hashes, with
108 completed real OCR calls. Order was 1/2/4, 4/2/1, 1/2/4. Wall time includes
launches, parsing/preparation, result writing and observer overhead; byte-checked
input copying is outside it. RSS was sampled every 100 ms, includes shared-page
double counting and excludes the observer/RQ parent. Available memory stayed at
least 42%, thermal state nominal, swap did not grow. This is makespan for this
short uneven batch, not a sustained-queue optimum, actual RQ RAM or VPS evidence.
Operational parallelism still needs the existing lease/resource admission; this
measurement does not enable it automatically.

## Focused verification

Checks use synthetic data and mocked OCR, except `test_ocr_transport`, which runs
four tiny real text/TSV comparisons when system Tesseract/rus and a Cyrillic font
are available. CI is configured to run it with its installed Tesseract/Leptonica.
Generated text checks liveness, not perfect spelling across fonts/model versions;
exact PNG/BMP output equality remains the contract. These checks never OCR the
private corpus or access production services. The optional native integration check
also verifies real synthetic text/TSV and rotated OSD against system Tesseract when
a qualified local runtime is present; otherwise it skips:

```sh
APP_DEBUG=1 \
APP_SECRET_KEY=synthetic-verification-session-key-0000000000000000 \
DATABASE_URL=postgresql+psycopg://synthetic:synthetic@127.0.0.1:1/sro \
MIGRATION_DATABASE_URL=postgresql+psycopg://synthetic:synthetic@127.0.0.1:1/sro \
REDIS_URL=redis://synthetic:synthetic@127.0.0.1:1/0 \
.venv/bin/python -m unittest \
  tests.test_ocr tests.test_ocr_recovery tests.test_ocr_transport tests.test_heic_ingestion \
  tests.test_native_tesseract tests.test_tesseract_native_tool \
  tests.test_worker_recognition tests.test_worker_duplicate_guard tests.test_worker_lease_guard \
  tests.test_worker_error_safety \
  tests.test_source_locations tests.test_entity_extraction \
  tests.test_mixed_classification tests.test_person_detection \
  tests.test_person_attributes tests.test_person_detection_review \
  tests.test_applicant_evidence tests.test_review_field_evidence \
  tests.test_bulk_preparation tests.test_mixed_lifecycle
```
