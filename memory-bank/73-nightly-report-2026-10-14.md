# Nightly reliability report — 2026-10-14

## Scope and status

This seven-hour run is local-only, started at 00:55:30 UTC and is due to close at 07:55:30 UTC. The scheduled stop for starting large changes is 07:35:30 UTC. No VPS, live KAD, SROArbitr, or real SRO record was contacted or changed. The worktree is `/Users/root1/Desktop/sro-robot-nightly`, branch `codex/nightly-100pdf-20261014`, based on `origin/demo/safe-office-demo` at `8db1e78791cce531689a142cb6b3420835f2ac35`; it is 83 commits ahead at this report checkpoint, unpushed. The original checkout remains clean on `e1bc8ba`, with its existing app on port 18080 and worker PID 4317 untouched. Nightly app PID 70669 answers `/readyz` on loopback port 18081; test PostgreSQL and Redis listen on `127.0.0.1:55442` and `127.0.0.1:56389`. Changes are local, unpushed, unmerged and require owner review. No database migration was added.

## Implemented

- One 100-file UI selection is sent in sequential 20-file chunks, retaining existing 25 MiB/file, 100 MiB/batch, request-size, timeout and concurrency limits. Per-chunk idempotency keys survive reload/tab closure. Upload is stopped before HTTP if persistent key state cannot be written and read back.
- Fixed FIO extraction for capitalized `Должник:` labels while preserving strict title-case rules for person-name tokens.
- Scanned PDF OCR uses the locally measured cutoff 215 with the legacy fallback. A 225 candidate helped two corpora and degraded another, so it was not adopted. Every extraction still requires human review.
- Repaired lost acknowledged OCR-job replay, added queue-depth, active-OCR replay-race and launcher-guard verification, and reduced task-status polling to one batch refresh that stops after terminal state.

## Main 100-PDF result

The primary corpus contains 100 distinct synthetic PDFs: 60 text-layer, 10 multi-page, 10 high-quality scans, 10 medium scans, 5 low-quality scans and 5 mixed documents. Manifest SHA-256: `59581d8a1b46f38441c3f58edb7ce8fdba7ea97843864fba5ec5191c8f191aeb`.

At exact current HEAD `cb6f805` with 8 workers, real local HTTP intake accepted 100 files in five chunks, queued 100 jobs, created 100 source-linked local cards, and processed all 100 to `needs_review`; there were zero OCR failures. Expected-field readback matched 98/100 complete records and 398/400 fields (case 100, date 99, FIO 100, INN 99). Same-key replay added zero tasks. Queue and registries drained. This is synthetic review-card evidence; it does not represent saved SRO records.

The independent actual-browser test selected the 100 PDFs once, accepted the third chunk while its response was deliberately dropped, closed the tab, reselected the same files, and completed 100 linked cards with no duplicates. The 100-job queue-depth test read back exactly 100 queued jobs, 100 new DB rows and 100 source links before starting OCR workers; eight workers then produced 100 review cards. Queue count, cards and worker count are reported separately. A read-only audit of the exact-current-head 100-file run confirmed 100 distinct source hashes, the expected item indexes 0–19 in each of five operations, all cards in review state, 98/100 exact records /398/400 fields, and zero expected-value matches across different documents.

## Speed and capacity

Same Mac (macOS 26.5.2, arm64, 12 logical CPUs, 25.7 GiB RAM), same primary corpus, 8 workers:

| Run | Package time | Docs/min | p50 | p95 | Peak worker-tree RSS | Sampled worker CPU | OCR errors | Exact fields | Exact docs |
|---|---:|---:|---:|---:|---:|---:|---:|
| Unchanged-tree baseline | 24.594 s | 243.96 | 0.487 s | 17.706 s | 4,182.73 MiB | 405.38% | 0 | 390/400 | 90/100 |
| Exact-current-HEAD cutoff 215 | 10.850 s | 552.99 | 0.509 s | 6.918 s | 2,605.67 MiB | 272.67% | 0 | 398/400 | 98/100 |

The current exact-head run measured about 2.27x baseline throughput and 37.7% lower sampled worker-tree RSS. Current `/tmp` free space is 14 GiB on a 460 GiB volume at 97% usage; no more large corpora are planned unless a new hypothesis requires them. It is one exact-head sample; earlier cutoff-215 repeats are recorded in the plan.

A prior optimized scan sweep measured 1/2/4/8/12 workers at cutoff 185: 62.15/32.92/18.28/10.70/11.11 s and 329/654/1,305/2,609/3,595 MiB. Twelve workers were slower than eight and used about 38% more RSS. Eight is the measured local ceiling, not a VPS recommendation. A 20-operator burst with 8 workers processed 2,000 PDFs in 188.55 s (636.44 docs/min), 2,000 source-linked cards, 0 failures and 2,220.98 MiB sampled peak RSS. Authentication sessions were prepared sequentially to preserve the login flood guard; a separate 20-simultaneous-fresh-login attempt returned 12 HTTP 503 responses and does not qualify login-burst capacity.

## Recovery and input boundaries

- Concurrent same-key requests returned 200/200 and produced one task/source/job. Five strict replay-during-OCR runs issued 80 same-key replays; each run produced one card, one recognition event and one completed OCR job.
- Deleted acknowledged queue-job recovery restored one deterministic OCR job after same-key replay without adding a card. Redis outage/outbox retry, PostgreSQL outage/retry, app restart persistence and worker termination during OCR/reclaim were exercised on isolated local services. No external operation was involved.
- Empty file returned HTTP 422; 25 MiB + 10 bytes returned 413. In a mixed valid/corrupt batch, the corrupt PDF received an explicit `failed` status and the valid sibling reached `needs_review`. A separate 100-PDF failure-isolation run retained 99 valid cards when one document was corrupted.
- The final read-only local `ops-status` after all checks showed 11,188 synthetic `needs_review` tasks and five expected corrupted-input `failed` tasks; no `new`/`recognizing` tasks, lease gaps, outbox entries, queued/started/deferred/scheduled/failed jobs or workers. These aggregate local DB totals are distinct from each per-run package result.

## KAD, SRO and deployment boundaries

- KAD helper/browser and intake tests used synthetic scenarios for exact case/date selection, candidate ambiguity, cancellation, timeouts, lost-tab separation and retry receipt behavior. No live KAD search or real KAD PDF acquisition occurred.
- SRO field preparation was exercised only on the synthetic test adapter with Save/Send blocked. No real SRO record was created, updated or saved.
- A Firefox launcher and runbook are prepared; launcher guard tests use stubbed commands. Server OS/user, ports, service manager, installed browser/helper versions, desktop session and private file ownership remain unknown because the VPS was explicitly excluded.
- Update and rollback instructions are in [`71-nightly-deploy-rollback.md`](71-nightly-deploy-rollback.md). Inspect the branch and deployment environment in the morning before applying it; no push or deployment is part of this run.

## Checks and commits

- Latest full Python suite after the FIO parser fix: 555 passed, 88 skipped. Combined KAD/helper/upload Node suite: 134 passed. PostgreSQL lease integration: 22 passed earlier on its pinned disposable database.
- Current evidence manifest: 451 entries, all verified. `/tmp/sro-nightly-100pdf-20261014/artifact-sha256-20261014-final.txt`, SHA-256 `f8309241c8f0f6245c4230b2b55af1e0a823a96f223ac576a598ebf67af9383a`. It includes the read-only batch isolation summary `/tmp/sro-nightly-100pdf-20261014/final-head-batch-isolation.json` (SHA-256 `8235343c4d768f07326b81cab4040545e299c9bdf5b77871cf735b49278fe8a4`).
- The final manifest excludes mutable checkpoint/report documents. A prior check found two stale documentation hashes; the failure and disposition are preserved in `/tmp/sro-nightly-100pdf-20261014/artifact-manifest-doc-hash-failure.log` (SHA-256 `5c6fb0dec231fa202cfa00e1cd82f61fee06d06236f1315c877873f057098e89`); the corrected 451-entry manifest passed. Validation output streams from successful checks were pruned only after recording hashes; `/tmp/sro-nightly-100pdf-20261014/pruned-nightly-streams-20261014.json` records 75 streams. Failed/incomplete diagnostics remain.
- Key commits: `f445f92` scanned-PDF threshold tuning; `7be451b` 100-job queue-depth check; `de4daf6` replay during active OCR; `f50ebbc` durable upload retry state; `7c8797c` capitalized debtor label parsing; `1d55f10` threshold tradeoff evidence; `0a2a0b7` deployment behavior note; `4001f42` exact-head throughput evidence; `cb6f805` current checkpoint. The complete local-only commit list is available with `git log --oneline origin/demo/safe-office-demo..HEAD`.

Local synthetic evidence does not establish production readiness, real-document accuracy, live KAD behavior, server capacity or saved SRO records.

## Safe stop (2026-10-14 06:56 UTC)

- No load test, upload, OCR task or external operation was in flight. Read-only local `ops-status` exited 0: 11,188 synthetic `needs_review`, five expected corrupted-PDF failures, no nonterminal tasks, lease gaps, pending outbox, queue jobs, RQ registry entries or registered workers.
- Nightly app PID 70669 on loopback port 18081 was stopped with SIGTERM and exited. Its isolated PostgreSQL and Redis containers remain running with test data preserved. The original checkout app (PID 1886, port 18080) and worker (PID 4317) were not changed; original readiness remained HTTP 200.
- Resume from worktree `/Users/root1/Desktop/sro-robot-nightly`, branch `codex/nightly-100pdf-20261014`. Safe-stop checkpoint and remaining tasks are in `memory-bank/.now.md`. No real server, SRO record or external service was changed.

## Resumed final evidence audit (2026-10-14 06:57 UTC)

- Rechecked the committed safe-stop state: branch `codex/nightly-100pdf-20261014`, HEAD `d9e14bdb9085a4c9588f091c36a6c0192695e8f8`, clean and 85 commits ahead of the base.
- Re-verified the pinned artifact manifest: 451/451 entries passed, zero failures; manifest SHA-256 `f8309241c8f0f6245c4230b2b55af1e0a823a96f223ac576a598ebf67af9383a`.
- Since the exact-code measurement revision `cb6f805`, only the plan, report and `.now.md` documentation changed. Existing test results therefore still cover the current code. No repeated workload or external action was started.
- Inspected the synthetic KAD scenario tests for identity/date selection, ambiguity, cancellation, timeouts, lost-tab handling and concurrent run isolation. Live KAD and server-side GUI/helper ownership remain unverified.
