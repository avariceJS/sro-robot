# Local document feedback

Run on the Mac from the sole checkout:

```sh
.venv/bin/python scripts/dev/review-local.py start
.venv/bin/python scripts/dev/review-local.py open
.venv/bin/python scripts/dev/review-local.py status
.venv/bin/python scripts/dev/review-local.py diagnostics
```

The authenticated page is `http://127.0.0.1:8767/`. The launcher opens its private
session bootstrap and redirects immediately to `/`; it never prints the secret.
It binds only to loopback and rejects foreign Host/Origin requests. No production
configuration, database, Redis or SROArbitr session is imported.

Originals, immutable SRO snapshots, corrections, comments, session key and SQLite
WAL are under `~/Library/Application Support/SRO Robot/review-queue` (0700).
The same root must be retained across code updates and restarts. Imports verify
original/capture hashes and frozen producer provenance. The initial 100 cached
results retain their historical producer identity; unchanged parser/core source
allows import without another OCR run. Reviews never overwrite the original.

The document viewer loads one original at a time. PDF uses the browser's native
viewer, and photos have zoom/page controls. All 17 card fields are editable;
participant context remains explicitly separate from the selected main card.
Approve confirms the displayed nonempty card values as owner feedback; blank
fields remain unreviewed. Edited/marked fields use Reject with corrections and a
comment. Skip changes no verdict. Ctrl/Cmd+Enter approves, Ctrl/Cmd+Shift+Enter
opens rejection, Alt+Right skips. Counters poll every two seconds.

Each mutation binds an action nonce, document revision and immutable snapshot
SHA. Repeated requests replay the same decision; conflicts keep the browser draft
for explicit reconciliation. HTTP-unknown actions survive reload in sessionStorage.
Owner feedback is useful immediately, but is not independent or double-read gold.
Serving results/originals records private review exposure for later case splitting.

The independent worker mirrors completed acquisition originals, deduplicates by
SHA, and processes one document at a time using the existing isolated capture
helper and native resource supervisor. Completed captures survive worker crashes.
Defaults: 50-second OCR budget, 65-second owned-process bound, one-thread native
limits, 1,000 pending reviews, 500 waiting processing jobs and 2 GiB free reserve.
The browser need not remain open for ingestion/processing. The ordinary KAD native
Save channel itself still needs Chrome and brings its window forward.

Rejected fields create durable private diagnostic rows with original/corrected
values, comment, producer, time and investigation status. The diagnostics command
exports only allowlisted counts/categories/code revisions plus references to the
maintained synthetic extraction fixtures. It never redacts or exports free text.
Derived categories are heuristic owner-feedback classifications, not adjudicated
OCR/field/participant error truth. Keep original details local.

Verification uses disposable synthetic documents only:

```sh
.venv/bin/python -m unittest tests.test_local_review tests.test_local_review_worker tests.test_local_review_diagnostics
node --test tests/test_research_review_ui.mjs
.venv/bin/python scripts/dev/review_browser_e2e.py
```

The browser check covers actual PDF/photo viewing, actions, live ingress, conflict
drafts, restart durability and cross-store stale-snapshot rejection. It never
approves or rejects owner corpus documents.
