# Local CPU OCR comparison

The development-only photo harness compares the existing `extract.parse_file`
policy with RapidOCR PP-OCRv5. It does not select the service engine or import the
application, database, Redis, queues, or saved application recognition results.
The existing HTTP/RQ synthetic bulk benchmark is unsuitable for this experiment:
it measures persisted application processing rather than uncached direct OCR.

## Setup and run

Use the sole checkout on `develop`. Never install experimental packages into the
service `.venv`. Python 3.13, Tesseract with `rus` and `osd`, and the existing local
HEIC samples are required. All commands run from the repository root:

```sh
mkdir -p .local-run/ocr-compare
chmod 700 .local-run/ocr-compare
.venv/bin/python -m venv .local-run/ocr-compare/venv
.local-run/ocr-compare/venv/bin/python -m pip install -r scripts/dev/requirements-ocr-compare.txt
.local-run/ocr-compare/venv/bin/python scripts/dev/ocr_compare.py models
SRO_OCR_COMPARE_INTEGRATION=1 .local-run/ocr-compare/venv/bin/python -m unittest tests.test_ocr_compare
.local-run/ocr-compare/venv/bin/python scripts/dev/ocr_compare.py run \
  --repeats 3 --output .local-run/ocr-compare/measurements
.local-run/ocr-compare/venv/bin/python scripts/dev/ocr_compare_check.py .local-run/ocr-compare/measurements
```

Use a new output directory for every experiment; existing results are never
resumed/reused. The harness requires at least three repeats. Current samples are
21 single-frame HEIC photographs. Corpus preflight checks frame counts before
measurements; the harness rejects multi-frame/non-photo input rather than silently
reading only page one. This result does not qualify PDF/TIFF processing.

Model downloads finish before timing. `ocr_compare_models.json` pins the official
URLs and SHA-256 hashes of the PP-OCRv5 server detector, eslav mobile recognizer,
and PP-LCNet x0.25 line-orientation classifier. Each RapidOCR worker verifies those hashes.
The eslav recognizer supports Russian, Belarusian, Ukrainian and English; see
[PaddleOCR language documentation](https://www.paddleocr.ai/latest/en/version3.x/algorithm/PP-OCRv5/PP-OCRv5_multi_languages.html)
and [RapidOCR's versioned model manifest](https://github.com/RapidAI/RapidOCR/blob/v3.10.0/python/rapidocr/default_models.yaml).
The recognizer contains its character dictionary in ONNX metadata. Runtime and
Python dependency versions are pinned separately from service requirements.

## Isolation and accounting

Every engine/document/repeat has a new Python process and private temporary
folder, a byte-identical source copy checked by SHA-256 in parent and worker, and
positive completed Tesseract/ONNX call counts. Temporary input, OCR intermediates,
and temporary logs are removed on success and handled failure. Child-failure logs
are retained only in the ignored private output directory for diagnosis and may
contain document-derived error details. No computer-wide cache is
cleared. OS page/model-file caches can remain warm; “fresh process” does not mean
cold kernel cache. No result cache is consulted even on later identical runs.

Runs are serial, and engine order alternates by document and repeat. OpenMP,
OpenBLAS, MKL, Accelerate and NumExpr limits are one thread. OpenCV uses one thread;
ONNX Runtime uses one intra-op and one inter-op thread and only CPU execution.
The child environment is allowlisted and the resolved Tesseract language path is
fixed. Manifest/worker readback records executable version/hash, `rus`/`osd`
traineddata hashes, Python package versions, original hashes, and current core and
harness source hashes. Python socket audit guards reject ordinary Python network
use; this is not an OS network sandbox. The recognition code uses local engines
and explicit local models, with no external OCR/LLM API.

Raw total wall time includes copy/checks, process startup, decoding, OCR, extraction
and result output. `accounted_seconds` reconciles exclusive worker phases, parent
copy/checks and remaining process/output/monitor overhead. Provenance checks have
a separate phase; an additional total excluding provenance is retained. Do not
add nested times or add medians of phases to reconstruct a median total.

- Preparation includes decoding, resizing, image serialization, segmentation and
  postprocessing outside backend calls.
- `backend_calls` is Tesseract process time or ONNX forward-call time. These are
  different internal boundaries: Tesseract also loads its model and performs
  internal preparation in each CLI call. Its model-load time cannot be isolated
  from that process. RapidOCR's model/session loading is measured separately.
- Extraction times the same person/entity parsers and application source binding.
- Memory is the maximum sampled sum of worker and recursive child RSS at 20 ms,
  including Tesseract children. It is a sampled process-tree maximum, can miss
  brief peaks, and can double-count shared resident pages.

The primary performance comparison is full fresh-process pipeline time. Baseline
retains all four PSM modes, original-image pass, OSD, TSV/source mapping and optional
role-bound INN recovery. The general PDF-only header recovery policy is retained
but is not exercised by these HEIC photographs. Rapid uses one neural pass, server detection at max side
1600, input max side 3400, RapidOCR detector mean/std 0.5, v5 line classification
(with the implementation-selected 3×80×160 input), and eslav recognition at
3×48×320; it has no
full-page OSD, sharpening/multi-pass recovery, or application word-span mapping.
Both use `extract.parse_file` and the same deterministic extractors. These policy
and geometry differences prevent attributing the full time difference to OCR
inference or treating the candidate as a qualified service replacement.

## Quality boundary

Private per-run fields support local source review, but neither filled fields,
confidence, valid ID checksums, agreement between engines nor repeatability are a
truth reference. Reference values must be checked against original photographs,
including whose identifiers/address they are, the apartment, current act/date,
and company attributes. Where a reliable source reference is absent, correctness,
wrong-owner errors and omissions remain **unscored**, not zero.

Never commit samples, identifiers, extracted text/fields, per-document hashes,
model weights, environments or detailed results. Only compact aggregate evidence
and reproducible instructions belong in tracked project documentation. Mac
measurements do not establish VPS performance. Keep the existing service OCR until
replacement quality and source geometry have been independently qualified.
