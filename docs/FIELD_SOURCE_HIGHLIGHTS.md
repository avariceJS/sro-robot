# Field source highlights

## Current behavior

Review fields can point to OCR word locations when the selected value has an
exact source span and the stored recognition result includes geometry. The
coordinates use one-based pages and fractional positions in the oriented source
page. The pointer locates text; it does not establish identity, debtor role,
correctness, or confidence. Review-only suggestions keep their own source spans.

The review page supports source marks for PDF, JPEG, PNG, and HEIC previews.
PDF locations are available from recognized PDF text boxes; raster locations
are available when the image OCR pass records word boxes. No location is guessed
for older tasks, unmatched text, unsupported formats, or OCR paths that did not
record geometry. In particular, TIFF has no field-source controls, and scanned
PDF OCR does not currently provide the same location coverage as text PDFs.
The original document remains the authority for review.

Marks are open underlines that follow OCR word baselines, grouped by printed
line. Each field's connector runs from the final source underline to its label.
The address-type selector has no separate pointer. Hovering a mark, field, or
label emphasizes the corresponding source. Clicking a mark or label pins that
emphasis for the current page; Enter and Space provide the keyboard equivalent.
The **Стрелки** switch hides or restores marks for the current page. Pinning is
view-only and never saves a task or field.

Zoom, rotation, scrolling, and resizing recalculate positions. Clipped words do
not produce off-screen connectors. Editing a value clears its pointer; restoring
the recognized value restores the pointer when its source still matches. A
failed preview hides overlays. Page changes clear the pin. Names use separate
component spans only when the parser has an unambiguous component boundary.

## Maintainer contract

- Keep source coordinates tied to the exact recognized variant and text span;
  do not substitute a nearby occurrence or invent page boundaries.
- Keep source pointers separate from confidence, role attribution, approval,
  and operator-entered values. Manual edits must not inherit stale geometry.
- Preserve the image's source orientation and normalized geometry through OCR,
  storage, preview transformation, zoom, and rotation.
- Do not add OCR passes solely for display geometry. Existing recognition paths
  collect text and geometry together where supported.
- Keep pointer interaction accessible, page-local, and non-persistent. The
  switch must control both visible marks and their interactive hit areas.
- When extending formats or OCR paths, verify the source preview, page mapping,
  clipping, field edits, and missing-geometry behavior before documenting
  support.

Implementation lives in `sro_robot/core/source_locations.py`,
`sro_robot/core/ocr.py`, `app/web/templates/review.html`, and
`app/web/static/field-source.js`.
