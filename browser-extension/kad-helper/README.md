# KAD ordinary-browser helper (qualification pending)

This Manifest V3 extension uses the employee's normal Firefox, Chrome, or Edge tab. It does not
start WebDriver, copy browser profiles, change fingerprints, solve challenges or
call unpublished search APIs. Safari packaging is not included.

## Current scope

The service's case-number form opens KAD with a local fragment handoff. The helper
removes the fragment, enters the exact number through KAD's visible controls,
requires a fresh unique matching result, opens its observed card URL in the same
tab, verifies the case identity and opens the electronic-case section. It lists
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

Version 0.1.1 was temporarily installed in Firefox 157.0.1 on 2026-10-12.
Its popup opened KAD and the content script populated the case field, but the
helper timed out before confirming a result. A matching result later appeared
while the helper was stopped; this is not evidence of an automated success.
The submit selector now rejects non-button ID wrappers and disabled controls;
a synthetic wrapper regression passes. A live rerun of this change remains
pending because native UI control lost access to Firefox page contents.
The site's live submit tag was not verified, so this is not a proven root cause.
Full Firefox and Chromium qualification remains pending.

On 2026-10-12, after explicit owner approval, version 0.1.0 was installed and
verified enabled in ordinary Brave, with KAD-only site access and private-mode
and local-file access disabled. Its popup launched a real search and populated
the case input. KAD produced no result; DevTools showed `RuntimeError: unreachable`
in `wasm_bg.wasm`, also seen before the extension was installed. The helper stopped
at its explicit timeout without repeating the search. No card/PDF was acquired
by this run. The cause of the browser difference remains unestablished.

A successful CUA Safari search is separate evidence, not extension qualification.
The extension uses DOM clicks rather than privileged native key events; a complete
real-site extension search still needs qualification.

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
