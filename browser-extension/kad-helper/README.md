# KAD ordinary-browser helper

This Manifest V3 extension uses the employee's normal Firefox, Chrome, or Edge tab. It does not
start WebDriver, copy browser profiles, change fingerprints, solve challenges or
call unpublished search APIs. Safari packaging is not included.

## Current scope

The service's case-number form opens KAD with a local fragment handoff. The helper
removes the fragment, enters the exact number through KAD's visible controls,
requires a fresh unique matching result, opens its observed card URL in the same
tab, verifies the case identity, expands the matching chronology section and lists
all qualified visible PDF links for explicit employee selection. It never chooses
the first/latest act or infers appointment from case identity alone.

PDF opening is an ordinary link navigation. Download, automatic PDF transfer to
the service, source persistence in the service database and parser integration
are not implemented by this extension. The existing local upload/OCR flow remains
available. Do not report this build as end-to-end document automation.

## Install and qualify

1. Download the allowlisted ZIP archive from the authenticated `/kad-helper` page.
2. **Firefox 128 or newer:** open `about:debugging#/runtime/this-firefox`, choose
   **Load Temporary Add-on…**, and select the downloaded ZIP. Firefox removes a
   temporary add-on when the browser restarts; load the ZIP again after restart.
3. **Chrome or Edge:** extract the ZIP to a separate folder. Open
   `chrome://extensions` or `edge://extensions`, enable Developer mode, choose
   **Load unpacked**, and select the extracted folder. `manifest.json` is at the
   archive root.
4. Review its access: only `https://kad.arbitr.ru/*`, plus extension storage.
5. Open SRO Robot in the same browser and use its case-number form. The extension
   popup can also start a search independently.
6. Verify a fresh result, exact card identity, candidate list and PDF opening.
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
Automatic electronic-case/PDF acquisition remains unverified.

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

## Popup recovery during temporary development installs

Open the helper through Firefox's Extensions menu rather than a saved internal
`moz-extension://` address. If the popup reports File not found after replacing
the loaded ZIP, open `about:debugging#/runtime/this-firefox` and click **Reload**
on this helper. Verify that the popup displays the case field before searching.
Do not overwrite a loaded ZIP during a run; use a separate versioned artifact for
the next installation. The 2026-10-12 reported popup error recovered with Reload;
the installed UUID was unchanged and the archive contained `popup.html`. Archive
replacement without reload is the likely cause, not a confirmed Firefox defect.

## State and privacy

Job state is bound to a tab and opaque run ID in extension `storage.session`, expires
after 30 minutes when next accessed, and is deleted on tab close/browser exit.
Content scripts cannot directly read this store. It retains case/card/candidate
metadata and the employee's selection reason, never PDF bytes or party text.
State transitions are serialized per tab in the worker. Reloading an interrupted
search does not resubmit it. There are no persistent retries or background queue
jobs. This is transient acquisition state, not a durable audit trail.

## Reproducible offline checks

```
node --test tests/test_kad_browser_automation.mjs tests/test_kad_helper_protocol.mjs tests/test_kad_helper_content.mjs
```

Tests use synthetic cases/DOMs and mocked extension APIs. They do not prove KAD
availability, extension installation, real browser selector coverage or successful
document download.
