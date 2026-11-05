# Operations

## Local runtime

Use `npm start` / `scripts/dev/start-local.sh` for the isolated development stack.
It creates private `.local-run/local.env`, loopback PostgreSQL/Redis, applies local
migrations, and supervises web, OCR, preview and safe outbox dispatch. Ctrl+C stops
owned services and keeps data volumes. Never delete a volume to resolve a conflict.
Do not start it over an unreconciled old queue or reuse another project's database.

For manual operation use `.env.example`: required database/Redis URLs, independent
strong secrets, `APP_DEBUG=1` only locally and mode-0600 secret files. Keep runtime
`DATABASE_URL` separate from administrative `MIGRATION_DATABASE_URL`; production
web/workers must never receive schema-owner credentials. Pool and request budgets
are per process. OCR multiprocessing and native/model threads must be budgeted
against total CPU/RAM; there is no resident neural-model extraction component.

Useful commands (only on the intended isolated/reconciled environment):

```sh
.venv/bin/python -m app.cli ops-status
.venv/bin/python -m app.cli schema-check
.venv/bin/python scripts/dev/run_worker.py ocr --workers 2
.venv/bin/python scripts/dev/run_worker.py sro_preview
.venv/bin/python -m app.cli outbox-dispatch --apply --watch
```

Read-only status reports are not retry decisions. A historical `recognizing`
task without a lease or an unknown SRO result requires explicit reconciliation.
Use the existing OCR reconciliation/reprocessing CLI only after reading its help,
source/attempt state and audit consequences. Do not dispatch retired KAD jobs.

## Schema, recovery and verification

New disposable databases use `app.cli init-db`. Do not run it blindly on a
pre-Alembic installation: verify backup/schema and use the reviewed adoption
procedure first. Preserve the complete forward migration chain and irreversible
guards; old migrations are not obsolete merely because production passed them.

Run `scripts/dev/verify.sh` for the deterministic Python/Node checks plus isolated
PostgreSQL migration, roles, restore/adoption and recovery drills. These scripts
create their own marked loopback containers and refuse arbitrary write targets.
They must not be pointed at office data. UI/extension browser checks under
`scripts/dev` use controlled synthetic inputs and isolated services.

Production deployment/activation/rollback: `../docs/RELEASE_WORKFLOW.md` and
`scripts/ops`. Backups: `backup_vps.py`, `pull_vps_backup.sh`,
`backup_store_restore_drill.py` and their help/current configuration. Keep backup
retention, stored-document verification, schema/runtime-role checks, exact-tag
validation and HTTPS read-back. An activation rollback and Git source promotion
are separate facts. No source push deploys automatically.

Keep original samples/uploads, private configuration, database volumes and session
profiles out of Git. Temporary test output stays private/ignored, bounded and
removed after a compact result is retained. Do not clear shared browser/model
caches or stop unrelated services as part of project cleanup.
