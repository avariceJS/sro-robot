# OCR Dataset Acquisition

## Evidence observed on 2026-11-07

The KAD public site exposes case search and document viewing through its
ordinary interface ([KAD](https://kad.arbitr.ru/)). Its current
[`robots.txt`](https://kad.arbitr.ru/robots.txt) allows only the listed index
paths and `/`, then applies `Disallow: /` to all user agents. This is evidence
against automated crawling. A robots file is not a dataset license or a full
statement of legal rights. No KAD-specific terms granting bulk acquisition or
reuse for OCR research were confirmed in this review; the site's About page was
not retrievable. Treat automated/bulk KAD collection as unapproved until the
operator or an authorized licensor confirms a permitted channel and scope in
writing. Do not bypass CAPTCHA, login/access challenges, rate limits, blocks, or
other controls.

The current product path remains ordinary browser navigation, explicit employee
selection of a complete PDF, and local office import/OCR; see
[`30-integrations.md`](../memory-bank/30-integrations.md),
[`KAD_HELPER_ADMIN.md`](KAD_HELPER_ADMIN.md), and
[`routes_kad_import.py`](../app/api/routes_kad_import.py). This workflow supports
case processing. It does not by itself authorize collecting a research corpus.

## Save the currently opened PDF on this Mac

Open an unedited complete KAD PDF in Chrome, then run:

```sh
scripts/dev/quality/Download-current-KAD.command
```

The launcher can also be opened from Finder. It uses the existing project Python
environment and the ordinary Chrome **Save Page As** PDF path, supplying the local
directory/name automatically. Keep Chrome available during the short save action.
The destination is ignored `storage/ocr-research-inbox`; files are content-addressed
and accompanied by private source receipts. Existing bytes are hash-checked rather
than overwritten. Documents/URLs/receipts never belong in source commits.

The command accepts only the active tab on the reviewed KAD PDF routes. It does
not search cases, follow case-document lists, copy cookies, inspect document DOM,
enable JavaScript from Apple Events, solve CAPTCHA or launch bulk acquisition.
Its browser integration needs macOS Automation/Accessibility access; it does not
change those permissions. Errors/uncertain saves require local state readback,
not a blind retry. Other corpus work can continue while an external source is
unavailable.

The browser session is necessary: a direct anonymous request did not return a
PDF during the live check and that implementation was removed. Chrome's scripting
`tab.save(..., 'only html')` is also not the native PDF save path. The supported
command invokes the browser menu and validates the resulting local PDF. See the
[Chrome 157 browser command](https://chromium.googlesource.com/chromium/src/+/refs/tags/157.0.8103.39/chrome/browser/ui/browser_commands.cc#2423)
and [AppleScript handler](https://chromium.googlesource.com/chromium/src/+/refs/tags/157.0.8103.39/chrome/browser/ui/cocoa/applescript/tab_applescript.mm#307).
Native saving can reflect PDF viewer edits; use an unedited source and do not infer
server-byte identity merely from a successful browser save. Screenshots/rasterized
copies are distinct derived inputs and never replace the original PDF silently.

The native helper is bounded to 30 seconds and searches only native Chrome
sheet/dialog controls, never the PDF accessibility content. An exclusive inbox
lock prevents interleaved saves. A pending marker preserves uncertain staging and
blocks retries until local reconciliation. A bounded stable snapshot is checked
by local `pdfinfo`; those exact bytes are hashed, atomically published and read
back with the receipt. Successful staging is removed; unresolved bytes remain
private and hidden from dataset imports.

Live Mac qualification saved one 236,140-byte PDF in 8.46 seconds; running the
launcher from another working directory recognized a repeat as duplicate. Hash
filename, receipt, mode 0600 and empty pending/staging state were read back. The
saved version's embedded text matches the earlier manual copy, while its bytes
differ after browser reload. These are versions of one selected act, not two new
cases, server-byte identity or an OCR truth reference. No acquisition-rate claim
or permitted bulk channel follows from this single-document check.

## Alternative source with published reuse terms

The official [Moscow courts open-data portal](https://data.mos-gorsud.ru/opendata)
publishes its [terms of use](https://data.mos-gorsud.ru/terms-of-use). The terms
state that its open datasets may be reused without a separate agreement or
license, including commercial use, with attribution by linking to the original
dataset; they also require lawful use, preserving the source link, and not
misrepresenting the data or its update time. The terms are published by this
portal and apply to datasets published there; do not extend them to KAD or other
sources.

The portal is a possible source for structured case/appeal metadata tests. The
catalog reviewed here did not establish a corpus of court-act PDFs suitable for
OCR benchmarking. Confirm the specific dataset, its fields, update date, and any
dataset-specific conditions before use. Open-data publication does not make it
an OCR document corpus by itself.

## Acquisition gate for an OCR corpus

For a 100–200-act benchmark, use a source whose terms or written authorization
cover the specific acquisition and local research use. Before collection,
confirm with the source: permitted acquisition method and volume; act/document
types and date range; local OCR evaluation or model-development purpose; access,
retention and deletion; redistribution restrictions; and handling of personal or
otherwise restricted information. Record the source, authorization/terms version,
date, and approved scope in private project records. Keep PDFs and OCR payloads
local; do not put them in cloud tools, issues, logs, Git, or this documentation.

Until an OCR corpus source is cleared, use synthetic fixtures and existing
owner-approved local samples for OCR pipeline checks. For ordinary case work,
continue the employee-selected browser import path. Neither path establishes
permission for automated KAD collection.
