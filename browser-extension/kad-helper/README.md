# KAD ordinary-browser helper (qualification build)

This Manifest V3 extension uses the employee's normal Chrome/Edge tab. It does not
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

1. Download the allowlisted archive from the authenticated `/kad-helper` page.
2. Extract it; load `sro-kad-helper` as an unpacked extension in Chrome/Edge.
3. Review its access: only `https://kad.arbitr.ru/*`, plus extension storage.
4. Open SRO Robot in the same browser and use its case-number form. The extension
   popup can also start a search independently.
5. Verify a fresh result, exact card identity, candidate list and PDF opening.
   A CAPTCHA, access denial or 429 is a stop condition, never a retry trigger.

No browser has been granted extension access automatically. Real-site extension
qualification requires installation and the user's specific permission for KAD
page access. A successful CUA Safari search is separate evidence, not extension
qualification. The extension uses DOM clicks rather than privileged native key
events; site handling of those clicks remains an explicit qualification boundary.

## State and privacy

Job state is bound to a tab and opaque run ID in `chrome.storage.session`, expires
after 30 minutes when next accessed, and is deleted on tab close/browser exit.
Content scripts cannot directly read this store. It retains case/card/candidate
metadata and the employee's selection reason, never PDF bytes or party text.
State transitions are serialized per tab in the worker. Reloading an interrupted
search does not resubmit it. There are no persistent retries or background queue
jobs. This is transient acquisition state, not a durable audit trail.

## Reproducible offline checks

```
node --test tests/test_kad_browser_automation.mjs tests/test_kad_helper_protocol.mjs
```

Tests use synthetic cases/DOMs and mocked extension APIs. They do not prove KAD
availability, extension installation, real browser selector coverage or successful
document download.
