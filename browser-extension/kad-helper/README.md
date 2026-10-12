# KAD ordinary-browser helper

This Manifest V3 extension uses the employee's normal Firefox, Chrome, or Edge tab. It does not
start WebDriver, copy browser profiles, change fingerprints, solve challenges or
call unpublished search APIs. Safari packaging is not included.

## Current scope (0.2.0)

Two entry points share the same search/card/chronology code:

- **Service mode:** on `http://localhost:8000/` or `http://127.0.0.1:8000/`, the
  employee enters a case number. The extension creates an inactive Firefox tab,
  finds the exact case and transfers the sole visible qualified PDF through the
  authenticated service page to `/kad-helper/import`. The service stores the PDF
  privately and dispatches the existing local OCR/text parser. No manual download
  or upload is needed. The originating service tab must stay open through receipt.
- **Popup mode:** the independent extension popup still opens KAD and the sole PDF
  in the browser viewer; multiple candidates remain a title-based selection.

Service mode stops on multiple PDFs. A sole visible document is not proof of its
legal type or appointment relevance. Parsed case mismatch/missing case stops OCR
completion. The service review page shows extracted fields and browser-reported
source links; employees still review/approve them before the existing unsaved SRO
form preparation. No SRO save/write or automatic approval is added.

This uses the employee's ordinary Firefox as the executor, not a qualified
headless/browserless server. An inactive tab is not a hidden browser process.
The version 0.1.7 live search/viewer result remains the evidence for the KAD DOM
path; service transfer in 0.2.0 still requires live Firefox qualification.

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
   This update expands access and requires explicit owner approval before loading.
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

PDF data is held in extension worker memory only (25 MiB limit, 60-second fetch
limit), then sent by same-origin authenticated multipart POST. No downloads API,
cloud OCR or browser PDF-viewer injection is used. Finishing/closing the paired
service tab cancels an in-flight fetch and clears bytes under the KAD tab lock.
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
node --test tests/test_kad_browser_automation.mjs tests/test_kad_helper_protocol.mjs tests/test_kad_helper_content.mjs tests/test_kad_service_bridge.mjs tests/test_kad_service_content.mjs
```

Tests use synthetic cases/DOMs and mocked extension APIs. They do not prove KAD
availability, extension installation, real browser selector coverage or successful
document download.

Python checks (use the isolated synthetic loopback-port-1 settings recorded in
`memory-bank/.now.md`, never a real database as a fixture):

```
.venv/bin/python -m unittest tests.test_kad_browser_import tests.test_kad_helper_distribution tests.test_http_application_integration tests.test_route_auth_contract tests.test_task_request_correlation -v
```
