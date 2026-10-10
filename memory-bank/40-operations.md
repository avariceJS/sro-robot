# Development, verification and incident handling

## Local development setup (observed)

`deploy/docker-compose.dev.yml` starts only PostgreSQL 16 and Redis 7. FastAPI and RQ workers run from the host `.venv` so OCR can call local Tesseract and Poppler. Defaults in `app/settings.py` use database port `55432`, Redis port `56379`, upload directory `storage/documents`, debug enabled and a development-only session key. These defaults are not production configuration.

The repository has `.env.example`; inspect it without printing `.env`. System dependencies for the OCR path are Tesseract with Russian data, Poppler, and Playwright Chromium. Dependency manifests/build packaging are incomplete; verify how the current environment was provisioned before documenting a fresh reproducible install.

Observed local commands:

```bash
docker compose -f deploy/docker-compose.dev.yml up -d
.venv/bin/python -m app.cli init-db
.venv/bin/python -m app.cli create-user 'Test User'
.venv/bin/uvicorn app.main:app --reload
.venv/bin/rq worker ocr sro --url redis://localhost:56379/0
```

Run server and worker in separate terminals. `create-user` prompts for a local application password. Do not run `app.cli init-db --reset` against data to preserve: it drops tables. Current initialization uses `create_all` and ad hoc enum changes; it is not a safe production migration process. `/healthz` reports only that the process is answering; it does not check DB, Redis or worker health.

CLI commands are `extract`, `inspect`, and `run`. In the current build, `run` performs local validation and `--live` fails closed. The SRO client also blocks Add, Save and Close by default. Do not enable registry mutations or use a real registry for tests.

## Test commands

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q app sro_robot tests
git diff --check
```

Last verified on 2026-10-10: 29 deterministic tests passed. The UI upload/OCR/review flow was exercised in an isolated local database using one synthetic PDF and the two owner-provided samples; the two real samples reached `needs_review`. A local validation job was then run through the UI and recorded `saved: false`; the SRO browser was not opened. MKAD UI submissions for a synthetic nonexistent case and one sample case failed because the automated browser received an empty page. No database concurrency test, crash-recovery test, 20-user load test, backup/restore drill, successful MKAD acquisition or SRO read-back verification exists.

Use fake SRO clients and isolated databases for stateful/write tests. Tests must cover duplicate intake, task claiming, retries, crashes around side-effect boundaries, worker restarts, out-of-order events, browser timeouts, partial form operations, missing/ambiguous reconciliation, and PII-safe logs. Never use a real SRO write as an end-to-end test.

## Logging and observability

Committed request handling logs unhandled exception stacks with request method/path and returns a generic 500 body. SRO runner logs exceptions with task ID. Dirty worker changes log OCR/KAD exceptions. This is not yet a complete observability system: there are no established structured log schema, metrics, tracing, alerting, queue-age dashboards, worker heartbeat, retention policy or production incident runbook. Do not log raw document/OCR text or credentials.

Every background job needs a durable task correlation ID, explicit result/state transition and enough diagnostic context to reproduce failure without exposing document contents. Avoid silently swallowing cleanup/close errors.

## Incident checks

1. Check service health, database/Redis availability and worker process/heartbeat.
2. Inspect queue counts and task/event state before retrying.
3. For OCR/acquisition failures, preserve original input and error classification; retry only if operation is idempotent and failure is known pre-side-effect.
4. For any SRO task that may have reached final Save, reconcile read-only before any retry. Unknown means unknown.
5. Record exact evidence and preserve logs; do not restart services or retry jobs blindly.

Last observed runtime (2026-10-10): app health HTTP 200; Postgres/Redis healthy; no RQ workers; one OCR queued, five OCR failed (empty zero-byte uploads), one SRO failed after unexpected worker termination. Do not assume this state remains current.

## Production gaps

Before deployment: reproducible dependency/package lock, container/service definitions, secret management, TLS/security headers, session/CSRF review, RBAC, file type/size limits and malware handling, retention/deletion policy, migrations, encryption/access control for stored documents, structured logs/metrics/alerts, worker supervision, dead-letter/reconciliation tooling, backup/restore drills, capacity/load tests and operator runbooks. Evaluate each against real requirements; do not claim present support based on this list.
