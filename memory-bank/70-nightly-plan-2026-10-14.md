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
4. Verify per-file task/source identity, extraction values, replay idempotency, malformed/empty/oversized rejection, and one-document failure isolation. Measure worker counts 1, 2, 4 only within available CPU/memory budget; report package time, docs/minute, p50/p95, peak RSS, CPU and errors separately from intake/queue/card counts.
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
