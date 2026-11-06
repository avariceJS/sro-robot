# Raw OCR engines and local reference review

This experiment compares **native engine text**, independently of SRO. The earlier
`scripts/dev/ocr_compare.py run` command and `OCR_COMPARISON.md` remain the historical
**full-pipeline** comparison; their numbers do not rank intrinsic OCR quality.

## Isolated setup

Use Python 3.13 and the existing experiment venv, never service `.venv`:

```sh
.local-run/ocr-compare/venv/bin/python -m pip install --no-binary=tesserocr -r scripts/dev/requirements-ocr-engines.txt
.local-run/ocr-compare/venv/bin/python scripts/dev/ocr_compare.py models
SRO_RAW_OCR_NATIVE=1 .local-run/ocr-compare/venv/bin/python -m unittest tests.test_ocr_engines
.venv/bin/python scripts/dev/ocr_reference_browser_check.py
```

`tesserocr` must be built against the intended native Tesseract **5.5.3**, not a
wheel bundling a different engine. Runtime readback fails closed on other versions.
This Mac already has Homebrew headers/libraries. If pkg-config is unavailable and
the binding's fallback expects `liblept` rather than Homebrew's `libleptonica`, the
experiment-local build alias used here is:

```sh
mkdir -p .local-run/ocr-compare/build-lib
test -e .local-run/ocr-compare/build-lib/liblept.dylib || ln -s /opt/homebrew/opt/leptonica/lib/libleptonica.dylib .local-run/ocr-compare/build-lib/liblept.dylib
CPPFLAGS='-I/opt/homebrew/include' LDFLAGS="-L$PWD/.local-run/ocr-compare/build-lib -L/opt/homebrew/lib" .local-run/ocr-compare/venv/bin/python -m pip install --no-binary=tesserocr -r scripts/dev/requirements-ocr-engines.txt
```

No system library, service dependency, production setting or working engine is
replaced. Model URLs/SHA-256 are reused from `ocr_compare_models.json`.

## Prepare and measure

```sh
.local-run/ocr-compare/venv/bin/python scripts/dev/ocr_compare.py engines prepare --output .local-run/ocr-engines/corpus
.local-run/ocr-compare/venv/bin/python scripts/dev/ocr_compare.py engines run --corpus .local-run/ocr-engines/corpus --output .local-run/ocr-engines/measurements --repeats 3
```

Use new corpus/output directories for a new experiment; existing measurements are
never resumed or used as recognition results. Preparation expects 21 existing
HEIC originals in `samples/img1` and four inputs in `samples/img2`. It decodes every
image frame at native dimensions and resolves HEIF irot/imir versus EXIF display
orientation without the service's 12 MP downscale. It accepts explicitly supported
8-bit modes, composites alpha on white equally, and records the conversion. The
four current PNGs have opaque alpha, so compositing changes no RGB values.

Canonical PNGs are lossless **for those shared RGB pixels**, not a claim that HEIC
compression/color conversion never occurred. Both workers verify the same encoded
SHA-256, RGB dimensions, pixel SHA-256 and page order. No sharpening, threshold,
deskew, OCR-based orientation, voting or recovery is added. PDF inputs are always
rasterized on every page at the same fixed 300 DPI; no PDF text-layer shortcut is
used. Decode/raster preparation is recorded separately from engine processing.

Workers block imports of SRO/application processing and ordinary Python network
use. They never query application results, databases, Redis or queues. Every call
uses a fresh temporary raster copy and a monotonic pass counter, with positive
native/runtime completion evidence; temporary inputs/process workspaces are
removed. Warm processing retains models, not previous recognition results. Kernel
file caches are allowed; a cold process does not imply a cold OS cache.

- **Tesseract:** `tesserocr`/native 5.5.3, `rus`, `OEM.DEFAULT`, one `PSM.AUTO` (3),
  the stock CLI page mode. One explicit `Recognize()` per page. No OSD/PSM sweep,
  second pass, SRO preprocessing or extraction. Text/line getters read the completed
  result. `Clear()` releases page state; `ClearAdaptiveClassifier()` at document
  boundaries prevents learning from earlier benchmark documents while retaining
  initialized language weights.
- **RapidOCR:** 3.10.0, ONNX Runtime 1.31.1, PP-OCRv5 **server** detector, eslav mobile
  recognizer and PP-LCNet x0.25 v5 classifier. One ordinary `RapidOCR(image)` call.
  Stock geometry-output flags remain false. No model-quality setting is tuned.
  Pinned package defaults include global max side 2000, detector min side 736,
  native confidence filters and line classification. Input dimensions, actual
  preprocessing dimensions, detector/classifier/recognizer tensor shapes and full
  effective configuration are recorded. These internal resizes are engine defaults,
  not a reduced-resolution raster supplied to only one engine.

OpenMP/OpenBLAS/MKL/Accelerate/NumExpr/OpenCV and ONNX intra/inter-op limits are one;
all three ONNX sessions must read back `CPUExecutionProvider` only. Runs are serial
with alternating engine order by document/repeat. Cold mode creates a new process
and model per document/repeat. Resident mode keeps one process per engine and
records one separate priming pass over the first document before three measured
passes over every document. Priming never supplies a scored or cached result.

Compare **document request/full wall times**, split by image group. Cold full time
also includes process startup, model loading and shutdown; resident full time does
not repeatedly charge model loading. Engine imports, model construction, shared
preparation, raster read/verification, format adapters, engine calls, output and
IPC have distinct recorded boundaries. Tesseract `Recognize()` and Rapid's whole
API call have different internal scopes; their phase times are not pure comparable
inference. RSS is sampled every 20 ms for the process tree: report lifetime/startup
and request peaks separately. Post-document RSS still includes the response until
it is sent; it is labelled accordingly, not called model-only idle memory.

This is an interactive Mac experiment with local review/development occurring
concurrently. Background CPU/cache/I/O activity can affect timings; it does not
qualify an isolated VPS or multi-worker deployment. Do not install a mobile
detector to improve the server detector's memory figure.

## Local full-text reference

```sh
.local-run/ocr-compare/venv/bin/python scripts/dev/ocr_reference_drafts.py --corpus .local-run/ocr-engines/corpus --runs .local-run/ocr-engines/measurements
.local-run/ocr-compare/venv/bin/python scripts/dev/ocr_engine_reference.py --corpus .local-run/ocr-engines/corpus --runs .local-run/ocr-engines/measurements --port 0
open .local-run/ocr-engines/corpus/open-review.html
```

The private launcher contains the session token; never copy its contents/URL into
logs or Git. The server uses loopback, token/Host/Origin checks, revision fencing,
private atomic fsync/replace saves, and no request logs. Stale tabs cannot overwrite
newer human work. Keep it running while reviewing; `reference.json` is the local
saved artifact. Browser unsaved text is not an accepted reference.

The initial set is all four PNGs plus four HEIC candidates selected by source
orientation metadata. This is a starting suggestion, **not a proven hardest-four
ranking**; the reviewer can select any other photographs. The form shows the
full-size original on the left with zoom and a full editable transcript on the
right. The first cold Tesseract pass may seed a draft; seed provenance is retained.
No best-output selection or engine voting occurs. Surface-form FIO/ID/address
hints are explicitly uncertain local review aids, never scored reference spans,
validated records or business-parser output. They run outside the OCR workers.

The reviewer must compare the entire transcript against the original, correct all
characters, and explicitly attest each selected page. Editing removes verification.
Do not infer unrecognizable characters: mark unreadable originals; they are excluded
from full-page CER/WER with an explicit denominator. OCR-seeded, manually corrected
references are **non-blind**; record that limitation rather than calling them blind
independent transcription. Unreviewed OCR output is never a truth reference.

```sh
.local-run/ocr-compare/venv/bin/python scripts/dev/ocr_compare.py engines score --runs .local-run/ocr-engines/measurements --reference .local-run/ocr-engines/corpus/reference.json
```

Scoring refuses incomplete/unverified selected pages and source-hash drift. The
fixed policy is Unicode NFC plus collapsed whitespace; preserve case, digits and
punctuation. CER/WER use Levenshtein errors divided by **reference** characters/
words, can exceed one, and aggregate counts rather than averaging page percentages.
Score only predeclared cold repeat 0, never the best of repeated outputs. Report
selected/scored pages and seeded-reference coverage for each corpus group.

Text-alignment line deletions are not a causal detector-error count: wrapping and
reading order can differ. A detection/reading split requires independently drawn
source-line polygons; without those it remains unscored. Optional manually selected
character spans are partial text-alignment diagnostics, include insertion errors,
and do not establish spatial/participant attribution. Do not call character recall
accuracy or trust automatic hints as a reference. Full-page CER/WER do not require
FIO/ID extraction. No accuracy winner is declared before original verification.

Never commit original/canonical documents, transcriptions, per-input hashes,
reference drafts, detailed results, environments or weights. Production and
`main` are outside this experiment.
