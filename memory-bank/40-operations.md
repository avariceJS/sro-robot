# Development, verification and incident handling

## Local development setup (observed)

`deploy/docker-compose.dev.yml` starts only PostgreSQL 16 and Redis 7. FastAPI and RQ workers run from the host `.venv` so OCR can call local Tesseract and Poppler. Defaults in `app/settings.py` use database port `55432`, Redis port `56379`, upload directory `storage/documents`, debug enabled and a development-only session key. These defaults are not production configuration.

`requirements.txt` pins the current Python 3.13 environment. The OCR path additionally needs local Tesseract with Russian data, Poppler, and Playwright Chromium. `.env.example` contains blank placeholders; never print `.env`.

Observed local commands:

```bash
docker compose -f deploy/docker-compose.dev.yml up -d
.venv/bin/python -m app.cli init-db
.venv/bin/python -m app.cli create-user 'Test User'
.venv/bin/uvicorn app.main:app --reload
.venv/bin/rq worker ocr sro --url redis://localhost:56379/0
```

Run server and worker in separate terminals. `create-user` prompts for a local application password. `init-db` applies Alembic migrations only after a read-only preflight: an unversioned database must contain no tables. A populated database without a recorded Alembic revision is refused before migrations run; its adoption requires a verified backup and exact read-only comparison of tables, indexes, defaults and PostgreSQL enum labels. A DBA may then stamp revision `bc518c7514d3`; never auto-stamp or use the command against an unverified schema. The legacy `--reset` flag refuses destructive drops. The baseline downgrade is intentionally blocked. `/healthz` is a process liveness check. `/readyz` checks PostgreSQL and Redis and returns 503 when either is unavailable; it does not check worker presence or queue age.

Production configuration must set `APP_DEBUG=0` and provide `APP_SECRET_KEY` with at least 32 characters. Startup rejects a missing or short key in that mode instead of using the development fallback. Keep the key in `.env` or the deployment secret store; do not pass it as a command-line argument or include it in logs. The development fallback is accepted only while debug mode is enabled.

CLI commands are `extract`, `inspect`, and `run`. In the current build, `run` performs local validation and `--live` fails closed. The SRO client also blocks Add, Save and Close by default. Do not enable registry mutations or use a real registry for tests.

## Test commands

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q app sro_robot tests
git diff --check
```

Last verified on 2026-10-10: 34 deterministic tests passed. A disposable PostgreSQL 16 container accepted the Alembic baseline upgrade and `alembic check` reported no metadata drift. The UI upload/OCR/review flow was exercised in an isolated local database using one synthetic PDF and the two owner-provided samples; the two real samples reached `needs_review`. A local validation job was then run through the UI and recorded `saved: false`; the SRO browser was not opened. MKAD UI submissions for a synthetic nonexistent case and one sample case failed because the automated browser received an empty page. No database concurrency test, crash-recovery test, 20-user load test, backup/restore drill, successful MKAD acquisition or SRO read-back verification exists.

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

Before deployment: production service definitions, secret management, TLS/security headers, session/CSRF review, RBAC, file type/size limits and malware handling, retention/deletion policy, encryption/access control for stored documents, structured logs/metrics/alerts, worker supervision, dead-letter/reconciliation tooling, backup/restore drills, capacity/load tests and operator runbooks. Evaluate each against real requirements; do not claim present support based on this list.
