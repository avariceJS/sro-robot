# Synthetic bulk benchmark, 2026-10-16

These artifacts compare the exact archived baseline at `8c46c9ac9b8a8009246b92f43bbad2b45142de4d` with pinned final processing commit `20541c65a8260f05527f92436d7310cc070b13d7`. All inputs are generated PDFs. PostgreSQL, Redis, and the app listened only on loopback; the app used an explicit empty environment file and signed persisted synthetic sessions. No SROArbitr or production endpoint was contacted.

The 100-file corpus contains 60 text PDFs, 10 multipage text PDFs, 10 high-quality scans, 10 medium scans, 5 low-quality scans, and 5 mixed text/scan PDFs. Its manifest SHA-256 is `59581d8a1b46f38441c3f58edb7ce8fdba7ea97843864fba5ec5191c8f191aeb`; the generator hash is `a7170edacec8ea3c382eec7f79d055ac612ebf40655627210b394d1d899a6feb`; the Arial font hash is `525979822591a3447cfc49d943d6f7683508e25543407871c0ed8fed05fd2bd9`. Each cohort uses the same corpus selection rule (half text, half scans). Every final 10-, 20-, and 50-document cohort used a fresh database and Redis logical database. The original baseline matrix reused an isolated database, so stored bytes/history can affect direct before/after timings. Measurements are single local observations. The older `preformat-15138ee-*.json` results are retained separately and excluded from final conclusions.

## Reproduction

At handoff, benchmark apps/workers and the two named disposable service
containers are stopped; their synthetic databases remain for readback. Restart
those containers for inspection, or use fresh service names/ports for a new run.

Use the repository `.venv`, Python 3.13, PostgreSQL 16 and Redis 7 on loopback, Tesseract 5.5.3, Poppler `pdftoppm` 25.11.0, and Playwright 1.63.0 with Chromium. Install `scripts/dev/requirements-benchmark.txt` only if the synthetic PDF corpus must be regenerated; reportlab is not a product dependency. The recorded host was macOS 26.5.2 arm64 with 12 logical CPUs and 24 GiB RAM.

The before-run source was materialized with `git archive 8c46c9ac9b8a8009246b92f43bbad2b45142de4d` under `/tmp/sro-bulk-benchmark-baseline/source`; `baseline-runner-original.py` is the exact harness used (SHA-256 `cbdbe522c9383f5245c83291eedd1515a3c835260540b78162bc1ea76bf5f77b`). The original DPI candidate harness is retained as `dpi-runner-original.py` (SHA-256 `b7eb00e0ca990cad49a86c3009d8f201f181fe440e012a5c23be0bc5258f071c`). `source-identity.json` maps the recorded abbreviated run labels to resolved source identities and file hashes.

The intermediate 15138ee benchmark harness source was not retained; its exact SHA-256 `86fab4ae263bc75a29e5af27460a61a5ed455c13028ef9616c6407eab93ae861` remains in the preformat result JSONs. Those results are preserved for traceability, not used as final evidence.

For component-only timing, `scripts/dev/profile_bulk_components.py` profiles a fixed five-file sample (two text PDFs and one high-, medium-, and low-quality scan) at 300 DPI. It reports local OCR wall and child-process CPU time separately from in-process `parse(variants)` wall/CPU time, with input/source hashes and field checks. `component-profile-baseline.json` and `component-profile-after-20541c6.json` use the same sample. This is a small stage profile, not an end-to-end result: three scans took 5.88 s OCR / 3.43 ms parse before and 5.90 s / 3.46 ms after; two text PDFs took 36.5 ms OCR / 33.3 ms parse before and 21.7 ms / 19.5 ms after. Scan OCR dominates; these samples show no meaningful scan-stage change. One low-quality scan had the same date mismatch in both profiles. The profile excludes app, DB, RQ, and card-write transactions; the full-pipeline results include those costs.

```sh
.venv/bin/python scripts/dev/profile_bulk_components.py --source-root /tmp/sro-bulk-benchmark-baseline/source --corpus /tmp/sro-bulk-benchmark/corpus --label baseline-8c46c9ac --output /tmp/results/component-baseline.json
.venv/bin/python scripts/dev/profile_bulk_components.py --source-root /Users/root1/Desktop/sro-robot --corpus /tmp/sro-bulk-benchmark/corpus --label after-20541c6 --output /tmp/results/component-after.json
```

Start disposable loopback services, then create an empty private env file. The service credentials below are synthetic examples:

```sh
docker run --name sro-bulk-benchmark-pg -e POSTGRES_USER=sro_benchmark -e POSTGRES_PASSWORD=synthetic-benchmark-db-password -e POSTGRES_DB=postgres -p 127.0.0.1:55443:5432 -d postgres:16@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea
docker run --name sro-bulk-benchmark-redis -p 127.0.0.1:56390:6379 -d redis:7@sha256:858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499
: > /tmp/sro-bulk-benchmark-empty.env
chmod 600 /tmp/sro-bulk-benchmark-empty.env
```

Generate the corpus once with the recorded font, and verify the manifest hash printed by the generator:

```sh
.venv/bin/pip install -r scripts/dev/requirements-benchmark.txt
.venv/bin/python scripts/dev/nightly/generate_corpus.py --output-dir /tmp/sro-bulk-benchmark/corpus --font /System/Library/Fonts/Supplemental/Arial.ttf
```

For each measured size, create a fresh database named with `benchmark` in its name, run `alembic upgrade head`, start the app with explicit environment values below, then run the benchmark. Set `DATABASE_URL` to that database and `REDIS_URL` to a fresh loopback logical database (the recorded final cohorts used `/6` through `/11`); set a unique private `SRO_UPLOAD_DIR` per run. `APP_SECRET_KEY` below signs synthetic local sessions only. Do not source `.env`.

```sh
export SRO_ROBOT_ENV_FILE=/tmp/sro-bulk-benchmark-empty.env
export APP_DEBUG=1
export APP_SECRET_KEY=synthetic-benchmark-session-secret-000000000000000000000000000000000000
export DATABASE_URL='postgresql+psycopg://sro_benchmark:synthetic-benchmark-db-password@127.0.0.1:55443/sro_benchmark_final20541_50'
export REDIS_URL=redis://127.0.0.1:56390/8
export SRO_UPLOAD_DIR=/tmp/sro-bulk-benchmark/final20541/50/storage
export OCR_TASK_LEASES_ENABLED=1
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 18083
```

In another shell with the same explicit environment, run one fresh cohort per database:

```sh
.venv/bin/python scripts/dev/bulk_benchmark.py --corpus /tmp/sro-bulk-benchmark/corpus --base-url http://127.0.0.1:18083 --counts 10 --workers 4 --worker-launcher-pool --run-id final20541-10-w4 --revision 20541c65a8260f05527f92436d7310cc070b13d7 --output /tmp/results/10-w4.json --log-dir /tmp/logs
.venv/bin/python scripts/dev/bulk_benchmark.py --corpus /tmp/sro-bulk-benchmark/corpus --base-url http://127.0.0.1:18083 --counts 20 --workers 4 --worker-launcher-pool --run-id final20541-20-w4 --revision 20541c65a8260f05527f92436d7310cc070b13d7 --output /tmp/results/20-w4.json --log-dir /tmp/logs
.venv/bin/python scripts/dev/bulk_benchmark.py --corpus /tmp/sro-bulk-benchmark/corpus --base-url http://127.0.0.1:18083 --counts 50 --workers 4 --worker-launcher-pool --run-id final20541-50-w4 --revision 20541c65a8260f05527f92436d7310cc070b13d7 --output /tmp/results/50-w4.json --log-dir /tmp/logs
```

The runtime guard requires an empty private env file, debug mode, benchmark-named PostgreSQL database, and loopback-only PostgreSQL/Redis/HTTP. The worker launcher enforces OCR-only parallelism with OCR leases and a maximum of four processes. The dashboard displays 50 rows per page. For the browser bulk path, use a fresh benchmark database and queue, start the app and a four-worker OCR pool, then run:

```sh
.venv/bin/python scripts/dev/bulk_browser_e2e.py --corpus /tmp/sro-bulk-benchmark/corpus --base-url http://127.0.0.1:18082 --count 50 --output /tmp/results/ui-e2e-bulk.json --screenshot-dir /tmp/sro-bulk-benchmark/ui
```

Run `scripts/dev/run_worker.py --workers 4 ocr` in its own terminal with the same explicit environment before browser/concurrency uploads. The database auditor scripts `audit_bulk_task_outcomes.py`, `recheck_benchmark_cards.py`, and `audit_bulk_concurrency.py` are read-only and enforce the empty-env plus loopback benchmark DB guard.

## Results

The original worker matrix is in `baseline.json`. Final pinned-source runs used four workers and finished 10 documents in 5.14 s (116.7 docs/min), 20 in 7.66 s (156.6 docs/min), and 50 in 17.96 s (167.0 docs/min). A fresh 50-document, one-worker run took 60.08 s (49.9 docs/min), making the four-worker throughput 3.34× higher. Four-worker peak combined RSS was 1.44 GiB; one-worker peak was 395 MiB. The 50/4 task-read query mean/p95 was 8.46/12.42 ms. Baseline 50/4 was 17.59 s, so final same-count runtime was 2.1% slower. The measured throughput gain comes from bounded worker parallelism, not an OCR algorithm speedup.

All 25 automatically prepared cards in the final 50-document cohort matched synthetic expected name, INN, address, case number, and date. They came from the 25 text PDFs. All 25 scans stayed review-only with `field_role_uncertain` preparation reason (10 high-, 10 medium-, 5 low-quality scans); no scan card was prepared with uncertain role-bound fields. Across all 50 tasks, case number and name matched 50/50; date matched 49/50, with `synthetic-091.pdf` expected `07.07.2099` but extracted as `01.07.2099`; INN matched 34/50, with 16 missing and none wrong. All tasks reached review state with zero failures. `after-20541-50-task-audit.json` contains per-file exceptions and reasons; `after-20541-50-w4-card-audit.json` independently verifies every prepared card field.

The pinned browser E2E in `ui-e2e-bulk-20541.json` uploaded 50 mixed PDFs plus one corrupt input through three operation IDs. All 50 valid tasks reached review, the corrupt input failed independently, and the dashboard paginated 50+1 rows. It showed 25 prepared cards, 25 needing review, and one error. The downloaded source hash matched the synthetic PDF. The browser edited a prepared card while SNILS/court remained empty, manually reviewed one ambiguous scan, and confirmed exact replay left 51 tasks, 3 operations, and 25 cards unchanged before manual review. Screenshots are in `/tmp/sro-bulk-benchmark-ui-20541/` and are not committed.

The 20-employee run in `concurrent-20-employees-20541.json` sent 20 simultaneous uploads of two unique documents each. All requests returned HTTP 200, each employee owned exactly two tasks, all 40 reached review with no failures, and all 20 automatic cards exactly matched all five expected fields. The run completed in 13.29 s (180.6 docs/min). Twelve scan task INNs were missing and none wrong; all prepared-card INNs were exact. The read-only audit verified 40 creation events, 20 actors, no actor/assignee mismatch, and valid 32-hex request IDs on every event.

The direct DPI experiment in `dpi-comparison.json` rejected 200 DPI: it shortened the 10-scan sample from 40.73 s to 32.37 s but reduced exact INN presence from 10/10 to 8/10. The production OCR setting was not changed. `raw-log-manifest.json` records hashes for 56 completed worker logs; hashes were verified before those streams were pruned. Corpus and source inputs remain available in `/tmp`.
