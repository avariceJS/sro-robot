# Nightly local reliability cycle — 2026-10-14

## Objective and boundaries

Work only in the isolated `codex/nightly-100pdf-20261014` worktree based on the freshly fetched `origin/demo/safe-office-demo` revision `8db1e78791cce531689a142cb6b3420835f2ac35`. No VPS, production service/database/queue, real KAD load, SRO site, or real SRO record will be touched. OCR and extracted text remain local. Test inputs use synthetic identities only.

## Schedule

- Started: 2026-10-14 00:55:30 UTC (03:55:30 Europe/Moscow)
- Deadline: 2026-10-14 07:55:30 UTC (10:55:30 Europe/Moscow)
- Stop starting large changes by: 2026-10-14 07:35:30 UTC (10:35:30 Europe/Moscow)

## Work plan

1. Reconcile baseline, inspect existing process and storage resource limits, and create a fresh worktree from remote HEAD.
2. Map the existing upload → durable storage → outbox/queue → OCR → task review path and its limits. Capture baseline throughput/latencies/resource use on the unchanged tree before edits.
3. Build a reproducible synthetic corpus of 100 distinct PDFs (text, scanned, multipage, varied quality) with expected fields and integrity manifest. Exercise the actual authenticated upload endpoint in batches and follow the real isolated queue/worker path.
4. Verify per-file task/source identity, extraction values, replay idempotency, malformed/empty/oversized rejection, and one-document failure isolation. Measure worker counts 1, 2, 4 and higher only within available CPU/memory budget; report package time, docs/minute, p50/p95, peak RSS, CPU and errors separately from intake/queue/card counts.
5. Exercise concurrency and restart/failure recovery against isolated PostgreSQL, Redis and file storage; inspect uncertain outcomes before replay. Use only local test adapters for any SRO payload preparation.
6. Add synthetic-only regression coverage and targeted implementation improvements for demonstrated gaps. Run relevant existing suites and exact reproducible load/recovery checks. Inspect deployment/helper version and service instructions without touching the VPS.
7. Update Memory Bank with verified facts and evidence, make selective commits, checkpoint status and prepare the Russian morning report.

## Acceptance criteria

- Isolated test services are demonstrably separate from any existing local services.
- 100 distinct deterministic synthetic PDFs are accepted through the app and tracked individually with source linkage; worker outcomes compared with expected data.
- Retry of the same idempotency request creates no extra tasks/cards; corrupted, empty and too-large files receive explicit rejection; a parser failure does not hide successful siblings.
- Measured 1/2/4 worker runs use identical corpus, environment, app revision and hardware; concurrency above one is attempted only with headroom.
- Restart/temporary service loss evidence distinguishes accepted, queued, completed, failed and unknown outcomes.
- KAD and SRO checks stay on synthetic adapters, with live-site claims explicitly excluded.
- Commands, versions, workload hash, concise results and remaining limits are recorded; raw streams stay outside Git.

## Initial environment snapshot

- Host: macOS 15.5, Apple Silicon arm64, 25,769,803,776 bytes RAM, 14 GiB available filesystem space.
- Original checkout: clean at `e1bc8ba57d838ec3aa23783b5cc9d830e0397fd2`, branch `demo/safe-office-demo`, one commit behind remote. No files were changed there.
- Remote baseline: `origin/demo/safe-office-demo` = `8db1e78791cce531689a142cb6b3420835f2ac35`.
- Existing Docker services observed: `sro-safe-office-pg-20261012` (127.0.0.1:53322), `sro-safe-office-redis-20261012` (127.0.0.1:53323), `deploy-db-1` (0.0.0.0:55432), `deploy-redis-1` (0.0.0.0:56379). These are not the load-test environment and will not be modified.
- Existing `scripts/dev/run_worker.py ocr` process PID 4317 is present. It will not be stopped or used by the load test.
- A dedicated worktree/branch is being created from the remote baseline. Dedicated database, Redis, storage directory and application port must be used for tests.

## Checkpoint

Update this file and `memory-bank/.now.md` after each meaningful stage with status, exact commands, evidence hashes and next action. Keep only reproducible inputs and compact summaries after successful reviewed validation; preserve raw output when a run fails or needs diagnosis.

## Isolated runtime created

- Added a dedicated Compose definition `deploy/docker-compose.nightly.yml` with pinned DB/Redis images, loopback-only ports `55442`/`56389`, and project-scoped persistent volumes. Runtime inspection verified the container bindings and separate volume names.
- A private synthetic `.env` was created only in this worktree (mode 0600) with fresh random PostgreSQL/Redis/application secrets; it is ignored and will not be committed. Do not print/read it for reporting. `SRO_UPLOAD_DIR` points at `/tmp/sro-nightly-storage-20261014` (mode 0700).
- Disposable DB `sro_nightly` reached Alembic head `c3a8e1d04b57` via `init-db`. A test-only local app user was created. Redis authenticated `PING` succeeded. No SRO/KAD endpoint was contacted.
- Exact Python 3.13.6 dependency pins installed from `requirements-bootstrap.txt`, `requirements.txt`, and `requirements-test.txt` into this worktree's `.venv`; Tesseract with `rus` and `pdftotext` are installed locally. Fixture generator may additionally use ReportLab 4.4.4, downloaded as a wheel and pinned by exact version/hash in evidence.
- Disposition of setup issue: first Redis healthcheck failed with WRONGPASS because the temporary config writer emitted a literal escaped newline. Corrected the writer to emit one config line and recreated only `sro-nightly-20261014-redis-1`; the isolated client then passed PING. No application jobs existed at the time.

## Unchanged-tree HTTP / OCR baseline

- Baseline product code: fetched remote `8db1e78791cce531689a142cb6b3420835f2ac35`; the only later commit before baseline was the isolated test-stack/documentation setup. The running web process and worker source were not changed during these measurements.
- Corpus: 100 distinct generated PDFs, 6,731,119 bytes, 60 text single-page, 10 text three-page, 10 high-quality scan, 10 medium scan, 5 low scan, 5 mixed two-page. Expected case/date/FIO/INN values are synthetic and checksum-valid. SHA-256 of deterministic `manifest.json`: `59581d8a1b46f38441c3f58edb7ce8fdba7ea97843864fba5ec5191c8f191aeb`; generator SHA-256 `a7170edacec8ea3c382eec7f79d055ac612ebf40655627210b394d1d899a6feb`; Cyrillic font SHA-256 `525979822591a3447cfc49d943d6f7683508e25543407871c0ed8fed05fd2bd9`; ReportLab 4.4.4 wheel SHA-256 `299b3b0534e7202bb94ed2ddcd7179b818dcda7de9d8518a57c85a58a1ebaadb`.
- Runtime: Python 3.13.6; Node 24.5.0; Tesseract 5.5.3 + Russian language data; Poppler `pdftotext` 25.11.0; pinned PostgreSQL 16 image digest `721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea`; pinned Redis 7 image digest `858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499`; macOS arm64, 12 logical CPUs, 25.7 GiB RAM.
- Command per point: `APP_DEBUG=1 .venv/bin/python scripts/dev/nightly/measure_http_batch.py --corpus /tmp/sro-nightly-100pdf-20261014/baseline-v2 --workers N --run-id baseline-wN-tree-20261014 --output /tmp/sro-nightly-100pdf-20261014/baseline-wN-tree.json`. For 4, 8 and 12, substitute each run ID/output listed below. Each run logged in using the synthetic app user, posted five actual HTTP multipart requests of 20 PDFs to `/upload`, used a separate registered RQ worker per process, waited for persisted review/error states, compared all four expected fields, then replayed identical chunk keys.

| OCR workers | Package seconds | Docs/min | Task latency p50 / p95 | Aggregate peak worker-tree RSS | Mean sampled CPU | Cards / OCR failures | Exact fields |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 143.64 | 41.77 | 6.20 / 141.82 s | 512 MiB | 63% | 100 / 0 | 390/400; 90/100 exact documents |
| 2 | 75.30 | 79.68 | 2.92 / 69.10 s | 1,051 MiB | 124% | 100 / 0 | 390/400; 90/100 exact documents |
| 4 | 41.89 | 143.23 | 1.48 / 35.56 s | 1,890 MiB | 234% | 100 / 0 | 390/400; 90/100 exact documents |
| 8 | 24.59 | 243.96 | 0.49 / 17.71 s | 4,183 MiB | 405% | 100 / 0 | 390/400; 90/100 exact documents |
| 12 | 23.21 | 258.50 | 0.42 / 16.23 s | 5,017 MiB | 528% | 100 / 0 | 390/400; 90/100 exact documents |

Worker-tree RSS is the sum of resident pages reported for each worker parent and OCR child; shared pages may be counted more than once, so treat it as a conservative aggregate rather than unique physical memory. Throughput gain from 8 to 12 workers was about 6%, while the aggregate RSS rose by about 20%; 8 is the measured local knee. This is an engineering recommendation for this host, not a deployment target until the server hardware is measured.

- Every run created 100 task/card rows, all reached `needs_review`, zero reached OCR `failed`, and all carried 100 distinct source hashes. Same-key replay added zero tasks. Across the corpus, 90/100 documents and 390/400 expected case/date/FIO/INN values matched. First-run mismatches were 6 INNs, 3 dates and 1 FIO; comparing case number, INN and FIO outputs to every other expected document value found zero cross-document matches. Operator review remains required.
- Replay exposed a concrete queue issue: baseline replays after completion made duplicate RQ OCR jobs despite creating no extra task rows. Read-only reconciliation found every leftover was `recognize(task_id)` for a task already in `needs_review`; these no-op jobs were drained locally without changing task states. One job interrupted during a manual drain appeared in the failed RQ registry for task 312, which was independently verified `needs_review` before removing that single terminal duplicate job. No SRO/external job type was present.
- Method corrections/dispositions: a diagnostic attempted `pypdf`, which is not installed in the pinned runtime; switched to the installed Poppler `pdftotext` for PDF text-layer verification. The first benchmark script invocation lacked the repo root on `sys.path`; corrected and rerun before timing. The first worker RSS probe omitted child OCR processes; all 1/2/4/8/12 measurements above use the corrected process-tree sampler. Raw worker streams and compact JSON summaries are in `/tmp/sro-nightly-100pdf-20261014` until final evidence review.

## Implemented after baseline

- The upload UI accepts a single selection of up to 100 files, preflights empty/count/name/per-file/total-byte constraints, and sends sequential requests of at most 20 files. The 100-file limit, 25 MiB per file, 100 MiB per batch, 105 MiB HTTP request ceiling, and request timeout/concurrency gates remain in place.
- Each batch gets a file-content/name fingerprint in tab-local session storage; retries of the same selected files after a same-tab page reload reuse the original batch key and per-chunk keys. Unknown outcomes stop the sequence and show the confirmed count. A new/changed selection gets a new key. Chunk commits remain independent, so a partially accepted batch is not one atomic DB transaction and closing the browser tab loses its session-storage resume key.
- Replays now dispatch only already-pending durable outbox intents. They do not recreate jobs after enqueue was acknowledged, even while a task still appears `new`; queue-admission failures keep their outbox intent and can be retried. Terminal-task replay also leaves the OCR queue untouched.
- Verification: `node --test tests/test_upload_batch.mjs` (7 passing); `.venv/bin/python -m unittest tests.test_settings tests.test_task_request_correlation -q` (49 passing); Python syntax and `git diff --check` passed. A real Chromium test selected 100 PDFs in one action, dropped the third chunk's response after the server accepted it, reloaded the page, reselected the same 100 files, and completed at exactly 100 source-linked cards with no duplicate rows or queued jobs.
- Same-manifest, same-host 8-worker HTTP measurement: baseline 24.59 s / 243.96 docs/min; patched upload 24.61 s / 243.81 docs/min. This change improves intake capacity/recovery and did not improve OCR throughput. Expected field comparison remained 390/400 and 90/100 exact documents.

## Concurrent intake and recovery evidence

- Two authenticated synthetic operators uploaded one 100-PDF packet each concurrently through actual local `/upload` requests (10 requests of 20 files total) with 8 OCR workers. Read-back found 200 task cards, 200 unique source items, exactly 100 source occurrences per user, zero OCR failures, 780/800 exact expected fields, 180/200 fully exact documents, and an empty queue. Package time was 43.94 s (273.10 docs/min combined); sampled worker-tree RSS peaked at 4,156.7 MiB and aggregate CPU averaged 486.9%.
- Edge run through the actual isolated HTTP route: empty PDF -> 422; 26,214,410-byte file (25 MiB + 1 byte) -> 413; one malformed PDF -> explicit `failed`; valid sibling -> `needs_review`. All three accepted tasks in the recovery repeat had source links.
- Same-key HTTP replay with no worker active held queue count at one (1 after first accepted request, still 1 after replay). Redis outage returned 503 with one durable task/outbox row; after Redis restart and the backoff interval, the same-key replay returned 303, created no second task, and dispatched one job. PostgreSQL outage returned 500; after read-back verified no new task existed, same-key retry after restart created exactly one task. Both recovered tasks finished in `needs_review` with source linkage and the queue/outbox empty.
- OCR crash drill used a manifest `scan_low` PDF and the lease-enabled worker mode. The work horse was killed after the task entered `recognizing` under a lease. The failed RQ job was read back by exact task ID, the expired lease was selected for reconciliation, and the same job was requeued after that verification. The restarted worker finished the single task in `needs_review`; audit read-back showed two claims, one recognized event and one completion. The authenticated app was also restarted separately; `/` and `/tasks/1613` remained available and the card remained in `needs_review`.
- Test-only OCR lease behavior was enabled for the isolated crash drill via process environment; the app/database did not migrate and the default remained unchanged.

## KAD and SRO adapter checks

- Commit `6650d16b4de9fb2db0b142039fe6531fb812c7f6` handles the owned KAD tab closing while its service tab remains open: the owning binding gets a terminal `stopped` state, buffered PDF state is cleared, and import admission is denied. An independent run is preserved. Focused Node tests: 51 passing; broader KAD Node suite: 119 passing; focused KAD/import/auth Python suite: 38 passing.
- The helper manifest and current docs/tests were advanced to 0.2.11 for this behavioral change; helper/backend URLs remain same-origin loopback on port 8000, with Firefox 128+ supported by the manifest. A Linux launcher and runbook now require an interactive user-owned profile and reloading the temporary add-on; no headless mode or root-owned browser profile is introduced.
- All KAD checks used synthetic browser/service doubles. No KAD browser, court site, PDF import against KAD, or server-side KAD lookup ran overnight. No current/version/service-user VPS inspection was performed because the owner explicitly restricted the night to local testing while VPS payment expires.
- Runbook: `memory-bank/71-nightly-deploy-rollback.md`. The server's OS, system user, display session, helper version, ports and private file ownership remain unknown until an authorized morning read-only inspection.
- SRO mutation guards and prepare-only route/runner tests passed in the Python test set. No SRO adapter was connected; no SRO record, Save or Send action occurred.

## Current checkpoint (2026-10-14 02:02 UTC)

- Selective commits: `76f7b7b` nightly plan, `5ffe3de` isolated services, `6650d16` KAD owned-tab close handling, `ceeeb18` bounded 100-file upload and outbox replay fix, `80d3114` complete worker test fixtures, `58916fe` helper version `0.2.11`, and `0cb86a3` synthetic SRO preparation adapter test. No branch push or merge occurred.
- Isolated stack is still running on `127.0.0.1:55442` PostgreSQL and `127.0.0.1:56389` Redis; app is on `127.0.0.1:18081`; OCR workers used by tests have been stopped. The separate 18080 test app and original local worker PID 4317 were not used for concurrent work after initial measurements. The original checkout is untouched.
- A test helper initially omitted the required `Origin` header. Its requests were rejected by the same-origin guard before reaching `/upload`; the helper was corrected, then empty/oversized and corrupt-sibling checks passed with the expected 422/413/failed states. An early concurrent-measurement script used a nonexistent `Task.user_id` field and exited after acceptance; the accepted tasks were reconciled by starting isolated workers, draining all 62 pending jobs and confirming empty registries before the corrected rerun.
- A second helper initially waited for two task rows after it had created three. The check was corrected from repository read-back; the repeated run passed. A PDF validation diagnostic used optional `pypdf`, absent from pinned dependencies; switched to Poppler `pdftotext`. No failed scenario was converted to success without read-back.
- Consolidated verification rerun on the committed tree: `.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -q` — 536 passed, 88 skipped; Node KAD/helper/upload batch suite — 126 passed, 0 failed; `.venv/bin/python -m py_compile scripts/dev/nightly/*.py tests/test_sro_prepare_adapter.py`, `bash -n scripts/ops/launch-kad-firefox.sh`, and `git diff --check` passed. Test logs are in `/tmp/sro-nightly-100pdf-20261014/final-validation-python.log` and `final-validation-node.log`; expected readiness/failure logger messages are from negative-path tests.
- Two same-host 8-worker HTTP runs on the current implementation took 24.60 s (243.93 docs/min) and 25.26 s (237.50 docs/min). Their p95s were 17.44–18.67 s and aggregate worker-tree RSS 3,807–4,072 MiB; both created 100 source-linked `needs_review` tasks, 0 failed, matched 390/400 fields and 90/100 documents, and identical replay added zero tasks. The second run broke accuracy down to case 100/100, date 97/100, FIO 99/100 and INN 94/100. Baseline was 24.59 s / 243.96 docs/min; these runs do not show an OCR throughput gain. The improvement is bounded single-selection intake and retry safety.
- The synthetic SRO adapter test passes with the SRO guard/read-back suite: it checks card field mapping and `Close` while its `click_save` fails immediately if called. No browser or network was started.
- Compact current evidence is `/tmp/sro-nightly-100pdf-20261014/nightly-summary-20261014.json` (SHA-256 `cac5e750b6954edfd3b5e6082fb914a97f3feebbe35343da8bf9723d72d833cf`) with 22 input/script/result hashes in `artifact-sha256-20261014.txt` (SHA-256 `fd51f5a195b6b47bdd5cc79bab657946068b4984bbae1aa096929dea3fc49aee`). Raw streams and unreviewed scratch artifacts remain isolated under `/tmp` pending final evidence pruning.
- Next: finish selective KAD/version and test-tool/runbook commits, review exact diff/status and checksums, preserve the isolated DB evidence, and continue bounded reliability work. Do not start new large changes after 07:35:30 UTC.

## KAD owned-tab loss check

- Read-only audit of the `8db1e787` KAD helper found that service-tab closure was covered, but externally closing the owned KAD tab while leaving its service page open had no explicit terminal state. The old removal listener cleared the KAD tab's session key, while the service binding remained and could poll until the generic deadline.
- Updated the helper bridge to persist a terminal reason against only the owning service binding when its KAD tab is removed, discard that tab's buffered PDF/job state, and return `stopped` to the still-open service page. The bridge also checks that the owned tab still exists immediately before local-import admission, closing the ready-to-import gap.
- Added synthetic tests for KAD-tab close with a second independent service run present, refusal of `bridge-importing` after closure, and service behavior that reports the reason and sends no `/kad-helper/import` POST.
- Focused command: `node --test tests/test_kad_service_bridge.mjs tests/test_kad_service_content.mjs tests/test_kad_helper_protocol.mjs` — 51 passing, 0 failing. No browser site, KAD, SRO, or app import route was contacted.
