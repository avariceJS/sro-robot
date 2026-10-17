# Stage 2 — Evidence Intake and Acquisition

**Assessment:** M2 bounded for the synthetic local workflow. This is not live KAD, office-document, or deployment qualification.

## M2 evidence matrix

| Criterion | Status | Evidence and boundary |
| --- | --- | --- |
| Individual PDF/image intake has replayable fixtures and explicit outcomes | Met for the supported synthetic formats | The upload contract preserves original bytes, SHA-256 and source occurrence; tests cover PDF/image intake, duplicate replay and terminal failures. Single-frame raster images are accepted. Multi-frame TIFF now fails explicitly instead of silently discarding later frames. |
| File size, page count, runtime and raster memory are bounded | Met for local worker admission | Existing upload limits remain 25 MiB/file, 100 MiB/batch and a 600-second RQ hard timeout. New defaults: 80 PDF pages, 12,000,000 pixels/image, 540 seconds shared OCR budget, 20 seconds PDF metadata/raster subprocess, 60 seconds PDF text extraction and 45 seconds per Tesseract call. Settings enforce that the OCR deadline and subprocess timeouts stay below the worker timeout and cap configurable PDF/image limits. Raster batches contain at most two pages. See the measurement below. |
| Case-number acquisition has replayable fixtures and explicit states | Met for employee-assisted synthetic KAD flow | The helper exact-searches a case, expands the visible chronology, presents visible titles/dates, and requires an explicit employee choice even when only one PDF is visible. It rechecks the observed case and inventory before capture. The fixture run found two candidates and selected only after the employee action. A visible inventory is always treated as incomplete; no title/date heuristic chooses the legal act. |
| Original bytes and source/retrieval provenance survive selection and import | Met for local import contract | Selected URL and reason, observed candidate inventory, case/card URL and run ID are bound to the import; the local intake keeps original bytes/hash, checks parsed case/date and uses durable source-occurrence and idempotency receipts. Existing service/bridge/import tests and the PostgreSQL verifier exercise these boundaries. |
| Incomplete candidate inventory cannot be auto-selected | Met | The helper explicitly marks the observed inventory incomplete and the import rejects automatic-selection reasons. The selection reason must be `employee_selected_visible_document`; the selected URL must still appear in the current observed chronology. |
| OCR result reaches the existing debtor review flow | Met locally; quality is synthetic-only | Existing mixed-batch and PostgreSQL scenarios process local PDFs/images, retain task state, show fields for staff review and preserve correction/resume behavior. The full user-interface journey for company/IP was previously run on isolated synthetic records; this mission's new OCR admission errors now surface a bounded employee instruction while retaining the original upload. |

## Page and memory policy evidence

The default cap is sized from a reproducible local synthetic scanned-PDF probe, not from assumed legal semantics. Run it with:

```sh
.venv/bin/python scripts/dev/nightly/measure_ocr_admission_bounds.py --raster-batch-pages 4 --output /tmp/ocr-four.json
.venv/bin/python scripts/dev/nightly/measure_ocr_admission_bounds.py --raster-batch-pages 2 --output /tmp/ocr-two.json
```

Both runs generate the same four-page, 300-DPI synthetic scan (`9a2ff353e52a33ee1614aa3422835632e2ae52b8c0bd7dec2d47876449fbb1ad`) and recognize four variants. On the checked Mac, four-page raster batching took 18.061 seconds and peaked at 543,178,752 bytes RSS; two-page batching took 18.010 seconds and peaked at 354,484,224 bytes, 34.7% lower with effectively unchanged elapsed time. Linear extrapolation of the two-page run gives about 360 seconds for 80 scan pages, leaving 40% of the 600-second RQ window. The worker also has a shared 540-second subprocess deadline, leaving time for parsing and persistence. These are one host and one synthetic layout; they do not predict office documents or target deployment memory. The compact measurement outputs had SHA-256 `f17ae9d65161d316714ac675e66e5e493b6eb3507b01ee9755a82e8085a99a29` (four-page batches) and `bd0456f7bf9588551b9d5f2bc0b7405dfa270f22c10d513130b99c001226f6a6` (two-page batches); the probe source SHA-256 is `280ae6699ec15453147ebcc9e48048d5cf81cad7f2855dd5fbeee09fbff832d2`.

The 12 MP raster cap accepts a typical 12 MP phone image and is above the approximately 8.7 MP raster size of an A4 page at 300 DPI. It is configurable up to 25 MP. The PDF page limit is configurable up to 10,000, but the overall OCR and RQ deadlines still bound processing. Administrators should increase limits only after measuring worker RSS, concurrency and target document duration. Multi-frame TIFF remains unsupported; staff receive guidance to split it or submit a PDF.

## Browser and integration checks

- `scripts/dev/nightly/verify_kad_browser_dom.py` passed in Playwright Chromium 153.0.8010.12 with two synthetic candidates, chronology expansion, a required employee choice, a stopped run that never acquired a document, and zero requests outside in-memory fixtures. It is explicitly not a full-extension backend import or live KAD check.
- `SRO_DRILL_TASKS=20 bash scripts/dev/verify.sh` passed on the exact development tree: 783 unit/HTTP tests (106 PostgreSQL-only cases are discovered and skipped in this first pass), 106 disposable PostgreSQL tests, 148 Node tests, migration/adoption/restore checks, queue-loss recovery after a disposable Redis restart, RQ duplicate/crash/restart/read-back checks, and 1,000/1,000 synthetic OCR claims in both thread and process modes.
- The OCR admission failure tests verify page rejection before text extraction (including searchable PDFs), image dimensions before image copy, explicit multi-frame rejection, subprocess and shared-deadline behavior, settings validation, and employee-safe failure text.

All verification inputs are synthetic or disposable local data. No real KAD case, SRO record, office document, production service, or VPS was used. The KAD browser helper itself had no current authorized KAD tab available during this mission.

The full verifier raw stream was reviewed, then summarized with SHA-256 `270ffea5d7123f31e173f4cd7a0f3c32d5a7192a43b199df887fe8a7f0b786d0`; the stream is temporary and is not committed. The Chromium fixture report SHA-256 is `1868b64a41ac67c82d34ba3851d2a99fef7a1532b41a9fb7a7abafe4fd0a2ba5`.

## M3/M4 gaps

| Level | Assessment |
| --- | --- |
| M3 operational | Partial. Disposable worker restart, queue loss, duplicate delivery and read-back are exercised, and KAD unavailable/incomplete states fail closed. The staff worklist shows queued and processing counts, but there is no configured queue-age/capacity threshold or admission backpressure policy. The target deployment's source-outage runbook, alerting, worker supervision, operator diagnosis and recovery objectives are not qualified. No current live KAD session was available to test source variations or current selectors. |
| M4 measured scale | Not met. The synthetic probe is one document layout on one host. Office OCR quality, representative KAD coverage, production worker memory, large-batch throughput, soak, outage and recovery targets are unmeasured. |

## Owner participation needed

No owner action is required for the implemented local bounds or synthetic checks. Later qualification needs an authorized ordinary-browser KAD session on the intended network for read-only selector/availability checks, plus an office-approved synthetic or safely redacted document corpus for quality and target-resource measurement. Neither is substituted by the current fixture results. No live SRO write is required for Stage 2.
