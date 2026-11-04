# Field source highlights

## Implemented scope

New JPEG/PNG recognition captures Tesseract word rectangles alongside text in
**the same five OCR invocations**. No extra OCR pass, network service, dependency
or database migration is introduced. Existing text, voting, role attribution and
review/approval behavior remain unchanged.

The parser's exact normalized-text variant/start/end span selects overlapping
word boxes. Each field persists `source_boxes` with one-based page and fractional
`x`, `y`, `width`, `height` coordinates relative to the oriented source image.
Review-only candidate fields carry their own geometry. This is an OCR location,
not proof of a person's role or confidence in a value.

On the review page, focus a mapped field or use **Показать на документе**. The
matching words are highlighted in the left preview and the first word is scrolled
into view. The overlay scales and rotates with the image. Manually changed values
clear their previous overlay and are not represented as an original OCR match.
Saved operator corrections already discard source metadata when the value differs.
Focusing an unmapped field or losing the preview also clears stale highlights.

Existing tasks without geometry remain usable and receive no invented locations.
An explicitly requested new recognition can collect geometry for them. PDF and
TIFF viewers do not show source-highlight controls in this version; their original
preview behavior remains. Internal debtor-card previews retain their existing
markup. OCR orientation uses the existing EXIF transpose; separate browser EXIF
qualification remains open.

## Verification — 2026-11-04

- Two owner-supplied `samples/designation` inputs: coordinate capture returned
  five byte-identical OCR variants compared with ordinary recognition. Local
  timings: 4.40 vs 4.45 seconds (JPEG), 6.69 vs 6.75 seconds (PNG). These paired
  measurements do not establish production latency or a broad overhead ceiling.
- Both documents have mapped case/date fields and mapped review candidates,
  including name, identifiers and address wherever those candidate spans exist.
  No personal values or document text are retained in this report.
- Focused Python checks include source-span mapping, same-result parse binding,
  storage/review round trip, operator changes, candidate geometry, one-process
  behavior when TSV is missing, HTTP image controls and omission for PDF, worker
  recognition and unchanged extraction tests.
- Node checks cover activation/manual edits/stale highlights, tall-image sizing,
  resize/page-size changes, image errors and existing zoom/TIFF controls.
- Brave visual check on a **synthetic** local document: case number highlight,
  zoom, rotation and manual-edit notice verified. This is rendered-template and
  browser interaction evidence, not a database-backed upload/approval E2E.
- Independent Sol source review identified stage/image size mismatch and failed
  image stale overlays; both were corrected and rechecked. No remaining concrete
  blocker was reported. No production deployment was performed.

Reproduce focused checks using the synthetic environment in scripts/dev/verify.sh:

```
python -m unittest tests.test_source_locations tests.test_ocr \
  tests.test_review_field_evidence tests.test_http_application_integration \
  tests.test_core tests.test_entity_extraction tests.test_worker_recognition \
  tests.test_worker_duplicate_guard tests.test_worker_error_safety
node --test tests/test_staff_controls.mjs tests/test_field_source.mjs
node scripts/dev/build-css.mjs
```
