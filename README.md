# SRO Robot

SRO Robot is an internal document-processing and workflow automation system for a legal office working with bankruptcy cases and SROArbitr. Its goal is to turn batches of court documents into reviewed, traceable SRO operations while keeping legal-data decisions under operator control.

> **Current status: engineering development, not production-ready.** The checked-in implementation supports one physical-person debtor-card scenario. It has not passed a complete external workflow, concurrency, recovery, security, load, backup/restore or production deployment qualification. No agent may create or modify real SRO records.

## Product workflow

Target intake includes PDFs, scanned PDFs, photos and case identifiers. The target pipeline acquires the relevant judicial act, preserves provenance, classifies the act and its participants, performs local OCR/extraction and validation, sends uncertain cases to an operator, reserves the intended identity/operation, prepares the SRO form and verifies a saved result by reading it back.

The current application includes upload/review/task flows, local OCR/PDF extraction, a scenario-based person-card workflow, Redis/RQ jobs and an MKAD intake implementation in the current worktree. The UI upload/OCR/review path has been exercised locally, including the two owner-provided designation samples. Current MKAD acquisition failed against an empty automated-browser page. Live SRO writes are disabled in this build. Organization cards, operation classification, robust identity linking, safe write reconciliation, large-batch controls and production operations remain incomplete.

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

`requirements.txt` pins the verified Python 3.13 environment. OCR still depends on local Tesseract with Russian language data and Poppler; Playwright Chromium must also be installed locally.

Create the pinned Python environment:

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
```

Install Tesseract with Russian language data and Poppler using the operating system's package manager before exercising OCR/PDF conversion.

Start local PostgreSQL and Redis:

```bash
docker compose -f deploy/docker-compose.dev.yml up -d
```

For a **new empty database**, apply the Alembic baseline and create an application user, then run the web server:

```bash
.venv/bin/python -m app.cli init-db
.venv/bin/python -m app.cli create-user 'Test Operator'
.venv/bin/uvicorn app.main:app --reload
```

Run workers in another terminal:

```bash
.venv/bin/rq worker ocr sro --url redis://localhost:56379/0
```

Before starting a worker against an existing database or Redis queue, run `.venv/bin/python -m app.cli ops-status`. It reports aggregate task/queue/worker health and, when the `task_lease` table exists, counts local upload/batch OCR tasks in `recognizing` without a lease that require read-only reconciliation. KAD acquisition uses a separate ownership path and is excluded. On an older schema without the lease table, OCR lease classification is reported as unavailable. If tasks are queued or stuck in processing, do not start workers until each item has been safely identified and its outcome is understood; the aggregate status command does not identify individual jobs. Use this command only for a fresh isolated development database or a reconciled queue.

To check the actual PostgreSQL and Redis port bindings without changing containers, run `.venv/bin/python scripts/dev/check_compose_runtime_bindings.py`. It exits nonzero when running containers do not match the loopback-only Compose configuration.

The application listens on `127.0.0.1:8000` in this development command. `/healthz` only confirms the web process responds; it does not establish database, Redis or worker readiness.

`init-db` applies `alembic upgrade head`. Its legacy `--reset` option now refuses to drop tables. Never run `init-db` unmodified against a pre-Alembic database: the baseline would try to create tables that already exist. For an existing installation, first make and verify a backup, compare all tables, indexes, PostgreSQL enum labels and defaults with the baseline, then have the database operator stamp revision `bc518c7514d3` before applying later revisions. Do not stamp automatically or against an unverified schema. No migration or stamp was applied to the shared development database during this work.

The baseline downgrade is intentionally blocked because it owns all application data. Rebuild only a disposable empty database for a clean-install test. Development compose credentials and the fallback application secret are not suitable for production.

In debug mode, `MIGRATION_DATABASE_URL` defaults to `DATABASE_URL`. With `APP_DEBUG=0`, configure both URLs explicitly and give them distinct PostgreSQL role names: web/worker runtime operations use `DATABASE_URL`, while `init-db` and Alembic use `MIGRATION_DATABASE_URL`. Do not give the runtime role schema-owner or migration privileges.

SRO credentials are optional for local core/UI tests. `.env.example` contains blank placeholders; never copy real credentials into examples or source control. After creating `.env`, set mode `0600`; POSIX startup rejects a secret file owned by another user or readable by group/others before loading it. Do not display or commit `.env` or `profile/`. Browser `profile/` and diagnostic `out/` directories are restricted to the service user because they may contain session state or screenshots.

## Tests and local checks

```bash
scripts/dev/verify.sh
```

The local verification entry point runs the deterministic unittest suite, byte-compilation, diff checks, shell syntax and a disposable PostgreSQL 16 migration/restore/adoption drill. On 2026-10-11, `scripts/dev/verify.sh` passed 167 discovered tests (149 non-PostgreSQL passed and 18 PostgreSQL-only skipped in discovery), then all 18 PostgreSQL integration tests in the disposable drill. The suite proves the legacy OCR handler atomically claims a `new` task once across eight concurrent attempts and refuses duplicate delivery without overwriting review/approval state; the KAD worker also skips repeated external reads when a queued task was already claimed. Five runtime-binding checker tests verify loopback success, missing bindings, IPv4/IPv6 wildcard rejection, wrong container identity, extra published ports, and missing containers. PostgreSQL write tests require a named restore-drill container and verified loopback port; a missing-marker invocation was rejected before any database connection. The drill also passed the 10,000-task/20-staff restore and adoption path, injected migration-failure rollback/retry, queue-loss recovery, worker heartbeat diagnostics, a 1,000-task/20-thread PostgreSQL OCR-claim diagnostic (652.6 claims/second, 29.0 ms p95 in the latest run), disposable backup/restore, and a loopback PostgreSQL role DDL-boundary probe (non-superuser migrator; runtime DML/sequence use allowed, schema DDL and Alembic revision writes denied). `ops-status` now identifies lease-less local upload/batch OCR tasks for read-only reconciliation, excludes KAD acquisition, and reports its classification unavailable on pre-lease schemas; the disposable worker-health scenario accepts exit 1 only when that legacy-task warning explains it and no queue is uncovered. Readiness failure tests verify that database/Redis outages yield 503 without connection details, Redis probe waits are bounded and clients are closed. Security response tests verify dynamic no-store and preserved static-asset cache headers. The read-only schema comparison checks unmodeled public RLS enable/force flags, policies, extensions and routines; a disposable PostgreSQL integration test creates, detects and removes synthetic RLS state and a routine. A separate 100,000-task synthetic full drill passed backup/restore, populated-schema adoption and identity-link read-back (zero mismatches), then its 5,000-claim/64-thread probe exposed pool saturation: 49 claims timed out and remained unclaimed. The harness reconciled state and cleaned up its disposable services, then returned nonzero for incomplete claims. Both disposable drill scripts are also checked for SIGINT/SIGTERM exit and private temporary-directory cleanup. These are local synthetic diagnostics, not production-like qualification, soak evidence or an office SLA. Production authorization/ownership, backup objectives, deployment security, approved workload targets and operational recovery remain unqualified.

The upper-size `SRO_DRILL_TASKS=100000` restore/adoption run also passed on 2026-10-11: 100,000 synthetic task/event pairs, 20 staff, zero checked identity-link mismatches, all 14 PostgreSQL integration tests, and a 1,000-task/20-thread claim diagnostic at 642.3 claims/second (30.5 ms p95). This remains synthetic local evidence, not an approved workload or recovery target.

`.github/workflows/verify.yml` runs the same entry point on pushes and pull requests with read-only repository permissions and SHA-pinned actions. The workflow definition is locally validated; no remote GitHub Actions result exists until it runs on GitHub.

HTML responses receive a restrictive Content Security Policy: scripts, forms, images, fonts and connections are same-origin; objects and framing are disabled. Inline styles remain allowed because current templates use `style` attributes; this policy does not replace deployment-specific TLS/HSTS configuration or a full browser-security review.

Dynamic application responses also carry `Cache-Control: no-store` to reduce retention of task data and screenshots in browser or intermediary caches. Static assets retain their configured cache policy.

Use fake adapters and isolated databases for integration and write-path tests. This build blocks live SRO writes: `run` performs local validation, `--live` fails closed, and the default SRO client refuses Add/Save/Close actions. Do not bypass these guards or use a real registry as a test fixture. Future form preparation must run against a local fixture or fake boundary.

## External system safety

- Court documents contain personal data. OCR and extraction must remain local; do not upload source documents or raw OCR text to external cloud AI services.
- Do not bypass KAD/MKAD challenges or access controls. Treat empty/challenged pages as acquisition failures.
- SROArbitr is ASP.NET WebForms. Use selectors from `memory-bank/30-integrations.md`, based on stable IDs, not labels. An Add action may create a draft; Save controls can have different scopes. The current build blocks state-changing controls.
- A click, current URL or vanished form is not proof that a record exists. After any future authorized write, require read-back and semantic verification. On uncertain outcomes, reconcile read-only and do not automatically resubmit.
- The owner has explicitly prohibited real SRO changes for this development. This applies to all agents and tests.

## Current engineering context

Start at [`memory-bank/.now.md`](memory-bank/.now.md), then follow [`memory-bank/00-start-here.md`](memory-bank/00-start-here.md). The active engineering stage is Stage 1: Platform Foundation and Control Plane. Continue with safe schema adoption/recovery evidence, explicit lifecycle and idempotency contracts, authorization/ownership policy, worker supervision and production operations. Keep the maturity label evidence-based; Stage 1 is not M4 and real SRO writes remain prohibited. Visual redesign is not a priority.
