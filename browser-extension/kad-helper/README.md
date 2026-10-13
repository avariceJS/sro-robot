# KAD ordinary-browser helper

This Manifest V3 extension uses the employee's normal Firefox, Chrome, or Edge tab. It does not
start WebDriver, copy browser profiles, change fingerprints, solve challenges or
call unpublished search APIs. Safari packaging is not included.

## Current scope (0.2.10)

Service acquisition works in ordinary Firefox and, by the same selection rules, in
Chrome or Edge. Firefox reads the browser's own response with `webRequest` stream
filters. Chrome and Edge have no equivalent filter, so the helper attaches the
DevTools Protocol to the single owned KAD tab. `Network.getResponseBody` can return
Chrome's PDF viewer page after the viewer has opened the file, so the original
document bytes are read with `Fetch.getResponseBody` at the response stage.
`Fetch.continueResponse` is then called with only the interception id; the helper
does not fulfill, rewrite, or replay the response. It sets `maxPostDataSize` to 0
and does not read, store, modify or replay request bodies, tokens or headers.
Known access checks still stop the run. Chrome shows a debugging notice while the
file is read; cancelling that notice stops the transfer. Chrome 121+ rejects
`debugger` as an optional permission, so it is declared in the install-time
`permissions` list; the popup cannot grant it. Firefox warns on that unknown
permission and keeps the separate optional response-filter permissions. This Chrome
path is covered by synthetic tests and is not yet qualified on a live court act.
The previously qualified Firefox act remains the live evidence for automatic import.

A bare gateway document (HTTP 502, 503 or 504, including “No server is available
to handle this request”) is reloaded at most twice on the search page and twice
on the card page, after a 2-second pause and then a 6-second pause. The reload
happens only before Find is submitted. The same error after Find stops the run
without submitting the search again. Challenge, captcha and rate-limit pages are
never reloaded. A manual refresh still does not repeat an interrupted search;
only the helper’s one-shot resume flag continues a search that had not been
submitted. Text inside the real KAD search or card shell is not treated as a
gateway error.

Two entry points share the same search/card/chronology code:

- **Service mode:** on `http://localhost:8000/` or `http://127.0.0.1:8000/`, the
  employee enters a case number and optionally the court act date. The extension creates an inactive tab,
  finds the exact case and transfers the uniquely selected PDF through the
  authenticated service page to `/kad-helper/import`. The service stores the PDF
  privately and dispatches the existing local OCR/text parser. No manual download
  or upload is needed. The originating service tab must stay open through receipt.
- **Popup mode:** the independent extension popup still opens KAD and the sole PDF
  in the browser viewer; an optional act date uses the same selection rule. Without
  a date, multiple candidates remain a title-based selection.

Without a date, service mode stops on multiple PDFs. With a date, all candidate
dates must be known and exactly one PDF must match the requested date. The date is
read from the document's chronology row (`data-date` / `.case-date`), never the
publication date, hearing date or PDF filename. Missing/conflicting dates, zero
matches or multiple matching documents stop automation. Date selection does not
establish legal type or appointment relevance. Parsed case or requested act-date
mismatch/missing values stop OCR completion. The service review page shows extracted fields and browser-reported
source links; employees still review/approve them before the existing unsaved SRO
form preparation. No SRO save/write or automatic approval is added.

This uses the employee's ordinary Firefox as the executor, not a qualified
headless/browserless server. An inactive tab is not a hidden browser process.
A 0.2.1 live service launch completed fresh exact search and chronology expansion
in an inactive Firefox tab, with no native KAD clicks or tab activation. It stopped
on multiple PDF candidates. Version 0.2.6 has since qualified automatic PDF
transfer and local parsing for one owner-authorized dated act, as recorded below.

## Passive browser PDF delivery (0.2.6, 2026-10-13)

Network Monitor established that ordinary Firefox navigation receives HTML at
GET of the exact final document address, then independently issues a new POST
and receives `200 application/pdf`. The previous static form gate rejected the
live template before this response, although the browser viewer opened the PDF.

The observer now waits for the browser's actual response instead of requiring a
particular HTML form shape. Unknown intermediate HTML is pending, never success.
Known challenge/denial markers, HTTP denial, rate limits, extra navigation and
unexpected request sequences stop capture. The extension never reads or computes
form values, submits a form, changes headers, replays a request or retries.

GET and POST streams are tracked separately. A browser-generated POST may arrive
before GET finishes; its PDF remains private in memory until GET is terminal and
all delivered HTML has been scanned. Only a single new POST to the exact final
address in the owned tab/top-level frame qualifies. It must return status 200,
PDF MIME and magic within 25 MiB and the shared 60-second deadline. HTML is bounded
to 128 KiB. Cancellation caused by this accepted browser navigation is distinct
from an unrelated GET network failure. Received bytes pass unchanged to Firefox.

Service receipt, source/date binding, local parsing and employee review remain
required. No automatic SRO approval or external save is added. A fresh owner-authorized service run with version 0.2.6 received a complete
two-page PDF without manual download/upload, created one task with source/hash
provenance, and populated all 11 review fields through the local text parser.
Parsed case and requested act date matched; required-field validation passed.
The originating service page redirected to the new task after acknowledged
receipt. This qualifies that observed document, not all KAD templates or acts.
SRO preparation is a separate approved, strictly unsaved preview path; import
does not automatically approve fields or create an external registry record.
After service approval, the existing SRO preview path filled a new blank form,
verified its values, produced a local screenshot and closed without saving. Its
RQ job finished successfully. An earlier attempt stopped at the schema guard
before constructing the SRO client; restarting the stale idle preview worker
against the matching current DB/code resolved that separate runtime issue. The
older failed preview was not retried.

## Early response capture (0.2.4, 2026-10-13)

The owner authorized optional response-reading access and Firefox confirmed it
was enabled. A live 0.2.3 run opened the selected two-page PDF but capture stopped
on HTML. Allowing the viewer's HTML MIME only with PDF bytes also stopped on
actual non-PDF bytes; it did not import a document.

Firefox's installed `PdfStreamConverter.sys.mjs` changes the request MIME to HTML
and proxies the internal viewer using the original request. The capture filter
is now attached in `onBeforeRequest`, before conversion, with response status and
MIME validated separately. Redirect responses are released and only the exact
validated destination is captured. A viewer HTML MIME is inconclusive: actual
bytes must start with `%PDF-`; real HTML stops on the first mismatching byte.
No response headers or access controls are changed. The live early-hook run also
stopped with `pdf_html`: it still did not obtain original PDF bytes. Automatic
service transfer and OCR remain unqualified. No new local task was created.
Ordinary PDF opening is working separately; this capture path is not a completed
solution for service import.

## Native Firefox PDF transfer (0.2.3, 2026-10-13)

The owner's date-filtered service run reached the correct chronology PDF but
background fetch failed with an unexpected content type. A separate anonymous
request received HTML with challenge markers; that is not the exact Firefox
response. Ordinary manual navigation opened a two-page PDF. Native viewing alone
does not qualify automatic import or local OCR.

Service acquisition now registers a Firefox response filter before navigating the
owned KAD tab to its selected PDF. It copies at most 25 MiB into memory and passes
response bytes unchanged to the built-in viewer. It accepts only the selected
card/document path and its corresponding `/Document/Pdf/` redirect, PDF MIME and
magic. HTML, denial, rate limits, changed identity and timeout stop without retry.
Failures preserve the KAD tab and expose a specific safe reason in the service.
Cancellation remains effective through PDF readiness. `bridge-importing` is the
synchronous local-intake admission boundary: later Stop requests report that
transfer has begun instead of promising cancellation; receipt reconciliation
remains required for an uncertain local POST.

Automatic transfer requires the optional Firefox `webRequest`,
`webRequestBlocking` and `webRequestFilterResponse` permissions, enabled using the
popup's PDF-transfer button. Permission admission is checked before any service
search starts. The listener observes only top-level KAD requests during capture
and filters only the owned tab and selected document. Chrome/Edge popup viewing
remains available; automatic native transfer is Firefox-only. New permissions
require owner confirmation before activation. Live native transfer is pending.

## Act-date selection (0.2.2, 2026-10-13)

An optional `actDate` ISO date is bound to the initial run, the local fragment,
full candidate inventory and durable intake metadata digest. Selection reason
`unique_act_date` requires exactly one match across an entirely dated inventory.
The original `only_visible_document` contract remains valid for requests without
a date, including existing stored provenance. The worker independently compares
the locally parsed `doc_date` with the requested date before accepting fields.
The review page shows the selection date. No automatic SRO approval/write is added.
The service blocks date submissions when the installed helper does not advertise
date support. Keep one installed helper instance; parallel old/new installations
are not qualified.
The bridge refuses oversized provenance before starting intake rather than
silently truncating candidates. Existing pending requests retain their original
number/date and receipt key if an operator edits the form before recovery.

The owner-provided image was read by local Tesseract and its requested header
identifiers confirmed without exporting OCR text. JavaScript/Python syntax and
selective diff checks passed; independent code review found no selection defects.
Existing test harnesses were updated for the optional field/shared protocol;
no test suite or real acquisition was run for this change. The field and helper
connection were observed in Firefox; date-filtered PDF import remains unqualified.

## Inactive-tab input correction (0.2.1, 2026-10-13)

The first live service launches filled the case input but never committed it. The
public layout bundle rejects plus clicks while the input retains `g-ph`, its empty
placeholder marker. Inactive-tab `focus()` did not reliably run the normal focus
handler. After setting input/change, the helper now ends editing with blur and an
explicit blur event so KAD's own handler updates its placeholder state. It still
clicks plus once, verifies the exact disabled committed criterion and submits Find
once. A synthetic inactive-focus regression covers that ordering.

Short polling waits now use validated 250 ms runtime messages in the background
context, avoiding [Firefox's content timer deferral during page loading](https://developer.mozilla.org/en-US/docs/Web/API/Window/setTimeout#deferral_of_timeouts_during_pageload).
Only an active same-tab, same-run search/card can request a tick. This is a live
message mechanism, not a durable scheduler; deadlines and stop guards remain.
The timer change alone did not fix the filter failure. The live run succeeded past
search only after the blur correction. Specific filter/readiness/chronology errors
now reach the service instead of a generic failure.

## Install and qualify

1. Download the allowlisted ZIP archive from the authenticated `/kad-helper` page.
2. **Firefox 128 or newer:** open `about:debugging#/runtime/this-firefox`, choose
   **Load Temporary Add-on…**, and select the downloaded ZIP. Firefox removes a
   temporary add-on when the browser restarts; load the ZIP again after restart.
3. **Chrome or Edge:** extract the ZIP to a separate folder. Open
   `chrome://extensions` or `edge://extensions`, enable Developer mode, choose
   **Load unpacked**, and select the extracted folder. `manifest.json` is at the
   archive root.
4. Review its access: `https://kad.arbitr.ru/*`, `http://localhost/*`,
   `http://127.0.0.1/*`, plus extension storage. Browser host patterns cannot narrow
   the localhost port; bridge code accepts only port 8000 and the root task page.
   New host access requires explicit owner approval before loading. For automatic
   Firefox PDF transfer, separately approve and enable the optional response-reading
   permissions through the popup button. Standalone viewing does not need them.
5. Reload the local service page after updating the helper. It must show that the
   helper is connected. Use its case-number form; the popup remains independent.
6. Verify a fresh exact result, automatic local import, parsed case match and
   review fields. Do not restart/retry old SRO jobs for this check.
   A CAPTCHA, access denial or 429 is a stop condition, never a retry trigger.

## Firefox search verification (0.1.5, 2026-10-12)

Two consecutive fresh popup launches in ordinary Firefox 157.0.1 opened KAD,
filled the owner-authorized case number, submitted Find and navigated to the exact
card without native site clicks by the agent. Version 0.1.5 was visible in the
helper panel. This qualifies the requested search flow for the observed case and
browser session; it does not establish coverage of all cases or PDF acquisition.
The installed development copy is loaded from this source directory and was
updated through Firefox's Reload control. Temporary-install limits still apply.

The prior wait for `readyState=complete` plus entries for every same-origin script
in Resource Timing could block a usable form. A live intermediate run still
waited while Firefox displayed a TLS handshake to the external support widget;
that run was stopped before submission. The final implementation accepts a parsed
DOM (`interactive` or `complete`) at `document_idle`, allows two seconds for ready
callbacks, then requires the actual visible controls and committed criterion.
Resource Timing is no longer a prerequisite. This remains a bounded DOM heuristic,
not an application-ready API.

The public desktop page and its parser-blocking bundles were inspected read-only:
[layout bundle](https://kad.arbitr.ru/Content/Static/Js/Common/layout.202610011946.js)
and [search bundle](https://kad.arbitr.ru/Content/Static/js/kad.202610011946.js).
The plus handler clones the draft into a disabled `.tag.added input` and clears
the draft. The Find handler is on `#b-form-submit`, a DIV containing the actual
`button[type="submit"]`. The helper focuses and fills the draft, emits input/change,
yields, clicks plus once, confirms the exact committed value, then reacquires and
clicks the scoped Find button once. Missing or changed commitment prevents submit.
Fresh exact-result and challenge/error guards remain active.

Earlier 0.1.1 results were owner-assisted, and the 0.1.2 no-commit experiment
produced broad results and was replaced. Versions 0.1.3/0.1.4 reached a card in
individual runs but later readiness/filter failures remained. These are historical
failures, not qualification evidence for those builds. Brave 0.1.0 encountered
KAD's `wasm_bg.wasm` runtime failure, and is still unqualified. Native Safari CUA
success is separate evidence; Safari extension packaging is not implemented.
Automatic electronic-case/PDF acquisition was not verified in that version.

## Chronology expansion and document inventory (0.1.6, 2026-10-12)

A fresh Firefox helper run opened the requested card, clicked the matching
instance's collapsed chronology control, waited for the timeline entries and
showed one qualified public PDF in the helper panel. The visible candidate was
an order accepting the bankruptcy petition. The PDF itself was not opened in
this check; employees still select the act. The helper does not infer that this
act appoints a manager.

The helper now locates `.b-chrono-item-header` by the exact
`.b-case-instance-number`, clicks its `.b-collapse.js-collapse` once only when
collapsed, then scans PDF links only in that header's adjacent
`.js-chrono-items-container`. Already expanded timelines are left open. Other
instances and PDFs elsewhere on the page are excluded. It waits for visible
candidates and reports a specific stop state if the timeline or documents do not
appear. Offline tests cover expansion, already-expanded state, irrelevant
instances, readiness and PDF scoping.

## Automatic PDF viewing verified (0.1.7, 2026-10-12)

A fresh popup launch in Firefox 157.0.1 completed search, exact-card navigation,
chronology expansion and automatic PDF navigation without native site clicks.
Firefox's built-in viewer visibly rendered a two-page document in that same tab.
The previous select/save/upload instruction is removed. This qualifies the one
observed case/session, not all cases or automatic document-type selection.

The helper waits until all matching chronology containers are open and the
visible URL inventory stays unchanged for 750 ms. This is a bounded DOM heuristic,
not proof of complete server pagination. A single candidate is revalidated before
and after the worker acknowledges selection metadata (`only_visible_document`);
then `location.assign` opens it. Multiple candidates require a title selection
(`employee_selected_visible_document`). Challenges, stale candidates, failed
metadata writes or an inventory change prevent navigation. Returning to a run
with a recorded selection does not automatically reopen the PDF.

Offline validation: 51 Node tests and three distribution tests passed. Coverage
includes delayed inventory growth, metadata-before-navigation, selection races,
manual choice for multiple candidates, stale links and no automatic reopening.
The live PDF view was distinct from service import/OCR, not implemented in 0.1.7.

## Popup recovery during temporary development installs

Open the helper through Firefox's Extensions menu rather than a saved internal
`moz-extension://` address. If the popup reports File not found after replacing
the loaded ZIP, open `about:debugging#/runtime/this-firefox` and click **Reload**
on this helper. Verify that the popup displays the case field before searching.
Do not overwrite a loaded ZIP during a run; use a separate versioned artifact for
the next installation. The 2026-10-12 reported popup error recovered with Reload;
the installed UUID was unchanged and the archive contained `popup.html`. Archive
replacement without reload is the likely cause, not a confirmed Firefox defect.

## State, receipt recovery and privacy

Per-tab metadata is stored in extension `storage.session`. The service page keeps
only its opaque run ID and import-started marker in tab `sessionStorage`; the
48-character intake key is derived consistently from that run ID. This permits a
page reload or explicit retry to first read `/kad-helper/receipt/{key}` under the
current authenticated user. An uncertain POST does not discard the run/PDF or
start a new search. If needed, a later employee click replays the same local intake
key, whose database manifest binds the source metadata and byte hash. Receipt is
acknowledged once committed even if OCR dispatch is temporarily unavailable; the
durable outbox retains pending local OCR work. Legacy KAD acquisition jobs are not
restored or dispatched.

PDF data is held in extension worker memory only (25 MiB limit, 60-second navigation
limit), then sent by same-origin authenticated multipart POST. No downloads API,
cloud OCR or browser PDF-viewer injection is used. Finishing/closing the paired
service tab cancels an in-flight capture and clears bytes under the KAD tab lock.
Worker/browser termination can lose the in-memory PDF; an unknown service receipt
must still be reconciled before another operation. This is not a durable browser
executor and is not qualified for unattended office deployment.

On import, source URLs, visible candidate label, selection reason, run ID and byte
hash are retained per task in reserved `_kad_source_v1` metadata inside the
existing JSONB recognition envelope. This avoids changing shared deduplicated
Document provenance and needs no schema migration. Field conversion ignores that
reserved entry; OCR preserves it and verifies the case before accepting fields.
The server validates its shape and byte hash but does not independently attest
that the browser obtained those bytes from that URL. The UI labels it accordingly.

## Reproducible offline checks

```
node --test tests/test_kad_browser_automation.mjs tests/test_kad_helper_protocol.mjs tests/test_kad_helper_content.mjs tests/test_kad_service_bridge.mjs tests/test_kad_service_content.mjs tests/test_kad_native_pdf.mjs
```

Tests use synthetic cases/DOMs and mocked extension APIs. They do not prove KAD
availability, extension installation, real browser selector coverage or successful
document download.

Python checks (use the isolated synthetic loopback-port-1 settings recorded in
`memory-bank/.now.md`, never a real database as a fixture):

```
.venv/bin/python -m unittest tests.test_kad_browser_import tests.test_kad_helper_distribution tests.test_http_application_integration tests.test_route_auth_contract tests.test_task_request_correlation -v
```
