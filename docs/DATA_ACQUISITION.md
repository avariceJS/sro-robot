# OCR Dataset Acquisition

## Owner authorization and source boundary (2026-11-07)

The owner, acting as a company developer, states that permission was obtained to
acquire published KAD acts in bulk for internal OCR/extraction research. This is
an owner authorization. No written authorization document was supplied or
independently verified; none is treated as a prerequisite in this workflow. The
authorization concerns isolated local research and does not qualify deployment,
production acquisition jobs, or any SROArbitr record operation.

The sanctioned source channel for this work is the existing ordinary KAD
integration, driven by explicitly selected case-act PDF URLs through the normal
Chrome browser and guarded native PDF Save. Internal export files may also be
ingested from a dedicated private directory. The queue makes repeated selected
downloads resumable; it does not search KAD or infer an act choice from chronology.
There is no issued source request quota. The queue uses one browser stream and at
least five seconds between requests as a temporary engineering safeguard, not as
an operator-agreed rate. There is no 200-document ceiling; collect in measured
batches within local disk and OCR-processing budgets.

KAD's public site exposes case search and document viewing through its ordinary
interface ([KAD](https://kad.arbitr.ru/)). The current
[`robots.txt`](https://kad.arbitr.ru/robots.txt) disallows automated crawling.
An earlier anonymous direct-PDF request returned an HTML access challenge. The
current KAD collection path therefore uses ordinary browser navigation and the
existing native Save guard. Never bypass CAPTCHA, login/access challenges,
throttling, blocks or other controls. A 401/403/challenge disables that adapter
until a person has inspected the source state; 429 pauses it for cooldown. A
source problem does not stop independent local-export processing.

The staff product path remains ordinary browser navigation, explicit employee
selection of a complete PDF, and local office import/OCR; see
[`30-integrations.md`](../memory-bank/30-integrations.md),
[`KAD_HELPER_ADMIN.md`](KAD_HELPER_ADMIN.md), and
[`routes_kad_import.py`](../app/api/routes_kad_import.py). Research acquisition
uses the same ordinary KAD source interaction under the separate owner
authorization stated above.

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

The native driver emits a fixed set of phase names and elapsed times to stderr.
The helper keeps only those fixed values, the terminal status and total elapsed
time in private `inbox/.native-save-diagnostics.json`, outside the strict empty
staging directory. Diagnostic writes are best-effort and cannot change a Save
or custody outcome.

Live Mac qualification saved one 236,140-byte PDF in 8.46 seconds; running the
launcher from another working directory recognized a repeat as duplicate. Hash
filename, receipt, mode 0600 and empty pending/staging state were read back. The
saved version's embedded text matches the earlier manual copy, while its bytes
differ after browser reload. This timing is one native Save observation, not a
sustained-rate measurement, server-byte identity or OCR truth reference.

## Resumable local acquisition queue

`scripts/dev/quality/corpus_acquisition.py` stores queue state, original PDFs,
source URLs, candidate metadata, and receipts in a private directory outside the
checkout. Each queue item has a source identifier, channel, exact PDF URL or
private local-export path, and optional candidate strata (`case_id`, case number,
court, year, act type, participant kind). Candidate metadata is source evidence
for later local case matching; it is not verified document truth.

Supported channels:

- `kad-browser-pdf`: normal Chrome navigation followed by the existing guarded
  current-tab native Save and local readback. The active URL must resolve to the
  same KAD case/document IDs as the queued URL.
- `internal-export` and `browser-selected-pdf`: copy a complete local PDF from a
  dedicated export directory into the private inbox.
- `kad-direct-pdf`: retained only for a separately approved direct/API source.
  Do not use it for KAD's known anonymous challenge response or without an
  approved API source.

Example JSONL (use real URLs and identifiers only in the private local feed):

```json
{"source_id":"private-source-id","source_url":"https://kad.arbitr.ru/Kad/PdfDocument/11111111-2222-3333-4444-555555555555/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/act.pdf","channel":"kad-browser-pdf","authority":"owner_assertion","case_id":"private-case-id","case_number":"private-case-number","court":"private-court","year":"2024","act_type":"decision","participant_kind":"individual"}
```

Use a dedicated private root and keep its feed outside Git:

```sh
ROOT="$HOME/Library/Application Support/SRO Robot/ocr-research/corpus-2026-11-07"
python3 scripts/dev/quality/corpus_acquisition.py init --root "$ROOT"
python3 scripts/dev/quality/corpus_acquisition.py enqueue --root "$ROOT" --manifest "$ROOT/feeds/kad.jsonl"
python3 scripts/dev/quality/corpus_acquisition.py run --root "$ROOT" --limit 20
python3 scripts/dev/quality/corpus_acquisition.py status --root "$ROOT"
```

`--limit` is a per-run pulse, not a total corpus cap. The queue defaults to a
512 MiB per-run intake budget and at least five seconds between source requests;
increase the spacing if the source shows load or asks for it. The queue caps
pending work at 1,000 items and preserves a 512 MiB free-space reserve, including
room for one maximum-size source document before pausing intake.
It enforces one queue owner at a time, content-hash deduplication, atomic
publication, local receipts and resumable pending items. Aggregate timings and
pause counts are saved privately in `acquisition-profile.json`; it contains only
fixed stage names and counts, total/max milliseconds, and fixed pause codes. HTTP
429 honors `Retry-After` where available; transient network failures cool down.
Access denial, challenge or an unexpected redirect disables that channel until
the source state is inspected.
Re-enable a stopped adapter only after the issue is resolved:

```sh
python3 scripts/dev/quality/corpus_acquisition.py reset-adapter --root "$ROOT" --channel kad-browser-pdf --confirm-source-restored
```

The private `inbox/` is suitable as input to the existing local dataset tooling.
Keep OCR, identifiers and source receipts local. Group acts from the same case
and duplicate derivatives together before freezing train/dev/control splits.
Never reuse previously tuned cases as independent control. PDF text is a weak
preliminary reference until a person checks it against the original PDF.

Research selection may rank visible act captions locally and screen embedded PDF
text for candidate name patterns, debtor-role proximity, checksum-valid tax-number
patterns, and address anchors. These are weak presence signals only; they do not
establish debtor identity, field truth, or extraction accuracy. The bounded local
feature summary stores only per-document booleans and aggregate counts, never the
text or identifier values. Scanned pages with no embedded text remain eligible.

## Ordinary-browser research discovery

With an existing KAD tab active in Chrome, the local native accessibility helper
can discover one visible results page and enqueue its complete PDF links:

```sh
.venv/bin/python scripts/dev/quality/kad_discovery.py --root "$ROOT" \
  --year 2015 --category bankruptcy --cases 25 --acts-per-case 1 --download
# Resume the same private results snapshot, skipping completed cases:
.venv/bin/python scripts/dev/quality/kad_discovery.py --root "$ROOT" \
  --year 2015 --category bankruptcy --resume-cases --download
# A regular bounded pulse with durable transient recovery:
.venv/bin/python scripts/dev/quality/corpus_acquisition_coordinator.py --root "$ROOT" \
  --year 2025 --category bankruptcy --cases 25 --acts-per-case 1
```

Keep the Mac/Chrome available during this operation. The navigator leases the
existing active KAD tab by window/tab identity and navigates that tab through the
search and selected PDF pages. Chrome comes to the foreground for native Save, so
run it when Chrome focus is available to the collection flow. Changed or unrelated
tabs are not silently overwritten. Native controls are matched again by label
before pressing. No browser profile, cookie export, Apple Events JavaScript setting
or CAPTCHA service is used.
The Swift transport compiles locally with the installed toolchain and caches only
its binary/source hash under ignored `.local-run/kad-discovery`.

The background review collector uses `--owned-browser`, a private window/tab
lease and an optional calendar `--month`. In this mode the official case-number
year must match `--year` before a case is opened; query dates alone are not year
evidence. It admits only the existing rich bankruptcy-caption rank zero and
enqueues only the qualified current pulse, never an entire historical feed.
Legacy discovery can request the same guards explicitly with `--case-year` and
`--max-caption-rank`. Caption qualification does not establish the operative act
or the truth of its participant fields.

`review-local.py collection-on` and `collection-off` persist the operator choice.
The initial plan covers elapsed/current months in 2025 and 2026, including the
active month for replenishment. Per-pulse limits and backlog/disk safeguards are
not a final corpus-size limit. The collector and OCR worker run independently of
the review page; a disabled upstream adapter remains held even if the daemon is
running. Preserve native pending markers, staging and cancellation receipts until
the exact outcome is reconciled.

Two cached-queue five-act pulses took 46.50 and 47.46 seconds, approximately 6.4
acts/minute; native Save accounted for roughly 91% of that measured wall time.
These pulses exclude search and are not a causal speedup comparison. A later
owned-window run exposed a nested Go To Folder sheet that is not qualified by
the native identity guard: it stopped before Save and was safely canceled.
Continuous native collection is therefore not qualified on this Mac; do not
enable aggressive retries or weaken the identity checks to obtain a success.

This command samples visible judicial-act candidates; it does not establish the
legally operative act, complete case history, participant category or population
representativeness. The filter press is recorded as requested, not independently
confirmed. Candidate captions and source identities stay in private `discovery/`.
The acquisition metadata `year` records the requested search stratum, **not** a
verified act year; establish actual dates separately from the original. Repeat
different year/category strata to expand the corpus. This helper currently reads
one results page per search, not all results or an unlimited-rate API.

A positively identified native no-save result can clear only a matching, entirely
empty owned stage under the inbox lock and enter a 60-second cooldown. Partial,
hidden or uncertain bytes retain custody and stop retries. Never reset an access
challenge or unknown Save outcome automatically. CLI output contains fixed status
codes and counts only; detailed source material remains private.

The coordinator launches a fresh collector process after a known transient
cooldown, at most twice and with at most 300 seconds of idle waiting per pulse.
It pins the exact cached case inventory before resuming it; initial search failures
retry a fresh search instead. Disabled/challenged adapters, changed cache identity,
unknown child outcomes and real native pending stages remain held for reconciliation.
Retries, deadlines and status survive interruption; repeated invocation cannot
erase an exhausted/unknown retry budget. An already loaded identical leased PDF is
reused without reloading it, while the tab/window/source guards remain active.

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

Keep the owner authorization and approved source scope private with the corpus.
Do not put documents, OCR payloads, participant identifiers, source URLs or
receipts in cloud tools, logs, Git, or shared fixtures. Do not reinterpret the
owner authorization as permission to bypass source controls or to use a different
service's terms.
