# Private document feedback

## Expert server

The owner authorized an isolated expert reviewer on Aeza service 666936 on
2026-11-13. This is separate from the production SRO Office release workflow.
Originals and feedback may be transferred to that owner-controlled server over
authenticated SSH. They remain outside Git and cloud AI.

`scripts/dev/review-remote.py` serves the existing queue behind one exact HTTPS
origin. It binds only to loopback and starts no OCR/acquisition workers. Configure
`--root`, `--public-origin`, `--expert`, and `--token-file` (an owned regular 0600
file outside the checkout). Experts sign in at `/login`; secrets never enter
URLs. Sessions expire after 12 hours. Host/Origin checks, secure cookies and
bounded login attempts protect document and review routes.

Approve, reject, edit and comment use the same durable review contract as the
local UI. The configured expert account is recorded transactionally with each
decision. Historical local decisions retain their original unknown identity.
An account is not proof of a person's real-world identity. One configured account
is supported; use a distinct deployment/account for a different reviewer.

Retrieve feedback on the Mac without replacing the live local queue:

```sh
.venv/bin/python scripts/dev/review-feedback-sync.py \
  --host sro-review-research \
  --destination "$HOME/Library/Application Support/SRO Robot/reviewer-feedback"
```

SSH must already have authenticated access and a verified host key. The fixed
remote database is `/var/lib/sro-review/queue/review.sqlite3`. SQLite backup
includes committed WAL data while reviews continue. The private mirror
`feedback.sqlite3` includes original captures, corrections and comments, but no
original PDF assets. Do not open its rows in cloud-agent output. Only the command's
allowlisted aggregate diagnostic packet may be shared with agents. Identity
drift, lost reviews and altered immutable feedback evidence refuse replacement.
Diagnostic status transitions remain allowed. Keep this mirror across updates.

Expert feedback informs diagnosis; it does not train or change the parser
automatically. Use repeated safe categories and maintained synthetic reproductions
to develop general fixes; verify against private development documents and
independent case groups. Card approval is not independent field gold, and empty
fields remain unreviewed.

The installed Mac job `ru.sro-robot.expert-feedback-sync` runs that command every
five minutes while the Mac is awake, with no private output logs. Its plist is
under the owner's `~/Library/LaunchAgents`; the mirror is retained across code
updates. Check its last exit status with:

```sh
launchctl print "gui/$(id -u)/ru.sro-robot.expert-feedback-sync"
```

Network failure leaves the last
validated mirror intact and the next scheduled run retries. The server remains
the authority for expert feedback; the Mac's original development queue is not
merged or overwritten.

Server source is under `/opt/sro-review/<published-commit>`; durable queue and
login key are under `/var/lib/sro-review`, outside releases. Code updates must
preserve that state. The one-time bootstrap helper refuses existing deployments;
never remove its refusal gates to update a live queue. Qualify an updated release
separately before switching the service, and retain the previous release until
readback succeeds. HTTPS certificate renewal uses
`sro-review-certificates.timer`, the maintained Certbot environment at
`/opt/sro-review-acme`, the public challenge-only webroot and an nginx reload hook.
No acquisition/OCR workers or main application are deployed by this reviewer.

## Mac service

Run on the Mac from the sole checkout:

```sh
.venv/bin/python scripts/dev/review-local.py start
.venv/bin/python scripts/dev/review-local.py open
.venv/bin/python scripts/dev/review-local.py status
.venv/bin/python scripts/dev/review-local.py diagnostics
.venv/bin/python scripts/dev/review-local.py reparse
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
viewer, and photos have zoom/page controls. The editable main form follows the
recognized subject kind: individual, legal entity, or sole proprietor. Common
case/document fields remain available for every kind. An unresolved kind needs
an explicit operator choice; it must not silently default to an individual.
Other participants remain supplemental context below the main form.

The local review schema has 20 fields, separately from the unchanged 17-field
evaluation schema. Individuals show 12 fields with separate editable last, first
and middle names; organizations show 13, and sole proprietors 10. Component
corrections update the canonical full name atomically and retain the displayed
original and exact corrections in the audit.

Civil acts may identify a plaintiff or defendant without naming a bankruptcy
debtor. Those source-located participants keep their actual roles. A unique
explicit defendant, or otherwise a single eligible participant, can populate a
review proposal; equally eligible alternatives require explicit selection. An
organization name or OGRN alone never establishes debtor authority.

Review suggestions can fill the corresponding editable fields as uncertain
proposals from one coherent occurrence. They do not become authoritative debtor
extraction merely because they are displayed. Original captures, provenance and
owner corrections remain retained. An ambiguous participant must not contribute
identifiers or addresses to another participant's form.

Approve confirms only the displayed, applicable nonempty values as owner
feedback; blank and hidden fields remain unreviewed. Edited/marked fields use
Reject with corrections and a comment. Skip changes no verdict. Ctrl/Cmd+Enter
approves, Ctrl/Cmd+Shift+Enter opens rejection, Alt+Right skips. Counters poll
every two seconds.

Each mutation binds an action nonce, document revision and display snapshot SHA,
including the immutable capture hash, projection version and persistent store
identity. The exact displayed proposal and reviewed field scope are retained
with the action. Hidden fields cannot be approved by accepting a typed form.
Repeated requests replay the same decision; conflicts keep the browser draft
for explicit reconciliation. HTTP-unknown actions survive reload in sessionStorage.
Owner feedback is useful immediately, but is not independent or double-read gold.
Serving results/originals records private review exposure for later case splitting.
Deliberate parser-development use is also recorded as `local-parser-development`;
it prevents later control reuse without creating an owner decision or reference.

After a parser improvement, `reparse` processes saved readings without OCR in
bounded batches. New captures are append-only generations with a predecessor
and original reading-producer identity. Only unreviewed pending cards activate
the new result; old captures and completed owner decisions remain intact. A
concurrent review prevents activation, and stale tabs receive a conflict. Repeat
the command for further pending work after a bounded batch. It emits counts only.

Before constructing an evaluation successor, supply the live private review DB
both when inheriting old cases and when freezing newly imported originals:

```sh
LEDGER="$HOME/Library/Application Support/SRO Robot/review-queue/review.sqlite3"
.venv/bin/python scripts/dev/quality/court_dataset.py --private-root "$NEXT" \
  extend --from-root "$PREVIOUS" --review-exposure-ledger "$LEDGER"
# Import additional originals and qualify case/duplicate identity, then:
.venv/bin/python scripts/dev/quality/court_dataset.py --private-root "$NEXT" \
  freeze --review-exposure-ledger "$LEDGER"
```

The bridge matches original SHA and official case aliases, propagates exposure
through existing case/page/derivative groups and marks the whole affected component
development-only. It reads only exposure identities/case metadata in a read-only
SQLite snapshot, never review comments, corrected fields or OCR. The new
manifest retains a ledger hash/count receipt
and the selected private path. Before freezing, that path is re-read even when
the flag is omitted, covering later imports and owner reviews since extend.
It never edits a previous frozen manifest or creates human reference values.
New exposure of an already frozen control requires a new generation; repeating
freeze with an already-applied exposure set leaves the frozen bytes unchanged.
Capture and score also recheck the linked ledger and refuse a control exposed
since freezing. A successor inherits that ledger and downgrades the affected
components without changing the previous frozen source.

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
