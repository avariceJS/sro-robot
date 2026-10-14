# Differential review: 2026-10-14 nightly batch work

## Scope and disposition

- Reviewed changes from `8db1e78791cce531689a142cb6b3420835f2ac35` (the remote demo tree) through `3777457fce279d63cfd7802738b70e8a21f9a26a`, plus the current uncommitted recovery change. The original checkout and remote branch were not modified.
- The review concentrated on upload intake/idempotency, outbox delivery and recovery, OCR queue claims, and the KAD helper's owned-tab lifecycle. The worktree contains 229 changed paths relative to the baseline; unrelated legacy changes were not each audited line-by-line.
- Disposition: **conditional approval for continued local validation only**. The batch intake result is useful as a reviewed development change, not a production-readiness claim. No live SRO/KAD operation or real record was part of this review.

## Finding: acknowledged OCR queue loss could strand an accepted task

- Severity: **Medium reliability / data-integrity risk**, conditional on Redis losing a job after its enqueue acknowledgement and before OCR claims the task.
- Before this fix, `pending_intake_intents` only returned an extant durable outbox row. Successful dispatch deletes that row. If Redis then lost the queued job while PostgreSQL still held `Task.status = new`, same-key upload replay found neither a retry intent nor work in the queue; the accepted task could remain `new` indefinitely. The issue was reproduced on the isolated app: first request returned 200, task 1919 was `new`, outbox and queue were empty; replay returned 200 but left the queue empty.
- The baseline behavior was introduced in `ceeeb18` (`app/api/routes_tasks.py`, upload replay branch) and the initial 100-file work in the same commit. `git log -S 'pending_intake_intents'` identifies that change. The current diff adds reconciliation in `app/services/outbox.py:96` and `app/workers/queues.py:44`.
- The replay path now checks tasks still `new`; it avoids creating a second outbox intent when an OCR job for that task is present in queued, started, scheduled, or deferred RQ state. If no job is present, it recreates the deterministic outbox intent. Queue-read uncertainty becomes `QueueUnavailable`; it does not assume loss or dispatch blindly. Terminal task states are not requeued.
- Reproduction and retest: `scripts/dev/nightly/verify_concurrent_idempotency.py` now deletes exactly the synthetic queued RQ job after acknowledgement, replays the same request, and checks that there is still one task, exactly one restored queue job, final `needs_review`, and an empty queue after one worker. Successful run: `/tmp/sro-nightly-100pdf-20261014/idempotency-recovery-pass-workers/`; the task stranded by the initial reproduction was separately reconciled and read back as `needs_review`.
- Added focused unit cases in `tests/test_outbox_intake_replay.py` and `tests/test_queues.py` for pending intents, existing active delivery, missing delivery, terminal tasks, and fail-closed Redis reads.

## Other reviewed boundaries

- Upload count and byte ceilings remain enforced at the UI and HTTP layers; empty, corrupt, oversized, and valid sibling behavior was exercised on the isolated app. Same-key upload admission remains backed by durable PostgreSQL operation/source/task identity.
- OCR results remain tied to the task/document source and protected by the existing lease/fencing logic. The separate pinned PostgreSQL lease drill previously passed 22 tests, including the concurrent duplicate-claim and late-result cases.
- KAD owned-tab removal now sets a terminal stop for that binding, discards its buffered transfer state, and blocks import admission. Synthetic tests preserve an independent run. The helper still sends the original selected document through the local import path; no live KAD interaction occurred in this nightly cycle.
- SRO adapter behavior remained test-only. The synthetic adapter checks prepared field mapping and close-without-save; guards still reject Save/Send. There was no live SRO client or network activity.

## Separately measured local OCR optimization

- After the queue review, scanned PDFs without a useful text layer were changed to a grayscale/185-threshold pass over the existing PSM modes. If that pass yields no text, the previous upscale/sharpen route runs as a fallback. Text-layer PDFs and image/photo uploads retain their prior routes.
- This is an accuracy/performance change, not part of the queue-loss security finding. On the 25 synthetic scan PDFs, the legacy path took 126.547 s and matched 15/25 complete records (90/100 expected fields); the new path took 49.705 s and matched 22/25 (97/100 fields). The end-to-end 100-PDF HTTP test was repeated at 8 workers three times: 10.603–10.877 s, 551.63–565.86 documents/min, p95 6.876–6.915 s and peak worker-tree RSS 2,604–2,617 MiB. Each run created 100 source-linked local cards, zero failures and matched 97/100 complete expected field sets (397/400 fields). No OCR text is written to measurement output.
- This optimization was tested only on the generated synthetic corpus and local hardware. It does not establish accuracy for every real PDF; operator review remains required, and no cloud OCR or external service was used.

## Blast radius and remaining risk

- `pending_intake_intents` has one production caller (`app/api/routes_tasks.py:209`) in the same-key upload replay path. `active_ocr_task_ids` is called only by that reconciliation function. `enqueue_recognize` remains dispatched through the durable outbox.
- A same-key replay with one or more unbacked `new` tasks scans current OCR queue and active registry IDs, then fetches the referenced RQ jobs. This work is proportional to queue depth and can add replay latency for a very large queue. It is not on the initial upload path. Measure this at the intended deployed queue depth before adopting the replay reconciliation in production; a durable queue-delivery receipt or deterministic RQ job identity could avoid a full scan later.
- Local synthetic lookup measurement (seven samples per depth, no worker): 100 jobs median 4.719 ms / p95 9.227 ms; 1,000 jobs 31.364 / 56.696 ms; 5,000 jobs 162.563 / 184.781 ms. The repeatable script refuses non-loopback Redis and nonempty starting queues. Result JSON is in `/tmp/sro-nightly-100pdf-20261014/replay-queue-depth.json`.
- There is a narrow check-to-enqueue race if two simultaneous replays both observe a missing job. The database's unique outbox dedupe key serializes the intent; workers' existing task claim/lease fencing prevents duplicate OCR results. The synthetic same-key concurrency scenario and PG lease drill cover the adjacent controls, but do not prove every Redis/network partition schedule.
- This review did not model malicious authenticated users, compromised browser sessions, production traffic, live KAD templates, a production queue backlog, or operating-system/browser ownership on the server. The server is intentionally outside the local-only scope.

## Validation evidence

- `.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -q`: **542 passed, 88 skipped**. Skips are opt-in external-service integration tests unless a disposable marker is set.
- `node --test` for the six KAD helper suites and `tests/test_upload_batch.mjs`: **126 passed, 0 failed**.
- The concurrent idempotency recovery helper passed against the local isolated PostgreSQL/Redis/app stack, including synthetic RQ deletion and replay recovery.
- PostgreSQL lease integration: 22 tests had passed earlier on the pinned disposable restore-drill container. A later rerun in this audit was blocked by missing disposable-drill environment identity/password setup and failed closed before test setup; the safety marker was not bypassed. No production database was contacted.
- `py_compile`, shell syntax check, and `git diff --check` passed for the changed Python/shell files at review time.
- All HTTP, OCR, and SRO adapter traffic was local and synthetic. No cloud extraction, VPS, live KAD, or live SRO checks were performed.

## Provenance

- Parent baseline: `8db1e78791cce531689a142cb6b3420835f2ac35`.
- Review-time committed HEAD: `3777457fce279d63cfd7802738b70e8a21f9a26a`.
- Recovery fix was uncommitted when this report was drafted; the commit ID and final evidence hashes will be recorded in the nightly checkpoint.
