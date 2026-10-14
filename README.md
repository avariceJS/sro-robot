# SRO Robot

SRO Robot is an internal document-processing and workflow automation system for a legal office working with bankruptcy cases and SROArbitr. Its goal is to turn batches of court documents into reviewed, traceable SRO operations while keeping legal-data decisions under operator control.

> **Current status: engineering development, not production-ready.** The checked-in implementation supports one physical-person debtor-card scenario. It has not passed a complete external workflow, concurrency, recovery, security, load, backup/restore or production deployment qualification. No agent may create or modify real SRO records.

## Product workflow

Target intake includes PDFs, scanned PDFs, photos and case identifiers. The target pipeline acquires the relevant judicial act, preserves provenance, classifies the act and its participants, performs local OCR/extraction and validation, sends uncertain cases to an operator, reserves the intended identity/operation, prepares the SRO form and verifies a saved result by reading it back.

The owner-requested case workflow uses the ordinary-browser KAD helper (0.2.11, Firefox and Chrome): enter a case number and optional act date on the main service page, search the exact case, select its unique dated PDF, transfer the complete browser response to local parsing, and review the extracted fields. One owner-authorized two-page act completed an earlier Firefox revision of this path. After approval, the guarded SRO preview fills a new blank person form, reads its values back and closes without saving. Staff can also upload PDFs/images directly. Review omits date of birth and birth place. Automatic field approval and live SRO Save remain disabled.

The document upload control accepts up to 100 selected files in one employee action and submits sequential requests of at most 20 files. Default limits remain 25 MiB per file, 100 MiB for the selected batch, 105 MiB per request, 300 seconds per request body and two concurrent multipart requests. A partial upload reports its accepted count; retrying the same selection on the same page reuses each chunk's idempotency key. Each chunk is a separate intake operation, so the 100-file action is not an atomic database transaction. Reloading the page does not resume the previous batch.

The unused public-source search and the Safari/SafariDriver diagnostic were removed. Historical KAD acquisition jobs stay withheld. The browser helper and `/kad-helper/import` are the active document routes.

To continue on another computer, start with [`memory-bank/65-development-handoff.md`](memory-bank/65-development-handoff.md).

## Architecture

- `sro_robot/core/`: local OCR, extraction, morphology, address handling, domain fields and validators.
- `sro_robot/documents/`: extraction profiles by document class.
- `sro_robot/platform/`: Playwright clients and SRO platform interactions.
- `sro_robot/scenarios/`: operation-specific validation, preflight and form workflows.
- `app/`: FastAPI, SQLAlchemy, PostgreSQL, Redis/RQ jobs and Jinja/HTMX UI.
- `deploy/docker-compose.dev.yml`: local PostgreSQL and Redis only.
- `memory-bank/.now.md`: current engineering state and next step.

The application depends on `sro_robot`; the library must not depend on `app`. Domain workflows must remain extensible beyond the current person-card path.

## Local development environment

`requirements-bootstrap.txt` pins the installer version with SHA-256 hashes; use it to upgrade the Python-provided pip before installing `requirements.txt`, which pins the verified Python 3.13 application environment by exact versions and SHA-256 hashes. OCR still depends on local Tesseract with Russian language data and Poppler, and Playwright Chromium must also be installed locally.

Create the pinned Python environment:

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-bootstrap.txt
.venv/bin/python -m pip install --require-hashes -r requirements.txt
.venv/bin/python -m pip install --require-hashes -r requirements-test.txt
.venv/bin/python -m pip install --require-hashes -r requirements-audit.txt
.venv/bin/python -m pip_audit --local --vulnerability-service pypi
.venv/bin/python -m playwright install chromium
```

Install Tesseract with Russian language data and Poppler using the operating system's package manager before exercising OCR/PDF conversion.

Copy `.env.example` to a private `.env` file and set independent high-entropy values for `POSTGRES_PASSWORD` and `REDIS_PASSWORD` (at least 32 hexadecimal characters for Redis). Set `DATABASE_URL` and `REDIS_URL` with the matching credentials; application startup now requires both URLs even in debug mode. Keep `.env` mode `0600`. Compose requires these credentials, and Redis starts with password authentication enabled.

Start local PostgreSQL and Redis only for a new disposable database or after the existing queue and data have been reconciled:

```bash
docker compose -f deploy/docker-compose.dev.yml up -d
```

For a **new empty database**, apply the Alembic baseline and create an application user, then run the web server:

```bash
.venv/bin/python -m app.cli init-db
.venv/bin/python -m app.cli create-user 'Test Operator'
bash scripts/dev/run-web.sh
```

Run each worker in a separate terminal, after checking `.venv/bin/python -m app.cli ops-status`:

```bash
.venv/bin/python scripts/dev/run_worker.py ocr
.venv/bin/python scripts/dev/run_worker.py sro_preview
```

The preview worker serves only approved blank-card preparation and Close without saving. Do not start a generic `sro` worker as part of this development handoff.

For an isolated development database and reconciled queue, run the safe internal outbox dispatcher as a separate process:

```bash
scripts/dev/supervise_worker.py -- .venv/bin/python -m app.cli outbox-dispatch --apply --watch --poll-seconds 5
```

The dispatcher handles upload OCR and local validation. Historical KAD acquisition outbox intents remain withheld and are never dispatched. Browser-helper KAD PDFs enter through `/kad-helper/import` and use the ordinary local OCR outbox path. Each pass has a bounded batch, transient database failures use capped backoff, and SIGINT/SIGTERM stop the loop between passes. The supervisor applies a finite restart budget if the dispatcher exits unexpectedly. This local wrapper is not a production service definition; use the operating-system service manager for deployment. Never start either process against an unreconciled queue.

Before starting a worker against an existing database or Redis queue, run `.venv/bin/python -m app.cli ops-status`. It reports aggregate task/queue/worker health, read-only started/deferred/scheduled/failed registry counts, and—when the `task_lease` table exists—local upload/batch OCR tasks in `recognizing` without a lease that require read-only reconciliation. A started RQ entry without a fresh worker heartbeat makes the command unhealthy. Legacy external-acquisition tasks require explicit read-only reconciliation before any worker restart. On an older schema without the lease table, OCR lease classification is reported as unavailable. If tasks are queued or stuck in processing, do not start workers until each item has been safely identified and its outcome is understood; the aggregate status command does not identify individual jobs. Use this command only for a fresh isolated development database or a reconciled queue.

To check the actual PostgreSQL and Redis port bindings without changing containers, run `.venv/bin/python scripts/dev/check_compose_runtime_bindings.py`. It exits nonzero when running containers do not match the loopback-only Compose configuration.

The application listens on `127.0.0.1:8000` in this development command. Successful login creates a revocable server-side session with an unpredictable cookie token; PostgreSQL stores only its SHA-256 digest. Logout and successful re-login revoke that browser session, while other sessions remain active. Signed cookies without a live database session are rejected. `/healthz` only confirms the web process responds. `/readyz` checks PostgreSQL, requires exactly the current Alembic head (`c3a8e1d04b57`), checks the OCR lease schema when leases are enabled, and requires a registered worker with a fresh heartbeat for each OCR or SRO queue containing waiting or Started jobs. Empty queues are ready without workers. Started-job counts are read without registry cleanup; this aggregate check cannot identify a job's owner, measure queue age or task progress, and is not worker supervision or a retry decision. Safe OCR and local-validation requests create PostgreSQL outbox intents in the same transaction as task/audit state; the requested KAD helper imports a browser-acquired PDF through the local OCR path; legacy KAD acquisition intents are withheld. Admission is at-least-once: HTTP dispatch is immediate, pending rows survive process/Redis failures, and `.venv/bin/python -m app.cli outbox-dispatch --apply` performs a bounded one-shot retry. `outbox-dispatch --apply --watch` provides a continuously running bounded dispatcher; supervise it as a separate process and use it only after queue reconciliation. `ops-status` and read-only `outbox-dispatch` report aggregate pending, eligible, withheld, leased and delayed counts, oldest pending age and recent error classes without task identifiers. The outbox deliberately rejects external SRO-write job kinds; workers still fence duplicate delivery.

`init-db` applies `alembic upgrade head`. Its legacy `--reset` option now refuses to drop tables. Never run `init-db` unmodified against a pre-Alembic database: the baseline would try to create tables that already exist. For an existing installation, first make and verify a backup, compare all tables, indexes, PostgreSQL enum labels and defaults with the baseline, then have the database operator stamp revision `bc518c7514d3` before applying later revisions. Do not stamp automatically or against an unverified schema. No migration or stamp was applied to the shared development database during this work.

The baseline downgrade is intentionally blocked because it owns all application data. Rebuild only a disposable empty database for a clean-install test. Locally supplied development credentials and the fallback application secret are not suitable for production.

`APP_DEBUG` defaults to `0`; the checked-in `.env.example` opts into `1` explicitly for local development. If deployment configuration omits the flag, startup follows production requirements instead of enabling debug mode or its development-only session-secret fallback. In debug mode, `MIGRATION_DATABASE_URL` defaults to `DATABASE_URL`. With `APP_DEBUG=0`, configure both URLs explicitly and give them distinct PostgreSQL role names: web/worker runtime operations use `DATABASE_URL`, while `init-db` and Alembic use `MIGRATION_DATABASE_URL`. Do not give the runtime role schema-owner or migration privileges.

SRO credentials are optional for local core/UI tests. `.env.example` contains blank placeholders; never copy real credentials into examples or source control. After creating `.env`, set mode `0600`; POSIX startup rejects a secret file owned by another user or readable by group/others before loading it. Do not display or commit `.env` or `profile/`. Browser `profile/` and diagnostic `out/` directories are restricted to the service user because they may contain session state or screenshots.

## Tests and local checks

```bash
scripts/dev/verify.sh
```

The local verification entry point runs the deterministic unittest suite, byte-compilation, diff checks, shell syntax and a disposable PostgreSQL 16 migration/restore/adoption drill. On 2026-10-11, `scripts/dev/verify.sh` passed 170 discovered tests (152 non-PostgreSQL passed and 18 PostgreSQL-only skipped in discovery), then all 18 PostgreSQL integration tests in the disposable drill. Three CI-contract tests protect read-only repository permissions, commit-pinned actions, the Python 3.13 environment and use of the shared local verifier; these older counts are superseded by the successful hosted verifier documented in `memory-bank/.now.md` (289 discovered tests and all 31 PostgreSQL cases). The suite proves the legacy OCR handler atomically claims a `new` task once across eight concurrent attempts and refuses duplicate delivery without overwriting review/approval state; source-acquisition worker claims remain one-shot and do not repeat a live external fetch after a task leaves `new`. Five runtime-binding checker tests verify loopback success, missing bindings, IPv4/IPv6 wildcard rejection, wrong container identity, extra published ports, and missing containers. PostgreSQL write tests require a named restore-drill container and verified loopback port; a missing-marker invocation was rejected before any database connection. The drill also passed the 10,000-task/20-staff restore with exact application-row and sequence signatures plus a synthetic mutation-detection control, adoption path, migration rollback/retry and downgrade refusals at both the lease migration and current head that preserved active synthetic lease ownership, all row signatures and uniqueness guards, queue-loss recovery, worker heartbeat diagnostics, a 1,000-task/20-thread PostgreSQL OCR-claim diagnostic (662.3 claims/second, 28.2 ms p95 in the latest full verification), disposable backup/restore, and a loopback PostgreSQL role DDL-boundary probe (non-superuser migrator; runtime DML/sequence use allowed, schema DDL and Alembic revision writes denied). `ops-status` now identifies lease-less local upload/batch OCR tasks for read-only reconciliation, excludes KAD acquisition, and reports its classification unavailable on pre-lease schemas; the disposable worker-health scenario accepts exit 1 only when that legacy-task warning explains it and no queue is uncovered. Readiness failure tests verify that database/Redis outages yield 503 without connection details, Redis probe waits are bounded and clients are closed. Security response tests verify dynamic no-store and preserved static-asset cache headers. The read-only schema comparison checks unmodeled public RLS enable/force flags, policies, extensions and routines; a disposable PostgreSQL integration test creates, detects and removes synthetic RLS state and a routine. The updated upper-size 100,000-task restore drill also matched every application-row and sequence signature, detected a synthetic data mutation, and verified both active-lease and current-head downgrade refusals preserve data and identity guards; its 1,000-task/20-thread probe measured 655.7 claims/second and 29.0 ms p95. A separate upper-size 5,000-claim/64-thread probe exposed pool saturation: 49 claims timed out and remained unclaimed. The harness reconciled state and cleaned up its disposable services, then returned nonzero for incomplete claims. Both disposable drill scripts are also checked for SIGINT/SIGTERM exit and private temporary-directory cleanup. These are local synthetic diagnostics, not production-like qualification, soak evidence or an office SLA. Production authorization/ownership, backup objectives, deployment security, approved workload targets and operational recovery remain unqualified.

The upper-size `SRO_DRILL_TASKS=100000` restore/adoption run also passed on 2026-10-11: 100,000 synthetic task/event pairs, 20 staff, zero checked identity-link mismatches, all 14 PostgreSQL integration tests, and a 1,000-task/20-thread claim diagnostic at 642.3 claims/second (30.5 ms p95). This remains synthetic local evidence, not an approved workload or recovery target.

`.github/workflows/verify.yml` runs the same entry point on pushes and pull requests with read-only repository permissions and SHA-pinned actions. The full hosted verifier passed on both `main` and `stage-1/foundation` at code commit `7bdfbd360a69414920c62ccb47d0f4eb84f0ebc6`; see `memory-bank/.now.md` for run links and hashes. Later documentation-only handoffs preserve that source and use `[skip ci]`.

HTML responses receive a restrictive Content Security Policy: scripts, forms, images, fonts and connections are same-origin; objects and framing are disabled. Inline styles remain allowed because current templates use `style` attributes; this policy does not replace deployment-specific TLS/HSTS configuration or a full browser-security review.

Dynamic application responses also carry `Cache-Control: no-store` to reduce retention of task data and screenshots in browser or intermediary caches. Static assets retain their configured cache policy.

Use fake adapters and isolated databases for integration and write-path tests. This build blocks live SRO writes: `run` performs local validation, `--live` fails closed, and the default SRO client refuses Add/Save/Close actions. The separate prepare-only path permits only the owner-authorized blank-card Add, approved-field fill, read-back and Close without saving. Do not bypass either guard or use persisted registry records as test fixtures.

## External system safety

- Court documents contain personal data. OCR and extraction must remain local; do not upload source documents or raw OCR text to external cloud AI services.
- Do not bypass KAD challenges or access controls. Treat empty/challenged pages and ambiguous document candidates as acquisition failures.
- SROArbitr is ASP.NET WebForms. Use selectors from `memory-bank/30-integrations.md`, based on stable IDs, not labels. The owner authorized only opening a blank person card with Add, filling and inspecting it, then closing without saving. The code blocks Save, Send and other unverified SRO mutations.
- A click, current URL or vanished form is not proof that a record exists. After any future authorized write, require read-back and semantic verification. On uncertain outcomes, reconcile read-only and do not automatically resubmit.
- Real SRO persistence or modification remains prohibited. Only the narrow owner-authorized blank-card Add → fill → inspect → Close-without-save preview is allowed; do not use Save, Send or uncertain controls.

## Current engineering context

Start at [`memory-bank/.now.md`](memory-bank/.now.md), then follow [`memory-bank/00-start-here.md`](memory-bank/00-start-here.md). The active engineering stage is Stage 1: Platform Foundation and Control Plane. Continue with safe schema adoption/recovery evidence, explicit lifecycle and idempotency contracts, enforcement of the owner-approved shared-access/audit policy, worker supervision and production operations. Keep the maturity label evidence-based; Stage 1 is not M4 and real SRO writes remain prohibited. Visual redesign is not a priority.
