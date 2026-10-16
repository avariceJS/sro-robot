# Bulk processing validation — 2026-10-16

## Scope and source identity

Implemented in the canonical checkout on `develop`; core implementation
`15138ee9403ca116b9a4f46c13079c57cf9cce32`, optional-empty-field UI correction
`06447ff`, and bounded OCR-formatting correction `20541c6`. The original baseline
is `8c46c9ac9b8a8009246b92f43bbad2b45142de4d`. The UI correction coalesces missing
optional card fields to empty text instead of the literal `None` and has unit
and real-browser regression coverage.

No main change, VPS deployment, real document, real SRO account, external record
write, old held-job replay or production endpoint was used. Local OCR/extraction
remain local. Internal cards and task approval are separate from SRO actions.

The benchmark ran from a pinned baseline archive, an intermediate processing
tree and final commit `20541c65a8260f05527f92436d7310cc070b13d7`.
`bulk-benchmark-2026-10-16/source-identity.json` resolves the original abbreviated
labels and verifies processing hashes against the relevant commits. Final
measurements have `20541` in their filenames; earlier compact measurements remain
explicit intermediate evidence. Baseline archived files were independently
compared byte-for-byte with Git objects.

## Resulting workflow

- One UI selection accepts up to 100 files, in bounded 20-file requests. Durable
  operation acknowledgements join the chunks into a shareable intake view.
  Exact ordered reselection in the same browser retains completed retry keys.
- The dashboard counts the complete selected scope, filters/paginates rows and
  polls until active work finishes. Source files, card state, possible duplicates
  and review actions are directly accessible to all active employees.
- Fenced OCR prepares an internal person card only with explicit role-bound
  identity, complete name, valid personal INN, case/date and no material field
  ambiguity. It never auto-approves a task. Other work stays in review.
- Labeled personal fields can continue across bounded OCR blank lines. Automatic
  use additionally requires the same field value in a directly bound, non-blank
  debtor section. A short split city fragment can join its street/house line;
  new roles, other people and missing/conflicting OCR values remain review work.
  Mentioned but unparsed addresses produce a warning, rather than silently
  disappearing. These changes improve staff review without inventing role proof.
- INN locking and database uniqueness prevent racing card insertions; another
  source with the same INN stays a possible duplicate without an automatic merge.
- One employee action saves corrections, approves the task and prepares the
  internal card. Stale forms are refused. Duplicate conflicts retain employee
  corrections. Reviewed cards cannot be silently replaced by changed source data.
- Card edits use row locks and versions; append-only snapshots retain the actor.
  Schema readiness checks include the new revision guards. Migration
  `f4b7c2d091a6` preserves existing reviewed cards and refuses destructive downgrade.
- Original-file downloads verify private storage, size, inode and hash using one
  descriptor; responses are authenticated attachments with no-store/sandbox.

## Throughput and quality

Measured locally on a 12-core, 24 GiB macOS host, Python 3.13, PostgreSQL 16,
Redis 7, Tesseract 5.5.3 and Poppler 25.11.0. Cohorts contain equal numbers of
text-layer PDFs and scans from a deterministic, hashed synthetic corpus. Worker
registration precedes timing; measurements include HTTP intake through terminal
OCR persistence and automatic internal-card preparation, excluding employee
review time. Final after cohorts use fresh databases/Redis logical databases.
The original baseline matrix reused an isolated database between runs; stored
bytes and prior task history may affect direct before/after timings. These are
single local observations, not statistical confidence intervals or a server SLA.

| Documents | Before, 1 worker (s) | Before, 4 workers (s) | After, 4 workers (s) | After docs/min | Auto cards / manual review / failed |
| --- | --- | --- | --- | --- | --- |
| 10 | 12.32 | 5.12 | 5.14 | 116.7 | 5 / 5 / 0 |
| 20 | 24.31 | 7.58 | 7.66 | 156.6 | 10 / 10 / 0 |
| 50 | 59.67 | 17.59 | 17.96 | 167.0 | 25 / 25 / 0 |

The current 50/1 pipeline took 60.08 s; current 50/4 took 17.96 s, a measured
3.34x gain from concurrency. Before 50/1 took 59.67 s; before 50/4 took 17.59 s.
Same-count observations were slower after the safety/card workflow (0.7% at one
worker, 2.1% at four); no algorithmic speedup or causal overhead estimate is
claimed. Four is the best tested local configuration among 1/2/4, not a proven
global optimum. The launcher defaults to one and permits only OCR pools of up to
four with durable leases. Final 50/4 sampled peak combined worker RSS was 1,474
MiB; task read query mean/p95 were approximately 8.46/12.42 ms. Intake took
0.87 s and the remaining queue/OCR/persistence/card completion took 17.09 s.

Every automatically prepared card in the final cohorts matched all five expected
fields (name, INN, address, case/date); 25/25 matched in the 50-document run.
Across all 50 tasks: name/case matched 50/50, date 49/50 (one wrong value in review),
and INN 34/50 (16 withheld, zero wrong nonempty INNs). All scanned documents in
this final cohort still needed employee review. This conservative synthetic
result is not office-document accuracy qualification or calibrated confidence.
Compared with the intermediate parser, ten more scan INNs were withheld. Two
same-OCR-variant comparisons confirmed newly visible malformed tokens taking the
existing fail-closed path; they are not conflicting valid identities. The other
eight were not individually diagnosed. This review-field recall loss, a possible
future manual-only candidate display and the exact cases are recorded in
`bulk-benchmark-2026-10-16/inn-recall-diagnostic-20541c6.md`. The task-quality audit
retains `result=findings` for the review-only date mismatch; it is not relabeled
as a passing accuracy qualification.

Reducing scans to 200 DPI shortened a separate ten-scan experiment from 40.73 s
to 32.37 s but reduced exact INNs from 10/10 to 8/10. The change was rejected;
product OCR remains at 300 DPI and its existing multiple-pass strategy.

An isolated component profile used two text PDFs and one scan of each quality.
The three scans spent 5.884 s in PDF/OCR on the original source and 5.896 s on
the final source; parsing spent 3.426/3.462 ms respectively. The final two text
PDFs spent 21.67 ms in text-layer extraction and 19.55 ms in parsing, including
the first parser initialization (the second parse took 0.80 ms). These tiny,
single-run samples identify scan OCR as the dominant local component; they do
not establish a speedup. Component profiles exclude queue/DB/card writes; the
HTTP cohort timings include durable persistence and card preparation, while
the separate database metric measures reads only.

## Verification

The complete processing gate passed:

```sh
MIGRATION_DATABASE_URL='' bash scripts/dev/verify.sh
```

The final `20541c6` run passed 689 discovered Python tests (97 database-only skips in discovery), then
all 97 PostgreSQL cases on disposable services, 137 Node tests, compilation,
role/schema checks, queue-loss and process recovery, 10,000 task/event identities
with 20 synthetic staff, 10,001 private-file checks, populated-schema adoption,
forward-only downgrade refusal and two 1,000-task claim probes with zero
unclaimed work. The optional-empty-field regression is included; the browser
also verifies before/after form values.
Nine new PostgreSQL tests cover automatic card/no external intent, same-INN races,
stale edits, immutable attributed revisions, scope/pagination/polling, and safe
combined review/duplicate handling. Raw gate hashes and resolved failure notes
are in `bulk-benchmark-2026-10-16/validation-streams.json`.

Twenty simultaneous already-authenticated synthetic employees uploaded two files
each: 20/20 HTTP 200, 40 source-linked terminal tasks, no failures, 20 exact
prepared cards, 13.29 s (180.6 docs/min). Read-only audit readback found 40 intake
creation events from 20 actors, no actor/source mismatch and no missing/invalid
request IDs. Sessions were prepared before timing; fresh-login bursts and live
SRO identity provisioning were not exercised. Two-employee edit conflicts and
shared access have separate PostgreSQL/HTTP regression coverage.

Real Chromium browser evidence exercises file selection, dashboard/filter links,
source download/hash, direct card correction/revisions, empty optional fields,
one-action manual review, corrupt-file isolation and same-selection replay from
a new page. The final browser selection contained 50 valid mixed PDFs plus one truncated
PDF: three upload chunks, 51 tasks, 25 automatic cards, 25 valid review tasks and
one isolated failure. Pagination showed 50+1 rows. Exact replay from a new page
kept 51 tasks / 3 operations / 25 cards. Details are in
`bulk-benchmark-2026-10-16/ui-e2e-bulk-20541.json`; no API-only substitution is used.
Synthetic screenshots were visually reviewed locally.

Three archive-based runtime post-patch validations cover wrong-role identifiers,
OCR identity conflicts, declined names, address overcapture and OCR-formatting
breaks plus independent variants/benign behavior/regressions/security. All final
reports are complete with no supplied-check findings or gaps. The archive adapter respects the
no-extra-worktree policy; reviewed streams were hash-verified and pruned. Source
review separately identified the RQ fork-socket issue, which was reproduced by
actual workers and fixed with parent engine disposal before fork. Partial-start
cleanup only signals/reaps owned child processes, with a bounded grace period;
actual four-worker registration and normal shutdown were verified.
The evidence retains `human_review_required=true`; independent agent reviews
and passing supplied checks are not human acceptance or production qualification.

## Limits and next work

- This mission does not deploy anything. Production remains on the consolidated
  main release; do not expect these changes on the VPS until an authorized release.
- The parser is a conservative heuristic for named physical-person debtors, not
  general entity resolution. Scans/ambiguous layouts still need staff review;
  organization/IP cards and calibrated confidence remain future work.
- Mixed PDFs and long/multipage documents lack complete page-aware provenance
  and workload qualification. The measured local worker count is not a VPS limit.
- Identical browser retries are idempotent; a new browser, renamed/reordered
  inputs or another employee can create another task. Shared INN conflicts are
  reviewed, not automatically merged. Explicit reprocessing and cross-source
  business identity need a separate durable contract.
- A successful local test suite does not establish production readiness. GitHub
  Actions execution and a production load/SLA were not claimed.

Next: build an annotated, locally retained office corpus and measure extraction
and review burden, then qualify page-aware OCR and a production-sized staging
worker profile before an owner-authorized rollout. User steps and supported
local launch commands are in `BULK_PROCESSING.md`; reproducible inputs, scripts,
metrics and artifact dispositions are under `bulk-benchmark-2026-10-16/`.
