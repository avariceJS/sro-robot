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

The application listens on `127.0.0.1:8000` in this development command. `/healthz` only confirms the web process responds; it does not establish database, Redis or worker readiness.

`init-db` applies `alembic upgrade head`. Its legacy `--reset` option now refuses to drop tables. Never run `init-db` unmodified against a pre-Alembic database: the baseline would try to create tables that already exist. For an existing installation, first make and verify a backup, compare all tables, indexes, PostgreSQL enum labels and defaults with the baseline, then have the database operator stamp revision `bc518c7514d3` before applying later revisions. Do not stamp automatically or against an unverified schema. No migration or stamp was applied to the shared development database during this work.

The baseline downgrade is intentionally blocked because it owns all application data. Rebuild only a disposable empty database for a clean-install test. Development compose credentials and the fallback application secret are not suitable for production.

SRO credentials are optional for local core/UI tests. `.env.example` contains blank placeholders; never copy real credentials into examples or source control. Do not display or commit `.env` or `profile/`.

## Tests and local checks

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q app sro_robot tests
git diff --check
```

Latest recorded verification is 34 deterministic tests passed on 2026-10-10. A disposable PostgreSQL 16 container accepted the baseline upgrade and `alembic check` found no metadata drift. The isolated local UI run covered upload, OCR, review, approval and local-only validation; sample documents remained in review. This does not prove production readiness. Successful MKAD acquisition, DB concurrency/load test, crash-recovery drill, backup/restore proof and SRO read-back workflow remain unverified.

Use fake adapters and isolated databases for integration and write-path tests. This build blocks live SRO writes: `run` performs local validation, `--live` fails closed, and the default SRO client refuses Add/Save/Close actions. Do not bypass these guards or use a real registry as a test fixture. Future form preparation must run against a local fixture or fake boundary.

## External system safety

- Court documents contain personal data. OCR and extraction must remain local; do not upload source documents or raw OCR text to external cloud AI services.
- Do not bypass KAD/MKAD challenges or access controls. Treat empty/challenged pages as acquisition failures.
- SROArbitr is ASP.NET WebForms. Use selectors from `memory-bank/30-integrations.md`, based on stable IDs, not labels. An Add action may create a draft; Save controls can have different scopes. The current build blocks state-changing controls.
- A click, current URL or vanished form is not proof that a record exists. After any future authorized write, require read-back and semantic verification. On uncertain outcomes, reconcile read-only and do not automatically resubmit.
- The owner has explicitly prohibited real SRO changes for this development. This applies to all agents and tests.

## Current engineering context

Start at [`memory-bank/.now.md`](memory-bank/.now.md), then follow [`memory-bank/00-start-here.md`](memory-bank/00-start-here.md). The current priority is fail-closed MKAD act selection, durable write-attempt/unknown-outcome/idempotency/reconciliation safety, and participant-bound extraction. Visual redesign is not a priority.
