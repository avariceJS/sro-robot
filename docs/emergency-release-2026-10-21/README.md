# Emergency release and local demonstration — 2026-10-21

## Accepted product source

`e0529b1f1102437b9317bf1ba039844bb71af619` is published on `develop`.
The exact clean source passed:

```sh
SRO_DRILL_TASKS=100000 MIGRATION_DATABASE_URL='' bash scripts/dev/verify.sh
```

The compact results and full-stream hash are in `verification.json`. The gate
covered 983 Python cases (172 PostgreSQL cases skipped in the initial pass and
then executed in the dedicated drills), 162 Node cases, synthetic Chromium,
runtime roles, migration/adoption/refusal, recovery/concurrency, 100,000 synthetic
tasks/events and 100,001 restored document files. These are synthetic engineering
checks, not representative office accuracy, staff acceptance or a production SLA.

Pillow is hash-locked at 13.0.0 and the audit environment's certifi at 2026.10.19.
Fresh installed-environment and source-lock audits found no known vulnerabilities.
`npm audit` also returned zero. The Tailwind/Flowbite stylesheet was rebuilt
reproducibly; three generated SVGs are served locally under the existing CSP.
There are no new Flowbite JavaScript components, external fonts/CDNs or paid blocks.

## Production decision

The owner authorized release conditionally. Production HTTPS `/version` still
reports `2827cd232ff6cc4287a6b880c2d310e21756e073` (`sro-v1.0.0`) and `/readyz`
is healthy. `main`, release tags and VPS application/data state were not changed.

GitHub Free's 2,000-minute quota is exhausted according to the owner. Provider
annotations independently report payment/spending refusal before job steps.
Local success is not GitHub-hosted acceptance. No billing or workflow change was
made to work around the account limit. `docs/RELEASE_WORKFLOW.md` explicitly
supports the checked-in local deploy script while Actions is unavailable.

Four SSH attempts to the verified host failed during banner exchange through
the current `utun6` route. The dedicated identity passed its local safety check.
The existing Aeza VNC console was reachable, but an authenticated shell was not
confirmed. Its credential-entry helper was unreliable; further entry stopped.
The owner was advised to rotate that password manually. No password is retained
in these records. No access reset, firewall change or service restart was made.

Fresh production backup/restore, the 17-migration rehearsal, data-integrity
readback and rollback proof remain unperformed. A private clone-rehearsal helper
is prepared in `.local-run/emergency-release-20261021/`; only its three helper
tests passed. It has **not** been qualified end to end on the VPS. Do not promote
or deploy until current recovery and exact-candidate gates are complete.

## Demonstration handoff

The isolated application is running at `http://127.0.0.1:64586`. Open
`.local-run/emergency-demo-20261021/Open-Demo.command` to use the prepared,
dedicated Chrome for Testing profile. `SHOW-RU.txt` provides the owner walkthrough.
The folder is ignored and private; never publish its config, cookie, dump,
browser profile or uploads. Its named-volume restore, app/OCR restart and
idempotent launcher were exercised. No generic SRO or preview worker was started.

The actual local UI chain is PNG task 3 → reviewed internal card 2 → assignment
draft 2 → explicitly curated case package 2 (`A99-00003/2099`). Membership was
read back from the database and fresh browser after removing unintended demo
memberships. Two distinct, unprocessed synthetic PNGs in `corpus/spare/` are ready
for a fresh upload during the presentation. The source image is visibly readable
beside OCR. All demonstration traffic was restricted to loopback HTTP.

The shared navigation and three worklists have the new Flowbite design. Detail
forms, intake and OCR content retain their previous inner layout within that
shell, with the review overflow corrected. Native PDF preview appears blank in
the checked Chrome environment; original downloads work. CSP was not weakened.
Internal cards/assignments do not create SROArbitr records. External writes and
unqualified acquisition/preview actions remain unavailable. No real SROArbitr
record or production data was used by the demonstration.
