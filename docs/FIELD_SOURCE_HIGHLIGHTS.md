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

On the review page, mapped rectangles and connector arrows appear automatically.
There are no numbered source badges on the document or beside the fields. Arrows
originate at the visible edge of the last source rectangle in OCR reading order
(for example, the last address or SNILS fragment) and end just before the
field label (for example **ИНН** or **Адрес**), leaving the word unobscured.
Hovering an arrow, field or label emphasizes the corresponding source. Click an
arrow to pin that emphasis after moving the pointer away; click it again to
unpin. Selecting a different arrow moves the pin. Enter and Space offer the same
toggle from keyboard focus. Pinning is local to the current page, survives image
zoom, rotation and scroll rerenders, and never saves a task or document value.

The broad invisible arrow hit area has no painted focus rectangle, shadow or tap
highlight; keyboard focus is communicated by the visible arrow/source emphasis.
Borders are one pixel with a near-transparent fill so document text remains
readable. Each outline has a one-CSS-pixel outer inset from OCR glyph bounds,
maintained through zoom and rotation. Same-line day/month/year words share one
rectangle; multiline date spans keep separate boxes.

Surname, given name and patronymic use separate word spans from the parser's
existing name grammar, including parenthesized previous surnames. Exact single
word spans are preserved; ambiguous multiword name spans receive no misleading
component box. Original field values, role attribution and approval are unchanged.

Zoom, rotation, document scrolling and image scrolling recalculate the arrows;
fully clipped sources have no connector and partially clipped sources use their
visible edge. Manually edited values lose their pointer and clear its pin; returning to
the recognized value restores it. Failed images remove overlays and recovered
images restore them. Internal debtor-card previews keep their existing markup.

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

## Owner refinement and real-document preview — 2026-11-05

The owner accepted the automatic-pointer direction and requested number-free
source images, readable thin rectangles and per-word name emphasis. These
requirements supersede the initial click-to-show interaction.

Both actual `samples/designation` documents were locally OCRed and rendered with
all available source geometry in a private loopback-only preview at
`http://127.0.0.1:64671/document-1.html` and `document-2.html`.
Submission controls are disabled; no production task, database or SROArbitr record
was modified. Owner documents and private rendered artifacts remain outside Git.
Brave visual checks confirmed automatic pointers on both files and a separate
surname rectangle on the first, with no overlaid image numbers. The owner is
reviewing a rendered-template preview, not an installed production feature.

Focused refinement checks: 68 Python tests (source mapping, review evidence and
HTTP rendering), 9 Node tests (pointer behavior and existing staff controls), CSS
build and `git diff --check` passed. Independent Sol review findings on stale
fields, clipping, scroll alignment and component attribution were corrected.
No production release or post-deployment verification was performed.

## Label targeting and pin acceptance — 2026-11-05

Owner clarified that arrows must end at the label words, with no numeric badges.
A click on an arrow now toggles persistent-in-page emphasis. The invisible click
area suppresses browser focus/tap painting that could show an extra rectangle.
Brave acceptance on the real first sample confirmed: label endpoint, no extra
focus rectangle, pressed state0→1 after click, still1 after zoom and pointer exit,
and1→0 after another click. Normal image scale was restored. Both existing real
preview pages were rerendered with the empty metadata spans; no OCR rerun or
backend write was needed.

Checks:44 HTTP tests,10 Node tests (staff controls plus source-pointer interaction),
CSS build and diff check passed. Implementation and tests were reviewed separately
by the parent agent. No deployment or external record mutation was performed.

## Wrapped apartment retention — 2026-11-05

The first designation document has a room label at the right end of one line and
the room number at the start of the next, closing a parenthesized biography before
an insolvency phrase. One of the five existing OCR passes retained the number;
the scoped address boundary stopped at the house. A shorter candidate also had
higher consensus confidence, so fixing extraction alone would not fix the form.

Review-only application candidates now retain this specific wrapped room when
there is one unmatched biography parenthesis, an immediate newline after the room
label, a numeric room closing the parenthesis, and the following insolvency phrase.
The role-bound address parser and automatic card preparation remain unchanged.
Manual-review selection prefers a unique apartment extension compatible with
all other exact address prefixes in the same person-candidate group. Conflicting
apartment numbers retain the previous uncertain selection; operator edits remain
untouched. The selected longer candidate keeps its own source span and geometry.
No OCR pass, dependency, network request or database migration was added.

Acceptance: the actual first sample's editable address includes the apartment,
uses source variant2 and13 word boxes, and remains uncertain for employee review.
The private preview was rerendered and Brave accessibility readback confirmed the
apartment is present. Source arrows for multiword fields now start at the final
rectangle, preventing the line from traversing subsequent words of the same field.
168 focused Python tests and11 Node tests passed; independent Sol review found no
remaining concrete defect. Production was not changed.
