# Development, verification and incident handling

## Local development setup (observed)

`deploy/docker-compose.dev.yml` starts only PostgreSQL 16 and Redis 7. FastAPI and RQ workers run from the host `.venv` so OCR can call local Tesseract and Poppler. Defaults in `app/settings.py` use database port `55432`, Redis port `56379`, upload directory `storage/documents`, debug enabled and a development-only session key. These defaults are not production configuration.

`requirements.txt` pins the current Python 3.13 environment. The OCR path additionally needs local Tesseract with Russian data, Poppler, and Playwright Chromium. `.env.example` contains blank placeholders; never print `.env`.

The Compose port mappings are restricted to `127.0.0.1`. Containers created from the previous wildcard mappings keep their existing published bindings until recreated. The currently running Postgres/Redis containers were observed bound to `0.0.0.0` and IPv6 wildcard; Redis has no persistent volume and currently has one queued OCR job. Do not recreate the stack until the queued job has been reconciled/preserved and a restart window is safe. The checked-in loopback change does not by itself alter existing containers.

Observed local commands:

```bash
docker compose -f deploy/docker-compose.dev.yml up -d
.venv/bin/python -m app.cli init-db
.venv/bin/python -m app.cli create-user 'Test User'
.venv/bin/python -m app.cli schema-check
.venv/bin/python -m app.cli ops-status
.venv/bin/uvicorn app.main:app --reload
.venv/bin/rq worker ocr sro --url redis://localhost:56379/0
```

Run server and worker in separate terminals. `create-user` prompts for a local application password. `schema-check` compares the live schema with ORM metadata and reports its Alembic revision without changing the database; exit code 1 means schema differences, and 2 means the check could not complete. `init-db` applies Alembic migrations only after a read-only preflight: an unversioned database must contain no tables. A populated database without a recorded Alembic revision is refused before migrations run; its adoption requires a verified backup and exact read-only comparison of tables, indexes, defaults and PostgreSQL enum labels. A DBA may then stamp revision `bc518c7514d3`; never auto-stamp or use the command against an unverified schema. The legacy `--reset` flag refuses destructive drops. The baseline downgrade is intentionally blocked. `/healthz` is a process liveness check. `/readyz` checks PostgreSQL and Redis and returns 503 when either is unavailable; it does not check worker presence or queue age.

`ops-status` is a read-only operator snapshot: task counts by state, oldest `recognizing.updated_at`, queue depths and registered RQ worker count. It does not expose task IDs or document data, alter states, or enqueue work. An old `recognizing` timestamp is a signal for investigation, not permission to retry; check the actual result and use a fenced recovery path before changing or re-enqueueing work.

Production configuration must set `APP_DEBUG=0` and provide `APP_SECRET_KEY` with at least 32 characters. Startup rejects a missing or short key in that mode instead of using the development fallback. Keep the key in `.env` or the deployment secret store; do not pass it as a command-line argument or include it in logs. The development fallback is accepted only while debug mode is enabled.

`Settings.__repr__` excludes the database URL, Redis URL and session-signing key so generic object logging does not expose connection credentials. This is defense in depth; never log the raw environment or settings object.

Cookie-authenticated browser mutations require a same-host `Origin` header, or a same-host `Referer` when `Origin` is absent. Requests with neither are rejected. A reverse proxy must preserve the public `Host` header; the check intentionally does not consume request bodies, including multipart uploads. Successful login clears any pre-authentication session data before storing the authenticated user ID. This is a browser CSRF/fixation control, not an authorization or task-ownership policy.

Each authenticated request reloads the user from PostgreSQL. A deleted or inactive account is rejected and its signed-cookie session is cleared on that request; active browser sessions therefore observe account deactivation on the next request. This does not yet provide roles or restrict access to tasks owned by another active employee.

CLI commands are `extract`, `inspect`, and `run`. In the current build, `run` performs local validation and `--live` fails closed. The SRO client also blocks Add, Save and Close by default. Do not enable registry mutations or use a real registry for tests.

## Test commands

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q app sro_robot tests
git diff --check
bash scripts/dev/postgres-restore-drill.sh
scripts/dev/verify.sh
```

The PostgreSQL restore drill starts a disposable PostgreSQL 16 container bound to loopback, applies the application baseline, inserts synthetic workflow rows, creates a custom-format `pg_dump`, restores into a second database, and checks the revision plus task/event linkage. It then removes the revision marker only from that restored copy, proves `init-db` refuses the populated unversioned schema, runs the read-only schema comparison, and rehearses an explicit baseline stamp and upgrade on the disposable copy while checking that the canary rows remain. The container and temporary upload directory are removed on exit. Never point this script at a shared or production database; the schema is generated from current ORM metadata and the data is synthetic, so this is a migration-path rehearsal, not proof of compatibility with every historical database or production-volume/RPO/RTO evidence.

`scripts/dev/verify.sh` is the single local verification entry point. It runs the unittest suite, byte-compilation, whitespace checks, shell syntax validation and disposable PostgreSQL restore/adoption drill. It requires the pinned `.venv` by default and a working Docker daemon; set `SRO_ROBOT_PYTHON` to another compatible interpreter when needed.

Last verified on 2026-10-10: 55 deterministic tests passed. Same-origin mutation middleware tests cover cross-origin and missing-origin rejection, same-origin and Referer acceptance, safe methods and proof that request bodies are not consumed; successful login replaces prior session data. `ops-status` was run read-only against the local runtime: 5 tasks were `recognizing`, oldest `updated_at` was 2026-09-23 14:12 UTC, OCR queue depth was 1, SRO queue depth was 0, and no RQ workers were registered. No task was retried or inspected. A fresh disposable PostgreSQL 16 database accepted the Alembic baseline. Read-only `schema-check` on the populated shared database reported no metadata differences and listed the expected enum labels; it remains unversioned and unstamped. Standard `alembic check` reports “Target database is not up to date” before comparing an unstamped schema. `bash scripts/dev/postgres-restore-drill.sh` passed: the linked synthetic task/event survived logical restore, a populated-unversioned preflight refusal, read-only comparison and isolated baseline adoption. This remains a synthetic rehearsal only. The UI upload/OCR/review flow was exercised in an isolated local database using one synthetic PDF and the two owner-provided samples; the two real samples reached `needs_review`. A local validation job was then run through the UI and recorded `saved: false`; the SRO browser was not opened. MKAD UI submissions for a synthetic nonexistent case and one sample case failed because the automated browser received an empty page. No database concurrency test, crash-recovery test, 20-user load test, production backup/restore drill, successful MKAD acquisition or SRO read-back verification exists.

Use fake SRO clients and isolated databases for stateful/write tests. Tests must cover duplicate intake, task claiming, retries, crashes around side-effect boundaries, worker restarts, out-of-order events, browser timeouts, partial form operations, missing/ambiguous reconciliation, and PII-safe logs. Never use a real SRO write as an end-to-end test.

## Logging and observability

Unhandled request failures log method, path and exception type without exception text or traceback; document-recognition failures log task ID and exception type and persist a generic stage/type message. The owner-pending KAD worker changes in the current worktree apply the same redaction but remain uncommitted. This avoids accidentally retaining parser messages, file paths or upstream response details, at the cost of omitting stack traces. This is not yet a complete observability system: there are no established structured log schema, metrics, tracing, alerting, queue-age dashboards, worker heartbeat, retention policy or production incident runbook. Do not log raw document/OCR text or credentials.

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
