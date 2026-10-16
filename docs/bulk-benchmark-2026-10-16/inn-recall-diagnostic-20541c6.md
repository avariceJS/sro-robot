# INN recall diagnostic for the final 50-document cohort

The pinned cohort audit reports 34 exact INNs, 16 missing, and zero incorrect
nonempty INNs at `20541c6`; the earlier `15138ee` cohort reported 44 exact
and six missing. All 25 text documents still produced exact internal cards.
The ten additional missing values occur in scanned documents, which remain
review-only under the automatic-preparation gate.

I recognized two affected **synthetic** PDFs once each from the pinned corpus
(manifest SHA-256 `59581d8a1b46f38441c3f58edb7ce8fdba7ea97843864fba5ec5191c8f191aeb`).
The exact same in-memory OCR variants were passed to the parsers from
`15138ee` and `20541c6`; among core extraction dependencies, only
`sro_robot/core/extract.py` changed between those revisions. No raw OCR text
or personal identifiers are retained here.

| File | Kind | OCR evidence in 20541c6 | Why the field is now empty |
| --- | --- | --- | --- |
| `synthetic-072.pdf` | scan_high | Three variants contain the same checksum-valid 12-digit INN; one contains a 13-digit invalid OCR token. | The bounded blank-line continuation exposes that invalid token inside the debtor section. `_section_inn` warns and `_role_field` clears the candidate as `role_ambiguous`. |
| `synthetic-082.pdf` | scan_medium | Two variants contain the same checksum-valid 12-digit INN; two contain invalid 13- and 11-digit OCR tokens. | The same warning path clears the candidate. |

At `15138ee`, the malformed variants were outside the section cut at a blank
line. The valid candidate was retained as `role_partial` with a review warning
in both cases. The new missing values therefore represent a conservative
**review-field recall loss caused by newly visible OCR uncertainty**, not a
different debtor or a conflicting checksum-valid INN. Neither case was
eligible for automatic card creation before or after the patch. No code change
was made for this diagnostic; retaining a sole valid candidate with a warning
could be considered separately for the manual review interface.

These two cases explain a concrete mechanism; the other eight newly missing
scan files were not individually OCR-diagnosed. Source artifacts:
`after-20541-50-task-audit.json`, `preformat-15138ee-after-50-w4-card-audit.json`,
and the pinned corpus manifest. Reproduction uses local `.venv` Python,
`sro_robot.core.ocr.recognize` once per PDF, and `git show
15138ee:sro_robot/core/extract.py` loaded under the same core dependencies;
no Git checkout, external service, or real document is involved.
